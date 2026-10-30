"""G28 exercises the real host attempt boundary with short, deterministic faults."""
from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from astrid.core.execution import generic_host
from astrid.core.execution.generic_host import HostCancelled, HostError, RuntimeProtocolClient
from tests.test_generic_host_result_json import host_case, _success_boundary


pytestmark = pytest.mark.timeout(9)


@pytest.fixture
def case(host_case, monkeypatch):
    case = host_case
    data = case.task["task"]
    data["status"] = "running"
    original_fail = case.runtime.fail

    def fail(*args, **kwargs):
        original_fail(*args, **kwargs)
        data["status"] = "failed"
        return {"task": dict(data)}

    def cancel(task_id, **kwargs):
        assert task_id == data["id"]
        assert kwargs == {"attempt_id": data["attempt_id"], "fence": data["fence"], "lease_id": "lease-result"}
        data["status"] = "cancelled"
        return {"task": dict(data)}

    monkeypatch.setattr(case.runtime, "fail", fail)
    monkeypatch.setattr(case.runtime, "cancel", cancel, raising=False)
    monkeypatch.setattr(generic_host, "_LEASE_POLL_SECONDS", 0.05)
    _success_boundary(case, monkeypatch)
    return case


def receipt(case):
    return json.loads((case.root / "result.json").read_text())


def assert_failed(case, started, *, code="executor_failed"):
    assert time.monotonic() - started < 10
    assert case.task["task"]["status"] == "failed"
    assert receipt(case)["status"] in {"failed", "timeout"}
    assert receipt(case)["error"]["code"] == code
    assert not any(t.name == "astrid-heartbeat-task-result" and t.is_alive()
                   for t in threading.enumerate())


@pytest.mark.parametrize("fault", ["executor", "failure_handler", "system_exit", "allocate", "heartbeat", "pump_start",
                                 "error_string", "error_properties", "error_normalizer", "normalized_attributes", "failure_rpc"])
def test_fault_reaches_terminal_state_and_receipt(case, monkeypatch, fault):
    class BrokenString(Exception):
        def __str__(self):
            raise OSError("error string failed")

    class BrokenProperties(Exception):
        @property
        def code(self):
            raise OSError("error code failed")

    def broken(*args, **kwargs):
        if fault == "failure_handler":
            # A failing decorator used to escape from the exception handler
            # before the Runtime failure mutation.
            monkeypatch.setattr(generic_host, "recovery_suffix", lambda *_: (_ for _ in ()).throw(OSError("recovery failed")))
        if fault == "system_exit":
            raise SystemExit(17)
        if fault == "error_string":
            raise BrokenString()
        if fault == "error_properties":
            raise BrokenProperties("executor failed")
        if fault == "error_normalizer":
            monkeypatch.setattr(generic_host, "coerce_astrid_error", lambda *_: (_ for _ in ()).throw(OSError("normalization failed")))
        if fault == "normalized_attributes":
            monkeypatch.setattr(generic_host, "coerce_astrid_error", lambda *_: object())
        raise OSError("executor boundary failed")

    if fault == "failure_rpc":
        original_fail = case.runtime.fail
        failures = []
        def fail(*args, **kwargs):
            failures.append(None)
            if len(failures) == 1:
                raise OSError("first failure write lost")
            return original_fail(*args, **kwargs)
        monkeypatch.setattr(case.runtime, "fail", fail)
    if fault == "allocate":
        monkeypatch.setattr(case.host, "_allocate_attempt_root", broken)
    elif fault == "heartbeat":
        monkeypatch.setattr(case.runtime, "heartbeat", broken)
    elif fault == "pump_start":
        monkeypatch.setattr(threading.Thread, "start", broken)
    else:
        monkeypatch.setattr(case.host, "_run_command_definition", broken)
    started = time.monotonic()
    with pytest.raises(AttributeError if fault == "normalized_attributes" else (OSError, HostError)):
        case.host.run_task(case.task, lease_token="lease-result")
    assert_failed(case, started)
    assert len(case.runtime.failures) == 1
    binding = case.runtime.failures[0][3]
    assert binding["attempt_id"] == "attempt-result" and binding["fence"] == 3


@pytest.mark.parametrize("stage", ["preflight", "contract_preflight", "materialize", "harvest", "settlement", "authority",
                                 "contract", "busy", "claim_deadline"])
