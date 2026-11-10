"""Recoverable Windows whole-collection publication (on a real local filesystem)."""
from __future__ import annotations

import argparse
import errno
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.core.pack import cli_manage, install as installer, transaction as txn
from astrid.pack_api import VERSION


@pytest.fixture
def source(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "pack.yaml").write_text(
        'schema_version: 3\nid: hello_ext\nname: Hello\nversion: 1.0.0\n'
        f'pack_api: "{VERSION[0]}.{VERSION[1]}"\n'
        'documentation: {kind: none, reason: Windows transaction fixture.}\n')
    (source / "payload.txt").write_text("new")
    return source


@pytest.fixture
def windows(monkeypatch):
    windows_os = SimpleNamespace(**vars(os))
    windows_os.name = "nt"
    if hasattr(windows_os, "O_DIRECTORY"):
        del windows_os.O_DIRECTORY

    def replace(source, destination):
        if Path(destination).is_dir():
            raise PermissionError(errno.EACCES, "Windows cannot replace an existing directory")
        os.replace(source, destination)

    windows_os.replace = replace
    monkeypatch.setattr(txn, "sys", SimpleNamespace(platform="win32", executable=sys.executable))
    monkeypatch.setattr(txn, "os", windows_os)
    monkeypatch.setattr(installer, "os", windows_os)
    publish = installer._publish
    policy = installer.PublicationPolicy.for_platform("win32")
    monkeypatch.setattr(installer, "_publish", lambda stage, dest, **k: publish(stage, dest, publication=policy))
    # A Linux host must not accidentally supply its atomic exchange to this test.
    monkeypatch.setattr(txn, "_publish", lambda *a, **k: pytest.fail("POSIX exchange used on Windows"))
    return windows_os


def install(source, root, **kwargs):
    return txn.install_group(source=source, packs={"hello_ext": "."},
                             packs_root=root, python_packages={}, **kwargs)


def seed(root):
    root.mkdir()
    (root / "personal.txt").write_text("complete old collection")


def assert_resolved(root, *, forward):
    assert (root / "personal.txt").read_text() == "complete old collection"
    assert (root / "hello_ext").exists() is forward
    assert not txn._exchange_backup(root).exists()
    assert not txn._exchange_journal(root).exists()
    retired = root.parent / ".astrid-retired"
    generations = list(retired.iterdir())
    if forward:
        state = txn.read_group(root)
        assert state.revision == 1
        assert (retired / state.transaction_id / "personal.txt").read_text() == "complete old collection"
        assert (root / "hello_ext/payload.txt").read_text() == "new"
    else:
        assert generations == []
        assert txn.read_group(root).revision == 0


def test_windows_packs_add_over_existing_collection(source, tmp_path, windows, monkeypatch, capsys):
    root = tmp_path / "packs"
    seed(root)
    monkeypatch.setattr(cli_manage, "_build_inputs", lambda *a: ({}, None))
    monkeypatch.setattr(cli_manage, "_validation_snapshot", lambda: ({}, {}))
    monkeypatch.setattr(cli_manage, "_composition_roots", lambda *a, **k: ())
    args = argparse.Namespace(pack_id="hello_ext", subpath=".", source=str(source),
                              revision=None, packs_root=str(root), toolchain=None, yes=True)
    assert cli_manage._handle_add(args) == 0
    assert "Installed hello_ext" in capsys.readouterr().out
    assert_resolved(root, forward=True)


class Crash(BaseException):
    pass


@pytest.mark.parametrize("phase", ["prepared", "old_moved", "published", "retired"])
@pytest.mark.parametrize("timing", ["before", "after"])
@pytest.mark.parametrize("restart", ["transaction", "host", "discovery"])
def test_windows_journal_step_crash_recovers(source, tmp_path, windows, monkeypatch, phase, timing, restart):
    root = tmp_path / "packs"
    seed(root)
    write = txn._write_exchange

    def crash(root, record, current):
        if current == phase and timing == "before":
            raise Crash()
        write(root, record, current)
        if current == phase and timing == "after":
            raise Crash()

    monkeypatch.setattr(txn, "_write_exchange", crash)
    with pytest.raises(Crash):
        install(source, root)
    monkeypatch.setattr(txn, "_write_exchange", write)
    forward = phase in {"published", "retired"}
    if restart == "host":
        # This is the host's startup probe, followed by its existing recovery API.
        txn.refresh_pending(packs_root=root)
        txn.retry_refresh(packs_root=root, refresh=lambda **k: {
            "version": 1, "accepted": True, "revision": k["committed_revision"], "reason": "ok"})
    elif restart == "discovery":
        # The host may discover packs before probing the refresh outbox. Its
        # first reader upgrades to exclusive custody and recovers before IO.
        with txn.group_read_lock(root):
            assert (root / "personal.txt").is_file()
        txn.read_group(root)
    else:
        # Every next transaction takes this lock before reading or creating root.
        with txn.group_read_lock(root, exclusive=True):
            pass
    assert_resolved(root, forward=forward)
    # Recovery is idempotent, and a subsequent real mutation succeeds.
    with txn.group_read_lock(root, exclusive=True):
        pass
    install(source, root)
    assert (root / "hello_ext/payload.txt").read_text() == "new"


