"""DEEP-BUG14/cpu/repro_cli_wait.py, using the SDK function and a virtual clock."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.sdk import invocation


@pytest.fixture
def expired_wait(monkeypatch):
    task = json.loads((Path(__file__).resolve().parents[1] / "fixtures/bug14/expired-task.json").read_text())
    clock = SimpleNamespace(t=0.0)
    origin = datetime(2026, 11, 1, 16, 29, tzinfo=timezone.utc)
    clock.monotonic = lambda: clock.t
    clock.sleep = lambda seconds: setattr(clock, "t", clock.t + seconds)

    class ClockDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return (origin + timedelta(seconds=clock.t)).astimezone(tz)

    monkeypatch.setattr(invocation, "time", clock)
    monkeypatch.setattr(invocation, "datetime", ClockDateTime)
    reads = []

    def show(task_id):
        reads.append(task_id)
        return dict(task)

    client = SimpleNamespace(tasks=SimpleNamespace(show=show))

    def wait(**kwargs):
        return invocation._wait_for_kernel_task(
            client, task_id=task["task_id"], run_id=task["run_id"],
            timeout_seconds=kwargs.pop("timeout_seconds", 3600), poll_seconds=1, **kwargs,
        )

    return SimpleNamespace(task=task, clock=clock, origin=origin, reads=reads, wait=wait)


def test_live_expired_unresolved_attempt_returns_after_one_read(expired_wait):
    case = expired_wait
    result, ok, attempt_id = case.wait()
    assert not ok and attempt_id == case.task["attempt_id"]
    assert case.clock.t == 0 and case.reads == [case.task["task_id"]]
    assert result["state"] == "reconciliation_required"
    assert result["error"]["code"] == "task_reconciliation_required"
    assert result["kernel_task_id"] == case.task["task_id"]
    assert result["kernel_attempt_id"] == case.task["attempt_id"]
    assert result["task"] == case.task
    command = f"astrid tasks cancel {case.task['task_id']}"
    assert case.task["task_id"] in result["error"]["message"]
    assert case.task["attempt_id"] in result["error"]["message"]
    assert command in result["error"]["message"]
    assert result["error"]["details"]["recovery_command"] == command


def test_wait_exits_when_the_observed_lease_expires(expired_wait):
    case = expired_wait
    case.task["lease_expires_at"] = (case.origin + timedelta(seconds=2)).isoformat()
    result, ok, _ = case.wait()
    assert not ok and result["error"]["code"] == "task_reconciliation_required"
    assert case.clock.t == 2 and len(case.reads) == 3


@pytest.mark.parametrize("changes", [
    {"lease_expires_at": None},
    {"lease_expires_at": "unreadable"},
    {"lease_expires_at": "2026-11-01T16:10:48"},
    {"lease_expires_at": "2026-11-01T17:10:48Z"},
    {"attempt_id": None},
    {"state": "queued"},
])
def test_missing_or_nonexpired_custody_keeps_the_requested_wait(expired_wait, changes):
    case = expired_wait
    case.task.update(changes)
    result, ok, _ = case.wait(timeout_seconds=3)
    assert not ok and result["error"]["code"] == "task_wait_timeout"
    assert case.clock.t == 3 and len(case.reads) == 4


@pytest.mark.parametrize("state", ["completed", "succeeded", "failed", "cancelled", "cancel_requested"])
def test_terminal_observation_takes_precedence_over_old_lease(expired_wait, state):
    case = expired_wait
    case.task["state"] = state
    result, ok, _ = case.wait()
    assert ok is (state in {"completed", "succeeded"})
    assert result.get("error", {}).get("code") != "task_reconciliation_required"
    assert case.clock.t == 0 and len(case.reads) == 1


def test_unknown_provider_state_with_expired_owner_requires_reconciliation(expired_wait):
    case = expired_wait
    case.task["state"] = "provider_state_unknown"
    result, ok, _ = case.wait()
    assert not ok and result["error"]["code"] == "task_reconciliation_required"
    assert case.clock.t == 0 and len(case.reads) == 1
