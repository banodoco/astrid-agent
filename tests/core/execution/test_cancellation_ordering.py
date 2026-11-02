"""Cancellation must retain failure evidence and the original child owner."""
from __future__ import annotations

import json
import threading

import pytest

from astrid.core.execution import generic_host
from astrid.core.execution.generic_host import HostCancelled, RuntimeProtocolClient
from tests.core.execution.test_fail_closed_g28 import case, lease_clock, receipt
from tests.test_generic_host_result_json import host_case


pytestmark = pytest.mark.timeout(15)


@pytest.fixture
def real_runtime(case, tmp_path, monkeypatch):
    from astrid_runtime_client.generated import Task, WorkspaceClient
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.store import RealmStore

    realm = tmp_path / "realm"
    RealmStore.initialize(realm).close()
    service = RuntimeService(realm)
    digest = "sha256:" + "2" * 64
    service.register_capability({"capability_id": "test.echo", "definition_digest": digest,
                                 "required_resource_keys": ["cpu"]})
    service.register_executor({"executor_id": "host-review", "capabilities": ["test.echo"],
                               "resource_keys": ["cpu"]}, idempotency_key="register-review")
    created = service.create_task({"capability_id": "test.echo", "capability_digest": digest,
                                   "idempotency_key": "task-review"})
    attempt = service.claim_next({"executor_id": "host-review", "capability_ids": ["test.echo"],
                                  "runtime_epoch": service.health()["runtime_epoch"]},
                                 idempotency_key="claim-review")
    data = case.task["task"]
    data.update(id=created["task"]["id"], attempt_id=attempt["attempt_id"], fence=attempt["fence"],
                runtime_epoch=attempt["runtime_epoch"])
    decoder = WorkspaceClient("http://unused.invalid")

    class Generated:
        timeout = 30

        def health(self):
            return service.health()

        def get_task(self, task_id):
            return Task.from_json(service._task_resource(service.task(task_id)))

        def fail_attempt(self, attempt_id, **kwargs):
            key = kwargs.pop("idempotency_key")
            return decoder._mutation_json(json.dumps(
                service.fail_attempt(attempt_id, kwargs, idempotency_key=key)).encode())

        def cancel_task(self, task_id, **kwargs):
            key = kwargs.pop("idempotency_key")
            return decoder._mutation_json(json.dumps(
                service.cancel_task_canonical(task_id, kwargs, idempotency_key=key)).encode())

        def heartbeat_attempt(self, attempt_id, **kwargs):
            key = kwargs.pop("idempotency_key")
            return service.heartbeat_attempt(attempt_id, kwargs, idempotency_key=key)

    protocol = object.__new__(RuntimeProtocolClient)
    protocol.generated = Generated()
    protocol._attempt_runtime_epochs = {attempt["attempt_id"]: attempt["runtime_epoch"]}
    protocol._heartbeat_lock = threading.RLock()
    protocol._heartbeat_sequence = 0
    protocol._heartbeat_session = "review"
    for operation in ("fail", "cancel", "task", "heartbeat"):
        monkeypatch.setattr(case.runtime, operation, getattr(protocol, operation))
    try:
        yield service, attempt
    finally:
        service.close()


