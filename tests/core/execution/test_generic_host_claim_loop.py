"""Focused custody/capacity proof for the activated generic host claim loop."""
from __future__ import annotations

from collections import deque
import json
import threading
import time
from types import SimpleNamespace

import pytest

from astrid.core.execution import generic_host
from astrid.core.execution.generic_host import GenericPackHost, HostError, RuntimeProtocolClient


class Backend:
    def __init__(self, count):
        self.tasks = deque(str(index) for index in range(count))
        self.condition = threading.Condition()
        self.release = threading.Event()
        self.claim_release = threading.Event()
        self.claim_release.set()
        self.active = 0
        self.peak = 0
        self.started = []
        self.claimed = []
        self.failed = []
        self.clients = []
        self.claim_times = {}
        self.fail_polls = 0
        self.claim_entries = 0
        self.uncertain = False

    def wait(self, predicate):
        with self.condition:
            assert self.condition.wait_for(predicate, timeout=3), "fixture did not progress"


class Client:
    """Shared coordinator fixture, private per-client attempt ownership."""
    def __init__(self, backend):
        self.backend = backend
        self.attempts = {}
        self.heartbeat_session = object()

    def fork_for_host_slot(self):
        client = Client(self.backend)
        self.backend.clients.append(client)
        return client

    def refresh_local_connection(self):
        return False

    def claim_next(self, **kwargs):
        backend = self.backend
        with backend.condition:
            backend.claim_entries += 1
            backend.condition.notify_all()
        assert backend.claim_release.wait(timeout=10)
        with backend.condition:
            times = backend.claim_times.setdefault(id(self), [])
            times.append(time.monotonic())
            if len(times) <= backend.fail_polls:
                raise RuntimeError("fixture claim transport failure")
            if not backend.tasks:
                return None
            task_id = backend.tasks.popleft()
            attempt = "attempt-" + task_id
            self.attempts[attempt] = task_id
            backend.claimed.append(task_id)
            return {"task_id": task_id, "lease_id": "lease-" + task_id,
                    "attempt_id": attempt, "fence": 1}

    def heartbeat(self, task_id, lease_id, *, attempt_id, fence):
        assert self.attempts[attempt_id] == task_id
        assert lease_id == "lease-" + task_id and fence == 1

    def task(self, task_id):
        return {"task": {"id": task_id, "capability": "cpu.claim-loop", "spec": {}}}

    def fail(self, task_id, lease_id, message, **kwargs):
        self.heartbeat(task_id, lease_id, attempt_id=kwargs["attempt_id"], fence=kwargs["fence"])
        self.backend.failed.append(task_id)


@pytest.fixture
def world(tmp_path, monkeypatch):
    pack = tmp_path / "pack"
    pack.mkdir()
    (pack / "executor.yaml").write_text(json.dumps({
        "schema_version": 1, "id": "cpu.claim-loop", "name": "CPU claim loop",
        "kind": "external", "version": "1.0", "command": {"argv": ["python", "-c", "pass"]},
        "outputs": [], "metadata": {"adapter_family": "cpu"},
    }))

    def execute(slot, task, *, lease_token, attempt_id, fence, **kwargs):
        task_id = task["task"]["id"]
        backend = slot.client.backend
        slot.client.heartbeat(task_id, lease_token, attempt_id=attempt_id, fence=fence)
        owned = SimpleNamespace(task_id=task_id, killed=False, finished=False)
        # The process/bridge/session/warmth mutations mirror the ownership
        # fields that real run_task uses; every serving slot must isolate them.
        with slot._process_lock:
            slot._active_processes.add(OwnedProcess(owned))
        slot._vibecomfy_current_warmth_hint = task_id
        previous = slot._vibecomfy_warmth_hint
        with backend.condition:
            backend.active += 1
            backend.peak = max(backend.peak, backend.active)
            backend.started.append({"host": slot, "task": task_id, "attempt": attempt_id,
                                    "session": slot.managed_tool_session, "previous_warmth": previous,
                                    "thread": threading.get_ident(), "owned": owned})
            backend.condition.notify_all()
        try:
            while not backend.release.wait(0.01):
                if slot._shutdown.is_set():
                    break
            assert slot._vibecomfy_current_warmth_hint == task_id
            slot._vibecomfy_warmth_hint = task_id
            slot._last_cleanup_receipt = {"path": task_id, "status": "deleted"}
            return {"task_id": task_id}
        finally:
            owned.finished = True
            with slot._process_lock:
                slot._active_processes.clear()
            with backend.condition:
                backend.active -= 1
                backend.condition.notify_all()

    monkeypatch.setattr(GenericPackHost, "run_task", execute)
    return pack


