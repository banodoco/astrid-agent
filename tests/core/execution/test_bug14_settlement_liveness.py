"""BUG-14 CPU replay through the real host and the pinned Runtime service.

The source bytes and descriptor pair are from DEEP-BUG14/sub/2/cpu_regression.py.
The successful history fixture is the same live Comfy contract used by that replay.
"""
from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core._shared.result_manifest import build_manifest, write_manifest
from astrid.core.execution import generic_host
from astrid.core.execution.generic_host import RuntimeProtocolClient
from astrid_runtime_client.generated import ApiError
from runtime_protocol.errors import ConflictError, LeaseError
from runtime_protocol.service import RuntimeService
from runtime_protocol.store import RealmStore
from tests.test_generic_host_result_json import host_case  # noqa: F401


FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"


@pytest.fixture
def settlement_case(host_case, tmp_path, monkeypatch):
    case = host_case
    realm = tmp_path / "realm"
    RealmStore.initialize(realm).close()
    service = RuntimeService(realm)
    case.service = service
    case.writes, case.failures, case.beats, case.cancels, case.executions = [], [], [], [], []
    case.release = threading.Event()
    case.renewed = threading.Event()
    case.heartbeat_inflight, case.committed, case.heartbeat_rejected = (
        threading.Event(), threading.Event(), threading.Event())
    case.mode = "conflict"
    case.hidden = False
    project = service.create_project({"slug": "bug14", "name": "BUG14"})
    source = (FIXTURES / "bug14/qwen_image_2_1_turbo.py").read_bytes()
    assert hashlib.sha256(source).hexdigest() == "a47e24e6da4dfb7a41413e563fa90dd4e2d45a49a423ee132e5c00cbc27371b9"
    service.ingest(project["id"], source, media_type="text/x-python",
                   original_name="qwen_image_2_1_turbo.py", idempotency_key="source")

    pack = case.host.capabilities["test.echo"].manifest_path
    definition = json.loads(pack.read_text())
    definition["outputs"].append({"name": "result_manifest", "type": "file"})
    pack.write_text(json.dumps(definition))
    case.host.discover()
    digest = case.host.capabilities["test.echo"].capability_digest
    service.register_capability({"capability_id": "test.echo", "definition_digest": digest})
    service.register_executor({"executor_id": "bug14-worker", "capabilities": ["test.echo"]}, idempotency_key="worker")
    created = service.create_task({"capability_id": "test.echo", "capability_digest": digest,
                                   "project": project["id"], "idempotency_key": "task"})
    attempt = service.claim_next({"executor_id": "bug14-worker", "capability_ids": ["test.echo"],
                                  "runtime_epoch": service.health()["runtime_epoch"]}, idempotency_key="claim")
    case.task_id, case.attempt = created["task"]["id"], attempt
    case.task["task"].update(id=case.task_id, attempt_id=attempt["attempt_id"], fence=attempt["fence"],
                             project_id=project["id"], run_id=created["run"]["id"],
                             runtime_epoch=attempt["runtime_epoch"],
                             lease_expires_at=attempt["lease_expires_at"])

    class Generated:
        def health(self):
            with service.store._mutex:
                return service.health()

        def get_task(self, task_id):
            with service.store._mutex:
                task = service.task(task_id)["task"]
            if case.hidden and not case.release.is_set():
                task = {**task, "status": "running", "result": None,
                        "attempt_id": attempt["attempt_id"], "fence": attempt["fence"]}
            return {**task, "version": int(task["attempt"]) + 1}

        def heartbeat_attempt(self, attempt_id, *, idempotency_key, **body):
            if case.mode == "racing_ack" and case.executions and not case.committed.is_set():
                case.heartbeat_inflight.set()
                assert case.committed.wait(2)
                # This heartbeat began while the task was running; Runtime
                # has now atomically completed that same attempt.
            try:
                with service.store._mutex:
                    response = service.heartbeat_attempt(attempt_id, body, idempotency_key=idempotency_key)
            except LeaseError as exc:
                case.heartbeat_rejected.set()
                raise ApiError(409, "lease_lost", str(exc)) from exc
            case.beats.append(time.monotonic())
            case.renewed.set()
            return response.get("data", response)

        def settle_attempt(self, attempt_id, body, *, idempotency_key):
            case.writes.append((attempt_id, copy.deepcopy(body), idempotency_key))
            if case.mode == "outage" and not case.release.is_set():
                raise OSError("ACK unavailable")
            if case.mode == "conflict":
                # Ensure the observer is still renewing during settlement.
                case.renewed.clear()
                assert case.renewed.wait(2)
            if case.mode == "racing_ack":
                assert case.heartbeat_inflight.wait(2)
            try:
                with service.store._mutex:
                    response = service.settle_attempt(attempt_id, body, idempotency_key=idempotency_key)
            except ConflictError as exc:
                raise ApiError(409, "conflict", str(exc)) from exc
            case.committed.set()
            if case.mode == "racing_ack":
                assert case.heartbeat_rejected.wait(2)
                # Let the observer handle the in-flight rejection before the
                # settlement transport reports its lost ACK to the caller.
                time.sleep(0.1)
            if case.mode in {"lost_ack", "hidden_ack", "racing_ack"} and len(case.writes) <= 2:
                raise OSError("ACK unavailable")
            return response

        def fail_attempt(self, attempt_id, *, idempotency_key, **body):
            case.failures.append((attempt_id, copy.deepcopy(body), idempotency_key))
            if case.mode == "conflict":
                assert any(t.name == f"astrid-heartbeat-{case.task_id}" and t.is_alive()
                           for t in threading.enumerate())
            with service.store._mutex:
                return service.fail_attempt(attempt_id, body, idempotency_key=idempotency_key)

        def cancel_task(self, task_id, *, idempotency_key, **body):
            case.cancels.append((task_id, copy.deepcopy(body), idempotency_key))
            with service.store._mutex:
                return service.cancel_task_canonical(task_id, body, idempotency_key=idempotency_key)

    protocol = RuntimeProtocolClient.__new__(RuntimeProtocolClient)
    protocol.generated = Generated()
    protocol._attempt_runtime_epochs = {attempt["attempt_id"]: attempt["runtime_epoch"]}
    protocol._heartbeat_lock = threading.RLock()
    protocol._heartbeat_sequence, protocol._heartbeat_session = 0, "bug14"
    case.host.client = protocol
    case.host.attempt_root = None
    case.host.busy_path = tmp_path / "busy.json"
    allocate_root = case.host._allocate_attempt_root

    def allocate(*args):
        case.root = allocate_root(*args)
        return case.root

    monkeypatch.setattr(case.host, "_allocate_attempt_root", allocate)
    monkeypatch.setattr(generic_host, "_LEASE_POLL_SECONDS", 0.01)
    monkeypatch.setattr(generic_host, "_TERMINAL_RECONCILE_POLL_SECONDS", 0.01, raising=False)

    def execute(record, inputs, output_root, attempt, **kwargs):
        case.executions.append(attempt)
        history = json.loads((FIXTURES / "comfyui/live-423cbd20/history.json").read_text())
        assert any(row["status"]["completed"] for row in history.values())
        output_root.mkdir(parents=True, exist_ok=True)
        (output_root / "evidence-source-source.json").write_bytes(
            source if case.mode == "conflict" else b'{"complete":true}')
        (output_root / "durable.json").write_text('{"kind":"generic-result"}')
        write_manifest(output_root / "manifest.json", build_manifest(
            kind="test.echo", inputs={}, created="2026-11-01T00:00:00Z", warnings=[],
            outputs=[{"name": "answer", "path": "evidence-source-source.json", "ordinal": 0,
                      "role": "result", "media_type": "application/json"},
                     {"name": "result_manifest", "path": "durable.json", "ordinal": 1,
                      "role": "manifest", "media_type": "application/json"}],
        ))
        return SimpleNamespace(payload={}, process_id=12345, returncode=0)

    monkeypatch.setattr(case.host, "_run_command_definition", execute)
    try:
        yield case
    finally:
        # Teardown supplies an authoritative cancellation if an assertion
        # interrupts the test while the background owner still has custody.
        case.release.set()
        case.host._shutdown.set()
        for owner in tuple(getattr(case.host, "_reconciliation_owners", {}).values()):
            if owner.is_alive() and service.task(case.task_id)["task"]["status"] == "running":
                service.fail_attempt(attempt["attempt_id"], {
                    **{key: attempt[key] for key in ("lease_id", "fence", "runtime_epoch")},
                    "error": {"code": "test_teardown_owner_cedes"},
                }, idempotency_key="test-teardown")
            owner.join(timeout=3)
            assert not owner.is_alive()
        service.close()


