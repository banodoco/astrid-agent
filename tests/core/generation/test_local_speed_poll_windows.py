"""Observe actual poll windows and sleep requests without changing operations."""

import subprocess
import sys
import time

import pytest

from tests.core.generation.test_local_speed_process_observer import observer


def test_sleep_requests_preserve_aliases_errors_and_the_original_function():
    original = time.sleep
    meter = observer()
    meter.start()
    try:
        original(0)  # An existing C-function alias is counted, its argument unknown.
        time.sleep(0.001)
        with pytest.raises(ValueError):
            time.sleep(-0.001)
        row = meter.snapshot()
    finally:
        meter.stop()
    assert time.sleep is original
    assert row["counts"]["sleep_attempt"] == 3
    assert row["counts"]["sleep_success"] == 2
    assert row["counts"]["sleep_error"] == 1
    site = "sleep_attempt|" + __name__ + ":" + test_sleep_requests_preserve_aliases_errors_and_the_original_function.__qualname__
    assert row["sleep_requests"][site] == {
        "calls": 3, "observed_calls": 1, "unknown_calls": 2,
        "minimum_ns": 1_000_000, "maximum_ns": 1_000_000,
    }


def test_real_child_poll_window_reconciles_sleeps_and_does_not_borrow_a_late_result():
    original = subprocess.Popen.poll
    meter = observer()
    command = [sys.executable, "-c", "import sys,time; sys.stdin.buffer.read(1); time.sleep(0.02)"]
    with subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) as process:
        meter.start()
        begin = time.perf_counter_ns()
        try:
            released = False
            while process.poll() is None:
                if not released:
                    process.stdin.write(b"x")
                    process.stdin.flush()
                    released = True
                time.sleep(0.001)
            end = time.perf_counter_ns()
            row = meter.snapshot(begin, end)
            window, = [item for item in row["poll_windows"] if item["pid"] == process.pid]
            clipped = meter.snapshot(begin, window["poll_end_ns"] - 1)
        finally:
            meter.stop()
        assert process.returncode == 0
        assert process.stderr.read() == b""
    assert subprocess.Popen.poll is original
    assert window["poll_calls"] == window["nonterminal_polls"] + 1
    assert window["nonterminal_polls"] == window["sleep_calls"] == row["counts"]["sleep_attempt"]
    assert window["sleep_calls"] >= 1
    assert window["terminal_polls"] == 1
    assert window["missing_results"] == 0
    assert window["carry_in"] is False and window["carry_out"] is False
    assert window["ambiguous_children"] is False
    assert begin <= window["poll_start_ns"] < window["poll_end_ns"] <= end
    assert window["duration_ns"] == window["poll_end_ns"] - window["poll_start_ns"]
    requests, = window["sleep_requests"].values()
    assert requests == {"calls": window["sleep_calls"], "observed_calls": window["sleep_calls"],
                        "unknown_calls": 0, "minimum_ns": 1_000_000, "maximum_ns": 1_000_000}
    short, = [item for item in clipped["poll_windows"] if item["pid"] == process.pid]
    assert short["carry_out"] is True
    assert short["terminal_polls"] == 0
    assert short["missing_results"] == 1


def test_poll_failure_is_forwarded_and_remains_an_incomplete_window():
    class PollFailure(RuntimeError):
        pass

    class FailingProcess:
        def __init__(self):
            self.pid = 123

        def _internal_poll(self, *args, **kwargs):
            raise PollFailure("original poll failure")

    original = subprocess.Popen.poll
    meter = observer()
    meter.start()
    begin = time.perf_counter_ns()
    try:
        with pytest.raises(PollFailure, match="original poll failure"):
            subprocess.Popen.poll(FailingProcess())
        row = meter.snapshot(begin, time.perf_counter_ns())
    finally:
        meter.stop()
    assert subprocess.Popen.poll is original
    window, = row["poll_windows"]
    assert window["poll_calls"] == 1
    assert window["nonterminal_polls"] == window["terminal_polls"] == 0
    assert window["missing_results"] == 1
    assert window["poll_end_ns"] is None
    assert window["carry_out"] is True
