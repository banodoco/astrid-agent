"""Command-local monotonic timings and progress for slow client stages."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import sys
import threading
import time


class TimingCollector:
    def __init__(self):
        self.timings = []


_current = ContextVar("astrid_command_timings", default=None)


@contextmanager
def timing_scope(collector):
    token = _current.set(collector)
    try:
        yield collector
    finally:
        _current.reset(token)


@contextmanager
def stage(name):
    collector = _current.get()
    if collector is None:
        yield
        return
    started = time.monotonic()
    done = threading.Event()
    def waiting():
        if not done.wait(2.0):
            print(f"Waiting for {name}...", file=sys.stderr, flush=True)
    timer = threading.Thread(target=waiting, daemon=True)
    timer.start()
    try:
        yield
    finally:
        done.set()
        timer.join()
        collector.timings.append({"stage": name, "seconds": time.monotonic() - started})
