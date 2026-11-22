"""Bounded waits for client-owned locks."""
from __future__ import annotations

import time
from contextlib import contextmanager

from runtime_protocol import cancellation, platform
from astrid.sdk.command_timing import stage

LOCK_TIMEOUT = 135.0


@contextmanager
def thread_lock(lock, name):
    with stage(name, report_without_collector=True):
        acquired = lock.acquire(timeout=LOCK_TIMEOUT)
    if not acquired:
        raise TimeoutError(f"{name} timed out after {LOCK_TIMEOUT:g} seconds")
    try:
        yield
    finally:
        lock.release()


def acquire_file_lock(fd, name):
    deadline = time.monotonic() + LOCK_TIMEOUT
    with stage(name, report_without_collector=True):
        while True:
            cancellation.check()
            try:
                platform.lock_file(fd, blocking=False)
                return
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"{name} timed out after {LOCK_TIMEOUT:g} seconds")
                cancellation.pause(min(0.05, remaining))
