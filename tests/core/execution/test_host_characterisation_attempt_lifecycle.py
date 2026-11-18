"""HM2: R11/H02/H12 attempt ownership across cancellation and publication.

The public run_task/shutdown paths remain real. Child and Runtime boundaries
are controlled locally; these cases do not claim native Plan 38 containment.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from astrid.core.execution.generic_host import HostCancelled
from tests import test_generic_host_result_json as result_cases

host_case = result_cases.host_case


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
