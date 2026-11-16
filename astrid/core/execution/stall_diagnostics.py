"""Process-wide native stack dumps for tasks with no semantic progress.

The faulthandler timer is process-global, so concurrent task slots share one
coordinator.  One overdue task owns the timer until that task makes semantic
progress or finishes.  Other tasks cannot postpone or cancel its dump.  The
timer writes to a duplicate of the service's existing stderr descriptor; the
Runtime supervisor routes that descriptor to ``service-pack-host.log``.

This is deliberately one native snapshot per active stall episode.  There is
no callback from faulthandler confirming that the snapshot reached disk.
"""

from __future__ import annotations

import faulthandler
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable


@dataclass
class _TaskProgress:
    last_change: float
    token: object
    diagnosed: bool = False


class TaskStallDiagnostics:
    """Coordinate one faulthandler timeout across independent task slots."""

    def __init__(self, *, timeout: float = 90.0,
                 clock: Callable[[], float] = time.monotonic,
                 timer=faulthandler.dump_traceback_later,
                 cancel=faulthandler.cancel_dump_traceback_later,
                 fd: int | None = None) -> None:
        if timeout <= 0:
            raise ValueError("stall timeout must be positive")
        self.timeout = float(timeout)
        self._clock = clock
        self._timer = timer
        self._cancel = cancel
        self._lock = threading.RLock()
        self._tasks: dict[str, _TaskProgress] = {}
        self._owner: str | None = None
        self._timer_armed = False
        self._armed_deadline: float | None = None
        self._file = None
        self._fd = fd

    def _ensure_file(self):
        if self._file is None:
            descriptor = os.dup(2 if self._fd is None else self._fd)
            try:
                self._file = os.fdopen(descriptor, "w", encoding="utf-8", buffering=1)
            except Exception:
                os.close(descriptor)
                raise
        return self._file

    def _arm(self) -> None:
        now = self._clock()
        if self._owner is not None:
            owner = self._tasks.get(self._owner)
            if owner is not None and not owner.diagnosed and now >= owner.last_change + self.timeout:
                # Keep the overdue owner pinned. Its C-level timer cannot be
                # cancelled by another slot's progress or completion.
                return
        candidates = [
            (task.last_change + self.timeout, key)
            for key, task in self._tasks.items()
            if not task.diagnosed
        ]
        next_deadline, next_owner = min(candidates) if candidates else (None, None)
        if (next_owner == self._owner and self._armed_deadline == next_deadline
                and (self._timer_armed if next_owner is not None else not self._timer_armed)):
            return
        if self._timer_armed:
            try:
                self._cancel()
            except (OSError, RuntimeError, ValueError):
                # Keep the descriptor alive if faulthandler may still use it.
                return
            self._timer_armed = False
            self._armed_deadline = None
        self._owner = next_owner
        if next_owner is not None:
            delay = max(0.001, next_deadline - now)
            try:
                self._timer(delay, repeat=False, file=self._ensure_file(), exit=False)
                self._timer_armed = True
                self._armed_deadline = next_deadline
            except (OSError, RuntimeError, ValueError, OverflowError):
                # Diagnostics must never change task outcomes.
                self._owner = None
                self._armed_deadline = None
                if self._file is not None:
                    try:
                        self._file.close()
                    except (OSError, ValueError):
                        pass
                    self._file = None
        elif self._file is not None:
            try:
                self._file.close()
            except (OSError, ValueError):
                pass
            self._file = None

    def begin(self, task_key: str, token: object) -> None:
        """Start a task's no-progress interval."""
        with self._lock:
            self._tasks[task_key] = _TaskProgress(self._clock(), token)
            self._arm()

    def update(self, task_key: str, token: object) -> None:
        """Reset only this task's interval when its semantic token changes."""
        with self._lock:
            current = self._tasks.get(task_key)
            if current is None:
                return
            if current.token != token:
                now = self._clock()
                if self._owner == task_key and now >= current.last_change + self.timeout:
                    # We cannot query whether the C timer has already emitted.
                    # Treat the expired active episode as consumed; never
                    # schedule a second dump for the same attempt.
                    current.diagnosed = True
                current.token = token
                current.last_change = now
            self._arm()

    def end(self, task_key: str) -> None:
        """Remove a task and hand the native timer to the next deadline."""
        with self._lock:
            self._tasks.pop(task_key, None)
            self._arm()


_PROCESS_STALL_DIAGNOSTICS = TaskStallDiagnostics()


def process_stall_diagnostics() -> TaskStallDiagnostics:
    return _PROCESS_STALL_DIAGNOSTICS
