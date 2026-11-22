"""Client lock deadlines leave the current owner in control."""
import multiprocessing
import threading
import time

import pytest

from astrid.sdk import lock_wait
from astrid.sdk.autobootstrap import _ACQUISITION_LOCK
from astrid.sdk.command_timing import TimingCollector, timing_scope
from astrid.sdk.timeline.outbox import Outbox
from runtime_protocol import platform


def _hold_file(path, ready, release):
    with open(path, "a+") as handle:
        platform.lock_file(handle.fileno())
        ready.set()
        release.wait(15)
        platform.unlock_file(handle.fileno())


def test_acquisition_thread_timeout_keeps_holder(monkeypatch, capsys):
    monkeypatch.setattr(lock_wait, "LOCK_TIMEOUT", 2.3)
    ready, release = threading.Event(), threading.Event()

    def hold():
        with _ACQUISITION_LOCK:
            ready.set()
            release.wait(15)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert ready.wait(5)
        started = time.monotonic()
        with timing_scope(TimingCollector()):
            with pytest.raises(TimeoutError, match="in-process Runtime acquisition lock timed out"):
                with lock_wait.thread_lock(_ACQUISITION_LOCK, "in-process Runtime acquisition lock"):
                    pytest.fail("acquired another thread's lock")
        assert 2.2 <= time.monotonic() - started < 4
        assert capsys.readouterr().err.count("Waiting for in-process Runtime acquisition lock...") == 1
        assert holder.is_alive()
        assert not _ACQUISITION_LOCK.acquire(blocking=False)
    finally:
        release.set()
        holder.join(5)
    assert not holder.is_alive()
    with lock_wait.thread_lock(_ACQUISITION_LOCK, "in-process Runtime acquisition lock"):
        with lock_wait.thread_lock(_ACQUISITION_LOCK, "in-process Runtime acquisition lock"):
            pass


@pytest.mark.parametrize("name", ["pack host lifecycle lock", "timeline outbox lock"])
def test_process_lock_timeout_keeps_holder(tmp_path, monkeypatch, capsys, name):
    monkeypatch.setattr(lock_wait, "LOCK_TIMEOUT", 2.3)
    path = tmp_path / ("outbox.lock" if name == "timeline outbox lock" else "generic-host.lock")
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    holder = ctx.Process(target=_hold_file, args=(path, ready, release))
    holder.start()
    try:
        assert ready.wait(5)
        started = time.monotonic()
        with timing_scope(TimingCollector()):
            with pytest.raises(TimeoutError, match=f"{name} timed out"):
                if name == "timeline outbox lock":
                    with Outbox(tmp_path / "outbox").locked():
                        pytest.fail("acquired another process's lock")
                else:
                    with path.open("a+") as handle:
                        lock_wait.acquire_file_lock(handle.fileno(), name)
        assert 2.2 <= time.monotonic() - started < 4
        assert capsys.readouterr().err.count(f"Waiting for {name}...") == 1
        assert holder.is_alive()
        with path.open("a+") as probe:
            with pytest.raises(BlockingIOError):
                platform.lock_file(probe.fileno(), blocking=False)
    finally:
        release.set()
        holder.join(5)
        if holder.is_alive():
            holder.terminate()
            holder.join()
    assert holder.exitcode == 0
    with path.open("a+") as probe:
        platform.lock_file(probe.fileno(), blocking=False)
        platform.unlock_file(probe.fileno())