class OwnedProcess:
    def __init__(self, state):
        self.state = state


def host_for(pack, backend, capacity=2, **kwargs):
    host = GenericPackHost(pack_roots=[pack], client=Client(backend), max_concurrency=capacity, **kwargs)
    host.discover()
    return host


def run_thread(host, **kwargs):
    outcome = {}
    def run():
        try:
            outcome["result"] = host.run(poll_seconds=0.01, **kwargs)
        except BaseException as exc:
            outcome["error"] = exc
    thread = threading.Thread(target=run)
    thread.start()
    return thread, outcome


@pytest.mark.parametrize("capacity", [2, 3])
def test_capacity_and_finite_claim_allowance_isolate_session_and_attempt_owners(world, capacity):
    backend = Backend(capacity * 2 + 1)
    host = host_for(world, backend, capacity)
    thread, outcome = run_thread(host, max_tasks=capacity * 2)
    try:
        backend.wait(lambda: backend.active == capacity)
        assert len(backend.claimed) == capacity and backend.peak == capacity
        started = list(backend.started)
        assert len({id(row["host"]) for row in started}) == capacity
        assert len({id(row["session"]) for row in started}) == capacity
        assert len({id(row["host"].client) for row in started}) == capacity
        assert len({id(row["host"].client.heartbeat_session) for row in started}) == capacity
        assert host._active_processes == set() and host._vibecomfy_warmth_hint is None
        backend.release.set()
        thread.join(timeout=3)
        assert not thread.is_alive() and "error" not in outcome
        assert len(outcome["result"]) == capacity * 2
        assert backend.peak == capacity and len(backend.tasks) == 1
        assert host._claim_slots == ()
        for row in backend.started:
            assert row["attempt"] in row["host"].client.attempts
            assert row["host"]._active_processes == set()
    finally:
        backend.release.set()
        host.shutdown()
        thread.join(timeout=3)


@pytest.mark.parametrize("once, limit", [(True, None), (False, 2)])
def test_single_slot_and_once_keep_synchronous_compatibility(world, once, limit):
    backend = Backend(3)
    backend.release.set()
    host = host_for(world, backend, capacity=4 if once else 1)
    caller = threading.get_ident()
    try:
        result = host.run(once=once, max_tasks=limit, poll_seconds=0.01)
        assert len(result) == (1 if once else 2)
        assert backend.peak == 1 and backend.clients == []
        assert {row["thread"] for row in backend.started} == {caller}
        if not once:
            assert backend.started[1]["previous_warmth"] == "0"
    finally:
        host.shutdown()


def test_failed_claims_back_off_per_slot_without_losing_the_other_slot(world, capsys):
    backend = Backend(2)
    backend.fail_polls = 2
    backend.release.set()
    host = host_for(world, backend)
    try:
        assert len(host.run(poll_seconds=0.01, max_tasks=2)) == 2
        for times in backend.claim_times.values():
            assert len(times) >= 3
            assert times[1] - times[0] >= 0.045
            assert times[2] - times[1] >= 0.095
        assert "fixture claim transport failure" in capsys.readouterr().err
    finally:
        host.shutdown()