def test_stalled_stage_deadline_publishes_before_stage_returns(case, monkeypatch, stage):
    case.host.execution_policy = replace(case.host.execution_policy, deadline_seconds=0.3)
    if stage == "claim_deadline":
        case.task["task"]["deadline_at"] = (datetime.now(timezone.utc) + timedelta(seconds=0.3)).isoformat()
        case.host.execution_policy = replace(case.host.execution_policy, deadline_seconds=30)
    if stage == "contract_preflight":
        from tests.core.execution.test_generic_host_contract import _task
        case.task = _task(limits={"max_runtime_seconds": 1})
        case.task["task"]["status"] = "running"
        case.runtime.tasks["task-contract"] = case.task
        case.host.execution_policy = replace(case.host.execution_policy, deadline_seconds=30)
        # Preserve the fixture's concrete state assertions for this identity.
        original_fail = case.runtime.fail
        def fail(*args, **kwargs):
            result = original_fail(*args, **kwargs)
            case.task["task"]["status"] = "failed"
            return result
        monkeypatch.setattr(case.runtime, "fail", fail)
    entered, release = threading.Event(), threading.Event()
    returned = []
    if stage in {"preflight", "contract_preflight", "claim_deadline"}:
        owner, name = case.host, "preflight"
    elif stage == "contract":
        owner, name = generic_host, "_execution_contract"
    elif stage == "busy":
        owner, name = generic_host, "_publish_busy"
    elif stage == "materialize":
        owner, name = case.host, "_materialize_inputs"
    elif stage == "harvest":
        owner, name = generic_host, "harvest_staged_outputs"
    elif stage == "authority":
        owner, name = generic_host, "_interactive_review_authority"
    else:
        owner, name = case.runtime, "settle"
    original = getattr(owner, name)

    def stalled(*args, **kwargs):
        entered.set()
        assert release.wait(3), "test must release its stalled stage"
        if stage == "authority":
            return None
        if stage == "settlement":
            # Model a transport returning a stale success after the guard won.
            return {"task": {"id": "task-result", "status": "completed"}}
        return original(*args, **kwargs)

    monkeypatch.setattr(owner, name, (lambda *_a, **_k: lambda: stalled()) if stage == "authority" else stalled)

    def run():
        try:
            returned.append(case.host.run_task(case.task, lease_token="lease-result"))
        except BaseException as error:
            returned.append(error)

    worker = threading.Thread(target=run)
    started = time.monotonic()
    worker.start()
    try:
        assert entered.wait(2)
        expiry = time.monotonic() + 2
        while not (case.root / "result.json").exists() and time.monotonic() < expiry:
            time.sleep(0.01)
        assert case.task["task"]["status"] == "failed"
        assert receipt(case)["status"] == "timeout"
        assert receipt(case)["error"]["code"] == "timeout"
        frozen = (case.root / "result.json").read_bytes()
        assert worker.is_alive(), "receipt must precede release of the blocked stage"
    finally:
        release.set()
        worker.join(timeout=3)
    assert not worker.is_alive()
    assert_failed(case, started, code="timeout")
    assert (case.root / "result.json").read_bytes() == frozen
    assert not case.runtime.settlements
    assert returned


@pytest.mark.parametrize("source", ["local", "runtime"])
def test_cancellation_records_terminal_state_and_receipt(case, monkeypatch, source):
    def execute(*args, **kwargs):
        if source == "runtime":
            case.task["task"]["status"] = "cancel_requested"
            assert kwargs["cancelled"]()
        raise HostCancelled("cancel this invocation")

    monkeypatch.setattr(case.host, "_run_command_definition", execute)
    started = time.monotonic()
    result = case.host.run_task(case.task, lease_token="lease-result")
    assert time.monotonic() - started < 10
    assert result["cancelled"]
    assert case.task["task"]["status"] == "cancelled"
    assert receipt(case)["status"] == "cancelled"
    assert receipt(case)["error"]["code"] == "cancelled"
    assert not case.runtime.failures


@pytest.mark.parametrize("loss", ["task_read", "queued", "successor", "fence", "heartbeat", "expired", "resource_expired",
                                "null_expiry", "invalid_expiry", "naive_expiry"])
