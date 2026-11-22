"""HM1 R11/H02/H12: cancellation request, containment, and terminal ownership.

A real ordinary Python action runs through run_task. Runtime is the existing
recording fixture; its two observers are sequenced explicitly. This is local
host characterization, not Plan 38 platform qualification.
"""
from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from astrid.core.execution import task_scope
from tests.core.execution import test_generic_host_actions_v3 as actions


@pytest.mark.parametrize("observer", ["execution", "heartbeat"])
def test_r11_fenced_cancel_request_keeps_attempt_owned_through_terminal_ack(tmp_path, monkeypatch, observer):
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path / "host-data"))
    monkeypatch.setenv("ASTRID_SOURCE_STATE", str(tmp_path / "absent-state.json"))
    monkeypatch.delenv("ASTRID_PACKS_PATH", raising=False)
    entered = tmp_path / "entered"
    _, host, runtime, _, _ = actions.pack(tmp_path, code=(
        "from pathlib import Path\nimport time\n"
        f"def run(value):\n    Path({str(entered)!r}).write_text('entered')\n"
        "    time.sleep(30)\n    return value\n"
    ))
    host.register()
    admitted = actions.task_for(runtime)
    admitted["task"].update(status="running", runtime_epoch=1)
    request_seen, terminal_started, acknowledge = (threading.Event() for _ in range(3))
    owned, calls, results, errors = [], [], [], []
    track = host._track_process
    record_heartbeat = runtime.heartbeat

    def retain(process):
        owned.append(process)
        track(process)

    def current_task(task_id):
        assert task_id == "python-task"
        is_pump = threading.current_thread().name.startswith("astrid-heartbeat-")
        selected = is_pump if observer == "heartbeat" else not is_pump
        status = "cancel_requested" if request_seen.is_set() and selected else "running"
        return {"task": {**admitted["task"], "status": status}}

    def heartbeat(task_id, lease_token, *, attempt_id, fence, progress=None):
        record_heartbeat(task_id, lease_token, attempt_id=attempt_id, fence=fence, progress=progress)
        return {
            "task_id": task_id, "attempt_id": attempt_id, "fence": fence,
            "lease_id": lease_token, "runtime_epoch": 1,
            "lease_expires_at": (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat(),
        }

    def cancel(task_id, **payload):
        calls.append((task_id, payload))
        if "stop_observed" not in payload:
            request_seen.set()
            return {"task": {"id": task_id, "status": "cancel_requested"}}
        terminal_started.set()
        assert acknowledge.wait(10), "test never acknowledged terminal cancellation"
        return {"task": {"id": task_id, "status": "cancelled"}}

    def serve():
        try:
            results.append(host.run_task(admitted, lease_token="lease", keep_attempt=True))
        except BaseException as exc:  # noqa: BLE001 - assert worker failures in the test thread
            errors.append(exc)

    monkeypatch.setattr(host, "_track_process", retain)
    monkeypatch.setattr(runtime, "task", current_task)
    monkeypatch.setattr(runtime, "heartbeat", heartbeat)
    monkeypatch.setattr(runtime, "cancel", cancel, raising=False)
    worker = threading.Thread(target=serve, name="hm2-cancel-execution")
    worker.start()
    try:
        deadline = time.monotonic() + 10
        while not entered.exists() and worker.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert entered.exists(), f"ordinary child never entered: {errors!r}"
        assert len(owned) == 1 and owned[0].poll() is None
        assert host.handoff_quiescence()["active_attempts"] == 1
        assert host.cancel_task(
            "python-task", attempt_id="python-attempt", fence=1, lease_id="lease",
            runtime_epoch=1, require_fence=True,
        ) == {"task": {"id": "python-task", "status": "cancel_requested"}}
        assert terminal_started.wait(10), f"cancellation never reached terminal reporting: {errors!r}"
        assert calls[0] == ("python-task", {
            "attempt_id": "python-attempt", "fence": 1, "lease_id": "lease", "runtime_epoch": 1,
        })
        expected_result = {
            "status": "cancelled", "error": {"code": "cancelled", "message": "execution cancelled"},
        }
        if observer == "execution":
            expected_result["outcomes"] = {"execution": {"status": "cancelled"}}
            assert owned[0].poll() is not None, "stop evidence requires observed producer exit"
        assert calls[1] == ("python-task", {
            "attempt_id": "python-attempt", "fence": 1, "lease_id": "lease", "runtime_epoch": 1,
            "outputs": [], "result": expected_result, "stop_observed": observer == "execution",
        })
        assert worker.is_alive() and host._session_task_lock.locked()
        assert host.managed_tool_session.occupied
        disposition, snapshot = host.pause_claim_admission()
        assert disposition == "active_work" and snapshot["active_attempts"] == 1
        assert snapshot["claim_gate_closed"] is False
        assert runtime.settlements == runtime.failures == []
        acknowledge.set()
        worker.join(timeout=10)
        assert not worker.is_alive() and errors == []
        assert results == [{"task_id": "python-task", "status": "cancelled", "cancelled": True}]
        assert len(calls) == 2, "the second observer must reuse the recorded terminal cancellation"
        assert owned[0].returncode is not None
        assert task_scope.stop(owned[0], 0.1).status == "settled"
        assert not host._active_processes and not host._unresolved_task_scopes
        assert not host.managed_tool_session.occupied and not host._session_task_lock.locked()
        final = host.handoff_quiescence()
        assert final["active_attempts"] == final["pending_settlements"] == 0
        assert runtime.settlements == runtime.failures == [] and runtime.uploaded_objects == {}
        assert runtime.heartbeats and all(row == ("python-task", "lease", "python-attempt", 1)
                                          for row in runtime.heartbeats)
        receipt = json.loads((Path(host.last_cleanup_receipt["path"]) / "result.json").read_text())
        assert receipt["status"] == "cancelled" and receipt["publication"]["status"] == "not_settled"
        assert receipt["outputs"]["artifacts"] == []
        assert not any(thread.name == "astrid-heartbeat-python-task" and thread.is_alive()
                       for thread in threading.enumerate())
    finally:
        acknowledge.set()
        host.shutdown()
        worker.join(timeout=10)
