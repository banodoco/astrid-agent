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