@pytest.mark.parametrize("failure", ["executor", "timeout", "executor_after_ack"])
def test_failure_after_runtime_cancel_is_not_clean_cancellation(case, real_runtime, monkeypatch, failure):
    from runtime_protocol.errors import LeaseError
    service, attempt = real_runtime
    terminal = {}
    implementation = case.host._run_task_impl

    def capture(*args, _terminal, **kwargs):
        terminal.update(value=_terminal)
        return implementation(*args, _terminal=_terminal, **kwargs)

    monkeypatch.setattr(case.host, "_run_task_impl", capture)

    def execute(*_args, **_kwargs):
        if failure == "executor_after_ack":
            terminal["value"]["force_abort"]("cancelled")
        else:
            service.cancel_task_canonical(case.task["task"]["id"], idempotency_key="external-cancel")
        if failure == "timeout":
            terminal["value"]["force_abort"]("timeout")
            raise HostCancelled("child stopped for deadline")
        raise OSError("executor failed while cancellation arrived")

    monkeypatch.setattr(case.host, "_run_command_definition", execute)
    if failure != "timeout":
        with pytest.raises((OSError, LeaseError)):
            case.host.run_task(case.task, lease_token=attempt["lease_id"])
    else:
        result = case.host.run_task(case.task, lease_token=attempt["lease_id"])
        assert result == {"task_id": case.task["task"]["id"], "status": "failed", "deadline_exceeded": True}
    stored = service.task(case.task["task"]["id"])["task"]
    assert stored["status"] == "cancelled"  # Remote terminal owner remains authoritative.
    assert receipt(case)["status"] == ("timeout" if failure == "timeout" else "failed")
    assert receipt(case)["error"]["code"] == ("timeout" if failure == "timeout" else "executor_failed")
    assert not case.runtime.settlements