def test_registration_renews_in_coordinator_while_slots_are_running(world, monkeypatch):
    backend = Backend(2)
    host = host_for(world, backend)
    renewals = []
    def renew(*, idle=True):
        renewals.append((threading.get_ident(), idle, backend.active))
        host._registration_refresh_deadline = time.monotonic() + 0.02
    monkeypatch.setattr(host, "_renew_executor_registration", renew)
    host._registration_refresh_deadline = 0.0
    thread, outcome = run_thread(host, max_tasks=2)
    try:
        backend.wait(lambda: backend.active == 2)
        deadline = time.monotonic() + 2
        while len(renewals) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        # Renewal runs in the coordinator, and while slots are running it is told the host is busy
        # (a busy host never adopts a restarted Runtime's credential mid-task).
        assert len(renewals) >= 2 and {ident for ident, _, _ in renewals} == {thread.ident}
        busy = [idle for _, idle, active in renewals if active == 2]
        assert busy and all(idle is False for idle in busy)
        backend.release.set()
        thread.join(timeout=3)
        assert not thread.is_alive() and "error" not in outcome
    finally:
        backend.release.set()
        host.shutdown()
        thread.join(timeout=3)


def test_shutdown_fences_each_slot_and_cleans_only_its_owned_processes(world, monkeypatch):
    backend = Backend(3)
    host = host_for(world, backend)
    killed = []
    unrelated = OwnedProcess(SimpleNamespace(task_id="unrelated", killed=False))
    def terminate(process, **kwargs):
        process.state.killed = True
        killed.append(process)
    monkeypatch.setattr(generic_host, "_terminate_process_group", terminate)
    thread, outcome = run_thread(host)
    try:
        backend.wait(lambda: backend.active == 2)
        slots = tuple(host._claim_slots)
        host.shutdown()
        thread.join(timeout=3)
        assert not thread.is_alive()
        assert "error" not in outcome
        assert all(slot._shutdown.is_set() for slot in slots)
        assert all(row["owned"].killed or row["owned"].finished for row in backend.started)
        assert {process.state.task_id for process in killed} <= set(backend.claimed)
        assert len(backend.claimed) == 2 and unrelated not in killed and not unrelated.state.killed
        assert backend.active == 0
    finally:
        backend.release.set()
        host.shutdown()
        thread.join(timeout=3)


def test_shutdown_during_claim_fails_exact_acquired_attempt_before_execution(world):
    backend = Backend(2)
    backend.claim_release.clear()
    host = host_for(world, backend)
    thread, outcome = run_thread(host)
    try:
        backend.wait(lambda: backend.claim_entries == 2)
        host.shutdown()
        backend.claim_release.set()
        thread.join(timeout=3)
        assert not thread.is_alive() and "error" not in outcome
        assert sorted(backend.failed) == ["0", "1"] and backend.started == []
    finally:
        backend.claim_release.set()
        host.shutdown()
        thread.join(timeout=3)


