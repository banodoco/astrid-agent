"""LS-7 Agent waits preserve attempt custody without task/claim polling."""
from __future__ import annotations

import copy
from datetime import datetime, timedelta, timezone
import json
import threading
import time
from types import SimpleNamespace

import pytest

from astrid.core.execution import generic_host
from astrid.core.execution.generic_host import GenericPackHost
from tests.test_generic_host_result_json import host_case, _success_boundary
from tests.core.execution.test_generic_host_claim_loop import Backend, Client, world


@pytest.fixture
def event_case(host_case, monkeypatch):
    case = host_case
    data = case.task["task"]
    data.update(status="running", runtime_epoch=7,
                lease_expires_at=(datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat())
    condition = threading.Condition()
    state = {"version": 0, "waits": [], "observers": [], "observed_expiry": None}
    case.events = state
    case.renewed, case.failed, case.cancelled = threading.Event(), threading.Event(), threading.Event()

    def changed():
        with condition:
            state["version"] += 1
            condition.notify_all()
    case.changed = changed

    class Observer:
        closed = False
        def wait_for_task(self, task_id, *, cursor, timeout_seconds):
            assert task_id == data["id"]
            with condition:
                state["waits"].append(timeout_seconds)
                condition.wait_for(lambda: self.closed or cursor != str(state["version"]), timeout_seconds)
                if self.closed:
                    raise OSError("subscription closed")
                snapshot = copy.deepcopy(data)
                if state["observed_expiry"] is not None:
                    snapshot["lease_expires_at"] = state["observed_expiry"]
                return {"task": snapshot, "cursor": str(state["version"]), "reason": "changed"}
        def cancel_waits(self):
            with condition:
                self.closed = True
                condition.notify_all()
    def observer():
        item = Observer()
        state["observers"].append(item)
        return item
    monkeypatch.setattr(case.runtime, "supports_task_waits", lambda: True, raising=False)
    monkeypatch.setattr(case.runtime, "new_task_observer", observer, raising=False)
    monkeypatch.setattr(case.runtime, "close_task_observer", lambda item: item.cancel_waits(), raising=False)
    monkeypatch.setattr(case.runtime, "cancel_waits", lambda: [item.cancel_waits() for item in state["observers"]], raising=False)
    original_read = case.runtime.task
    def read(task_id):
        assert not threading.current_thread().name.startswith("astrid-heartbeat-"), "lease observer polled GET task"
        return original_read(task_id)
    monkeypatch.setattr(case.runtime, "task", read)
    original_heartbeat = case.runtime.heartbeat
    def heartbeat(task_id, lease_id, **kwargs):
        original_heartbeat(task_id, lease_id, **kwargs)
        expiry = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
        data["lease_expires_at"] = expiry
        changed()
        case.renewed.set()
        return {"task_id": task_id, "attempt_id": kwargs["attempt_id"], "lease_id": lease_id,
                "fence": kwargs["fence"], "runtime_epoch": 7, "lease_expires_at": expiry}
    monkeypatch.setattr(case.runtime, "heartbeat", heartbeat)
    original_settle, original_fail = case.runtime.settle, case.runtime.fail
    def settle(*args, **kwargs):
        result = original_settle(*args, **kwargs)
        data["status"] = "completed"
        changed()
        return result
    def fail(*args, **kwargs):
        result = original_fail(*args, **kwargs)
        data["status"] = "failed"
        changed()
        case.failed.set()
        return result
    def cancel(*args, **kwargs):
        data["status"] = "cancelled"
        changed()
        case.cancelled.set()
        return {"task": dict(data)}
    monkeypatch.setattr(case.runtime, "settle", settle)
    monkeypatch.setattr(case.runtime, "fail", fail)
    monkeypatch.setattr(case.runtime, "cancel", cancel, raising=False)
    _success_boundary(case, monkeypatch)
    yield case
    case.host.shutdown()
    assert all(item.closed for item in state["observers"])
    assert not any(t.name == "astrid-heartbeat-task-result" and t.is_alive() for t in threading.enumerate())


def start_attempt(case):
    outcome = []
    def run():
        try:
            outcome.append(case.host.run_task(case.task, lease_token="lease-result"))
        except BaseException as error:
            outcome.append(error)
    thread = threading.Thread(target=run)
    thread.start()
    return thread, outcome


def test_fresh_claim_has_no_initial_or_final_renewal_and_keeps_progress(event_case, monkeypatch):
    case = event_case
    execute = case.host._run_command_definition
    def complete(*args, **kwargs):
        result = execute(*args, **kwargs)
        (case.root / ".astrid-progress.json").write_text(json.dumps({"phase": "complete", "percent": 100}))
        return result
    monkeypatch.setattr(case.host, "_run_command_definition", complete)
    claim = {**case.task["task"], "task_id": "task-result", "lease_id": "lease-result"}
    case.host._execute_claimed_task(claim)
    assert case.runtime.heartbeats == []
    assert len(case.runtime.settlements) == 1
    assert case.runtime.settlements[0][2]["progress"] == {"phase": "complete", "percent": 100}
    # Lead rework: reserve about half of the acknowledged 30-second lease
    # for renewal, while a completed fast job still sends no heartbeat.
    assert case.events["waits"] and 10 < max(case.events["waits"]) <= 15


def test_long_attempt_renews_on_acknowledged_expiry_even_if_read_claims_later_time(event_case, monkeypatch):
    case = event_case
    # A short acknowledged lease makes its halfway renewal observable within
    # the existing one-second bound; the later task read grants no extension.
    case.task["task"]["lease_expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=.8)).isoformat()
    case.events["observed_expiry"] = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    entered, release = threading.Event(), threading.Event()
    execute = case.host._run_command_definition
    def stall(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return execute(*args, **kwargs)
    monkeypatch.setattr(case.host, "_run_command_definition", stall)
    started = time.monotonic()
    thread, outcome = start_attempt(case)
    try:
        assert entered.wait(1)
        assert case.renewed.wait(1), "long attempt did not renew before the acknowledged lease expired"
        assert time.monotonic() - started >= .15
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive() and outcome and not isinstance(outcome[0], BaseException)
    assert len(case.runtime.heartbeats) == 1


@pytest.mark.parametrize("transition", ["deadline", "cancel", "epoch"])
def test_wait_observer_keeps_deadline_cancel_and_epoch_guards(event_case, monkeypatch, transition):
    case = event_case
    entered, release = threading.Event(), threading.Event()
    execute = case.host._run_command_definition
    if transition == "deadline":
        case.task["task"]["deadline_at"] = (datetime.now(timezone.utc) + timedelta(seconds=.3)).isoformat()
    def stall(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return execute(*args, **kwargs)
    monkeypatch.setattr(case.host, "_run_command_definition", stall)
    thread, outcome = start_attempt(case)
    try:
        assert entered.wait(1)
        if transition == "cancel":
            case.task["task"]["status"] = "cancel_requested"
            case.changed()
            assert case.cancelled.wait(1)
        else:
            if transition == "epoch":
                case.task["task"]["runtime_epoch"] = 8
                case.changed()
            assert case.failed.wait(1)
    finally:
        release.set()
        thread.join(3)
    assert not thread.is_alive() and outcome
    assert case.runtime.settlements == []
    assert case.runtime.heartbeats == []


class WaitingClient(Client):
    def __init__(self, backend):
        super().__init__(backend)
        self.closed = False
    def supports_task_waits(self):
        return True
    def fork_for_host_slot(self):
        client = WaitingClient(self.backend)
        self.backend.clients.append(client)
        return client
    def wait_for_work(self, *, capability_ids, timeout_seconds, **kwargs):
        with self.backend.condition:
            self.backend.wait_entered.set()
            self.backend.condition.wait_for(lambda: self.closed or self.backend.tasks, timeout_seconds)
            return {"available": bool(self.backend.tasks) and not self.closed,
                    "capability_ids": list(capability_ids), "reason": "shutdown" if self.closed else "changed"}
    def cancel_waits(self):
        with self.backend.condition:
            self.closed = True
            self.backend.condition.notify_all()


@pytest.mark.parametrize("capacity", [1, 2])
def test_host_waits_without_empty_claims_or_success_path_sleep(world, monkeypatch, capacity):
    backend = Backend(0)
    backend.wait_entered = threading.Event()
    backend.release.set()
    host = GenericPackHost(pack_roots=[world], client=WaitingClient(backend), max_concurrency=capacity)
    host.discover()
    outcome = []
    def run():
        try:
            outcome.append(host.run(max_tasks=capacity, poll_seconds=10))
        except BaseException as error:
            outcome.append(error)
    monkeypatch.setattr(generic_host.time, "sleep", lambda _: pytest.fail("success path slept"))
    thread = threading.Thread(target=run)
    thread.start()
    try:
        assert backend.wait_entered.wait(1)
        assert backend.claim_entries == 0
        with backend.condition:
            backend.tasks.extend(str(index) for index in range(capacity))
            backend.condition.notify_all()
        thread.join(2)
        assert not thread.is_alive(), "claim waited for the old polling interval"
        assert outcome and isinstance(outcome[0], list) and len(outcome[0]) == capacity
        assert backend.claim_entries == capacity
    finally:
        host.shutdown()
        thread.join(3)
