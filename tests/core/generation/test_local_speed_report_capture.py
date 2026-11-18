"""Meter telemetry must survive normal job cleanup without changing custody."""
from __future__ import annotations
import json
import shutil
import pytest
from tests._local_speed_capture import ProcessReportCapture

PREFIX = "PLAN55_PROCESS_OBSERVATION "

def line(pid=12):
    return (PREFIX + json.dumps({"pid": pid, "counts": {"workflow_compile": 2}}) + "\n").encode()

def observer(**kwargs):
    return ProcessReportCapture(PREFIX, max_report_bytes=256, **kwargs)

@pytest.mark.parametrize("cut", [1, 5, len(PREFIX), 42])
def test_report_split_at_any_boundary_is_retained(cut):
    capture = observer()
    capture.feed("child", b"ordinary log\n" + line()[:cut])
    capture.feed("child", line()[cut:])
    texts, gaps = capture.snapshot()
    assert gaps == [] and texts == [line().decode()]


def test_interleaved_workers_are_not_spliced_together():
    capture = observer()
    capture.feed("one", line(1)[:8])
    capture.feed("two", line(2)[:9])
    capture.feed("one", line(1)[8:])
    capture.feed("two", line(2)[9:])
    assert capture.snapshot() == ([line(1).decode(), line(2).decode()], [])


def test_unrelated_very_large_logs_are_discarded_and_next_report_survives():
    capture = observer()
    capture.feed("one", b"unrelated " + b"x" * 1000000)
    capture.feed("one", b"\n" + line())
    assert capture.snapshot() == ([line().decode()], [])
    assert len(capture._streams["one"].pending) <= 256


def test_oversized_telemetry_is_a_gap_not_silent_success():
    capture = observer()
    capture.feed("one", PREFIX.encode() + b"x" * 10000 + b"\n" + line())
    texts, gaps = capture.snapshot()
    assert texts == [line().decode()]
    assert gaps == ["process telemetry line exceeded its byte bound"]


def test_truncated_telemetry_is_a_gap():
    capture = observer()
    capture.feed("one", line()[:-1])
    texts, gaps = capture.snapshot()
    assert not texts and gaps == ["process telemetry line was truncated before newline"]


def test_report_limit_is_a_gap():
    capture = observer(max_reports=1)
    capture.feed("one", line(1) + line(2))
    texts, gaps = capture.snapshot()
    assert texts == [line(1).decode()]
    assert gaps == ["process telemetry report count exceeded its bound"]


def test_invalid_utf8_is_a_gap():
    capture = observer()
    capture.feed("one", PREFIX.encode() + b"\xff\n")
    assert capture.snapshot() == ([], ["process telemetry was not valid UTF-8"])


def test_actual_host_drain_is_unchanged_and_cleanup_does_not_lose_report(tmp_path, monkeypatch):
    from astrid.core.execution.generic_host import _TaskOutputCapture
    attempt = tmp_path / "attempt"
    logs = attempt / ".host-logs"
    logs.mkdir(parents=True)
    capture = observer().install(monkeypatch)
    drain = _TaskOutputCapture(attempt, {}, lambda text, _secrets: text, logs)
    drain._receive("stderr", line()[:6])
    drain._receive("stderr", line()[6:])
    drain._receive("stdout", b"unchanged stdout\n")
    assert (logs / "job.stderr").read_bytes() == line()
    assert (logs / "job.stdout").read_bytes() == b"unchanged stdout\n"
    shutil.rmtree(attempt)
    assert capture.snapshot() == ([line().decode()], [])


def test_original_drain_failure_is_not_hidden(monkeypatch):
    class Drain:
        def _receive(self, name, chunk):
            raise OSError("original drain failure")
    capture = observer().install(monkeypatch, Drain)
    with pytest.raises(OSError, match="original drain failure"):
        Drain()._receive("stderr", line())
    assert capture.snapshot() == ([], [])


def test_stdout_is_not_accepted_as_worker_telemetry(monkeypatch):
    class Drain:
        def _receive(self, name, chunk):
            return "kept"
    capture = observer().install(monkeypatch, Drain)
    assert Drain()._receive("stdout", line()) == "kept"
    assert capture.snapshot() == ([], [])


def test_stream_limit_is_a_gap_without_unbounded_state():
    capture = observer(max_reports=1)
    capture.feed("one", line(1))
    capture.feed("two", line(2))
    texts, gaps = capture.snapshot()
    assert texts == [line(1).decode()]
    assert gaps == ["process telemetry stream count exceeded its bound"]
    assert len(capture._streams) == 1
