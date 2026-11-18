"""HM2: R11/H02/H12 attempt ownership across cancellation and publication.

The public run_task/shutdown paths remain real. Child and Runtime boundaries
are controlled locally; these cases do not claim native Plan 38 containment.
"""

from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from astrid.core.execution.generic_host import HostCancelled
from tests import test_generic_host_result_json as result_cases
from tests.core.execution import test_bug14_settlement_liveness as settlement_cases

host_case = result_cases.host_case
settlement_case = settlement_cases.settlement_case


@pytest.fixture(autouse=True)
def _private_runtime_data_root(tmp_path, monkeypatch):
    """Retained publication files belong to this case's private support root."""
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "host-data"))


class _ObservedSessionLock:
    """Expose a failed acquisition without changing the underlying lock."""

    def __init__(self):
        self.lock = threading.Lock()
        self.waiting = threading.Event()

    def acquire(self, *args, **kwargs):
        acquired = self.lock.acquire(*args, **kwargs)
        if not acquired:
            self.waiting.set()
        return acquired

    def release(self):
        self.lock.release()


def test_r11_shutdown_while_waiting_for_session_keeps_attempt_counted_until_cancelled(
    host_case, monkeypatch,
):
    """R11/H02/H12: the waiting task owns its attempt, never the busy session."""
    case = host_case
    case.task["task"].update(runtime_epoch=1, status="running")
    session_lock = _ObservedSessionLock()
    session_lock.lock.acquire()
    monkeypatch.setattr(case.host, "_session_task_lock", session_lock)
    cancellations = []

    def cancel(task_id, **evidence):
        cancellations.append((task_id, evidence))
        return {"task": {"id": task_id, "status": "cancelled"}}

    monkeypatch.setattr(case.runtime, "cancel", cancel, raising=False)
    with ThreadPoolExecutor(max_workers=1) as pool:
        running = pool.submit(case.host.run_task, case.task, lease_token="lease-result")
        try:
            assert session_lock.waiting.wait(5), "run_task never reached the occupied session"
            assert case.host.handoff_quiescence()["active_attempts"] == 1
            disposition, snapshot = case.host.pause_claim_admission()
            assert disposition == "active_work"
            assert snapshot["active_attempts"] == 1
            assert snapshot["claim_gate_closed"] is False

            case.host.shutdown()
            with pytest.raises(HostCancelled, match="session wait cancelled"):
                running.result(timeout=5)

            # The cancelled waiter must not release a lock owned by another task.
            assert session_lock.lock.locked()
        finally:
            case.host.shutdown()
            session_lock.lock.release()

    assert cancellations == [("task-result", {
        "attempt_id": "attempt-result", "fence": 3,
        "lease_id": "lease-result", "runtime_epoch": 1,
        "outputs": [],
        "result": {"status": "cancelled", "error": {
            "code": "cancelled", "message": "execution cancelled",
        }},
        "stop_observed": True,
    })]
    assert case.runtime.settlements == []
    assert case.runtime.failures == []
    assert case.runtime.uploaded_objects == {}
    receipt = json.loads((case.root / "result.json").read_text(encoding="utf-8"))
    assert receipt["status"] == "cancelled"
    assert receipt["outputs"]["artifacts"] == []
    assert receipt["publication"]["status"] == "not_settled"
    snapshot = case.host.handoff_quiescence()
    assert snapshot["active_attempts"] == snapshot["pending_settlements"] == 0
    assert not any(thread.name == "astrid-heartbeat-task-result" and thread.is_alive()
                   for thread in threading.enumerate())


def _retained_outputs(case):
    receipt = json.loads((case.root / "result.json").read_text(encoding="utf-8"))
    artifacts = receipt["outputs"]["artifacts"]
    assert len(artifacts) == 2
    for artifact in artifacts:
        path = Path(artifact["path"])
        assert path.is_relative_to(case.root / "outputs")
        data = path.read_bytes()
        assert artifact["digest"] == "sha256:" + hashlib.sha256(data).hexdigest()
        assert artifact["size"] == len(data)
    return receipt


