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
    def __init__(self, started=None, startup=None, startup_child=None):
        self.timings = []
        self.next_occurrence_id = 1
        if started is None:
            started = time.monotonic()
        self.started = min(started, time.monotonic())
        self.active = []
        self.lock = threading.Lock()
        self.changed = threading.Condition(self.lock)
        self.monitor = None
        self.closed = False
        self.last_change = time.monotonic()
        self.start_provenance = None
        if startup is not None:
            start, self.start_provenance, import_start, import_end, cli_entry = startup
            intervals = []
            if self.start_provenance == "process start" and import_start is not None:
                intervals.append(("interpreter start", start, import_start))
            if import_start is not None and import_end is not None:
                intervals.append(("gateway imports", import_start, import_end))
            dispatch_start = import_end if import_end is not None else start
            intervals.append(("command dispatch", dispatch_start, cli_entry))
            for name, begin, end in intervals:
                seconds = max(0.0, end - begin)
                self.timings.append({'stage': name, 'seconds': seconds, 'total_seconds': seconds,
                                     'occurrence_id': self.next_occurrence_id,
                                     'parent': None, 'parent_id': None})
                self.next_occurrence_id += 1
            if startup_child is not None:
                child_name, child_start, child_end = startup_child
                parent = next((row for row in self.timings if row['stage'] == "command dispatch"), None)
                if parent is not None and dispatch_start <= child_start <= child_end <= cli_entry:
                    child_seconds = child_end - child_start
                    parent['seconds'] = max(0.0, parent['seconds'] - child_seconds)
                    self.timings.append({
                        'stage': child_name,
                        'seconds': child_seconds,
                        'total_seconds': child_seconds,
                        'occurrence_id': self.next_occurrence_id,
                        'parent': parent['stage'],
                        'parent_id': parent['occurrence_id'],
                    })
                    self.next_occurrence_id += 1

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
        return self.snapshot_with_span()[0]

    def snapshot_with_span(self):
        """Return rows and collector span at one monotonic snapshot instant."""
        with self.lock:
            now = time.monotonic()
            self._advance(now)
            rows = [dict(row) for row in self.timings]
            rows.extend(_row(item, now) for item in self.active)
            measured = sum(row['seconds'] for row in rows)
            rows.append({'stage': 'other command work', 'seconds': max(0.0, now - self.started - measured)})
            return rows, max(0.0, now - self.started)


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
    parent_token = _parent.set(None)
    try:
        yield collector
    finally:
        _parent.reset(parent_token)
        _current.reset(token)
        collector.close()


def current_timings():
    collector = _current.get()
    return collector.snapshot() if collector is not None else []


class TimingOutput:
    """Enrich complete JSON objects, optionally deferring successful output."""
    def __init__(self, stream, collector, *, defer_success=False, include_collector_span=False):
        self.stream, self.collector = stream, collector
        self.defer_success = defer_success
        self.include_collector_span = include_collector_span
        self.pending = None

    def _write_pending(self):
        if self.pending is None:
            return
        value, terminator = self.pending
        self.pending = None
        self._attach_timings(value)
        self.stream.write(json.dumps(value, ensure_ascii=False) + terminator)

    def _attach_timings(self, value):
        rows, span = self.collector.snapshot_with_span()
        value['timings'] = rows
        if self.include_collector_span:
            value['collector_span_seconds'] = span

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
            self._attach_timings(value)
            if self.collector.start_provenance is not None:
                value['start'] = self.collector.start_provenance
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


def _row(item, end):
    parent = item['parent']
    row = {'stage': item['name'], 'seconds': item['seconds'],
           'total_seconds': end - item['started'], 'occurrence_id': item['id'],
           'parent': parent['name'] if parent is not None else None,
           'parent_id': parent['id'] if parent is not None else None}
    if item['detail'] is not None:
        row['detail'] = item['detail']
    return row


def print_timings(rows, *, file=None):
    """Print occurrences grouped only when their complete ancestry matches."""
    file = sys.stderr if file is None else file
    nodes = {}
    roots = []
    remainder = None
    for row in rows:
        if 'occurrence_id' not in row:
            remainder = row
        else:
            nodes[row['occurrence_id']] = (row, [])
    for row, children in nodes.values():
        parent = nodes.get(row['parent_id'])
        (parent[1] if parent else roots).append((row, children))

    def render(siblings, depth):
        groups = {}
        for row, children in siblings:
            key = (row['stage'], row.get('detail'))
            group = groups.setdefault(key, [0, 0.0, 0.0, []])
            group[0] += 1
            group[1] += row['seconds']
            group[2] += row['total_seconds']
            group[3].extend(children)
        for (name, detail), (count, seconds, total, children) in sorted(
                groups.items(), key=lambda item: -item[1][2]):
            label = name + (f" ({detail})" if detail is not None else '')
            label += f" ×{count}" if count > 1 else ''
            print(f"{'  ' * depth}{label}: {seconds:.3f}s (total {total:.3f}s)", file=file)
            render(children, depth + 1)

    render(roots, 0)
    if remainder is not None:
        wall = sum(row['seconds'] for row in rows)
        share = 100 * remainder['seconds'] / wall if wall else 0.0
        print(f"{remainder['stage']}: {remainder['seconds']:.3f}s ({share:.1f}%)", file=file)


@contextmanager
def stage(name, *, detail=None, report_without_collector=False):
    collector = _current.get()
    if collector is None:
        if report_without_collector:
            with timing_scope(TimingCollector()), stage(name, detail=detail):
                yield
        else:
            yield
        return
    with collector.changed:
        parent = _parent.get()
        if parent is not None and parent['collector'] is not collector:
            parent = None
        item = {'name': name, 'detail': detail, 'started': time.monotonic(), 'seconds': 0.0,
                'reported': False, 'parent': parent, 'collector': collector,
                'id': collector.next_occurrence_id}
        collector.next_occurrence_id += 1
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
            now = time.monotonic()
            collector._advance(now)
            collector.active[:] = [active for active in collector.active if active is not item]
            collector.timings.append(_row(item, now))
            collector.changed.notify_all()


def set_stage_detail(detail):
    """Attach an already-computed detail to the current occurrence, if any."""
    collector = _current.get()
    item = _parent.get()
    if collector is not None and item is not None and item['collector'] is collector:
        with collector.changed:
            item['detail'] = detail


def timed_call(name, function, *args, **kwargs):
    with stage(name):
        return function(*args, **kwargs)
