from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from astrid.core.execution import generic_host
from tests.test_generic_host_result_json import _success_boundary, host_case


def _ack(task_id, lease_token, *, attempt_id, fence, progress=None):
    return SimpleNamespace(
        task_id=task_id,
        lease_id=lease_token,
        attempt_id=attempt_id,
        fence=fence,
        runtime_epoch=1,
        lease_expires_at=(datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat(),
    )


def _capture_lease_logs(monkeypatch, signal_category=None):
    rows = []
    signaled = threading.Event()
    original = generic_host.log_lease_diagnostic

    def capture(**event):
        rows.append(dict(event))
        original(**event)
        if event.get("category") == signal_category:
            signaled.set()

    monkeypatch.setattr(generic_host, "log_lease_diagnostic", capture)
    return rows, signaled


@pytest.fixture
def lease_pump_case(host_case, monkeypatch):
    case = host_case
    task = case.task["task"]
    task.update(status="running", attempt_id="attempt-result", fence=3, runtime_epoch=1)
    monkeypatch.setattr(generic_host, "_LEASE_POLL_SECONDS", 0.02)
    monkeypatch.setattr(case.runtime, "heartbeat", _ack)
    return case


def _hold_executor_until(host, signal, *, timeout=3):
    original = host._run_command_definition

    def execute(*args, **kwargs):
        if not signal.wait(timeout):
            raise TimeoutError("test did not observe the lease pump event")
        return original(*args, **kwargs)

    host._run_command_definition = execute


def test_lease_pump_logs_entry_read_gate_ack_and_exit(lease_pump_case, monkeypatch, capsys):
    case = lease_pump_case
    rows, gate_seen = _capture_lease_logs(monkeypatch, "task_read_gate")
    _success_boundary(case, monkeypatch)
    _hold_executor_until(case.host, gate_seen)

    result = case.host.run_task(case.task, lease_token="lease-result")

    categories = [row["category"] for row in rows]
    assert result["task"]["status"] == "completed"
    assert "lease_pump_enter" in categories
    assert any(row["category"] == "task_read_gate" and row["reason"] == "owner_matches"
               for row in rows)
    assert any(row["category"] == "heartbeat_acknowledged" and row["reason"] == "accepted"
               for row in rows)
    assert "lease_pump_exit" in categories
    assert "lease-result" not in capsys.readouterr().err


@pytest.mark.parametrize("failure", ["task_read", "heartbeat"])
def test_lease_pump_logs_failed_transport_and_skipped_heartbeat(
    lease_pump_case, monkeypatch, capsys, failure,
):
    case = lease_pump_case
    trigger = "heartbeat_skipped" if failure == "task_read" else "heartbeat_acknowledged"
    rows, observed = _capture_lease_logs(monkeypatch, trigger)
    _success_boundary(case, monkeypatch)
    _hold_executor_until(case.host, observed)

    if failure == "task_read":
        original_task = case.runtime.task
        calls = 0

        def fail_first_read(task_id):
            nonlocal calls
            if threading.current_thread().name.startswith("astrid-heartbeat"):
                calls += 1
            if calls == 1 and threading.current_thread().name.startswith("astrid-heartbeat"):
                raise TimeoutError("PRIVATE-TEST-MESSAGE")
            return original_task(task_id)

        monkeypatch.setattr(case.runtime, "task", fail_first_read)
    else:
        original_heartbeat = case.runtime.heartbeat
        calls = 0

        def fail_first_heartbeat(*args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise TimeoutError("PRIVATE-TEST-MESSAGE")
            return original_heartbeat(*args, **kwargs)

        monkeypatch.setattr(case.runtime, "heartbeat", fail_first_heartbeat)

    result = case.host.run_task(case.task, lease_token="lease-result")

    assert result["task"]["status"] == "completed"
    if failure == "task_read":
        assert any(row["category"] == "task_read_gate" and row["reason"] == "read_failed"
                   for row in rows)
        assert any(row["category"] == "heartbeat_skipped" and row["reason"] == "task_read_failed"
                   for row in rows)
    else:
        assert any(row["category"] == "heartbeat_failed" and row["reason"] == "TimeoutError"
                   for row in rows)
        assert any(row["category"] == "heartbeat_acknowledged" for row in rows)
    assert "PRIVATE-TEST-MESSAGE" not in capsys.readouterr().err


def test_runtime_heartbeat_logs_lock_wait_without_request_data(monkeypatch, capsys):
    client = object.__new__(generic_host.RuntimeProtocolClient)
    client.execution_policy = generic_host._DEFAULT_EXECUTION_POLICY
    client._attempt_runtime_epochs = {"attempt-lock": 1}
    client._heartbeat_sequence = 0
    client._heartbeat_session = "test-session"
    client._heartbeat_lock = threading.Lock()
    client.health = lambda **_kwargs: {"runtime_epoch": 1}
    generated = SimpleNamespace(timeout=5, heartbeat_attempt=lambda *_args, **_kwargs: {"ok": True})
    client._lifecycle_client = lambda **_kwargs: generated

    events = []
    original = generic_host.log_lease_diagnostic

    def capture(**event):
        events.append(dict(event))
        original(**event)

    monkeypatch.setattr(generic_host, "log_lease_diagnostic", capture)
    client._heartbeat_lock.acquire()
    result = []
    worker = threading.Thread(target=lambda: result.append(client.heartbeat(
        "task-lock", "PRIVATE-LEASE", attempt_id="attempt-lock", fence=4,
        deadline=time.monotonic() + 2,
    )))
    worker.start()
    time.sleep(0.02)
    client._heartbeat_lock.release()
    worker.join(timeout=2)

    assert not worker.is_alive()
    assert result == [{"ok": True}]
    lock_events = [event for event in events if event["category"] == "heartbeat_lock_wait"]
    assert len(lock_events) == 1
    assert lock_events[0]["reason"] == "acquired"
    assert lock_events[0]["wait_seconds"] >= 0.01
    assert "PRIVATE-LEASE" not in capsys.readouterr().err