def test_lease_loss_is_failure_with_terminal_receipt(case, monkeypatch, loss):
    class RuntimeFault(OSError):
        code = "runtime_unavailable"
        request_id = "req-lease-17"

        def __init__(self, message):
            super().__init__(message)
            self.message = message

    def execute(*args, **kwargs):
        if loss == "task_read":
            monkeypatch.setattr(case.runtime, "task", lambda *_: (_ for _ in ()).throw(RuntimeFault("task read unavailable")))
        elif loss == "queued":
            case.task["task"]["status"] = "queued"
        elif loss == "successor":
            case.task["task"]["attempt_id"] = "successor"
        elif loss == "fence":
            case.task["task"]["fence"] = 4
        elif loss == "heartbeat":
            monkeypatch.setattr(case.runtime, "heartbeat", lambda *_a, **_k: (_ for _ in ()).throw(RuntimeFault("bearer token: do-not-record")))
            (case.root / ".astrid-progress.json").write_text('{"phase":"changed","percent":30}')
        else:
            lease_expiry = {"null_expiry": None, "invalid_expiry": "invalid", "naive_expiry": "2000-01-01T00:00:00"}.get(
                loss, "2000-01-01T00:00:00+00:00")
            if loss == "resource_expired":
                monkeypatch.setattr(case.runtime, "task", lambda *_a: SimpleNamespace(
                    state=case.task["task"]["status"], attempt_id="attempt-result", lease_fence=3,
                    lease_expires_at=lease_expiry))
            else:
                case.task["task"]["lease_expires_at"] = lease_expiry
        polling_deadline = time.monotonic() + 2
        while case.task["task"]["status"] != "failed" and time.monotonic() < polling_deadline:
            time.sleep(0.01)
        raise HostCancelled("lease observer contained executor")

    monkeypatch.setattr(case.host, "_run_command_definition", execute)
    started = time.monotonic()
    with pytest.raises(HostError, match="lease lost"):
        case.host.run_task(case.task, lease_token="lease-result")
    assert_failed(case, started, code="lease_lost")
    assert len(case.runtime.failures) == 1
    categories = {
        "task_read": "task_read_exception", "queued": "task_state_changed",
        "successor": "attempt_changed", "fence": "fence_changed",
        "heartbeat": "heartbeat_exception", "expired": "lease_expired",
        "resource_expired": "lease_expired", "null_expiry": "lease_metadata_invalid",
        "invalid_expiry": "lease_metadata_invalid", "naive_expiry": "lease_metadata_invalid",
    }
    diagnostic = case.runtime.failures[0][3]["failure_diagnostic"]
    assert diagnostic["category"] == categories[loss]
    assert diagnostic["schema_version"] == 1
    assert diagnostic["observed_at_utc"].endswith("+00:00")
    last_heartbeat = datetime.fromisoformat(diagnostic["last_successful_heartbeat_at_utc"])
    assert last_heartbeat.tzinfo == timezone.utc
    assert len(json.dumps(diagnostic)) < 1200
    if loss in {"task_read", "heartbeat"}:
        assert diagnostic["exception_type"] == "RuntimeFault"
        assert diagnostic["runtime_code"] == "runtime_unavailable"
        assert diagnostic["request_id"] == "req-lease-17"
    if loss == "task_read":
        assert diagnostic["runtime_message"] == "task read unavailable"
    if loss == "heartbeat":
        assert "runtime_message" not in diagnostic
        assert "do-not-record" not in json.dumps(diagnostic)
    terminal_diagnostic = receipt(case)["lease_diagnostic"]
    assert terminal_diagnostic == diagnostic
    assert case.task["task"]["status"] == "failed"
    assert not case.runtime.settlements


def test_lease_loss_keeps_last_successful_heartbeat_receipt(case, monkeypatch):
    pulsed = threading.Event()
    expiry = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
    original_heartbeat = case.runtime.heartbeat

    def heartbeat(*args, **kwargs):
        original_heartbeat(*args, **kwargs)
        pulsed.set()
        return SimpleNamespace(lease_expires_at=expiry)

    monkeypatch.setattr(case.runtime, "heartbeat", heartbeat)

    def execute(*args, **kwargs):
        assert pulsed.wait(2)
        case.task["task"]["status"] = "queued"
        deadline = time.monotonic() + 2
        while case.task["task"]["status"] != "failed" and time.monotonic() < deadline:
            time.sleep(0.01)
        raise HostCancelled("lease observer contained executor")

    monkeypatch.setattr(case.host, "_run_command_definition", execute)
    with pytest.raises(HostError, match="lease lost"):
        case.host.run_task(case.task, lease_token="lease-result")
    diagnostic = receipt(case)["lease_diagnostic"]
    assert diagnostic["category"] == "task_state_changed"
    assert datetime.fromisoformat(diagnostic["last_successful_heartbeat_at_utc"]).tzinfo == timezone.utc
    assert diagnostic["last_successful_heartbeat_expiry"] == expiry
    assert diagnostic["observed"] == {"state": "queued", "attempt_id": "attempt-result", "fence": "3"}
    assert case.task["task"]["status"] == "failed"
    assert not case.runtime.settlements