def _run(case):
    return case.host.run_task(case.task, lease_token=case.attempt["lease_id"])


def _assert_resolved(case, state):
    assert case.service.task(case.task_id)["task"]["status"] == state
    assert case.host._handoff_active_attempts == 0
    assert not getattr(case.host, "_reconciliation_owners", {})
    assert json.loads(case.host.busy_path.read_text())["active"] == 0
    assert not any(t.name == f"astrid-heartbeat-{case.task_id}" and t.is_alive()
                   for t in threading.enumerate())
    assert case.service.store.conn.execute(
        "SELECT count(*) FROM reservations WHERE task_id=? AND released_at IS NULL", (case.task_id,)
    ).fetchone()[0] == 0
    assert len(case.executions) == 1


def test_conflict_fails_exact_owner_without_conflicting_evidence(settlement_case):
    case = settlement_case
    with pytest.raises(RuntimeError, match="output metadata does not match existing object"):
        _run(case)
    _assert_resolved(case, "failed")
    assert len(case.writes) == len(case.failures) == 1
    attempt_id, body, key = case.failures[0]
    assert attempt_id == case.attempt["attempt_id"]
    assert key == f"fail-{attempt_id}-{case.attempt['fence']}"
    assert {k: body[k] for k in ("lease_id", "fence", "runtime_epoch")} == {
        k: case.attempt[k] for k in ("lease_id", "fence", "runtime_epoch")}
    assert "outputs" not in body and "result" not in body
    assert body["error"]["code"] == "publication_rejected"
    assert "output metadata does not match existing object" in body["error"]["message"]
    assert case.root.exists() and (case.root / "outputs/manifest.json").exists()
    assert case.host.last_cleanup_receipt["intended_disposition"] == "retained"
    request = json.loads((case.root / "settle-request.json").read_text())
    assert request == {"operation": "settle_attempt", "identifier": case.writes[0][0],
                       "idempotency_key": case.writes[0][2], "body": case.writes[0][1]}