def test_late_cancel_exception_replaces_already_published_cancellation(case, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    error = OSError("cancel RPC failed after the foreground stopped")

    def cancel(*_args, **_kwargs):
        entered.set()
        assert release.wait(2)
        raise error

    monkeypatch.setattr(case.runtime, "cancel", cancel)

    def execute(*_args, _terminal, **_kwargs):
        observer = threading.Thread(target=_terminal["force_abort"], args=("cancelled",))
        observer.start()
        try:
            assert entered.wait(2)
            assert _terminal["cancel_signal"].is_set()
            case.host._write_task_result(_terminal)
            assert receipt(case)["status"] == "cancelled"
        finally:
            release.set()
            observer.join(timeout=2)
        assert not observer.is_alive()
        assert _terminal["reporting_error"] is error
        assert receipt(case)["status"] == "failed"
        assert receipt(case)["error"]["message"] == str(error)
        return _terminal

    monkeypatch.setattr(case.host, "_run_task_impl", execute)
    with pytest.raises(OSError, match="cancel RPC failed after the foreground stopped"):
        case.host.run_task(case.task, lease_token="lease-result")


def test_existing_runtime_failure_survives_local_cancel(case, real_runtime, monkeypatch):
    service, attempt = real_runtime

    def execute(*_args, **_kwargs):
        case.runtime.fail(case.task["task"]["id"], attempt["lease_id"], "real Runtime failure",
                          attempt_id=attempt["attempt_id"], fence=attempt["fence"],
                          structured_error={"code": "provider_failed", "fix": "Inspect provider failure"})
        raise HostCancelled("local cancellation races the terminal failure")

    monkeypatch.setattr(case.host, "_run_command_definition", execute)
    with pytest.raises(Exception, match="real Runtime failure"):
        case.host.run_task(case.task, lease_token=attempt["lease_id"])
    assert service.task(case.task["task"]["id"])["task"]["status"] == "failed"
    assert receipt(case)["status"] == "failed"
    assert receipt(case)["error"]["code"] == "provider_failed"


def test_cancel_reply_preserves_failure_committed_after_read(case, real_runtime, monkeypatch):
    service, attempt = real_runtime
    generated = case.runtime.cancel.__self__.generated
    cancel_task = generated.cancel_task

    def race(task_id, **kwargs):
        case.runtime.fail(task_id, attempt["lease_id"], "failure after cancel read",
                          attempt_id=attempt["attempt_id"], fence=attempt["fence"],
                          structured_error={"code": "provider_failed"})
        return cancel_task(task_id, **kwargs)

    monkeypatch.setattr(generated, "cancel_task", race)

    def execute(*_args, **_kwargs):
        raise HostCancelled("local stop")

    monkeypatch.setattr(case.host, "_run_command_definition", execute)
    with pytest.raises(generic_host.AstridError, match="failure after cancel read"):
        case.host.run_task(case.task, lease_token=attempt["lease_id"])
    # Pinned Runtime's version is attempt-based: the remote race remains, but
    # its retained error must never become a clean local cancellation.
    stored = service.task(case.task["task"]["id"])["task"]
    assert stored["status"] == "cancelled"
    assert stored["result"]["error"]["code"] == "provider_failed"
    assert receipt(case)["status"] == "failed"
    assert receipt(case)["error"]["code"] == "provider_failed"


def test_already_cancelled_runtime_ack_is_accepted(case, real_runtime, monkeypatch):
    service, attempt = real_runtime

    def execute(*_args, **_kwargs):
        service.cancel_task_canonical(case.task["task"]["id"], idempotency_key="external-cancel")
        raise HostCancelled("remote stop")

    monkeypatch.setattr(case.host, "_run_command_definition", execute)
    assert case.host.run_task(case.task, lease_token=attempt["lease_id"])["cancelled"]
    assert receipt(case)["status"] == "cancelled"
    assert service.task(case.task["task"]["id"])["task"]["status"] == "cancelled"


def test_deadline_after_local_cancel_ack_keeps_timeout(case, lease_clock):
    def before(terminal):
        terminal["force_abort"]("cancelled")
        assert terminal["recorded"] and receipt(case)["status"] == "cancelled"
        lease_clock.at = 10
        assert terminal["lifetime"].expired()
        terminal["force_abort"]("timeout")

    terminal = lease_clock.run(before=before)
    assert terminal["forced"] == "timeout"
    assert receipt(case)["status"] == "timeout"
    assert receipt(case)["error"]["code"] == "timeout"
    assert case.task["task"]["status"] == "cancelled"
    assert not case.runtime.failures  # No second terminal mutation after acknowledgement.


@pytest.mark.parametrize("fault", ["rejected", "transport", "transport_timeout", "absent"])
def test_heartbeat_cancel_error_keeps_owned_process_containment(tmp_path, monkeypatch, fault):
    from astrid_runtime_client.generated import ApiError
    from tests.core.execution.test_generic_host_actions_v3 import pack, task_for
    from astrid.core.execution import task_scope

    entered = tmp_path / "entered"
    _, host, runtime, _, _ = pack(tmp_path, code=(
        "from pathlib import Path\nimport time\n"
        f"def run(value):\n    Path({str(entered)!r}).write_text('entered')\n"
        "    time.sleep(30)\n    return value\n"))
    host.attempt_root = tmp_path / "attempt"
    host.register()
    admitted = task_for(runtime)
    owned, calls = [], []
    track = host._track_process

    def track_original(process):
        owned.append(process)
        track(process)

    def current_task(task_id):
        pump = threading.current_thread().name.startswith("astrid-heartbeat-")
        return {"state": "cancel_requested" if entered.exists() and pump else "running"}

    error = ApiError(409, "conflict", "cancel rejected; inspect task version") if fault == "rejected" else OSError("cancel transport unavailable")
    if fault == "transport_timeout":
        error = ApiError(0, "transport_timeout", "runtime request timed out")

    def cancel(*args, **kwargs):
        calls.append(kwargs)
        raise error

    monkeypatch.setattr(host, "_track_process", track_original)
    monkeypatch.setattr(runtime, "task", current_task)
    monkeypatch.setattr(generic_host, "_LEASE_POLL_SECONDS", 0.05)
    if fault != "absent":
        monkeypatch.setattr(runtime, "cancel", cancel, raising=False)
    try:
        if fault == "absent":
            assert host.run_task(admitted, lease_token="lease")["cancelled"]
        else:
            with pytest.raises(type(error), match=str(error)):
                host.run_task(admitted, lease_token="lease")
        assert len(owned) == 1 and owned[0].returncode is not None
        assert task_scope.stop(owned[0], 0.1).status == "settled"
        assert not host._active_processes and not host._unresolved_task_scopes
        assert not runtime.settlements and not runtime.uploaded_objects
        result = json.loads((host.attempt_root / "result.json").read_text())
        assert result["status"] == ("cancelled" if fault == "absent" else "failed")
        if fault != "absent":
            assert str(error).split(": ")[-1] in result["error"]["message"]
            assert calls and calls[0] == {"attempt_id": "python-attempt", "fence": 1, "lease_id": "lease"}
    finally:
        for process in owned:
            generic_host._terminate_process_group(process)
