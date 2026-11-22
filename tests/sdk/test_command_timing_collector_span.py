"""Collector spans use the same clock reading as their timing rows."""
import io
import json

from astrid.sdk import command_timing


def test_repeated_output_snapshots_report_independent_collector_span(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(command_timing.time, "monotonic", lambda: clock[0])
    collector = command_timing.TimingCollector(started=10.0)
    stream = io.StringIO()
    output = command_timing.TimingOutput(stream, collector, include_collector_span=True)
    for now in (12.5, 14.0):
        clock[0] = now
        output.write('{"ok": true}')
        stream.write("\n")
    first, second = (json.loads(line) for line in stream.getvalue().splitlines())
    assert first["collector_span_seconds"] == 2.5
    assert second["collector_span_seconds"] == 4.0
    assert first["timings"][-1]["seconds"] == 2.5
    assert second["timings"][-1]["seconds"] == 4.0


def test_collector_span_is_opt_in(monkeypatch):
    monkeypatch.setattr(command_timing.time, "monotonic", lambda: 5.0)
    stream = io.StringIO()
    output = command_timing.TimingOutput(stream, command_timing.TimingCollector())
    output.write('{"ok": true}')
    assert "collector_span_seconds" not in json.loads(stream.getvalue())