def test_lost_ack_reads_success_without_duplicate_submission(settlement_case):
    case = settlement_case
    case.mode = "lost_ack"
    _run(case)
    _assert_resolved(case, "completed")
    assert len(case.writes) == 1 and not case.failures and not case.cancels
    assert len(case.service.managed_outputs(case.task_id)) == 2


def test_inflight_heartbeat_rejection_cannot_abort_committed_lost_ack(settlement_case):
    case = settlement_case
    case.mode = "racing_ack"
    _run(case)
    _assert_resolved(case, "completed")
    assert case.heartbeat_rejected.is_set()
    assert len(case.writes) == 1 and not case.failures and not case.cancels
    assert len(case.service.managed_outputs(case.task_id)) == 2


def test_outage_has_active_owner_and_renewal_until_exact_replay_resolves(settlement_case):
    case = settlement_case
    case.mode = "outage"
    with pytest.raises(OSError, match="ACK unavailable"):
        _run(case)
    owner = case.host._reconciliation_owners[case.attempt["attempt_id"]]
    assert owner.is_alive() and case.root.exists()
    assert case.host._handoff_active_attempts == 1
    assert json.loads(case.host.busy_path.read_text())["active"] == 1
    case.renewed.clear()
    assert case.renewed.wait(2)
    assert not case.failures
    case.release.set()
    owner.join(timeout=3)
    _assert_resolved(case, "completed")
    assert len(case.writes) >= 3
    assert all(write == case.writes[0] for write in case.writes)
    assert len(case.service.managed_outputs(case.task_id)) == 2
    receipt = json.loads((case.root / "result.json").read_text())
    assert receipt["status"] == "succeeded" and receipt["publication"]["status"] == "settled"


def test_lost_ack_with_delayed_readback_retains_owner_without_duplicate_outputs(settlement_case):
    case = settlement_case
    case.mode, case.hidden = "hidden_ack", True
    with pytest.raises(OSError, match="ACK unavailable"):
        _run(case)
    owner = case.host._reconciliation_owners[case.attempt["attempt_id"]]
    assert owner.is_alive() and case.host._handoff_active_attempts == 1
    assert case.root.exists() and not case.failures
    case.release.set()
    owner.join(timeout=3)
    _assert_resolved(case, "completed")
    assert all(write == case.writes[0] for write in case.writes)
    assert len(case.service.managed_outputs(case.task_id)) == 2
    assert not case.failures and not case.cancels