def test_finally_fences_sibling_claim_before_delayed_first_slot_cleanup(world, monkeypatch):
    backend = Backend(2)
    backend.claim_release.clear()
    host = host_for(world, backend)
    cleanup_started = threading.Event()
    allow_cleanup = threading.Event()
    sibling_failed = threading.Event()
    first = None
    original_shutdown = GenericPackHost.shutdown

    def delayed_shutdown(slot):
        if slot is first:
            cleanup_started.set()
            assert allow_cleanup.wait(timeout=5), "fixture cleanup was not released"
        original_shutdown(slot)

    monkeypatch.setattr(GenericPackHost, "shutdown", delayed_shutdown)
    monkeypatch.setattr(generic_host, "_terminate_process_group",
                        lambda process, **kwargs: setattr(process.state, "killed", True))
    thread, outcome = run_thread(host)
    try:
        backend.wait(lambda: backend.claim_entries == 2)
        first, sibling = host._claim_slots
        fail = sibling.client.fail

        def record_exact_failure(task_id, lease_id, message, **kwargs):
            fail(task_id, lease_id, message, **kwargs)
            assert message == "host shutdown before execution"
            sibling_failed.set()

        monkeypatch.setattr(sibling.client, "fail", record_exact_failure)
        # Trigger the coordinator's finally directly. Public shutdown would
        # already fence every slot and hide the ordering regression.
        host._shutdown.set()
        assert cleanup_started.wait(timeout=2)
        assert not allow_cleanup.is_set() and thread.is_alive()
        backend.claim_release.set()
        assert sibling_failed.wait(timeout=2)
        assert not allow_cleanup.is_set() and thread.is_alive()
        [(attempt_id, task_id)] = sibling.client.attempts.items()
        assert attempt_id == "attempt-" + task_id and task_id in backend.failed
        assert backend.started == []
        allow_cleanup.set()
        thread.join(timeout=3)
        assert not thread.is_alive() and "error" not in outcome
        assert sorted(backend.failed) == ["0", "1"]
    finally:
        backend.claim_release.set()
        backend.release.set()
        allow_cleanup.set()
        host.shutdown()
        thread.join(timeout=3)


def test_unresponsive_claim_owner_has_bounded_join_and_is_retained_fenced(world):
    backend = Backend(2)
    backend.claim_release.clear()
    host = host_for(world, backend)
    thread, outcome = run_thread(host)
    slots = ()
    try:
        backend.wait(lambda: backend.claim_entries == 2)
        slots = tuple(host._claim_slots)
        host.shutdown()
        thread.join(timeout=6)
        assert not thread.is_alive()
        assert isinstance(outcome.get("error"), HostError)
        assert "five seconds" in str(outcome["error"])
        assert host._unresolved_task_scopes and host._claim_slots == slots
        assert all(slot._shutdown.is_set() for slot in slots)
    finally:
        # Only these fixture-owned claim owners are released. They record the
        # exact late-acquired leases as failures, never execute a new attempt.
        backend.claim_release.set()
        host.shutdown()
        thread.join(timeout=2)
        deadline = time.monotonic() + 2
        while len(backend.failed) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
    assert backend.started == [] and sorted(backend.failed) == ["0", "1"]


def test_failed_registration_refresh_backs_off_and_blocks_new_claims(world, monkeypatch):
    backend = Backend(2)
    backend.release.set()
    host = host_for(world, backend)
    times = []
    def renew(*, idle=True):
        times.append(time.monotonic())
        if len(times) == 1:
            assert backend.claimed == []
            raise RuntimeError("registration unavailable")
        host._registration_refresh_deadline = float("inf")
    monkeypatch.setattr(host, "_renew_executor_registration", renew)
    host._registration_refresh_deadline = 0.0
    assert len(host.run(poll_seconds=0.01, max_tasks=2)) == 2
    assert len(times) == 2 and times[1] - times[0] >= 0.045
    host.shutdown()


def test_parallel_caller_owned_attempt_root_is_rejected_before_claim(world, tmp_path):
    backend = Backend(1)
    host = host_for(world, backend, attempt_root=tmp_path / "caller-owned")
    with pytest.raises(HostError, match="attempt_base or ephemeral"):
        host.run(max_tasks=1)
    assert backend.claim_entries == 0 and backend.clients == []


def test_runtime_slot_fork_does_not_share_transport_or_attempt_heartbeat_state():
    client = RuntimeProtocolClient("http://127.0.0.1:1", "fixture-only")
    client.executor_id = "host"
    client._attempt_runtime_epochs["parent"] = 1
    other = client.fork_for_host_slot()
    assert other.executor_id == "host"
    assert other.generated is not client.generated
    assert other._attempt_runtime_epochs == {}
    assert other._heartbeat_lock is not client._heartbeat_lock
    assert other._heartbeat_session != client._heartbeat_session
