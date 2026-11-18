"""Retain test telemetry at the existing host pipe drain, before log cleanup.

No descriptor, environment, source admission or worker permission changes.
Only prefixed bounded telemetry lines are retained, never general child logs.
Install only in the counter run; this is not a production logging mechanism.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import threading
from typing import Any


@dataclass
class _LineState:
    pending: bytearray = field(default_factory=bytearray)
    discard: bool = False


class ProcessReportCapture:
    def __init__(self, prefix: str, *, max_report_bytes: int, max_reports: int = 128):
        if not prefix or "\n" in prefix or "\r" in prefix:
            raise ValueError("a nonempty single-line prefix is required")
        self.prefix = prefix.encode("ascii")
        if max_report_bytes <= len(self.prefix) or max_reports < 1:
            raise ValueError("positive telemetry bounds are required")
        self.max_report_bytes = max_report_bytes
        self.max_reports = max_reports
        self._streams: dict[Any, _LineState] = {}
        self._lines: list[str] = []
        self._gaps: set[str] = set()
        self._lock = threading.Lock()

    def install(self, monkeypatch, capture_type=None):
        if capture_type is None:
            from astrid.core.execution.generic_host import _TaskOutputCapture
            capture_type = _TaskOutputCapture
        original = capture_type._receive
        owner = self

        def receive(capture, name, chunk):
            # Preserve the actual drain/log/redaction path and its exceptions.
            result = original(capture, name, chunk)
            if name == "stderr":
                owner.feed(capture, chunk)
            return result

        monkeypatch.setattr(capture_type, "_receive", receive)
        return self

    def feed(self, stream_key: Any, chunk: bytes) -> None:
        with self._lock:
            # Retain the object identity so Python cannot recycle an id into
            # another child's stream while a partial line is buffered.
            if stream_key not in self._streams and len(self._streams) >= self.max_reports:
                self._gaps.add("process telemetry stream count exceeded its bound")
                return
            state = self._streams.setdefault(stream_key, _LineState())
            for offset, fragment in enumerate(chunk.split(b"\n")):
                if offset:
                    self._finish_line(state)
                if state.discard:
                    continue
                # Never allocate an unbounded line from unrelated child output.
                remaining = self.max_report_bytes + 1 - len(state.pending)
                state.pending.extend(fragment[:remaining])
                prefix_length = min(len(state.pending), len(self.prefix))
                if state.pending[:prefix_length] != self.prefix[:prefix_length]:
                    state.pending.clear()
                    state.discard = True
                elif len(state.pending) > self.max_report_bytes:
                    self._gaps.add("process telemetry line exceeded its byte bound")
                    state.pending.clear()
                    state.discard = True

    def _finish_line(self, state: _LineState) -> None:
        if not state.discard and state.pending.startswith(self.prefix):
            if len(self._lines) >= self.max_reports:
                self._gaps.add("process telemetry report count exceeded its bound")
            else:
                try:
                    self._lines.append(state.pending.decode("utf-8") + "\n")
                except UnicodeDecodeError:
                    self._gaps.add("process telemetry was not valid UTF-8")
        state.pending.clear()
        state.discard = False

    def snapshot(self) -> tuple[list[str], list[str]]:
        with self._lock:
            gaps = set(self._gaps)
            if any(not state.discard and state.pending for state in self._streams.values()):
                gaps.add("process telemetry line was truncated before newline")
            return list(self._lines), sorted(gaps)