def test_bounded_reconciliation_explicitly_cancels_before_renewal_stops(settlement_case, monkeypatch):
    case = settlement_case
    case.mode = "outage"
    monkeypatch.setattr(generic_host, "_TERMINAL_RECONCILE_SECONDS", 0.1, raising=False)
    with pytest.raises(OSError, match="ACK unavailable"):
        _run(case)
    owner = case.host._reconciliation_owners[case.attempt["attempt_id"]]
    owner.join(timeout=3)
    _assert_resolved(case, "cancelled")
    assert not case.failures and len(case.cancels) == 1
    assert case.cancels[0][1]["stop_observed"] is True
    assert case.cancels[0][1]["outputs"] == []
    assert case.root.exists()


def test_expired_reconciliation_cedes_exact_owner_without_evidence(settlement_case, monkeypatch):
    case = settlement_case
    case.mode = "outage"
    monkeypatch.setattr(generic_host, "_TERMINAL_RECONCILE_SECONDS", 0.1, raising=False)
    with pytest.raises(OSError, match="ACK unavailable"):
        _run(case)
    owner = case.host._reconciliation_owners[case.attempt["attempt_id"]]
    past = (datetime.now(timezone.utc) - timedelta(minutes=18)).isoformat()
    with case.service.store._mutex:
        for table, identity in (("tasks", case.task_id), ("attempts", case.attempt["attempt_id"])):
            case.service.store.conn.execute(f"UPDATE {table} SET lease_expires_at=? WHERE id=?", (past, identity))
        case.service.store.conn.execute("UPDATE reservations SET lease_expires_at=? WHERE task_id=?", (past, case.task_id))
    owner.join(timeout=3)
    assert not owner.is_alive()
    _assert_resolved(case, "failed")
    assert len(case.failures) == 1
    body = case.failures[0][1]
    assert "outputs" not in body and "result" not in body
    assert body["error"]["code"] == "reconciliation_required"


def test_reconciliation_keeps_parallel_coordinator_custody_counted(settlement_case, monkeypatch):
    case = settlement_case
    case.mode = "outage"
    coordinator = case.host
    monkeypatch.setattr(coordinator.client, "fork_for_host_slot", lambda: coordinator.client)
    slot = coordinator._new_claim_slot()
    monkeypatch.setattr(slot, "_allocate_attempt_root", coordinator._allocate_attempt_root)
    monkeypatch.setattr(slot, "_run_command_definition", coordinator._run_command_definition)
    case.host = slot
    with pytest.raises(OSError, match="ACK unavailable"):
        _run(case)
    owner = slot._reconciliation_owners[case.attempt["attempt_id"]]
    assert owner.is_alive() and coordinator._handoff_active_attempts == 1
    case.release.set()
    owner.join(timeout=3)
    assert coordinator._handoff_active_attempts == 0
    _assert_resolved(case, "completed")


@pytest.mark.parametrize("status,state,attempt_id,fence,rejected", [
    (400, "running", "attempt-1", 3, True),
    (409, "running", "attempt-1", 3, True),
    (422, "running", "attempt-1", 3, True),
    (0, "running", "attempt-1", 3, False),
    (408, "running", "attempt-1", 3, False),
    (429, "running", "attempt-1", 3, False),
    (503, "running", "attempt-1", 3, False),
    (409, "completed", "attempt-1", 3, False),
    (409, "running", "attempt-new", 3, False),
    (409, "running", "attempt-1", 4, False),
])
def test_rejection_requires_validation_response_and_exact_nonterminal_readback(
    status, state, attempt_id, fence, rejected,
):
    from tests.core.execution.test_terminal_evidence_transport_f05 import _client

    protocol = _client(None)
    protocol._claimed_runtime_epoch = lambda _attempt: 9
    writes = []
    protocol.generated.get_task = lambda _task: {
        "id": "task-1", "state": state, "attempt_id": attempt_id, "lease_fence": fence, "result": None,
    }

    def settle(*args, **kwargs):
        writes.append((args, kwargs))
        raise ApiError(status, "conflict", "rejected publication")

    protocol.generated.settle_attempt = settle
    with pytest.raises(RuntimeError) as raised:
        protocol.settle("task-1", "lease-1", attempt_id="attempt-1", fence=3,
                        result={"manifest_ref": {"object_id": "sha256:" + "d" * 64, "size": 1}},
                        outputs=[], effect=None)
    assert (type(raised.value).__name__ == "TerminalPublicationRejected") is rejected
    assert len(writes) == (1 if rejected else 2)
