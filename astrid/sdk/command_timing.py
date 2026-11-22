"""Command-local, monotonic client timings and progress messages."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import json
import sys
import threading
import time


class TimingCollector:
    def __init__(self, started=None):
        self.timings = []
        self.started = time.monotonic() if started is None else started
        self.active = []
        self.lock = threading.Lock()

    def snapshot(self):
        with self.lock:
            rows = [dict(row) for row in self.timings]
            elapsed = time.monotonic() - self.started
            # Include an active stage if output is produced inside it.
            now = time.monotonic()
            for index, item in enumerate(self.active):
                child = (now - self.active[index + 1]['started'] if index + 1 < len(self.active) else 0.0)
                rows.append({'stage': item['name'], 'seconds': max(0.0, now - item['started'] - item['children'] - child)})
            measured = sum(row['seconds'] for row in rows)
            rows.append({'stage': 'other command work', 'seconds': max(0.0, elapsed - measured)})
            return rows


_current = ContextVar('astrid_command_timings', default=None)


@contextmanager
def timing_scope(collector):
    token = _current.set(collector)
    try:
        yield collector
    finally:
        _current.reset(token)


def current_timings():
    collector = _current.get()
    return collector.snapshot() if collector is not None else []


class TimingOutput:
    """Enrich complete JSON objects as they are written, without buffering output."""
    def __init__(self, stream, collector):
        self.stream, self.collector = stream, collector

    def write(self, text):
        try:
            value = json.loads(text)
        except (ValueError, TypeError):
            return self.stream.write(text)
        if isinstance(value, dict):
            value['timings'] = self.collector.snapshot()
            text = json.dumps(value, ensure_ascii=False)
        return self.stream.write(text)

    def flush(self):
        return self.stream.flush()

    def __getattr__(self, name):
        return getattr(self.stream, name)


@contextmanager
def stage(name):
    collector = _current.get()
    if collector is None:
        yield
        return
    item = {'name': name, 'started': time.monotonic(), 'children': 0.0,
            'active_seconds': 0.0, 'active_since': None}
    done = threading.Event()
    with collector.lock:
        now = time.monotonic()
        if collector.active:
            parent = collector.active[-1]
            parent['active_seconds'] += now - parent['active_since']
            parent['active_since'] = None
        item['active_since'] = now
        collector.active.append(item)

    def waiting():
        while not done.wait(0.025):
            with collector.lock:
                if collector.active and collector.active[-1] is item:
                    active = item['active_seconds'] + time.monotonic() - item['active_since']
                    if active >= 2.0:
                        print(f'Waiting for {name}...', file=sys.stderr, flush=True)
                        return

    timer = threading.Thread(target=waiting, daemon=True)
    timer.start()
    try:
        yield
    finally:
        done.set()
        timer.join()
        ended = time.monotonic()
        elapsed = ended - item['started']
        with collector.lock:
            collector.active.remove(item)
            if collector.active:
                collector.active[-1]['children'] += elapsed
                collector.active[-1]['active_since'] = ended
            collector.timings.append({'stage': name, 'seconds': max(0.0, elapsed - item['children'])})


def timed_call(name, function, *args, **kwargs):
    with stage(name):
        return function(*args, **kwargs)
