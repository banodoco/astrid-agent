"""Idle release of warm holders, driven by explicit timers instead of sleeps."""
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from astrid.core.env_vars import ASTRID_WARM_IDLE_SECONDS
from astrid.core.execution import generic_host, process_group
from astrid.core.execution.generic_host import GenericPackHost
from astrid.core.execution.guards import EVIDENCE_STATUS_NAME, read_evidence_status
from astrid.core.execution.managed_tool_session import CapabilityDescriptor, SessionBinding
from astrid.core.execution.process_group import popen_owned_group
from astrid.core.execution.remotion_host_session import frame_owner_idle_seconds
from astrid.core.execution.warm_hold import WarmHolds, idle_seconds


class Timers:
    """Records each armed deadline; a test fires the live one explicitly."""

    class Timer:
        def __init__(self, seconds, callback):
            self.seconds, self.callback, self.cancelled = seconds, callback, False

        def cancel(self):
            self.cancelled = True

    def __init__(self):
        self.armed = []

    def __call__(self, seconds, callback):
        self.armed.append(self.Timer(seconds, callback))
        return self.armed[-1]

    def live(self):
        live = [timer for timer in self.armed if not timer.cancelled]
        assert len(live) <= 1
        return live[0] if live else None

    def fire(self):
        timer = self.live()
        timer.cancelled = True  # spent, as a real timer is once it has run
        timer.callback()


def holder():
    lock, stops = threading.RLock(), []
    return lock, stops, (lambda: stops.append(lock._is_owned()))


def test_hold_expires_after_the_last_release_by_running_the_holders_stop():
    timers, (lock, stops, stop) = Timers(), holder()
    holds = WarmHolds(timer=timers, clock=lambda: 0.0)
    lease = holds.lease("engine", stop=stop, lock=lock)
    assert holds.status() == [] and timers.live() is None
    holds.release(lease, holding="fixture.hold", on_idle="child process ended", idle_seconds=600)
    assert holds.status() == [{"holding": "fixture.hold", "on_idle": "child process ended", "leases": 0,
                               "idle_seconds": 600.0, "expires_at": "1970-01-01T00:10:00+00:00"}]
    assert timers.live().seconds == 600 and stops == []
    timers.fire()
    assert stops == [True], "stop runs once, under the holder's own lock"
    assert holds.status() == []
    holds.release(lease, holding="fixture.hold", idle_seconds=600)
    assert timers.live() is None, "a released lease cannot arm twice"


def test_a_new_task_cancels_the_pending_expiry():
    timers, (lock, stops, stop) = Timers(), holder()
    holds = WarmHolds(timer=timers)
    holds.release(holds.lease("engine", stop=stop, lock=lock), holding="vibecomfy.run", idle_seconds=600)
    armed = timers.live()
    again = holds.lease("engine", stop=stop, lock=lock)
    assert armed.cancelled and holds.status()[0]["leases"] == 1
    assert holds.status()[0]["expires_at"] is None
    armed.callback()
    assert stops == [], "a stale deadline firing late changes nothing"
    holds.release(again, holding="vibecomfy.run", idle_seconds=600)
    timers.fire()
    assert stops == [True]


def test_concurrent_leases_arm_one_deadline_after_the_last_release():
    timers, (lock, stops, stop) = Timers(), holder()
    holds = WarmHolds(timer=timers)
    first = holds.lease("frames", stop=stop, lock=lock)
    second = holds.lease("frames", stop=stop, lock=lock)
    holds.release(first, holding="Remotion frame owner", idle_seconds=45)
    assert timers.live() is None
    holds.release(second, holding="Remotion frame owner", idle_seconds=45)
    assert len(timers.armed) == 1 and timers.live().seconds == 45
    timers.fire()
    assert stops == [True]