@pytest.mark.parametrize("move", [1, 2, 3])
def test_windows_crash_between_rename_and_journal_recovers(source, tmp_path, windows, monkeypatch, move):
    root = tmp_path / "packs"
    seed(root)
    replace = windows.replace
    count = 0

    def crash(src, dest):
        nonlocal count
        directory = Path(src).is_dir()
        replace(src, dest)
        if directory and (Path(src) == root or Path(dest) == root or Path(src) == txn._exchange_backup(root)):
            count += 1
            if count == move:
                raise Crash()

    monkeypatch.setattr(windows, "replace", crash)
    with pytest.raises(Crash):
        install(source, root)
    monkeypatch.setattr(windows, "replace", replace)
    txn.refresh_pending(packs_root=root)
    assert_resolved(root, forward=move >= 2)


def test_windows_lock_covers_renames_journal_and_backup_retirement(source, tmp_path, windows, monkeypatch):
    fcntl = pytest.importorskip("fcntl")

    root = tmp_path / "packs"
    seed(root)
    lock = root.parent / ".packs.transaction.lock"
    seen = []
    replace, write, unlink = windows.replace, txn._write_exchange, Path.unlink

    def check():
        # Probe a distinct descriptor, not just the context variable.
        with lock.open("rb") as stream:
            with pytest.raises(BlockingIOError):
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        # A nested reader on the publishing thread must also refuse the gap.
        if txn._exchange_journal(root).exists():
            with pytest.raises(installer.PackInstallError, match="retry"):
                txn.read_group(root)

    def observed_replace(src, dest):
        if Path(src).is_dir():
            check()
            seen.append((Path(src), Path(dest)))
        replace(src, dest)

    def observed_write(root, record, phase):
        check()
        write(root, record, phase)
        check()

    def observed_unlink(path, *args, **kwargs):
        if path == txn._exchange_journal(root):
            check()
        unlink(path, *args, **kwargs)
        if path == txn._exchange_journal(root):
            check()

    monkeypatch.setattr(Path, "unlink", observed_unlink)
    monkeypatch.setattr(windows, "replace", observed_replace)
    monkeypatch.setattr(txn, "_write_exchange", observed_write)
    install(source, root)
    assert len(seen) == 4  # one staged pack publication plus three collection renames
    with lock.open("rb") as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)


@pytest.mark.parametrize("platform", ["linux", "darwin"])
def test_posix_collection_still_uses_existing_atomic_exchange(tmp_path, monkeypatch, platform):
    root, candidate = tmp_path / "packs", tmp_path / "candidate"
    root.mkdir()
    candidate.mkdir()
    calls = []
    monkeypatch.setattr(txn, "sys", SimpleNamespace(platform=platform))
    monkeypatch.setattr(txn, "_publish", lambda *args: calls.append(args))
    monkeypatch.setattr(txn, "_write_exchange", lambda *a: pytest.fail("Windows journal used on POSIX"))
    txn._publish_collection(candidate, root)
    assert calls == [(candidate, root)]
    assert not txn._exchange_journal(root).exists()


@pytest.mark.parametrize("phase", ["prepared", "old_moved", "published", "retired"])
def test_windows_process_death_recovers_on_host_start(source, tmp_path, windows, phase):
    root = tmp_path / "packs"
    seed(root)
    # os._exit skips every finally block: the journal and actual on-disk names
    # are the only information the next host has, as after process termination.
    script = '''
import os, sys
from pathlib import Path
from types import SimpleNamespace
from astrid.core.pack import transaction as txn, install as installer
source, root, phase = sys.argv[1:]
windows = SimpleNamespace(**vars(os))
windows.name = "nt"
def replace(src, dst):
    if Path(dst).is_dir():
        raise PermissionError("existing Windows directory")
    os.replace(src, dst)
windows.replace = replace
txn.os = installer.os = windows
txn.sys = SimpleNamespace(platform="win32", executable=sys.executable)
write = txn._write_exchange
def crash(root, record, current):
    write(root, record, current)
    if current == phase:
        os._exit(73)
txn._write_exchange = crash
txn.install_group(source=source, packs={"hello_ext": "."}, packs_root=root, python_packages={})
'''
    child = subprocess.run([sys.executable, "-c", script, str(source), str(root), phase],
                           capture_output=True, timeout=30)
    assert child.returncode == 73, child.stderr.decode()
    assert json.loads(txn._exchange_journal(root).read_text())["phase"] == phase
    txn.refresh_pending(packs_root=root)
    assert_resolved(root, forward=phase in {"published", "retired"})


