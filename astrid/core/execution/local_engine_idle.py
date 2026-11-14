"""Host-wide idle retention for the installation-owned local ComfyUI engine.

Lock order: the admission condition is innermost with respect to engine
locks and is never held across I/O. Idle release takes no installation lock;
the supervisor's work lock is taken only inside the release RPC. The spawned
production HTTP worker submits prompts outside that work lock.

ComfyUI 6bfaacc6 server.py:1195-1203 only sets free flags; main.py:386-399
applies them after execution/history publication. Release cannot interrupt a
prompt. Admission therefore progresses during release, without a deadline-free
wait. Preparation RPCs can wait behind release for its own at-most-10s budget.
"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import nullcontext
from typing import Any

from .warm_hold import DEFAULT_IDLE_SECONDS, WarmHolds, _start_timer, idle_seconds

_LOGGER = logging.getLogger(__name__)


class LocalEngineIdle:
    """Count admitted jobs across slots, including jobs waiting for a host slot.

    WarmHolds owns the single deadline. The supervisor owns run/release
    serialization, so its RPC must run without the admission condition held.
    Busy or failed releases consume the deadline; only the next task end
    re-arms it. There is no polling or retry loop.
    """

    def __init__(self, *, timer=_start_timer, clock=time.time, on_change=None):
        self._condition = threading.Condition(threading.RLock())
        self._holds = WarmHolds(timer=timer, clock=clock)
        # No installation-lock edge: the HTTP worker bypasses supervisor work,
        # and ComfyUI handles free only at prompt boundaries. Admission must
        # progress while the bounded release RPC is in flight.
        self._holder_lock = nullcontext()
        self._lease = None
        self._supervisor = None
        self._active_jobs = 0
        self._generation = 0
        self._idle_seconds = float(DEFAULT_IDLE_SECONDS)
        self._closed = False
        self._releasing = 0
        self._last_release = None
        self._release_record = None
        self._warning = None
        self._on_change = on_change

    def begin(self, *, requested=None):
        seconds = idle_seconds(DEFAULT_IDLE_SECONDS, requested=requested)
        with self._condition:
            if self._closed:
                return False
            self._active_jobs += 1
            self._idle_seconds = seconds
            if self._active_jobs == 1:
                # Record the generation of the idle deadline this admission
                # group will arm in finish(), before WarmHolds can call back.
                generation = self._generation + 1
                self._lease = self._holds.lease(
                    self, stop=lambda: self._release_if_idle(generation), lock=self._holder_lock)
            self._condition.notify_all()
        self._changed()
        return True

    def retain(self, supervisor):
        """Retain only the authenticated client from successful local preparation."""
        with self._condition:
            if not self._closed:
                self._supervisor = supervisor

    def finish(self):
        with self._condition:
            self._active_jobs -= 1
            if self._active_jobs == 0:
                self._generation += 1
                self._holds.release(self._lease,
                    holding="local managed ComfyUI" if self._supervisor is not None else None,
                    on_idle="models unloaded; engine process retained", idle_seconds=self._idle_seconds)
                self._lease = None
            self._condition.notify_all()
        self._changed()

    def _release_if_idle(self, generation):
        with self._condition:
            if self._closed or self._active_jobs or generation != self._generation:
                return
            supervisor = self._supervisor
            self._releasing += 1
        try:
            # Admission can land between the condition's final generation/busy
            # check above and /free. ComfyUI 6bfaacc6 main.py:386-399 applies
            # that one flag only between prompts: at most one model reload,
            # never an interrupted execution. Do not fence HTTP preparation
            # with an installation lock. Its supervisor RPC may instead wait
            # behind this release for at most this independent 10s budget.
            record = supervisor.release_owned(deadline=time.monotonic() + 10.0)
            status = record.get("status") if isinstance(record, dict) else "failed"
            reason = record.get("reason") if isinstance(record, dict) else None
            warning = None if status == "completed" else f"release returned {status}" + (f": {reason}" if reason else "")
        except Exception as exc:  # noqa: BLE001 - idle release must never fail a task
            record, status, warning = None, "failed", str(exc)
        if warning:
            _LOGGER.warning("local managed engine idle release: %s", warning)
        with self._condition:
            self._last_release, self._release_record, self._warning = status, record, warning
            self._releasing -= 1
            self._condition.notify_all()
        self._changed()

    def status(self) -> dict[str, Any]:
        with self._condition:
            return {"active_jobs": self._active_jobs, "closed": self._closed,
                    "last_release": self._last_release, "release_record": self._release_record,
                    "warning": self._warning, "warm_holds": self._holds.status()}

    def close(self):
        with self._condition:
            self._closed = True
            self._holds.close()
            self._condition.notify_all()
            self._condition.wait_for(lambda: not self._releasing)
        self._changed()

    def _changed(self):
        if self._on_change is not None:
            self._on_change()