def test_a_task_that_leaves_nothing_warm_forgets_the_hold():
    timers, (lock, stops, stop) = Timers(), holder()
    holds = WarmHolds(timer=timers)
    holds.release(holds.lease("engine", stop=stop, lock=lock), holding="fixture.hold", idle_seconds=600)
    armed = timers.live()
    # The next task replaced or released the session itself.
    holds.release(holds.lease("engine", stop=stop, lock=lock), holding=None, idle_seconds=600)
    assert holds.status() == [] and timers.live() is None
    armed.callback()
    assert stops == []


def test_expiry_waiting_for_the_holder_lock_yields_to_a_task_that_arrives_first():
    timers, (lock, stops, stop) = Timers(), holder()
    holds = WarmHolds(timer=timers)
    holds.release(holds.lease("engine", stop=stop, lock=lock), holding="fixture.hold", idle_seconds=600)
    expire = timers.live().callback
    with lock:  # the holder is busy: a task is inspecting its session
        expiry = threading.Thread(target=expire)
        expiry.start()
        holds.lease("engine", stop=stop, lock=lock)
    expiry.join()
    assert stops == [] and holds.status()[0]["leases"] == 1


def test_a_lease_waits_for_an_expiry_whose_stop_is_already_running():
    timers, inner = Timers(), threading.RLock()
    holds = WarmHolds(timer=timers)
    stopping, proceed, progressed = threading.Event(), threading.Event(), threading.Event()
    order, leaser = [], None

    class HolderLock:
        def __enter__(self):
            if threading.current_thread() is leaser:
                progressed.set()  # the new lease has reached the holder's lock
            inner.acquire()

        def __exit__(self, *exc_info):
            inner.release()

    def stop():
        stopping.set()
        proceed.wait()
        order.append("stopped")

    def lease():
        holds.lease("engine", stop=stop, lock=lock)
        order.append("leased")
        progressed.set()

    lock = HolderLock()
    holds.release(holds.lease("engine", stop=stop, lock=lock), holding="fixture.hold", idle_seconds=600)
    expiry = threading.Thread(target=timers.fire, daemon=True)
    expiry.start()
    stopping.wait()
    leaser = threading.Thread(target=lease, daemon=True)
    leaser.start()
    try:
        progressed.wait()
        assert order == [], "a new lease must not return while the holder's stop runs"
    finally:
        proceed.set()
    expiry.join()
    leaser.join()
    assert order == ["stopped", "leased"]


def test_close_cancels_pending_expiries_and_later_leases_are_inert():
    timers, (lock, stops, stop) = Timers(), holder()
    holds = WarmHolds(timer=timers)
    holds.release(holds.lease("engine", stop=stop, lock=lock), holding="vibecomfy.run", idle_seconds=600)
    armed = timers.live()
    holds.close()
    assert armed.cancelled and holds.status() == []
    holds.release(holds.lease("engine", stop=stop, lock=lock), holding="vibecomfy.run", idle_seconds=600)
    assert timers.live() is None and stops == []


def test_request_wins_then_the_one_knob_then_each_holders_default(monkeypatch):
    monkeypatch.delenv(ASTRID_WARM_IDLE_SECONDS, raising=False)
    budget = {"ASTRID_REMOTION_WARM_MEMORY_BUDGET_BYTES": str(512 * 1024**2)}
    assert idle_seconds(600) == 600
    assert frame_owner_idle_seconds({}) == 45 and frame_owner_idle_seconds(budget) == 300
    monkeypatch.setenv(ASTRID_WARM_IDLE_SECONDS, "120")
    assert idle_seconds(600) == 120
    assert frame_owner_idle_seconds({}) == 120 and frame_owner_idle_seconds(budget) == 120
    assert idle_seconds(600, requested=30) == 30
    assert frame_owner_idle_seconds(budget, requested=30) == 30
    for invalid in ("soon", "-1", "nan", "inf"):
        monkeypatch.setenv(ASTRID_WARM_IDLE_SECONDS, invalid)
        assert idle_seconds(600) == 600

