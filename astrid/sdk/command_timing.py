"""Command-local, monotonic client timings and progress messages."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import json
import os
import sys
import threading
import time


class TimingCollector:
    def __init__(self, started=None):
        self.timings = []
        if started is None:
            started = time.monotonic()
        self.started = min(started, time.monotonic())
        self.active = []
        self.lock = threading.Lock()
        self.changed = threading.Condition(self.lock)
        self.monitor = None
        self.closed = False
        self.last_change = time.monotonic()

    def _advance(self, now):
        if self.active:
            self.active[-1]['seconds'] += now - self.last_change
        self.last_change = now

    def _watch(self):
        with self.changed:
            while not self.closed:
                now = time.monotonic()
                eligible = [item for item in self.active
                            if not item['reported'] and now - item['started'] >= 2.0]
                if eligible:
                    item = eligible[-1]
                    item['reported'] = True
                    # A nested wait explains its ancestors' wait as well.
                    parent = item['parent']
                    while parent is not None:
                        parent['reported'] = True
                        parent = parent['parent']
                    print(f"Waiting for {item['name']}...", file=sys.stderr, flush=True)
                    continue
                deadlines = [item['started'] + 2.0 for item in self.active if not item['reported']]
                self.changed.wait(max(0.0, min(deadlines) - now + 0.025) if deadlines else None)

    def close(self):
        with self.changed:
            self.closed = True
            self.changed.notify_all()
        if self.monitor is not None:
            self.monitor.join()

    def snapshot(self):
        with self.lock:
            now = time.monotonic()
            self._advance(now)
            rows = [dict(row) for row in self.timings]
            rows.extend({'stage': item['name'], 'seconds': item['seconds']} for item in self.active)
            measured = sum(row['seconds'] for row in rows)
            rows.append({'stage': 'other command work', 'seconds': max(0.0, now - self.started - measured)})
            return rows


def command_started():
    try:
        return float(os.environ["ASTRID_CLI_START_MONO"])
    except (KeyError, ValueError):
        return time.monotonic()


_current = ContextVar('astrid_command_timings', default=None)
_parent = ContextVar('astrid_timing_parent', default=None)


@contextmanager
def timing_scope(collector):
    token = _current.set(collector)
    try:
        yield collector
    finally:
        _current.reset(token)
        collector.close()


def current_timings():
    collector = _current.get()
    return collector.snapshot() if collector is not None else []


class TimingOutput:
    """Enrich complete JSON objects, optionally deferring successful output."""
    def __init__(self, stream, collector, *, defer_success=False):
        self.stream, self.collector = stream, collector
        self.defer_success = defer_success
        self.pending = None

    def _write_pending(self):
        if self.pending is None:
            return
        value, terminator = self.pending
        self.pending = None
        value['timings'] = self.collector.snapshot()
        self.stream.write(json.dumps(value, ensure_ascii=False) + terminator)

    def write(self, text):
        if self.pending is not None:
            if not text.strip('\r\n'):
                value, terminator = self.pending
                self.pending = (value, terminator + text)
                return len(text)
            self._write_pending()
        try:
            value = json.loads(text)
        except (ValueError, TypeError):
            return self.stream.write(text)
        if isinstance(value, dict):
            if self.defer_success and value.get('ok') is True:
                self.pending = (value, '')
                return len(text)
            value['timings'] = self.collector.snapshot()
            text = json.dumps(value, ensure_ascii=False)
        return self.stream.write(text)

    def flush(self):
        self._write_pending()
        return self.stream.flush()

    def finalize(self):
        """Write a deferred successful result at the end of command dispatch."""
        self._write_pending()

    def __getattr__(self, name):
        return getattr(self.stream, name)


@contextmanager
def stage(name, *, report_without_collector=False):
    collector = _current.get()
    if collector is None:
        if report_without_collector:
            with timing_scope(TimingCollector()), stage(name):
                yield
        else:
            yield
        return
    item = {'name': name, 'started': time.monotonic(), 'seconds': 0.0,
            'reported': False, 'parent': _parent.get()}
    with collector.changed:
        collector._advance(item['started'])
        collector.active.append(item)
        if collector.monitor is None:
            collector.monitor = threading.Thread(target=collector._watch, daemon=True)
            collector.monitor.start()
        collector.changed.notify_all()
    token = _parent.set(item)
    try:
        yield
    finally:
        _parent.reset(token)
        with collector.changed:
            collector._advance(time.monotonic())
            collector.active.remove(item)
            collector.timings.append({'stage': name, 'seconds': item['seconds']})
            collector.changed.notify_all()


def timed_call(name, function, *args, **kwargs):
    with stage(name):
        return function(*args, **kwargs)
