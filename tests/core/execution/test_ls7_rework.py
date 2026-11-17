"""Lead probes for ready/claim disagreement and redundant observations."""
from __future__ import annotations

import json
import threading
import time

import pytest

from astrid.core.execution.generic_host import GenericPackHost
from tests.core.execution.test_generic_host_claim_loop import Backend, Client, world


class ReadyClient(Client):
    def supports_task_waits(self):
        return True

    def fork_for_host_slot(self):
        client = ReadyClient(self.backend)
        self.backend.clients.append(client)
        return client

    def wait_for_work(self, *, capability_ids, **kwargs):
        with self.backend.condition:
            self.backend.wait_times.setdefault(id(self), []).append(time.monotonic())
            self.backend.condition.notify_all()
        return {"available": True, "capability_ids": list(capability_ids), "reason": "changed"}

    def claim_next(self, **kwargs):
        result = super().claim_next(**kwargs)
        if result is None and self.backend.mode == "waiting-reason":
            return {"task": {"task_id": "blocked", "capability_id": "cpu.claim-loop"},
                    "waiting_reason": "waiting_for_worker"}
        return result


@pytest.mark.parametrize("capacity", [1, 2])
@pytest.mark.parametrize("mode", ["closed-gate", "empty-claim", "waiting-reason"])
def test_ready_but_empty_claim_rate_is_bounded(world, capacity, mode):
    backend = Backend(0)
    backend.wait_times, backend.mode = {}, mode
    host = GenericPackHost(pack_roots=[world], client=ReadyClient(backend), max_concurrency=capacity)
    host.discover()
    if mode == "closed-gate":
        with host._handoff_condition:
            host._handoff_claim_gate_closed = True
    outcome = {}
    def run():
        try:
            outcome["results"] = host.run(poll_seconds=0)
        except BaseException as error:
            outcome["error"] = error
    thread = threading.Thread(target=run)
    thread.start()
    try:
        backend.wait(lambda: bool(backend.wait_times))
        started = time.monotonic()
        threading.Event().wait(.26)
        with backend.condition:
            observations = {key: list(value) for key, value in backend.wait_times.items()}
            claims = backend.claim_entries
        elapsed = time.monotonic() - started
        waits = sum(map(len, observations.values()))
        print(json.dumps({"mode": mode, "capacity": capacity, "seconds": elapsed,
                          "work_wait_calls": waits, "claim_calls": claims}))
        assert waits <= capacity * 7, "ready-but-empty work spun instead of backing off"
        for times in observations.values():
            assert all(right - left >= .045 for left, right in zip(times, times[1:])), "immediate retry or redundant pre-claim wait"
        assert claims == 0 if mode == "closed-gate" else claims <= capacity * 7
    finally:
        host.shutdown()
        thread.join(3)
    assert not thread.is_alive() and "error" not in outcome


@pytest.mark.parametrize("capacity", [1, 2])
def test_successful_claim_consumes_the_prior_ready_observation_once(world, capacity):
    backend = Backend(capacity)
    backend.wait_times, backend.mode = {}, "success"
    backend.release.set()
    host = GenericPackHost(pack_roots=[world], client=ReadyClient(backend), max_concurrency=capacity)
    host.discover()
    try:
        assert len(host.run(max_tasks=capacity, poll_seconds=10)) == capacity
        assert backend.claim_entries == capacity
        assert sum(map(len, backend.wait_times.values())) == capacity
    finally:
        host.shutdown()
