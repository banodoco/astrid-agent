"""Idle release for everything the pack host keeps warm between tasks.

The Wan2GP child, the VibeComfy session and the Remotion frame owner stay
resident after a task so the next one starts warm.  Each holder leases its
hold while a task uses it.  A new lease cancels any pending expiry.  When the
last lease ends and the holder still keeps something warm, one deadline is
armed; on expiry the holder's own stop runs, under the holder's own lock and
only if no lease arrived meanwhile.  Packs write no timers.

This is idle retention only.  Task lease expiry and billable lifetime are
separate policies, and RunPod pods are not held here.
"""
from __future__ import annotations

import math
import os
import threading
import time
from datetime import datetime, timezone
from functools import partial
from typing import Any, Callable, Hashable, Mapping

from astrid.core.env_vars import ASTRID_WARM_IDLE_SECONDS

# A GPU/model holder keeps its weights this long after its last task.
DEFAULT_IDLE_SECONDS = 600.0


def idle_seconds(default: float, *, requested: float | None = None,
                 environ: Mapping[str, str] | None = None) -> float:
    """A request's ``lifecycle.idle_timeout_seconds`` wins for its holder,
    then ASTRID_WARM_IDLE_SECONDS for every holder, then the holder's default."""
    if requested is not None:
        return float(requested)
    configured = (os.environ if environ is None else environ).get(ASTRID_WARM_IDLE_SECONDS)
    try:
        value = float(configured) if configured is not None else math.nan
    except ValueError:
        value = math.nan
    return value if math.isfinite(value) and value >= 0 else default


def _start_timer(seconds: float, callback: Callable[[], None]) -> threading.Timer:
    timer = threading.Timer(seconds, callback)
    timer.daemon = True
    timer.start()
    return timer


class _Hold:
    def __init__(self, key: Hashable, stop: Callable[[], None], lock: Any) -> None:
        self.key, self.stop, self.lock = key, stop, lock
        self.leases = 0
        self.holding: str | None = None
        self.on_idle: str | None = None
        self.idle_seconds = 0.0
        self.expires_at: float | None = None
        self.timer: Any = None
        self.generation = 0


class WarmLease:
    """One task's use of a hold; releasing it twice is a no-op."""

    def __init__(self, hold: _Hold | None) -> None:
        self._hold = hold


class WarmHolds:
    """One idle deadline per warm holder; each pack host and serving slot owns one.

    ``timer(seconds, callback)`` must return an object with ``cancel()``;
    ``clock`` gives wall time for status.  Both are injectable so expiry is
    driven explicitly in tests.  ``on_change`` runs after a visible hold
    changes, outside this object's lock.
    """

    def __init__(self, *, timer: Callable[[float, Callable[[], None]], Any] = _start_timer,
                 clock: Callable[[], float] = time.time,
                 on_change: Callable[[], None] | None = None) -> None:
        self._timer, self._clock, self._on_change = timer, clock, on_change
        self._lock = threading.Lock()
        self._holds: dict[Hashable, _Hold] = {}
        self._closed = False

    def lease(self, key: Hashable, *, stop: Callable[[], None], lock: Any) -> WarmLease:
        """Hold ``key`` for one task and cancel its pending expiry.

        ``lock`` guards the holder's resource; ``stop`` ends that resource and
        runs under ``lock``.  Leasing takes ``lock`` first, so a lease never
        returns while an expiry's stop is still running; ``lock`` must be
        reentrant if callers hold it while leasing.
        """
        with lock, self._lock:
            if self._closed:
                return WarmLease(None)
            hold = self._holds.get(key)
            if hold is None:
                hold = self._holds[key] = _Hold(key, stop, lock)
            hold.leases += 1
            visible = hold.holding is not None
            self._disarm(hold)
        if visible:
            self._changed()
        return WarmLease(hold)

    def release(self, lease: WarmLease, *, holding: str | None, idle_seconds: float,
                on_idle: str | None = None) -> None:
        """End one lease.  After the last, arm one deadline while ``holding``
        names what stays warm; ``None`` means nothing does, so forget the hold.
        ``on_idle`` says exactly what the holder's stop will do, for status."""
        with self._lock:
            hold, lease._hold = lease._hold, None
            if hold is None or self._holds.get(hold.key) is not hold:
                return
            hold.leases -= 1
            if hold.leases:
                return
            if holding is None or self._closed:
                del self._holds[hold.key]
            else:
                hold.holding, hold.on_idle, hold.idle_seconds = holding, on_idle, float(idle_seconds)
                hold.expires_at = self._clock() + hold.idle_seconds
                hold.timer = self._timer(hold.idle_seconds, partial(self._expire, hold, hold.generation))
        self._changed()

    def status(self) -> list[dict[str, Any]]:
        """What each hold keeps warm and when it would expire (null while in use)."""
        with self._lock:
            return [
                {
                    "holding": hold.holding,
                    "on_idle": hold.on_idle,
                    "leases": hold.leases,
                    "idle_seconds": hold.idle_seconds,
                    "expires_at": None if hold.leases or hold.expires_at is None else
                    datetime.fromtimestamp(hold.expires_at, timezone.utc).isoformat(timespec="seconds"),
                }
                for hold in self._holds.values() if hold.holding is not None
            ]

    def close(self) -> None:
        """Cancel every pending expiry; holders close their own resources."""
        with self._lock:
            self._closed = True
            for hold in self._holds.values():
                self._disarm(hold)
            self._holds.clear()
        self._changed()

    def _disarm(self, hold: _Hold) -> None:
        hold.generation += 1
        hold.expires_at = None
        if hold.timer is not None:
            hold.timer.cancel()
            hold.timer = None

    def _expire(self, hold: _Hold, generation: int) -> None:
        # Holder lock first, as on the lease path, so a task that leased
        # meanwhile either cancelled this expiry or waits for the stop.
        expired = False
        try:
            with hold.lock:
                with self._lock:
                    if self._holds.get(hold.key) is not hold or hold.generation != generation or hold.leases:
                        return
                    del self._holds[hold.key]
                expired = True
                hold.stop()
        finally:
            if expired:
                self._changed()

    def _changed(self) -> None:
        if self._on_change is not None:
            self._on_change()
