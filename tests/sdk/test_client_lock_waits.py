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


def test_bootstrap_wait_without_collector(tmp_path, monkeypatch, capsys):
    from astrid.sdk import autobootstrap

    monkeypatch.setattr(lock_wait, "LOCK_TIMEOUT", 2.3)
    monkeypatch.setattr(autobootstrap, "_manifest_from_environment", lambda: None)
    monkeypatch.setattr(autobootstrap, "_launcher_command", lambda: ["fake-launcher"])
    ready, release = threading.Event(), threading.Event()

    def hold():
        with _ACQUISITION_LOCK:
            ready.set()
            release.wait(15)

    holder = threading.Thread(target=hold)
    holder.start()
    try:
        assert ready.wait(5)
        with pytest.raises(autobootstrap.AutoBootstrapError, match="in-process Runtime acquisition lock timed out"):
            autobootstrap.ensure_runtime(data_root=tmp_path, start_pack_host=False)
        assert capsys.readouterr().err.count("Waiting for in-process Runtime acquisition lock...") == 1
        assert holder.is_alive()
        assert not _ACQUISITION_LOCK.acquire(blocking=False)
    finally:
        release.set()
        holder.join(5)
    assert not holder.is_alive()


def test_outbox_wait_without_collector(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(lock_wait, "LOCK_TIMEOUT", 2.3)
    path = tmp_path / "outbox.lock"
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    holder = ctx.Process(target=_hold_file, args=(path, ready, release))
    holder.start()
    try:
        assert ready.wait(5)
        with pytest.raises(TimeoutError, match="timeline outbox lock timed out"):
            with Outbox(tmp_path / "outbox").locked():
                pytest.fail("acquired another process's lock")
        assert capsys.readouterr().err.count("Waiting for timeline outbox lock...") == 1
        assert holder.is_alive()
    finally:
        release.set()
        holder.join(5)
        if holder.is_alive():
            holder.terminate()
            holder.join()
    assert holder.exitcode == 0


def test_file_lock_interruption_does_not_unlock(tmp_path, monkeypatch):
    attempts = []

    def interrupt(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise BlockingIOError
        raise KeyboardInterrupt

    unlocked = []
    monkeypatch.setattr(platform, "lock_file", interrupt)
    monkeypatch.setattr(platform, "unlock_file", lambda fd: unlocked.append(fd))
    with pytest.raises(KeyboardInterrupt):
        with Outbox(tmp_path / "outbox").locked():
            pytest.fail("interrupted lock was acquired")
    assert len(attempts) == 2
    assert unlocked == []


def test_host_wait_without_collector(tmp_path, monkeypatch, capsys):
    from pathlib import Path
    from types import SimpleNamespace
    from astrid.sdk import host_bootstrap
    from astrid.core.execution import generic_host
    from astrid.core.pack import source_setup

    monkeypatch.setattr(lock_wait, "LOCK_TIMEOUT", 2.3)
    source = tmp_path / "source"
    (source / "astrid" / "packs").mkdir(parents=True)
    support = tmp_path / "support"
    credential = support / "nested" / "worker.token"
    credential.parent.mkdir(parents=True)
    credential.touch(mode=0o600)
    original_read_text = Path.read_text
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **kw:
                        "synthetic" if self == credential else original_read_text(self, *a, **kw))
    monkeypatch.setattr(host_bootstrap.platform, "is_owner_only", lambda _: True)
    monkeypatch.setattr(source_setup, "active_source_inventory", lambda: SimpleNamespace(sources=(), identity=""))
    monkeypatch.setattr(generic_host, "source_checkout_digest", lambda _: "digest")
    monkeypatch.setattr(generic_host, "source_checkout_closure_digest", lambda _: "closure")
    monkeypatch.setattr(generic_host, "RuntimeProtocolClient", lambda *_: SimpleNamespace(
        health=lambda: {"status": "ok", "runtime_epoch": "1", "runtime_instance_id": "1"}))
    monkeypatch.setattr(host_bootstrap, "_await_supervised_startup", lambda *a: None)
    monkeypatch.setattr(host_bootstrap, "_execution_profile", lambda: "source")
    value = {"worker_credential_file": str(credential), "source_checkout": str(source),
             "worker_actor": host_bootstrap.PACK_HOST_ACTOR,
             "worker_scopes": host_bootstrap.PACK_HOST_SCOPES, "endpoint": "http://runtime.test"}
    path = support / "generic-host.lock"
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    holder = ctx.Process(target=_hold_file, args=(path, ready, release))
    holder.start()
    try:
        assert ready.wait(5)
        with pytest.raises(host_bootstrap.PackHostBootstrapError, match="pack host lifecycle lock timed out"):
            host_bootstrap.ensure_pack_host(value, reconfigure_action="retry")
        assert capsys.readouterr().err.count("Waiting for pack host lifecycle lock...") == 1
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


def test_outbox_cooperative_cancellation_keeps_process_holder(tmp_path):
    from runtime_protocol import cancellation

    path = tmp_path / "outbox.lock"
    ctx = multiprocessing.get_context("spawn")
    ready, release = ctx.Event(), ctx.Event()
    holder = ctx.Process(target=_hold_file, args=(path, ready, release))
    holder.start()
    timer = None
    try:
        assert ready.wait(5)
        stop = threading.Event()
        started = time.monotonic()
        with cancellation.startup_scope(stop):
            timer = threading.Timer(0.2, stop.set)
            timer.start()
            with pytest.raises(cancellation.StartupCancelled):
                with Outbox(tmp_path / "outbox").locked():
                    pytest.fail("acquired another process's lock")
        assert time.monotonic() - started < 2
        assert holder.is_alive()
        with path.open("a+") as probe:
            with pytest.raises(BlockingIOError):
                platform.lock_file(probe.fileno(), blocking=False)
    finally:
        if timer is not None:
            timer.cancel()
            timer.join()
        release.set()
        holder.join(5)
        if holder.is_alive():
            holder.terminate()
            holder.join()
    assert holder.exitcode == 0
