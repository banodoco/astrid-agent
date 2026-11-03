"""The stopped-owner observation never reads the lock or holds it into upgrade."""

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.installer.backend import Backend
from runtime_protocol import platform


def test_owner_wait_retries_without_reading_and_releases(tmp_path, monkeypatch):
    lock = tmp_path / "owner.lock"
    lock.write_bytes(b"locked bytes")
    descriptor = os.open(lock, os.O_RDWR)
    platform.lock_file(descriptor, blocking=False)
    sleeps = []

    def release(_delay):
        sleeps.append(True)
        platform.unlock_file(descriptor)

    monkeypatch.setattr("astrid.installer.backend.time.sleep", release)
    monkeypatch.setattr(os, "read", lambda *_: pytest.fail("owner lock was read"))
    try:
        Backend.wait_for_realm_owner(SimpleNamespace(), {"realm_root": str(tmp_path)})
        assert sleeps == [True]
        platform.lock_file(descriptor, blocking=False)
        platform.unlock_file(descriptor)
        assert lock.read_bytes() == b"locked bytes"
    finally:
        os.close(descriptor)


def test_owner_wait_timeout_leaves_lock_and_bytes_untouched(tmp_path):
    lock = tmp_path / "owner.lock"
    lock.write_bytes(b"retained")
    descriptor = os.open(lock, os.O_RDWR)
    platform.lock_file(descriptor, blocking=False)
    try:
        with pytest.raises(TimeoutError, match="did not finish stopping"):
            Backend.wait_for_realm_owner(SimpleNamespace(), {"realm_root": str(tmp_path)}, timeout=0)
        assert lock.read_bytes() == b"retained"
    finally:
        platform.unlock_file(descriptor)
        os.close(descriptor)


def test_owner_wait_refuses_missing_and_linked_lock(tmp_path):
    lock = tmp_path / "owner.lock"
    with pytest.raises(OSError):
        Backend.wait_for_realm_owner(SimpleNamespace(), {"realm_root": str(tmp_path)})
    assert not lock.exists()
    target = tmp_path / "target"
    target.write_bytes(b"original")
    try:
        lock.symlink_to(target)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises((OSError, ValueError)):
        Backend.wait_for_realm_owner(SimpleNamespace(), {"realm_root": str(tmp_path)})
    assert target.read_bytes() == b"original"


def test_update_waits_after_stop_before_backup_and_upgrade(tmp_path, monkeypatch):
    from astrid.installer import core

    root, data = tmp_path / "program", tmp_path / "data"
    root.mkdir()
    old = core.Version(root / "versions/old", root / "old-python", root / "old-profile", {})
    candidate = core.Version(root / "versions/new", root / "new-python", root / "new-profile", {})
    events = []
    backend = SimpleNamespace(
        wait_for_realm_owner=lambda _realm: events.append("wait"),
        finish=lambda *_: events.append("finish"),
    )
    monkeypatch.setattr(core, "install_runtime", lambda *_: None)
    monkeypatch.setattr(core, "install_launcher", lambda *_: None)
    installer = core.Installer(root, data, backend)
    installer._run_update_transaction(
        old, {"realm_id": "r0", "realm_root": str(data / "realm")},
        prepare=lambda: candidate,
        stop=lambda *_: events.append("stop"),
        backup=lambda *_: events.append("backup") or {"root": str(root / "backups/b")},
        migrate=lambda *_: events.append("upgrade"),
        switch_candidate=lambda *_: events.append("switch"),
        switch_old=lambda *_: events.append("switch-old"),
        activate=lambda *_: events.append("activate"),
        activate_old=lambda *_: events.append("activate-old"),
    )
    assert events == ["stop", "wait", "backup", "upgrade", "switch", "activate", "finish"]


def test_owner_wait_failure_runs_existing_rollback_before_backup(tmp_path, monkeypatch):
    from astrid.installer import core

    root, data = tmp_path / "program", tmp_path / "data"
    root.mkdir()
    old = core.Version(root / "versions/old", root / "old-python", root / "old-profile", {})
    candidate = core.Version(root / "versions/new", root / "new-python", root / "new-profile", {})
    events = []

    def wait(_realm):
        events.append("wait")
        raise TimeoutError("owner busy")

    monkeypatch.setattr(core, "install_runtime", lambda *_: None)
    installer = core.Installer(root, data, SimpleNamespace(wait_for_realm_owner=wait))
    with pytest.raises(TimeoutError, match="owner busy"):
        installer._run_update_transaction(
            old, {"realm_id": "r0", "realm_root": str(data / "realm")},
            prepare=lambda: candidate,
            stop=lambda *_: events.append("stop"),
            backup=lambda *_: pytest.fail("backup ran after owner wait failure"),
            migrate=lambda *_: pytest.fail("upgrade ran after owner wait failure"),
            switch_candidate=lambda *_: pytest.fail("selected candidate after owner wait failure"),
            switch_old=lambda *_: events.append("switch-old"),
            activate=lambda *_: pytest.fail("activated candidate after owner wait failure"),
            activate_old=lambda *_: events.append("activate-old"),
        )
    assert events == ["stop", "wait", "stop", "switch-old", "activate-old"]
    assert core.read_json(root / "update-transaction.json")["step"] == "rolled_back"
