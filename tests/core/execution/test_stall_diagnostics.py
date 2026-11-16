"""Task stall timer coordination and native faulthandler routing."""

from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
import sys

import pytest

from astrid.core.execution.stall_diagnostics import TaskStallDiagnostics


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class TimerSpy:
    def __init__(self) -> None:
        self.arms: list[tuple[float, dict]] = []
        self.cancels = 0

    def arm(self, delay: float, **kwargs) -> None:
        self.arms.append((delay, kwargs))

    def cancel(self) -> None:
        self.cancels += 1


class CancelFailsOnce(TimerSpy):
    def cancel(self) -> None:
        super().cancel()
        if self.cancels == 1:
            raise OSError("synthetic timer cancellation failure")


def _diagnostics(clock: Clock, timers: TimerSpy, *, timeout: float = 10.0, fd: int | None = None):
    return TaskStallDiagnostics(timeout=timeout, clock=clock, timer=timers.arm,
                                cancel=timers.cancel, fd=fd)


def test_heartbeat_or_identical_progress_does_not_reset_stall_deadline():
    clock, timers = Clock(), TimerSpy()
    diagnostics = _diagnostics(clock, timers)
    diagnostics.begin("task-a", {"phase": "importing", "percent": 10})
    assert len(timers.arms) == 1 and timers.arms[0][0] == 10

    clock.advance(4)
    diagnostics.update("task-a", {"phase": "importing", "percent": 10})
    assert len(timers.arms) == 1 and timers.cancels == 0

    clock.advance(2)
    diagnostics.update("task-a", {"phase": "loading", "percent": 10})
    assert len(timers.arms) == 2 and timers.cancels == 1
    assert timers.arms[1][0] == 10
    diagnostics.end("task-a")
    assert timers.cancels == 2


def test_later_task_progress_or_completion_cannot_cancel_overdue_owner():
    clock, timers = Clock(), TimerSpy()
    read_fd, write_fd = os.pipe()
    diagnostics = _diagnostics(clock, timers, fd=write_fd)
    try:
        diagnostics.begin("task-a", "phase-a")
        clock.advance(2)
        diagnostics.begin("task-b", "phase-b")
        assert diagnostics._owner == "task-a"
        assert len(timers.arms) == 1
        dump_file = timers.arms[0][1]["file"]
        duplicated_fd = dump_file.fileno()

        # A's original deadline is now due. B's activity and completion leave
        # the already armed all-thread snapshot owned by A intact.
        clock.advance(8)
        diagnostics.update("task-b", "phase-b-progress")
        diagnostics.end("task-b")
        assert diagnostics._owner == "task-a"
        assert len(timers.arms) == 1 and timers.cancels == 0
        assert not dump_file.closed
        os.fstat(duplicated_fd)

        diagnostics.update("task-a", "phase-a-progress")
        assert diagnostics._owner is None
        assert len(timers.arms) == 1 and timers.cancels == 1
        assert dump_file.closed
        with pytest.raises(OSError):
            os.fstat(duplicated_fd)
    finally:
        diagnostics.end("task-a")
        os.close(read_fd)
        os.close(write_fd)


def test_overdue_task_is_diagnosed_once_even_after_later_phase_changes():
    clock, timers = Clock(), TimerSpy()
    diagnostics = _diagnostics(clock, timers)
    diagnostics.begin("task-a", "phase-a")
    clock.advance(10)
    diagnostics.update("task-a", "phase-b")
    assert diagnostics._tasks["task-a"].diagnosed is True
    assert diagnostics._owner is None

    clock.advance(20)
    diagnostics.update("task-a", "phase-c")
    assert len(timers.arms) == 1
    assert timers.cancels == 1
    diagnostics.end("task-a")


def test_last_task_end_closes_duplicated_stderr_sink():
    clock, timers = Clock(), TimerSpy()
    read_fd, write_fd = os.pipe()
    diagnostics = _diagnostics(clock, timers, fd=write_fd)
    try:
        diagnostics.begin("task-a", "phase-a")
        dump_file = timers.arms[0][1]["file"]
        duplicate_fd = dump_file.fileno()
        assert not dump_file.closed
        diagnostics.end("task-a")
        assert dump_file.closed
        assert timers.cancels == 1
        with pytest.raises(OSError):
            os.fstat(duplicate_fd)
    finally:
        os.close(read_fd)
        os.close(write_fd)


def test_update_after_end_does_not_resurrect_task_or_rearm_timer():
    clock, timers = Clock(), TimerSpy()
    diagnostics = _diagnostics(clock, timers)
    diagnostics.begin("task-a", "phase-a")
    diagnostics.end("task-a")
    assert timers.cancels == 1
    assert diagnostics._file is None

    diagnostics.update("task-a", "late-progress-after-end")
    assert diagnostics._tasks == {}
    assert len(timers.arms) == 1
    assert timers.cancels == 1
    assert diagnostics._file is None


def test_cancel_failure_keeps_old_timer_sink_until_later_cancel_succeeds():
    clock, timers = Clock(), CancelFailsOnce()
    diagnostics = _diagnostics(clock, timers)
    diagnostics.begin("task-a", "phase-a")
    dump_file = timers.arms[0][1]["file"]

    # The progress change requires replacing the active timer. If cancellation
    # fails, retain that timer generation and fd instead of rearming over it.
    clock.advance(1)
    diagnostics.update("task-a", "phase-b")
    assert len(timers.arms) == 1
    assert timers.cancels == 1
    assert not dump_file.closed

    diagnostics.end("task-a")
    assert timers.cancels == 2
    assert dump_file.closed
    assert len(timers.arms) == 1


def test_real_faulthandler_timer_dumps_all_threads_to_existing_stderr(tmp_path):
    root = Path(__file__).resolve().parents[3]
    script = r'''
import ctypes
import os
import threading
from astrid.core.execution.stall_diagnostics import TaskStallDiagnostics

ready = threading.Event()
release = threading.Event()

def stall_marker_worker():
    ready.set()
    release.wait(3)

worker = threading.Thread(target=stall_marker_worker, name="stall-marker-worker")
worker.start()
assert ready.wait(1)
diagnostics = TaskStallDiagnostics(timeout=0.2, fd=2)
diagnostics.begin("held-gil-task", {"phase": "importing numpy"})

def hold_gil_in_native_call():
    if os.name == "nt":
        sleep = ctypes.PyDLL("kernel32").Sleep
        sleep.argtypes = [ctypes.c_ulong]
        sleep.restype = None
        sleep(700)
    else:
        sleep = ctypes.PyDLL(None).usleep
        sleep.argtypes = [ctypes.c_uint]
        sleep.restype = ctypes.c_int
        sleep(700_000)

hold_gil_in_native_call()
diagnostics.end("held-gil-task")
release.set()
worker.join(1)
'''
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(root), existing)))
    log_path = tmp_path / "host-stderr.log"
    with log_path.open("wb") as sink:
        completed = subprocess.run(
            [sys.executable, "-c", script], cwd=root, env=env,
            stdout=subprocess.PIPE, stderr=sink, timeout=8, check=False,
        )
    contents = log_path.read_text(encoding="utf-8", errors="replace")
    assert completed.returncode == 0, completed.stdout.decode("utf-8", "replace") + contents
    assert re.search(r"Timeout \(0:00:00\.\d{6}\)!", contents)
    assert contents.count("Timeout (") == 1
    assert "stall_marker_worker" in contents
    assert "hold_gil_in_native_call" in contents