def test_windows_concurrent_reader_waits_until_complete_new_collection(source, tmp_path, windows, monkeypatch):
    import threading
    from concurrent.futures import TimeoutError

    root = tmp_path / "packs"
    seed(root)
    assert txn.read_group(root).revision == 0
    write = txn._write_exchange
    started = threading.Event()
    futures = []

    def read():
        started.set()
        return txn.read_group(root)

    with ThreadPoolExecutor(max_workers=1) as pool:
        def observed_write(root, record, phase):
            write(root, record, phase)
            if phase == "old_moved":
                assert not root.exists()
                future = pool.submit(read)
                futures.append(future)
                assert started.wait(1)
                with pytest.raises(TimeoutError):
                    future.result(timeout=0.05)

        monkeypatch.setattr(txn, "_write_exchange", observed_write)
        result = install(source, root)
        observed = futures[0].result(timeout=2)
    assert observed.transaction_id == result.transaction_id
    assert set(observed.paths) == {"hello_ext"}


@pytest.mark.parametrize("forward", [False, True])
def test_windows_recovery_itself_can_be_interrupted(source, tmp_path, windows, monkeypatch, forward):
    root = tmp_path / "packs"
    seed(root)
    write = txn._write_exchange
    phase = "published" if forward else "old_moved"

    def crash(root, record, current):
        write(root, record, current)
        if current == phase:
            raise Crash()

    monkeypatch.setattr(txn, "_write_exchange", crash)
    with pytest.raises(Crash):
        install(source, root)
    monkeypatch.setattr(txn, "_write_exchange", write)
    replace = windows.replace

    def interrupt_recovery(src, dst):
        replace(src, dst)
        if Path(src) == txn._exchange_backup(root):
            raise Crash()

    monkeypatch.setattr(windows, "replace", interrupt_recovery)
    with pytest.raises(Crash):
        txn.refresh_pending(packs_root=root)
    monkeypatch.setattr(windows, "replace", replace)
    txn.refresh_pending(packs_root=root)
    assert_resolved(root, forward=forward)


def test_windows_rollback_keeps_retired_generation(source, tmp_path, windows):
    root = tmp_path / "packs"
    seed(root)
    install(source, root)
    (source / "payload.txt").write_text("updated")
    updated = install(source, root, intent="update")
    prior = txn.read_retired_group(packs_root=root, transaction_id=updated.transaction_id)
    assert (prior.paths["hello_ext"] / "payload.txt").read_text() == "new"
    restored = txn.rollback_group(packs_root=root, pack_ids=("hello_ext",),
                                  transaction_id=updated.transaction_id)
    assert restored.revision == updated.revision + 1
    assert (root / "hello_ext/payload.txt").read_text() == "new"
    assert not txn._exchange_backup(root).exists()
    assert not txn._exchange_journal(root).exists()


@pytest.mark.parametrize("timing", ["before", "after"])
def test_windows_crash_removing_finished_journal(source, tmp_path, windows, monkeypatch, timing):
    root = tmp_path / "packs"
    seed(root)
    unlink = Path.unlink
    journal = txn._exchange_journal(root)

    def crash(path, *args, **kwargs):
        if path == journal and timing == "before":
            raise Crash()
        unlink(path, *args, **kwargs)
        if path == journal and timing == "after":
            raise Crash()

    monkeypatch.setattr(Path, "unlink", crash)
    with pytest.raises(Crash):
        install(source, root)
    monkeypatch.setattr(Path, "unlink", unlink)
    txn.refresh_pending(packs_root=root)
    assert_resolved(root, forward=True)


def test_windows_reader_reports_retry_if_recovery_has_a_sharing_violation(source, tmp_path, windows, monkeypatch):
    root = tmp_path / "packs"
    seed(root)
    write = txn._write_exchange

    def crash(root, record, phase):
        write(root, record, phase)
        if phase == "old_moved":
            raise Crash()

    monkeypatch.setattr(txn, "_write_exchange", crash)
    with pytest.raises(Crash):
        install(source, root)
    monkeypatch.setattr(txn, "_write_exchange", write)
    replace = windows.replace

    def sharing_violation(src, dst):
        raise PermissionError(errno.EACCES, "sharing violation")

    monkeypatch.setattr(windows, "replace", sharing_violation)
    with pytest.raises(installer.PackInstallError, match="retry") as caught:
        txn.read_group(root)
    assert caught.value.code == "transaction_busy"
    assert not root.exists()
    assert txn._exchange_backup(root).is_dir()
    monkeypatch.setattr(windows, "replace", replace)
    txn.read_group(root)
    assert_resolved(root, forward=False)