@pytest.mark.parametrize("read_model", ["mapping", "resource"])
def test_renewal_requires_semantic_progress_and_never_begins_after_deadline(case, monkeypatch, read_model):
    case.host.execution_policy = replace(case.host.execution_policy, deadline_seconds=0.5)
    pulses = []
    if read_model == "resource":
        monkeypatch.setattr(case.runtime, "task", lambda *_: SimpleNamespace(
            state=case.task["task"]["status"], attempt_id="attempt-result", lease_fence=3))
    original = case.runtime.heartbeat
    monkeypatch.setattr(case.runtime, "heartbeat", lambda *a, **k: (pulses.append((time.monotonic(), k.get("progress"))), original(*a, **k))[1])
    launched = time.monotonic()

    def execute(*args, **kwargs):
        checkpoint = case.root / ".astrid-progress.json"
        checkpoint.write_text('{"phase":"work","percent":10}')
        time.sleep(0.13)
        stable_count = len(pulses)
        # Rewriting JSON or changing irrelevant keys must not earn a lease.
        checkpoint.write_text('{"percent":10,"phase":"work","noise":"new bytes"}')
        time.sleep(0.13)
        assert len(pulses) == stable_count
        checkpoint.write_text('{"phase":"work","percent":20}')
        time.sleep(0.13)
        assert len(pulses) == stable_count + 1
        time.sleep(0.2)
        assert kwargs["cancelled"]()
        raise HostCancelled("deadline interrupted invocation")

    monkeypatch.setattr(case.host, "_run_command_definition", execute)
    case.host.run_task(case.task, lease_token="lease-result")
    assert_failed(case, launched, code="timeout")
    assert pulses and all(at - launched < 0.5 for at, _ in pulses)
    assert len([p for _, p in pulses if p == {"phase": "work", "percent": 10}]) == 1
    assert len([p for _, p in pulses if p == {"phase": "work", "percent": 20}]) == 1


def test_failure_rpc_uses_claimed_epoch_and_private_bounded_transport():
    seen = []

    class Generated:
        timeout = 30
        def health(self):
            pytest.fail("failure must not read health before its fenced mutation")
        def fail_attempt(self, attempt_id, **kwargs):
            seen.append((self.timeout, attempt_id, kwargs))
            return {"status": "failed"}

    client = object.__new__(RuntimeProtocolClient)
    client.generated = Generated()
    client._attempt_runtime_epochs = {"attempt-result": 7}
    client.fail("task-result", "lease-result", "executor broke", attempt_id="attempt-result", fence=3)
    assert client.generated.timeout == 30
    timeout, attempt_id, payload = seen[0]
    assert timeout <= 2 and attempt_id == "attempt-result"
    assert payload["runtime_epoch"] == 7 and payload["error"]["code"] == "executor_failed"


