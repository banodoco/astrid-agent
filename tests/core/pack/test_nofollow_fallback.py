"""Symlink refusal where the platform has no O_NOFOLLOW.

Each lock or fsync open must refuse a linked path itself. Monkeypatching the
flag away stands in for Windows, where os.O_NOFOLLOW does not exist.
"""

from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

from astrid.core.pack import install, transaction


def _without_nofollow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)


def _real_file(tmp_path: Path, name: str = "real.lock") -> Path:
    target = tmp_path / name
    target.write_bytes(b"")
    return target


def test_sync_directory_refuses_linked_directory_without_nofollow(tmp_path, monkeypatch):
    _without_nofollow(monkeypatch)
    real = tmp_path / "real-dir"
    real.mkdir()
    link = tmp_path / "linked-dir"
    link.symlink_to(real, target_is_directory=True)

    with pytest.raises(OSError) as refused:
        transaction._sync_directory(link)

    assert refused.value.errno == errno.ELOOP


def test_group_lock_refuses_linked_lock_file_without_nofollow(tmp_path, monkeypatch):
    _without_nofollow(monkeypatch)
    root = tmp_path / "collection"
    lock = tmp_path / ("." + root.name + ".transaction.lock")
    lock.symlink_to(_real_file(tmp_path))

    with pytest.raises(OSError) as refused:
        with transaction.group_read_lock(root, exclusive=True):
            pass

    assert refused.value.errno == errno.ELOOP


def test_install_lock_refuses_linked_lock_file_without_nofollow(tmp_path, monkeypatch):
    _without_nofollow(monkeypatch)
    root = tmp_path / "collection"
    root.mkdir()
    (root / ".install.lock").symlink_to(_real_file(tmp_path))

    with pytest.raises(OSError) as refused:
        with install._install_lock(root):
            pass

    assert refused.value.errno == errno.ELOOP


def test_group_lock_opens_regular_lock_file_without_nofollow(tmp_path, monkeypatch):
    _without_nofollow(monkeypatch)
    root = tmp_path / "collection"

    with transaction.group_read_lock(root, exclusive=True):
        assert (tmp_path / ("." + root.name + ".transaction.lock")).is_file()


def test_install_lock_opens_regular_lock_file_without_nofollow(tmp_path, monkeypatch):
    _without_nofollow(monkeypatch)
    root = tmp_path / "collection"
    root.mkdir()

    with install._install_lock(root):
        assert (root / ".install.lock").is_file()


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="platform has the real flag only on POSIX")
def test_group_lock_refuses_linked_lock_file_with_real_nofollow(tmp_path):
    root = tmp_path / "collection"
    lock = tmp_path / ("." + root.name + ".transaction.lock")
    lock.symlink_to(_real_file(tmp_path))

    with pytest.raises(OSError) as refused:
        with transaction.group_read_lock(root, exclusive=True):
            pass

    assert refused.value.errno == errno.ELOOP
