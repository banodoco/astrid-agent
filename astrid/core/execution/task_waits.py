"""Cancellable task observations and acknowledged-lease scheduling.

These helpers never claim work, infer a renewal from a task read, or cancel an
attempt. Closing an observation releases only its local HTTP subscription.
"""
from __future__ import annotations

import math
import threading
from collections.abc import Mapping
from datetime import datetime, timezone


def renewal_delay(expiry, margin=5.0):
    """Seconds until renewal is due; missing/untrusted time requires renewal."""
    try:
        deadline = datetime.fromisoformat(expiry.replace("Z", "+00:00"))
        if deadline.tzinfo is None:
            return 0.0
        return max(0.0, (deadline - datetime.now(timezone.utc)).total_seconds() - margin)
    except (AttributeError, TypeError, ValueError, OverflowError):
        return 0.0


def renewal_margin(expiry):
    """Reserve half the acknowledged lease for a stalled renewal request.

    Freeze this at claim/heartbeat acknowledgement, never from an observation
    or on each loop iteration: neither may keep moving the renewal deadline.
    """
    return max(0.05, renewal_delay(expiry, margin=0.0) / 2)


def settlement_progress(value):
    """Keep a valid final checkpoint; invalid producer progress cannot fail work.

    The renewal path already omits invalid progress after a typed refusal. A
    fast attempt has no renewal, so apply the same wire constraints before
    freezing its terminal request. Runtime validates the checkpoint again.
    """
    if not isinstance(value, Mapping) or not value or set(value) - {"phase", "percent", "current", "total"}:
        return None
    if "phase" in value and (not isinstance(value["phase"], str) or not value["phase"].strip() or len(value["phase"]) > 128):
        return None
    for key in ("percent", "current", "total"):
        if key not in value:
            continue
        number = value[key]
        if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
            return None
        if (key == "percent" and not 0 <= number <= 100) or (key == "current" and number < 0) or (key == "total" and number < 1):
            return None
    return dict(value)


class AttemptWait:
    """Interrupt an in-flight read when a local lifecycle deadline changes."""

    def __init__(self, client, task_id):
        self.client, self.task_id = client, task_id
        self._lock = threading.Lock()
        self._observer = None
        self._generation = 0
        self._closed = False

    def wake(self, *, close=False):
        with self._lock:
            self._closed = self._closed or close
            self._generation += 1
            observer, self._observer = self._observer, None
        if observer is not None:
            self.client.close_task_observer(observer)

    def close(self):
        self.wake(close=True)

    def wait(self, *, cursor, timeout_seconds):
        with self._lock:
            if self._closed:
                return None
            if self._observer is None:
                self._observer = self.client.new_task_observer()
            observer, generation = self._observer, self._generation
        try:
            return observer.wait_for_task(self.task_id, cursor=cursor, timeout_seconds=timeout_seconds)
        except BaseException:
            with self._lock:
                interrupted = self._closed or generation != self._generation
            if interrupted:
                return None
            raise