def test_r11_shutdown_during_uncertain_publication_retains_verified_result(settlement_case):
    """R11/H12/H15 + R02/R03: publication custody survives producer/session exit."""
    case = settlement_case
    case.mode = "outage"
    with pytest.raises(OSError, match="ACK unavailable"):
        case.host.run_task(case.task, lease_token=case.attempt["lease_id"])
    owner = case.host._reconciliation_owners[case.attempt["attempt_id"]]
    assert owner.is_alive()
    assert case.host.managed_tool_session.occupied is False
    assert not case.host._active_processes
    disposition, snapshot = case.host.pause_claim_admission()
    assert disposition == "active_work"
    assert snapshot["active_attempts"] == 1
    assert snapshot["claim_gate_closed"] is False
    pending = _retained_outputs(case)
    assert pending["publication"]["status"] == "not_settled"
    request = (case.root / "settle-request.json").read_bytes()
    outputs = {path.name: path.read_bytes() for path in (case.root / "outputs").iterdir()
               if path.is_file()}

    case.host.shutdown()
    owner.join(timeout=5)
    assert not owner.is_alive()
    settlement_cases._assert_resolved(case, "cancelled")
    assert len(case.cancels) == 1 and case.failures == []
    assert case.cancels[0][1]["stop_observed"] is True
    assert case.cancels[0][1]["outputs"] == []
    assert case.service.managed_outputs(case.task_id) == []
    assert (case.root / "settle-request.json").read_bytes() == request
    assert {path.name: path.read_bytes() for path in (case.root / "outputs").iterdir()
            if path.is_file()} == outputs
    cancelled = _retained_outputs(case)
    assert cancelled["status"] == "cancelled"
    assert cancelled["outputs"] == pending["outputs"]
    assert cancelled["publication"]["status"] == "not_settled"
    cleanup = case.host.last_cleanup_receipt
    assert cleanup["intended_disposition"] == "retained"
    cleanup["path"] = "caller mutation"
    assert case.host.last_cleanup_receipt["path"] == str(case.root)


def test_r11_committed_result_readback_wins_shutdown_during_publication_handoff(
    settlement_case, monkeypatch,
):
    """R11/H12/H15 + R02: shutdown cannot overwrite an already committed result."""
    case = settlement_case
    case.mode, case.hidden = "hidden_ack", True
    readback_started, allow_readback = threading.Event(), threading.Event()
    get_task = case.host.client.generated.get_task

    def read_task(task_id):
        if threading.current_thread().name == f"astrid-reconcile-{case.attempt['attempt_id']}":
            readback_started.set()
            assert allow_readback.wait(5), "test never released reconciliation readback"
        return get_task(task_id)

    monkeypatch.setattr(case.host.client.generated, "get_task", read_task)
    try:
        with pytest.raises(OSError, match="ACK unavailable"):
            case.host.run_task(case.task, lease_token=case.attempt["lease_id"])
        assert readback_started.wait(5)
        owner = case.host._reconciliation_owners[case.attempt["attempt_id"]]
        assert case.service.task(case.task_id)["task"]["status"] == "completed"
        pending = _retained_outputs(case)
        request = (case.root / "settle-request.json").read_bytes()
        writes = list(case.writes)
        assert case.host.handoff_quiescence()["active_attempts"] == 1
        case.host.shutdown()
    finally:
        case.release.set()
        allow_readback.set()
    owner.join(timeout=5)
    assert not owner.is_alive()
    settlement_cases._assert_resolved(case, "completed")
    assert case.writes == writes
    assert case.cancels == case.failures == []
    assert (case.root / "settle-request.json").read_bytes() == request
    resolved = _retained_outputs(case)
    assert resolved["status"] == "succeeded"
    assert resolved["ok"] is True
    assert resolved["error"] is None
    assert resolved["publication"]["status"] == "settled"
    assert resolved["outputs"] == pending["outputs"]
    assert len(case.service.managed_outputs(case.task_id)) == 2
