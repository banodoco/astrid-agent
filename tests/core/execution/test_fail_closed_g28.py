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
    data.setdefault("runtime_epoch", 7)
    original_fail = case.runtime.fail
    original_heartbeat = case.runtime.heartbeat

    def heartbeat(task_id, lease_token, **kwargs):
        original_heartbeat(task_id, lease_token, **kwargs)
        return SimpleNamespace(task_id=task_id, lease_id=lease_token, attempt_id=kwargs["attempt_id"],
                               fence=kwargs["fence"], runtime_epoch=case.runtime.tasks[task_id]["task"]["runtime_epoch"],
                               lease_expires_at=(datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat())

    monkeypatch.setattr(case.runtime, "heartbeat", heartbeat)

    def fail(*args, **kwargs):
        original_fail(*args, **kwargs)
        data["status"] = "failed"
        return {"task": dict(data)}

    def cancel(task_id, **kwargs):
        assert task_id == data["id"]
        assert kwargs["attempt_id"] == data["attempt_id"]
        assert kwargs["fence"] == data["fence"]
        assert kwargs["lease_id"] == "lease-result"
        assert kwargs["runtime_epoch"] == data["runtime_epoch"]
        assert kwargs["stop_observed"] is True
        assert isinstance(kwargs["outputs"], list)
        assert isinstance(kwargs["result"], dict)
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
    fault_calls = []

    class BrokenString(Exception):
        def __str__(self):
            raise OSError("error string failed")

    class BrokenProperties(Exception):
        @property
        def code(self):
            raise OSError("error code failed")

    def broken(*args, **kwargs):
        fault_calls.append(None)
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
    if fault == "heartbeat":
        case.host.run_task(case.task, lease_token="lease-result")
        assert fault_calls
        assert receipt(case)["status"] == "succeeded"
        assert receipt(case)["lease_diagnostic"]["category"] == "heartbeat_exception"
        assert not case.runtime.failures
        assert time.monotonic() - started < 10
        assert not any(t.name == "astrid-heartbeat-task-result" and t.is_alive()
                       for t in threading.enumerate())
        return
    with pytest.raises(AttributeError if fault == "normalized_attributes" else (OSError, HostError)):
        case.host.run_task(case.task, lease_token="lease-result")
    if fault == "failure_rpc":
        # Publication owns the attempt after an uncertain write; wait for that
        # owner before asserting that its observer and custody were released.
        for owner in list(case.host._reconciliation_owners.values()):
            owner.join(timeout=2)
        assert not case.host._reconciliation_owners
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


@pytest.mark.parametrize("caller_owned", [False, True])
def test_cancel_completion_error_propagates_and_retains_pending_evidence(case, monkeypatch, caller_owned):
    attempts = []
    error = OSError("Runtime cancel transport failed")
    captured = {}
    run_impl = case.host._run_task_impl

    def capture_terminal(*args, **kwargs):
        captured["terminal"] = kwargs["_terminal"]
        return run_impl(*args, **kwargs)

    monkeypatch.setattr(case.host, "_run_task_impl", capture_terminal)
    if not caller_owned:
        case.host.attempt_root = None
        allocate_root = case.host._allocate_attempt_root

        def capture_root(*args):
            case.root = allocate_root(*args)
            return case.root

        monkeypatch.setattr(case.host, "_allocate_attempt_root", capture_root)

    def cancel(task_id, **kwargs):
        attempts.append((task_id, kwargs))
        raise error

    monkeypatch.setattr(case.runtime, "cancel", cancel)

    def execute(*args, **kwargs):
        case.task["task"]["status"] = "cancel_requested"
        assert kwargs["cancelled"]()
        raise HostCancelled("cancel this invocation")

    monkeypatch.setattr(case.host, "_run_command_definition", execute)
    with pytest.raises(OSError, match="Runtime cancel transport failed") as raised:
        case.host.run_task(case.task, lease_token="lease-result")

    assert raised.value is error
    assert len(attempts) == 1
    assert attempts[0][1]["result"]["status"] == "cancelled"
    assert captured["terminal"]["evidence_pending"] is True
    assert not captured["terminal"].get("recorded")
    assert not case.runtime.failures
    cleanup = case.host.last_cleanup_receipt
    assert cleanup["intended_disposition"] == ("caller_owned" if caller_owned else "retained")
    assert case.root.exists()
    assert receipt(case)["publication"]["status"] == "not_settled"


@pytest.mark.parametrize("loss", ["task_read", "queued", "successor", "fence", "heartbeat", "expired", "resource_expired",
                                "null_expiry", "invalid_expiry", "naive_expiry"])
def test_lease_observation_ends_work_only_on_runtime_verdict(case, monkeypatch, loss, capsys):
    categories = {
        "task_read": "task_read_exception", "queued": "task_state_changed",
        "successor": "attempt_changed", "fence": "fence_changed",
        "heartbeat": "heartbeat_exception", "expired": "lease_expired",
        "resource_expired": "lease_expired", "null_expiry": "lease_metadata_invalid",
        "invalid_expiry": "lease_metadata_invalid", "naive_expiry": "lease_metadata_invalid",
    }
    class RuntimeFault(OSError):
        code = "runtime_unavailable"
        request_id = "req-lease-17"

        def __init__(self, message):
            super().__init__(message)
            self.message = message

    success = case.host._run_command_definition
    unknown = loss in {"task_read", "heartbeat", "expired", "resource_expired", "null_expiry", "invalid_expiry", "naive_expiry"}

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
        if unknown:
            time.sleep(0.12)
            return success(*args, **kwargs)
        polling_deadline = time.monotonic() + 2
        while case.task["task"]["status"] != "failed" and time.monotonic() < polling_deadline:
            time.sleep(0.01)
        raise HostCancelled("lease observer contained executor")

    monkeypatch.setattr(case.host, "_run_command_definition", execute)
    started = time.monotonic()
    if unknown:
        case.host.run_task(case.task, lease_token="lease-result")
        assert time.monotonic() - started < 10
        assert not any(t.name == "astrid-heartbeat-task-result" and t.is_alive()
                       for t in threading.enumerate())
        assert not case.runtime.failures and len(case.runtime.settlements) == 1
        assert receipt(case)["status"] == "succeeded"
        log = capsys.readouterr().err
        assert "do-not-record" not in log and "task read unavailable" not in log
        lines = [json.loads(line.split("generic host: lease ", 1)[1])
                 for line in log.splitlines() if "generic host: lease " in line]
        assert any(line["category"] == categories[loss] for line in lines)
        diagnostic = receipt(case)["lease_diagnostic"]
    else:
        with pytest.raises(HostError, match="lease lost"):
            case.host.run_task(case.task, lease_token="lease-result")
        assert_failed(case, started, code="lease_lost")
        assert len(case.runtime.failures) == 1
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
        assert "runtime_message" not in diagnostic
        assert "task read unavailable" not in json.dumps(diagnostic)
    if loss == "heartbeat":
        assert "runtime_message" not in diagnostic
        assert "do-not-record" not in json.dumps(diagnostic)
    terminal_diagnostic = receipt(case)["lease_diagnostic"]
    assert terminal_diagnostic == diagnostic
    if not unknown:
        assert case.task["task"]["status"] == "failed"
        assert not case.runtime.settlements


def test_lease_loss_keeps_last_successful_heartbeat_receipt(case, monkeypatch):
    pulsed = threading.Event()
    expiry = (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()
    original_heartbeat = case.runtime.heartbeat

    def heartbeat(*args, **kwargs):
        pulsed.set()
        result = original_heartbeat(*args, **kwargs)
        result.lease_expires_at = expiry
        return result

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
    assert diagnostic["observed"] == {"state": "queued", "attempt_id": "attempt-result", "fence": "3", "runtime_epoch": "7"}
    assert case.task["task"]["status"] == "failed"
    assert not case.runtime.settlements


@pytest.mark.parametrize("read_model", ["mapping", "resource"])
def test_renewal_reports_only_semantic_progress_and_never_begins_after_deadline(case, monkeypatch, read_model):
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
        # Liveness keeps renewing, but an equivalent rewrite earns no progress.
        checkpoint.write_text('{"percent":10,"phase":"work","noise":"new bytes"}')
        time.sleep(0.13)
        assert len(pulses) > stable_count
        assert [progress for _, progress in pulses if progress is not None] == [{"phase": "work", "percent": 10}]
        checkpoint.write_text('{"phase":"work","percent":20}')
        time.sleep(0.13)
        assert [progress for _, progress in pulses if progress is not None] == [
            {"phase": "work", "percent": 10}, {"phase": "work", "percent": 20}]
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
                # A definitive fenced lease error must contain immediately.
                # Transient transport recovery has separate bounded cases below.
                if outcome == "lease_lost":
                    from astrid_runtime_client import ApiError
                    monkeypatch.setattr(case.runtime, "task", lambda *_: (_ for _ in ()).throw(ApiError(409, "lease_fenced", "attempt lease has expired")))
                    assert kwargs["cancelled"]()
                else:
                    # Expiry alone is unknown; a renewal refusal ends work.
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
        if outcome in {"lease_lost", "lease_expired"}:
            diagnostic = receipt(case)["lease_diagnostic"]
            # Existing adapter -> Runtime fail carrier preserves this bounded
            # record inside result.error before the local scope is released.
            assert stored["result"]["error"]["diagnostic"] == diagnostic
            assert len(json.dumps(diagnostic)) < 1200
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
            assert 0 < self.timeout <= 0.3
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


@pytest.fixture
def lease_clock(case, monkeypatch):
    """Drive the actual attempt observer synchronously, with no elapsed sleeps."""
    clock = SimpleNamespace(at=0.0, ttl=0.2, pulses=[], reads=[], tick=None,
                            heartbeat_fault=None, read_fault=None, terminal=None)
    origin = datetime(2035, 1, 1, tzinfo=timezone.utc)

    class ClockDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            value = origin + timedelta(seconds=clock.at)
            return value.astimezone(tz) if tz is not None else value.replace(tzinfo=None)

    class PumpThread:
        def __init__(self, *, target, **_kwargs):
            self.target, self.ident = target, None
        def start(self):
            self.ident = 1
        def join(self, timeout=None):
            pass

    monkeypatch.setattr(generic_host.time, "monotonic", lambda: clock.at)
    monkeypatch.setattr(generic_host, "datetime", ClockDateTime)
    monkeypatch.setattr(generic_host.threading, "Thread", PumpThread)
    monkeypatch.setattr(generic_host, "_LEASE_POLL_SECONDS", 0.05)
    case.host.execution_policy = replace(case.host.execution_policy, deadline_seconds=10)
    data = case.task["task"]

    def expiry():
        return (origin + timedelta(seconds=clock.at + clock.ttl)).isoformat()

    data["lease_expires_at"] = expiry()

    def heartbeat(task_id, lease_token, **kwargs):
        assert (task_id, lease_token, kwargs["attempt_id"], kwargs["fence"]) == (
            "task-result", "lease-result", "attempt-result", 3)
        clock.pulses.append((clock.at, dict(kwargs)))
        if clock.heartbeat_fault is not None:
            clock.heartbeat_fault()
        data["lease_expires_at"] = expiry()
        # Resource responses exercise the generated client's supported shape.
        return SimpleNamespace(attempt_id="attempt-result", fence=3, runtime_epoch=7,
                               task_id="task-result", lease_id="lease-result",
                               lease_expires_at=data["lease_expires_at"])

    def task(task_id, **_kwargs):
        assert task_id == "task-result"
        clock.reads.append(clock.at)
        if clock.read_fault is not None:
            clock.read_fault()
        return {"task": dict(data)}

    monkeypatch.setattr(case.runtime, "heartbeat", heartbeat)
    monkeypatch.setattr(case.runtime, "task", task)

    def run(*, until=0.85, before=None, after=None):
        def execute(*_args, _terminal, **_kwargs):
            clock.terminal = _terminal
            _terminal["stage"] = "execution"
            _terminal["progress"] = {"phase": "work", "percent": 10}
            try:
                _terminal["renew_progress"]()
            except Exception as exc:
                if not _terminal["lease_transport_fault"]("heartbeat_exception", exc):
                    raise
            clock.initial_pulses = len(clock.pulses)
            stop = _terminal["pump_stop"]

            def wait(seconds):
                if stop.is_set():
                    return True
                clock.at = round(clock.at + seconds, 9)
                if clock.tick is not None:
                    clock.tick()
                return clock.at > until

            monkeypatch.setattr(stop, "wait", wait)
            if before is not None:
                before(_terminal)
            if not _terminal.get("forced"):
                _terminal["pump_thread"].target()
            if after is not None:
                after(_terminal)
            if not _terminal.get("forced"):
                _terminal["status"] = "succeeded"
            return _terminal

        monkeypatch.setattr(case.host, "_run_task_impl", execute)
        return case.host.run_task(case.task, lease_token="lease-result")

    clock.run = run
    return clock


def test_silent_owner_renews_across_multiple_ttls_without_progress_credit(case, lease_clock):
    terminal = lease_clock.run()
    assert lease_clock.at > 4 * lease_clock.ttl
    assert len(lease_clock.pulses) >= 5
    assert not terminal.get("forced") and not case.runtime.failures
    assert terminal["progress"] == {"phase": "work", "percent": 10}
    progress_pulses = [kwargs["progress"] for _, kwargs in lease_clock.pulses if "progress" in kwargs]
    assert progress_pulses == [{"phase": "work", "percent": 10}]
    assert all(at < 10 for at, _ in lease_clock.pulses)
    assert terminal["lease_diagnostic"]["last_successful_heartbeat_expiry"] == case.task["task"]["lease_expires_at"]


def test_silent_owner_renewal_cannot_extend_immutable_absolute_deadline(case, lease_clock):
    case.task["task"]["deadline_at"] = "2035-01-01T00:00:00.450000+00:00"
    terminal = lease_clock.run()
    assert terminal["lifetime"].absolute_deadline == 0.45
    assert terminal["forced"] == "timeout" and terminal["cancel_signal"].is_set()
    assert lease_clock.at == 0.45
    assert len(lease_clock.pulses) > 2
    assert all(at < 0.45 for at, _ in lease_clock.pulses)
    assert receipt(case)["error"]["code"] == "timeout"
    assert not case.runtime.settlements


@pytest.mark.parametrize("change,category", [
    ({"attempt_id": "successor"}, "attempt_changed"),
    ({"fence": 4}, "fence_changed"),
    ({"status": "queued"}, "task_state_changed"),
])
def test_changed_owner_stops_before_liveness_or_publication(case, lease_clock, change, category):
    def before(_terminal):
        case.task["task"].update(change)

    def after(terminal):
        with pytest.raises(HostCancelled):
            terminal["completion_call"](case.runtime.settle, "task-result", "lease-result",
                                        result={}, outputs=[], effect=None,
                                        attempt_id="attempt-result", fence=3)

    terminal = lease_clock.run(before=before, after=after)
    assert terminal["forced"] == "lease_lost"
    assert terminal["lease_diagnostic"]["category"] == category
    assert len(lease_clock.pulses) == lease_clock.initial_pulses
    assert not case.runtime.settlements
    assert receipt(case)["error"]["code"] == "lease_lost"


@pytest.mark.parametrize("operation", ["read", "heartbeat"])
@pytest.mark.parametrize("typed", [False, True])
def test_transient_transport_recovers_inside_last_acknowledged_validity(case, lease_clock, operation, typed):
    faults = []

    def transient():
        if lease_clock.at > 0 and not faults:
            faults.append(lease_clock.at)
            if typed:
                from astrid_runtime_client import ApiError
                raise ApiError(0, "transport_timeout", "private transport detail must not be recorded")
            raise TimeoutError("private transport detail must not be recorded")

    if operation == "read":
        lease_clock.read_fault = transient
    else:
        lease_clock.heartbeat_fault = transient
    terminal = lease_clock.run()
    assert faults and 0 < faults[0] < lease_clock.ttl
    assert not terminal.get("forced") and not case.runtime.failures
    assert any(at > faults[0] for at, _ in lease_clock.pulses)
    assert lease_clock.at > 4 * lease_clock.ttl
    assert terminal["progress"] == {"phase": "work", "percent": 10}


def test_initial_transport_fault_can_recover_within_acknowledged_claim_validity(case, lease_clock, monkeypatch):
    faults, acknowledged = [], []
    original_heartbeat = case.runtime.heartbeat

    def heartbeat(*args, **kwargs):
        result = original_heartbeat(*args, **kwargs)
        acknowledged.append((lease_clock.at, result.lease_expires_at))
        return result

    monkeypatch.setattr(case.runtime, "heartbeat", heartbeat)

    def transient():
        if not faults:
            faults.append(lease_clock.at)
            raise TimeoutError("private transport detail must not be recorded")

    lease_clock.heartbeat_fault = transient
    terminal = lease_clock.run()
    assert faults == [0.0]
    assert not terminal.get("forced") and not case.runtime.failures
    assert lease_clock.at > 4 * lease_clock.ttl
    successful_times = [at for at, _ in acknowledged]
    assert len(successful_times) > 4
    assert any(at > faults[0] for at in successful_times)
    interval = [faults[0], *successful_times, lease_clock.at]
    assert all(right - left <= lease_clock.ttl for left, right in zip(interval, interval[1:]))
    diagnostic = terminal["lease_diagnostic"]
    assert diagnostic["last_successful_heartbeat_expiry"] == acknowledged[-1][1]
    assert datetime.fromisoformat(diagnostic["last_successful_heartbeat_at_utc"]) == (
        datetime(2035, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=acknowledged[-1][0]))


@pytest.mark.parametrize("operation", ["read", "heartbeat"])
@pytest.mark.parametrize("verdict", ["runtime", "deadline"])
def test_sustained_transport_keeps_work_until_runtime_verdict_or_deadline(case, lease_clock, monkeypatch, operation, verdict):
    case.host.execution_policy = replace(case.host.execution_policy, deadline_seconds=0.6 if verdict == "deadline" else 10)
    consumed = []
    original_task = case.runtime.task

    def task(*args, **kwargs):
        result = original_task(*args, **kwargs)
        if result["task"]["fence"] == 4:
            consumed.append((lease_clock.at, len(lease_clock.pulses)))
        return result

    monkeypatch.setattr(case.runtime, "task", task)

    def unavailable():
        if 0 < lease_clock.at < 0.6 or verdict == "deadline":
            if lease_clock.at > 0:
                raise ConnectionError("private transport detail must not be recorded")

    if operation == "read":
        lease_clock.read_fault = unavailable
    else:
        lease_clock.heartbeat_fault = unavailable

    def tick():
        if verdict == "runtime" and lease_clock.at >= 0.6:
            case.task["task"]["fence"] = 4

    lease_clock.tick = tick
    terminal = lease_clock.run()
    assert terminal["forced"] == ("timeout" if verdict == "deadline" else "lease_lost")
    assert terminal["cancel_signal"].is_set()
    if verdict == "deadline":
        assert lease_clock.at == 0.6
        assert all(at < 0.6 for at, _ in lease_clock.pulses)
    else:
        assert 0.6 <= lease_clock.at <= 0.65
        assert len(consumed) == 1
        consumed_at, pulse_count = consumed[0]
        assert len(lease_clock.pulses) == pulse_count
        assert all(at < consumed_at for at, _ in lease_clock.pulses)
    assert lease_clock.at > lease_clock.ttl
    assert any(at > lease_clock.ttl for at in lease_clock.reads)
    assert not case.runtime.settlements
    diagnostic = terminal["lease_diagnostic"]
    assert diagnostic["last_successful_heartbeat_expiry"] == "2035-01-01T00:00:00.200000+00:00"
    assert "private transport detail" not in json.dumps(diagnostic)
    assert len(json.dumps(diagnostic)) < 1200
    assert receipt(case)["lease_diagnostic"] == diagnostic
    assert receipt(case)["error"]["code"] == ("timeout" if verdict == "deadline" else "lease_lost")
    assert len(case.runtime.failures) == 1
    if verdict == "runtime":
        assert case.runtime.failures[0][3]["failure_diagnostic"] == diagnostic


def test_committed_but_unacknowledged_heartbeat_does_not_fail_attempt(case, lease_clock):
    case.host.execution_policy = replace(case.host.execution_policy, deadline_seconds=60)
    committed = []

    def dropped_reply():
        if lease_clock.at > 0:
            committed.append(lease_clock.at)
            # Runtime renews before the response disappears for 25 seconds.
            case.task["task"]["lease_expires_at"] = "2035-01-01T00:05:00+00:00"
            raise TimeoutError("reply dropped after commit")

    lease_clock.heartbeat_fault = dropped_reply

    def complete(terminal):
        assert not terminal.get("forced")
        terminal["completion_call"](case.runtime.settle, "task-result", "lease-result",
                                    result={}, outputs=[], effect=None,
                                    attempt_id="attempt-result", fence=3)

    terminal = lease_clock.run(until=24.95, after=complete)
    assert lease_clock.at == 25
    assert committed and committed[-1] == 24.95
    assert not terminal.get("forced") and not terminal["cancel_signal"].is_set()
    assert not case.runtime.failures
    assert len(case.runtime.settlements) == 1
    assert terminal["lease_diagnostic"]["last_successful_heartbeat_expiry"] == "2035-01-01T00:00:00.200000+00:00"


def test_heartbeat_acknowledgement_after_prior_expiry_restores_observation(case, lease_clock):
    delayed = []

    def late_success():
        if lease_clock.at > 0 and not delayed:
            delayed.append(lease_clock.at)
            lease_clock.at = 0.25

    lease_clock.heartbeat_fault = late_success
    terminal = lease_clock.run()
    assert delayed and lease_clock.at > 4 * lease_clock.ttl
    assert not terminal.get("forced") and not case.runtime.failures
    assert len(lease_clock.pulses) > lease_clock.initial_pulses + 1
    assert terminal["lease_diagnostic"]["last_successful_heartbeat_expiry"] == case.task["task"]["lease_expires_at"]


@pytest.mark.parametrize("code", ["lease_fenced", "forbidden"])
def test_typed_rejection_is_definitive_despite_remaining_validity(case, lease_clock, code):
    class RuntimeRejection(OSError):
        request_id = "req-bounded-lease"

        def __init__(self):
            super().__init__("private exception payload")
            self.code = code
            self.status = 403 if code == "forbidden" else 409 if code == "lease_fenced" else 0

    def rejected():
        if lease_clock.at > 0:
            raise RuntimeRejection()

    lease_clock.heartbeat_fault = rejected
    terminal = lease_clock.run()
    assert terminal["forced"] == "lease_lost"
    assert 0 < lease_clock.at < lease_clock.ttl
    assert len(lease_clock.pulses) == lease_clock.initial_pulses + 1
    assert terminal["lease_diagnostic"]["runtime_code"] == code
    assert not case.runtime.settlements



def test_task_read_does_not_replace_last_acknowledged_heartbeat(case, lease_clock):
    def before(_terminal):
        case.task["task"]["lease_expires_at"] = "2035-01-01T00:00:05+00:00"

    def unavailable():
        if lease_clock.at > 0:
            raise ConnectionError("heartbeat transport unavailable")

    lease_clock.heartbeat_fault = unavailable
    terminal = lease_clock.run(before=before)
    assert not terminal.get("forced") and not case.runtime.failures
    assert lease_clock.at > 4 * lease_clock.ttl
    assert any(at > lease_clock.ttl for at, _ in lease_clock.pulses)
    assert terminal["lease_diagnostic"]["last_successful_heartbeat_expiry"] == "2035-01-01T00:00:00.200000+00:00"


def test_periodic_liveness_keeps_asking_without_inventing_a_deadline(case, lease_clock):
    def before(terminal):
        terminal["lifetime"].authority = lambda: None
        terminal["lifetime"].phase = "execution"
        assert terminal["lifetime"].deadline() is None

    terminal = lease_clock.run(before=before)
    assert not terminal.get("forced") and not case.runtime.failures
    assert lease_clock.at > 4 * lease_clock.ttl
    assert len(lease_clock.pulses) > lease_clock.initial_pulses


def test_invalid_runtime_response_is_unknown_and_keeps_polling(case, lease_clock):
    from astrid_runtime_client import ApiError

    def malformed():
        if lease_clock.at > 0:
            raise ApiError(0, "invalid_response", "unreadable response")

    lease_clock.heartbeat_fault = malformed
    terminal = lease_clock.run()
    assert not terminal.get("forced") and not case.runtime.failures
    assert lease_clock.at > 4 * lease_clock.ttl
    assert terminal["lease_diagnostic"]["runtime_code"] == "invalid_response"


def test_lease_verdict_is_logged_with_reason(case, lease_clock, capsys):
    def unavailable():
        if 0 < lease_clock.at < 0.4:
            raise TimeoutError("private transport details")

    lease_clock.read_fault = unavailable

    def tick():
        if lease_clock.at >= 0.4:
            case.task["task"]["fence"] = 4

    lease_clock.tick = tick
    terminal = lease_clock.run()
    assert terminal["forced"] == "lease_lost"
    lines = [json.loads(line.split("generic host: lease ", 1)[1])
             for line in capsys.readouterr().err.splitlines() if "generic host: lease " in line]
    assert [line["category"] for line in lines if line["verdict"] is None] == ["task_read_exception", "fence_changed"]
    verdicts = [line for line in lines if line["verdict"]]
    assert len(verdicts) == 1
    assert verdicts[0] == {
        "task_id": "task-result", "attempt_id": "attempt-result", "category": "fence_changed",
        "last_acknowledged_heartbeat": "2035-01-01T00:00:00+00:00",
        "elapsed_seconds": 0.4, "verdict": "lease_lost", "runtime_code": None,
    }


def test_heartbeat_requests_declared_authority_window(monkeypatch):
    from astrid.core.execution.guards import ExecutionGuardPolicy

    client = RuntimeProtocolClient("http://127.0.0.1:1", "test-worker")
    policy = ExecutionGuardPolicy(authority_window_seconds=120)
    host = generic_host.GenericPackHost(pack_roots=[], client=client, execution_policy=policy)
    assert host.execution_policy is client.execution_policy is policy
    client._attempt_runtime_epochs = {"attempt-result": 7}
    monkeypatch.setattr(client, "_current_runtime_epoch", lambda **_kwargs: 7)
    seen = []

    def request(_self, method, path, **kwargs):
        assert method == "POST" and path == "/v1/attempts/attempt-result/heartbeat"
        seen.append(json.loads(kwargs["body"]))
        return 200, {}, json.dumps({"data": {}, "receipt": {}}).encode()

    monkeypatch.setattr(type(client.generated), "_request", request)
    client.heartbeat("task-result", "lease-result", attempt_id="attempt-result", fence=3)
    assert seen == [{"lease_id": "lease-result", "fence": 3, "runtime_epoch": 7}]
    assert generic_host._LEASE_POLL_SECONDS == generic_host._DEFAULT_EXECUTION_POLICY.authority_window_seconds / 60
    assert client._lifecycle_client().timeout == policy.authority_window_seconds / 150


@pytest.mark.parametrize("late_lock", [False, True])
def test_adapter_caps_each_transport_and_lock_wait_to_remaining_validity(monkeypatch, late_lock):
    clock = SimpleNamespace(at=10.0)
    deadline, seen = 10.2, []
    monkeypatch.setattr(generic_host.time, "monotonic", lambda: clock.at)

    class Lock:
        def acquire(self, *, timeout):
            seen.append(("lock", timeout))
            assert timeout == pytest.approx(0.17)
            clock.at = deadline if late_lock else 10.05
            return True
        def release(self):
            seen.append(("release", None))

    class Generated:
        timeout = 30
        def health(self):
            seen.append(("health", self.timeout))
            assert self.timeout == pytest.approx(0.2)
            clock.at = 10.03
            return {"runtime_epoch": 7}
        def heartbeat_attempt(self, attempt_id, **kwargs):
            seen.append(("heartbeat", self.timeout))
            assert self.timeout == pytest.approx(0.15)
            assert attempt_id == "attempt-result" and kwargs["fence"] == 3
            assert kwargs["runtime_epoch"] == 7
            return {"lease_expires_at": "2035-01-01T00:00:01+00:00"}
        def get_task(self, task_id):
            seen.append(("task", self.timeout))
            assert self.timeout == pytest.approx(0.15)
            return {"id": task_id}

    client = object.__new__(RuntimeProtocolClient)
    client.generated, client._heartbeat_lock = Generated(), Lock()
    client._attempt_runtime_epochs = {"attempt-result": 7}
    client._heartbeat_sequence, client._heartbeat_session = 0, "bounded"
    if late_lock:
        with pytest.raises(TimeoutError):
            client.heartbeat("task-result", "lease-result", attempt_id="attempt-result", fence=3,
                             deadline=deadline)
        assert not any(kind == "heartbeat" for kind, _ in seen)
    else:
        client.heartbeat("task-result", "lease-result", attempt_id="attempt-result", fence=3,
                         deadline=deadline)
        assert client.task("task-result", deadline=deadline)["id"] == "task-result"
    assert seen[-1][0] == ("release" if late_lock else "task")
    assert client.generated.timeout == 30


def test_hung_command_is_stopped_and_reaped_by_unchanged_absolute_deadline(case, lease_clock, monkeypatch):
    """Compose the actual host command finalizer with retained fake custody."""
    import signal
    from astrid.core.execution import process_group as groups
    from tests.core.execution.test_process_group import _process, _snapshot

    case.task["task"]["deadline_at"] = "2035-01-01T00:00:00.450000+00:00"
    class OwnedProcess(SimpleNamespace):
        __hash__ = object.__hash__

    process, census, signals, waits = OwnedProcess(**vars(_process())), _snapshot(), [], []
    process.poll = lambda: None if process.pid in census else 0
    process.wait = lambda **kwargs: waits.append(kwargs) or 0

    def signal_owned(pid, signum):
        signals.append((pid, signum, lease_clock.at))
        census.pop(pid)

    process._astrid_custody.signal_owner = signal_owned
    monkeypatch.setattr(groups, "_process_snapshot", lambda: dict(census))
    monkeypatch.setattr(generic_host, "spawn_task_scope", lambda *_args, **_kwargs: process)
    monkeypatch.setattr(case.host, "_child_environment", lambda *_args, **_kwargs: ({}, {}))
    monkeypatch.setattr(generic_host, "_network_sandbox_argv", lambda argv, *_args: argv)
    capture = SimpleNamespace(error=None, start=lambda *_: None, persist=lambda: None,
                              finish=lambda: None, tails=lambda: ("", ""))
    monkeypatch.setattr(generic_host, "_TaskOutputCapture", lambda *_args: capture)

    def before(terminal):
        output_root = case.root / "outputs"
        output_root.mkdir()

        def cancelled():
            terminal["pump_thread"].target()
            return terminal["cancel_signal"].is_set()

        with pytest.raises(HostCancelled):
            generic_host.GenericPackHost._run_command_definition(
                case.host, case.host.capabilities["test.echo"], {}, output_root, case.root,
                cancelled=cancelled)
        assert lease_clock.at == 0.45
        assert terminal["lifetime"].absolute_deadline == 0.45
        assert receipt(case)["error"]["code"] == "timeout"
        assert not case.host._active_processes

    terminal = lease_clock.run(before=before)
    assert terminal["forced"] == "timeout"
    assert signals == [(200, signal.SIGTERM, 0.45), (100, signal.SIGTERM, 0.45)]
    assert waits == [{"timeout": 0}]
    assert set(census) == {900}
    assert not case.runtime.settlements


@pytest.mark.parametrize("status,code", [
    (400, "protocol_error"), (401, "invalid_response"), (403, "invalid_response"),
    (404, "invalid_response"), (409, "conflict"), (410, "retired_route"), (422, "validation_error"),
])
def test_validation_and_route_failures_are_unknown(case, lease_clock, status, code):
    from astrid_runtime_client import ApiError

    def refused():
        if lease_clock.at > 0:
            raise ApiError(status, code, "untrusted transport detail")

    lease_clock.heartbeat_fault = refused
    terminal = lease_clock.run()
    assert not terminal.get("forced") and not case.runtime.failures
    assert lease_clock.at > 4 * lease_clock.ttl
    assert terminal["lease_diagnostic"]["runtime_code"] == code


@pytest.mark.parametrize("field,value", [
    ("fence", "3"), ("fence", True), ("runtime_epoch", "7"), ("runtime_epoch", None),
    ("attempt_id", None), ("attempt_id", 3), ("task_id", []), ("lease_id", ""),
    ("lease_expires_at", None), ("lease_expires_at", "yesterday"),
])
def test_malformed_heartbeat_acknowledgement_is_unknown(case, lease_clock, monkeypatch, field, value):
    original = case.runtime.heartbeat

    def malformed(*args, **kwargs):
        result = original(*args, **kwargs)
        if lease_clock.at > 0:
            setattr(result, field, value)
        return result

    monkeypatch.setattr(case.runtime, "heartbeat", malformed)
    terminal = lease_clock.run()
    assert not terminal.get("forced") and not case.runtime.failures
    assert len(lease_clock.pulses) > lease_clock.initial_pulses + 1
    assert terminal["lease_diagnostic"]["last_successful_heartbeat_expiry"] == "2035-01-01T00:00:00.200000+00:00"
    assert terminal["lease_diagnostic"]["category"] == "heartbeat_exception"


def test_empty_heartbeat_acknowledgement_never_records_success(case, lease_clock, monkeypatch):
    monkeypatch.setattr(case.runtime, "heartbeat", lambda *_a, **_k: {})
    terminal = lease_clock.run()
    assert not terminal.get("forced")
    assert terminal["lease_diagnostic"]["last_successful_heartbeat_at_utc"] is None
    assert terminal["lease_diagnostic"]["category"] == "heartbeat_exception"


@pytest.mark.parametrize("field,value", [("fence", 4), ("attempt_id", "successor"), ("runtime_epoch", 8)])
def test_valid_different_heartbeat_identity_is_a_verdict(case, lease_clock, monkeypatch, field, value):
    original = case.runtime.heartbeat

    def changed(*args, **kwargs):
        result = original(*args, **kwargs)
        if lease_clock.at > 0:
            setattr(result, field, value)
        return result

    monkeypatch.setattr(case.runtime, "heartbeat", changed)
    terminal = lease_clock.run()
    assert terminal["forced"] == "lease_lost"
    assert terminal["lease_diagnostic"]["category"] == f"heartbeat_{field}_changed"


def test_invalid_progress_retries_heartbeat_without_progress(case, lease_clock, monkeypatch, capsys):
    from astrid_runtime_client import ApiError
    original = case.runtime.heartbeat
    rejected = []

    def validate(*args, **kwargs):
        if kwargs.get("progress", {}).get("percent") == 101:
            rejected.append(kwargs["progress"])
            raise ApiError(422, "validation_error", "progress.percent must be between 0 and 100")
        return original(*args, **kwargs)

    monkeypatch.setattr(case.runtime, "heartbeat", validate)

    def before(terminal):
        terminal["progress"] = {"percent": 101}

    terminal = lease_clock.run(before=before)
    assert rejected == [{"percent": 101}]
    assert not terminal.get("forced") and not case.runtime.failures
    assert len(lease_clock.pulses) > 4
    assert "heartbeat_progress_invalid" in capsys.readouterr().err
    assert terminal["lease_diagnostic"]["category"] == "observing"
    assert "runtime_code" not in terminal["lease_diagnostic"]


def test_task_epoch_change_is_a_verdict_without_health_or_heartbeat(case, lease_clock):
    def before(_terminal):
        case.task["task"]["runtime_epoch"] = 8

    terminal = lease_clock.run(before=before)
    assert terminal["forced"] == "lease_lost"
    assert terminal["lease_diagnostic"]["category"] == "runtime_epoch_changed"
    assert len(lease_clock.pulses) == lease_clock.initial_pulses


def test_late_settle_after_runtime_silence_is_a_reasoned_failure(case, lease_clock, monkeypatch, capsys):
    from astrid_runtime_client import ApiError
    case.host.execution_policy = replace(case.host.execution_policy, deadline_seconds=60)
    lease_clock.ttl = 30.0
    sent = []

    def silence():
        if lease_clock.at > 0:
            raise TimeoutError("Runtime silent")

    lease_clock.read_fault = lease_clock.heartbeat_fault = silence
    protocol = RuntimeProtocolClient("http://127.0.0.1:1", "test-worker")
    protocol._attempt_runtime_epochs = {"attempt-result": 7}

    class Generated:
        timeout = 30
        def health(self):
            raise TimeoutError("health remains silent")
        def settle_attempt(self, attempt_id, body, **_kwargs):
            assert lease_clock.at > 30
            assert attempt_id == "attempt-result"
            assert body["runtime_epoch"] == 7 and body["fence"] == 3
            sent.append(body)
            raise ApiError(409, "lease_fenced", "attempt lease is stale or already settled")

    protocol.generated = Generated()
    monkeypatch.setattr(case.runtime, "settle", protocol.settle)

    def complete(terminal):
        terminal["completion_call"](case.runtime.settle, "task-result", "lease-result",
                                    result={}, outputs=[], effect=None,
                                    attempt_id="attempt-result", fence=3)

    with pytest.raises(generic_host.HostLeaseLost, match="Runtime refused settle: lease_fenced"):
        lease_clock.run(until=35, after=complete)
    assert len(sent) == 1 and not case.runtime.settlements and not case.runtime.failures
    assert lease_clock.terminal["lease_diagnostic"]["last_successful_heartbeat_expiry"] == "2035-01-01T00:00:30+00:00"
    assert lease_clock.terminal["status"] == "failed"
    assert lease_clock.terminal["recorded"] and lease_clock.terminal["retain_spool"]
    assert receipt(case)["status"] == "failed"
    assert "lease_fenced" in receipt(case)["error"]["message"]
    lines = [json.loads(line.split("generic host: lease ", 1)[1])
             for line in capsys.readouterr().err.splitlines() if "generic host: lease " in line]
    assert any(line["category"] == "publication_refused" and line["verdict"] == "lease_lost"
               and line["runtime_code"] == "lease_fenced" for line in lines)


@pytest.mark.parametrize("outcome", ["success", "not_found", "lease_fenced", "epoch", "cancel_requested"])
def test_publication_replays_past_thirty_seconds_until_runtime_disposition(case, monkeypatch, capsys, outcome):
    from astrid_runtime_client import ApiError
    original_settle = case.runtime.settle
    now, attempts = [time.monotonic()], []
    terminal_seen = []
    original_impl = case.host._run_task_impl

    def execute(*args, _terminal, **kwargs):
        terminal_seen.append(_terminal)
        return original_impl(*args, _terminal=_terminal, **kwargs)

    def settle(*args, **kwargs):
        attempts.append((now[0], args, kwargs))
        if len(attempts) < 12:
            raise TimeoutError("settlement reply unavailable")
        if outcome == "lease_fenced":
            raise ApiError(409, "lease_fenced", "attempt fenced")
        return original_settle(*args, **kwargs)

    original_task = case.runtime.task

    def task(task_id):
        if len(attempts) >= 11:
            if outcome == "not_found":
                raise ApiError(404, "not_found", "task missing")
            if outcome == "epoch":
                return {"task": {**case.task["task"], "runtime_epoch": 8}}
            if outcome == "cancel_requested":
                return {"task": {**case.task["task"], "status": "cancel_requested"}}
        return original_task(task_id)

    monkeypatch.setattr(case.host, "_run_task_impl", execute)
    monkeypatch.setattr(case.runtime, "settle", settle)
    monkeypatch.setattr(case.runtime, "task", task)
    monkeypatch.setattr(generic_host.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(generic_host.time, "sleep", lambda delay: now.__setitem__(0, now[0] + delay))
    with pytest.raises(TimeoutError):
        case.host.run_task(case.task, lease_token="lease-result")
    for owner in list(case.host._reconciliation_owners.values()):
        owner.join(timeout=2)
    assert not case.host._reconciliation_owners
    assert now[0] - attempts[0][0] > 30
    assert len(attempts) == (12 if outcome in {"success", "lease_fenced"} else 11)
    assert all(args == attempts[0][1] and kwargs == attempts[0][2] for _, args, kwargs in attempts[1:])
    terminal = terminal_seen[0]
    assert terminal["recorded"]
    expected = "succeeded" if outcome == "success" else "cancelled" if outcome == "cancel_requested" else "failed"
    assert receipt(case)["status"] == ("partial" if expected == "failed" else expected)
    assert terminal["status"] == expected
    if outcome == "success":
        assert len(case.runtime.settlements) == 1
        assert terminal["settled"] is True
        assert receipt(case)["publication"]["status"] == "settled"
    else:
        assert terminal["retain_spool"] and terminal["settled"] is False
        assert not case.runtime.settlements
        log = capsys.readouterr().err
        if outcome == "cancel_requested":
            assert log.count('"verdict": "cancelled"') == 1
        else:
            assert "Runtime" in receipt(case)["error"]["message"]
            assert '"verdict": "lease_lost"' in log


def test_effective_authority_window_controls_observer_polling(case, lease_clock):
    case.host.execution_policy = replace(case.host.execution_policy, authority_window_seconds=600)
    terminal = lease_clock.run(until=0.45)
    times = [at for at, _ in lease_clock.pulses if at > 0]
    # Polls run on 50 ms ticks; 0.2 + 0.1 falls just after the 0.3 tick.
    assert times == [0.1, 0.2, 0.35, 0.45]
    assert all(right - left >= 0.099 for left, right in zip(times, times[1:]))
    assert not terminal.get("forced")


def test_expired_metadata_logs_once_per_diagnostic_change(case, lease_clock, monkeypatch, capsys):
    original = case.runtime.heartbeat

    def heartbeat(*args, **kwargs):
        result = original(*args, **kwargs)
        case.task["task"]["lease_expires_at"] = "2000-01-01T00:00:00+00:00"
        return result

    monkeypatch.setattr(case.runtime, "heartbeat", heartbeat)
    terminal = lease_clock.run()
    assert not terminal.get("forced")
    lines = [json.loads(line.split("generic host: lease ", 1)[1])
             for line in capsys.readouterr().err.splitlines() if "generic host: lease " in line]
    assert [line["category"] for line in lines] == ["lease_expired"]
    assert len(lease_clock.pulses) > 5


def test_health_route_absence_is_unknown_in_heartbeat_context(case, lease_clock, monkeypatch):
    from astrid_runtime_client import ApiError
    protocol = RuntimeProtocolClient("http://127.0.0.1:1", "test-worker")
    protocol._attempt_runtime_epochs = {"attempt-result": 7}

    class Generated:
        def health(self):
            raise ApiError(404, "not_found", "health route unavailable")

    protocol.generated = Generated()
    original = case.runtime.heartbeat

    def heartbeat(*args, **kwargs):
        if lease_clock.at == 0:
            return original(*args, **kwargs)
        return protocol.heartbeat(*args, **kwargs)

    monkeypatch.setattr(case.runtime, "heartbeat", heartbeat)
    terminal = lease_clock.run()
    assert not terminal.get("forced") and not case.runtime.failures
    assert terminal["lease_diagnostic"]["runtime_code"] == "not_found"


@pytest.mark.parametrize("state", ["cancelled", "failed", "queued"])
def test_observer_consumes_cleared_runtime_owner(case, lease_clock, state):
    def before(_terminal):
        case.task["task"].update(state=state, attempt_id=None, lease_id=None,
                                 executor_id=None, lease_expires_at=None)

    terminal = lease_clock.run(before=before)
    assert terminal["forced"] == ("cancelled" if state == "cancelled" else "lease_lost")
    assert terminal["cancel_signal"].is_set()
    assert len(lease_clock.pulses) == lease_clock.initial_pulses
    assert not case.runtime.settlements


def test_parent_read_outage_logs_once_across_successful_child_heartbeats(case, lease_clock, capsys):
    cycles = []

    def before(terminal):
        def parent_authority():
            cycles.append(lease_clock.at)
            terminal["lease_transport_fault"]("interactive_authority_exception", TimeoutError("parent read lost"))
        lease_clock.tick = lambda: parent_authority() if lease_clock.at in {0.05, 0.10, 0.20} else None

    terminal = lease_clock.run(until=0.20, before=before)
    assert cycles == [0.05, 0.1, 0.2]
    assert len(lease_clock.pulses) >= lease_clock.initial_pulses + 3
    assert not terminal.get("forced")
    lines = [json.loads(line.split("generic host: lease ", 1)[1])
             for line in capsys.readouterr().err.splitlines() if "generic host: lease " in line]
    assert [line["category"] for line in lines] == ["interactive_authority_exception"]


@pytest.mark.parametrize("category,verdict", [("observing", None), ("publication_terminal", "cancelled")])
def test_recovery_and_publication_verdict_clear_old_transport_fields(case, lease_clock, category, verdict):
    from astrid_runtime_client import ApiError

    def after(terminal):
        error = ApiError(0, "transport_timeout", "reply lost")
        error.request_id = "old-request"
        terminal["lease_transport_fault"]("task_read_exception", error)
        assert terminal["lease_diagnostic"]["request_id"] == "old-request"
        terminal["record_lease_diagnostic"](category, verdict=verdict)
        assert not {"runtime_code", "request_id", "exception_type"} & terminal["lease_diagnostic"].keys()

    lease_clock.run(after=after)


def test_fast_cancellation_logs_consumed_verdict_once(case, monkeypatch, capsys):
    def execute(*args, **kwargs):
        case.task["task"]["status"] = "cancel_requested"
        assert kwargs["cancelled"]()
        assert kwargs["cancelled"]()
        raise HostCancelled("cancel this invocation")

    monkeypatch.setattr(generic_host, "_LEASE_POLL_SECONDS", 60)
    monkeypatch.setattr(case.host, "_run_command_definition", execute)
    result = case.host.run_task(case.task, lease_token="lease-result")
    assert result["cancelled"] and receipt(case)["status"] == "cancelled"
    lines = [json.loads(line.split("generic host: lease ", 1)[1])
             for line in capsys.readouterr().err.splitlines() if "generic host: lease " in line]
    assert [(line["category"], line.get("verdict")) for line in lines] == [("cancel_requested", "cancelled")]
