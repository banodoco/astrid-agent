"""The pack custody lock must work without fcntl (Windows)."""

from __future__ import annotations

import os
import types

import pytest

from astrid.core.pack import _filelock
from astrid.core.pack.install import _install_lock
from astrid.core.pack.transaction import group_read_lock


class _FakeMsvcrt(types.SimpleNamespace):
    def __init__(self):
        super().__init__(LK_LOCK=1, LK_UNLCK=0, calls=[])

    def locking(self, fd, mode, nbytes):
        self.calls.append((mode, nbytes, os.lseek(fd, 0, os.SEEK_CUR)))


@pytest.fixture
def no_fcntl(monkeypatch):
    fake = _FakeMsvcrt()
    monkeypatch.setattr(_filelock, "_fcntl", None)
    monkeypatch.setattr(_filelock, "_msvcrt", fake)
    monkeypatch.setattr(_filelock.sys, "platform", "win32")
    return fake


def test_lock_helper_uses_msvcrt_when_fcntl_absent(no_fcntl, tmp_path):
    fd = os.open(tmp_path / "x.lock", os.O_RDWR | os.O_CREAT)
    try:
        assert _filelock.locking_supported()
        _filelock.lock_fd(fd, exclusive=False)
        _filelock.unlock_fd(fd)
    finally:
        os.close(fd)
    assert no_fcntl.calls == [(no_fcntl.LK_LOCK, 1, 0), (no_fcntl.LK_UNLCK, 1, 0)]


def test_install_and_group_locks_work_without_fcntl(no_fcntl, tmp_path):
    root = tmp_path / "packs"
    root.mkdir()
    with group_read_lock(root, exclusive=True):
        pass
    with _install_lock(root):
        assert (root / ".install.lock").is_file()
    assert no_fcntl.calls


def test_unsupported_platform_is_refused_cleanly(monkeypatch, tmp_path):
    monkeypatch.setattr(_filelock, "_fcntl", None)
    monkeypatch.setattr(_filelock, "_msvcrt", None)
    with pytest.raises(Exception, match="supported filesystem"):
        with group_read_lock(tmp_path / "packs", exclusive=True):
            pass


def test_directory_sync_is_skipped_without_o_directory(monkeypatch, tmp_path):
    from astrid.core.pack import transaction

    monkeypatch.delattr(os, "O_DIRECTORY")
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    opened = []
    monkeypatch.setattr(os, "open", lambda *a, **k: opened.append(a) or 0)
    transaction._sync_directory(tmp_path)
    assert opened == []


def test_directory_sync_still_fsyncs_where_supported(tmp_path):
    from astrid.core.pack import transaction

    transaction._sync_directory(tmp_path)


def test_locks_work_without_fcntl_o_directory_or_o_nofollow(no_fcntl, monkeypatch, tmp_path):
    monkeypatch.delattr(os, "O_DIRECTORY")
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)
    root = tmp_path / "packs"
    root.mkdir()
    with group_read_lock(root, exclusive=True):
        pass
    with _install_lock(root):
        pass