@pytest.mark.parametrize("outcome", ["executor", "cancelled", "lease_lost", "lease_expired"])
def test_real_runtime_fenced_terminal_state_and_result(case, monkeypatch, tmp_path, outcome):
    """Protocol adapter -> RuntimeService -> real attempt/reservation tables."""
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.store import RealmStore

    realm = tmp_path / "realm"
    RealmStore.initialize(realm).close()
    service = RuntimeService(realm)
    digest = "sha256:" + "2" * 64
    try:
        service.register_capability({"capability_id": "test.echo", "definition_digest": digest,
                                     "required_resource_keys": ["cpu"]})
        service.register_executor({"executor_id": "host-g28", "capabilities": ["test.echo"],
                                   "resource_keys": ["cpu"]}, idempotency_key="register-g28")
        created = service.create_task({"capability_id": "test.echo", "capability_digest": digest,
                                       "idempotency_key": "task-g28"})
        attempt = service.claim_next({"executor_id": "host-g28", "capability_ids": ["test.echo"],
                                      "runtime_epoch": service.health()["runtime_epoch"]}, idempotency_key="claim-g28")
        data = case.task["task"]
        data.update(id=created["task"]["id"], attempt_id=attempt["attempt_id"], fence=attempt["fence"],
                    runtime_epoch=attempt["runtime_epoch"])

        class Generated:
            timeout = 30
            def health(self):
                return service.health()
            def get_task(self, task_id):
                raw = service.task(task_id)["task"]
                return {**raw, "version": int(raw["attempt"]) + 1}
            def fail_attempt(self, attempt_id, **kwargs):
                key = kwargs.pop("idempotency_key")
                return service.fail_attempt(attempt_id, kwargs, idempotency_key=key)
            def cancel_task(self, task_id, **kwargs):
                key = kwargs.pop("idempotency_key")
                return service.cancel_task_canonical(task_id, kwargs, idempotency_key=key)
            def heartbeat_attempt(self, attempt_id, **kwargs):
                key = kwargs.pop("idempotency_key")
                return service.heartbeat_attempt(attempt_id, kwargs, idempotency_key=key)

        protocol = object.__new__(RuntimeProtocolClient)
        protocol.generated = Generated()
        protocol._attempt_runtime_epochs = {attempt["attempt_id"]: attempt["runtime_epoch"]}
        protocol._heartbeat_lock = threading.RLock()
        protocol._heartbeat_sequence = 0
        protocol._heartbeat_session = "g28"
        for operation in ("fail", "cancel", "task", "heartbeat"):
            monkeypatch.setattr(case.runtime, operation, getattr(protocol, operation))

        def execute(*args, **kwargs):
            if outcome == "cancelled":
                raise HostCancelled("cancel this local attempt")
            if outcome in {"lease_lost", "lease_expired"}:
                # Keep exact attempt/fence, expire only the lease. Failing
                # cedes ownership; it must not require renewing expired work.
                with service.store._mutex:
                    service.store.conn.execute("UPDATE tasks SET lease_expires_at='2000-01-01T00:00:00Z' WHERE id=?", (data["id"],))
                    service.store.conn.execute("UPDATE attempts SET lease_expires_at='2000-01-01T00:00:00Z' WHERE id=?", (attempt["attempt_id"],))
                    service.store.conn.commit()
                # Exercise the host's lost-transport containment without a
                # read that could reap/reassign the just-expired lease first.
                if outcome == "lease_lost":
                    monkeypatch.setattr(case.runtime, "task", lambda *_: (_ for _ in ()).throw(OSError("lease lost")))
                    assert kwargs["cancelled"]()
                else:
                    # Runtime retains this attempt's custody after ordinary
                    # expiry; unchanged running state/fence is not authority.
                    assert service.task(data["id"])["task"]["status"] == "running"
                    expiry = time.monotonic() + 2
                    while not (case.root / "result.json").exists() and time.monotonic() < expiry:
                        time.sleep(0.01)
                    assert receipt(case)["error"]["code"] == "lease_lost"
                raise HostCancelled("lost lease")
            raise OSError("executor failed")

        monkeypatch.setattr(case.host, "_run_command_definition", execute)
        started = time.monotonic()
        if outcome == "cancelled":
            case.host.run_task(case.task, lease_token=attempt["lease_id"])
        else:
            with pytest.raises((OSError, HostError)):
                case.host.run_task(case.task, lease_token=attempt["lease_id"])
        assert time.monotonic() - started < 10
        stored = service.task(data["id"])["task"]
        expected = "cancelled" if outcome == "cancelled" else "failed"
        assert stored["status"] == expected
        assert receipt(case)["status"] == expected
        assert receipt(case)["error"]["code"] == {"executor": "executor_failed", "cancelled": "cancelled", "lease_lost": "lease_lost", "lease_expired": "lease_lost"}[outcome]
        assert stored["lease_expires_at"] is None
        reservation = service.store.conn.execute("SELECT released_at FROM reservations WHERE task_id=?", (data["id"],)).fetchone()
        assert reservation[0] is not None
    finally:
        service.close()


def test_failure_receipt_exists_before_optional_output_harvest_returns(case, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    returned = []
    original = generic_host.harvest_staged_outputs

    def execute(*args, **kwargs):
        raise OSError("executor failed before output")

    def stalled(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    monkeypatch.setattr(case.host, "_run_command_definition", execute)
    monkeypatch.setattr(generic_host, "harvest_staged_outputs", stalled)

    def run():
        try:
            case.host.run_task(case.task, lease_token="lease-result")
        except BaseException as error:
            returned.append(error)

    worker = threading.Thread(target=run)
    started = time.monotonic()
    worker.start()
    try:
        assert entered.wait(2)
        assert case.task["task"]["status"] == "failed"
        assert receipt(case)["status"] == "failed"
        assert receipt(case)["error"]["code"] == "executor_failed"
        assert worker.is_alive()
    finally:
        release.set()
        worker.join(timeout=3)
    assert not worker.is_alive() and returned
    assert_failed(case, started)


def test_heartbeat_contention_cannot_wait_past_the_deadline(case, monkeypatch):
    """Use the real adapter lock shared with delegated child admission."""
    calls = []
    class Generated:
        timeout = 30
        def health(self):
            assert self.timeout == 2.0
            return {"runtime_epoch": 7}
        def heartbeat_attempt(self, *_args, **_kwargs):
            calls.append(time.monotonic())

    client = object.__new__(RuntimeProtocolClient)
    client.generated = Generated()
    client._attempt_runtime_epochs = {"attempt-result": 7}
    client._heartbeat_lock = threading.RLock()
    client._heartbeat_sequence = 0
    client._heartbeat_session = "g28"
    entered, release = threading.Event(), threading.Event()

    def hold_admission():
        with client._heartbeat_lock:
            entered.set()
            assert release.wait(3)

    holder = threading.Thread(target=hold_admission)
    holder.start()
    try:
        assert entered.wait(1)
        case.host.execution_policy = replace(case.host.execution_policy, deadline_seconds=0.3)
        monkeypatch.setattr(case.runtime, "heartbeat", client.heartbeat)
        started = time.monotonic()
        with pytest.raises(HostError, match="heartbeat authority is busy"):
            case.host.run_task(case.task, lease_token="lease-result")
        assert time.monotonic() - started < 3
        assert_failed(case, started, code="timeout")
        # Releasing the lock after expiry must never send the queued heartbeat.
        time.sleep(0.35)
        assert not calls
    finally:
        release.set()
        holder.join(timeout=1)
    assert not holder.is_alive()


def test_heartbeat_short_contention_preserves_normal_serialization():
    calls = []
    class Generated:
        timeout = 30
        def health(self):
            assert self.timeout == 2.0
            return {"runtime_epoch": 7}
        def heartbeat_attempt(self, *_args, **_kwargs):
            calls.append(self.timeout)

    client = object.__new__(RuntimeProtocolClient)
    client.generated = Generated()
    client._attempt_runtime_epochs = {"attempt-result": 7}
    client._heartbeat_lock = threading.RLock()
    client._heartbeat_sequence = 0
    client._heartbeat_session = "g28"
    errors = []
    entered = threading.Event()

    def heartbeat():
        entered.set()
        try:
            client.heartbeat("task-result", "lease-result", attempt_id="attempt-result", fence=3)
        except BaseException as exc:
            errors.append(exc)

    with client._heartbeat_lock:
        worker = threading.Thread(target=heartbeat)
        worker.start()
        assert entered.wait(1)
        time.sleep(0.05)
        assert not calls
    worker.join(timeout=1)
    assert not worker.is_alive() and not errors
    assert calls == [2.0]


@pytest.mark.parametrize("fault", ["heartbeat", "task_read", "contract", "handoff_identity", "shutdown"])
def test_claim_setup_failure_uses_same_terminal_receipt_boundary(case, monkeypatch, fault):
    claim = {"task_id": "task-result", "attempt_id": "attempt-result", "lease_id": "lease-result",
             "fence": 3, "capability_id": "test.echo", "spec": case.task["task"]["spec"]}
    monkeypatch.setattr(case.runtime, "claim_next", lambda **_kwargs: claim)
    if fault == "heartbeat":
        monkeypatch.setattr(case.runtime, "heartbeat", lambda *_a, **_k: (_ for _ in ()).throw(OSError("claim heartbeat failed")))
    elif fault == "task_read":
        monkeypatch.setattr(case.runtime, "task", lambda *_a: (_ for _ in ()).throw(OSError("claim task read failed")))
    elif fault == "contract":
        claim["execution_request"] = {"malformed": True}
    elif fault == "handoff_identity":
        monkeypatch.setattr(case.runtime, "task", lambda *_a: {"task": {**case.task["task"], "id": "foreign-task"}})
    else:
        original_task = case.runtime.task
        def read_and_shutdown(task_id):
            result = original_task(task_id)
            case.host._shutdown.set()
            return result
        monkeypatch.setattr(case.runtime, "task", read_and_shutdown)
    started = time.monotonic()
    if fault == "shutdown":
        assert case.host.claim_once() is None
        assert time.monotonic() - started < 10
        assert case.task["task"]["status"] == "cancelled"
        assert receipt(case)["status"] == "cancelled"
        assert receipt(case)["error"]["code"] == "cancelled"
        assert not case.runtime.settlements
        return
    with pytest.raises((HostError, OSError)):
        case.host.claim_once()
    assert_failed(case, started)
    assert len(case.runtime.failures) == 1
    assert not case.runtime.settlements
    assert receipt(case)["task_id"] == "task-result"


@pytest.mark.parametrize("claim_model", ["mapping", "resource"])
def test_claim_forwards_immutable_deadline_to_existing_attempt(case, monkeypatch, claim_model):
    deadline = (datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat()
    claim = {"task_id": "task-result", "attempt_id": "attempt-result", "lease_id": "lease-result",
             "fence": 3, "spec": case.task["task"]["spec"], "deadline_at": deadline}
    monkeypatch.setattr(case.runtime, "claim_next", lambda **_kwargs: claim if claim_model == "mapping" else SimpleNamespace(**claim))
    captured = []
    monkeypatch.setattr(case.host, "run_task", lambda task, **_kwargs: captured.append(task))
    case.host.claim_once()
    assert captured[0]["task"]["deadline_at"] == deadline


@pytest.mark.parametrize("phase", ["setup", "execution", "collection"])
def test_absolute_deadline_bounds_all_existing_lifetime_phases(case, phase):
    lifetime = generic_host._AttemptLifetime(case.host.execution_policy, time.monotonic(), None,
                                            authority=lambda: None, phase=phase,
                                            absolute_deadline=time.monotonic())
    assert lifetime.expired()


@pytest.mark.parametrize("deadline", ["not-a-timestamp", "2026-10-29T12:00:00", 42])
def test_malformed_claim_deadline_fails_with_terminal_receipt(case, deadline):
    case.task["task"]["deadline_at"] = deadline
    started = time.monotonic()
    with pytest.raises(HostError, match="invalid claimed task deadline_at"):
        case.host.run_task(case.task, lease_token="lease-result")
    assert_failed(case, started)


def test_provider_setup_failure_preserves_g21_error_metadata(case, monkeypatch):
    from astrid.core.contracts.errors import AstridError

    record = case.host.capabilities["test.echo"]
    record = replace(record, ready=True)
    monkeypatch.setattr(case.host, "_preflight", lambda **_kwargs: [record])
    case.host.capabilities["test.echo"] = SimpleNamespace(
        adapter=SimpleNamespace(family="provider"),
        definition=replace(record.definition, isolation=replace(record.definition.isolation, network=True)),
    )
    claim = {"task_id": "task-result", "attempt_id": "attempt-result", "lease_id": "lease-result",
             "fence": 3, "capability_id": "test.echo", "spec": case.task["task"]["spec"]}
    monkeypatch.setattr(case.runtime, "claim_next", lambda **_kwargs: claim)
    error = AstridError("route refused", code="provider_denied", field="target", fix="select an admitted route")
    monkeypatch.setattr(case.host, "request_provider_route_grant", lambda *_args: (_ for _ in ()).throw(error))
    started = time.monotonic()
    with pytest.raises(HostError, match="provider route preparation failed"):
        case.host.claim_once()
    assert_failed(case, started, code="provider_denied")
    assert receipt(case)["error"]["field"] == "target"
    assert receipt(case)["error"]["fix"] == "select an admitted route"
    assert case.runtime.failures[0][3]["structured_error"] == {
        "code": "provider_denied", "field": "target", "fix": "select an admitted route"}
