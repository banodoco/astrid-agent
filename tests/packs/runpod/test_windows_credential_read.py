"""Private credential reads use the platform's no-follow open on Windows too."""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from astrid.packs.runpod import worker_owner


def _credential(tmp_path):
    path = tmp_path / "executor.token"
    path.write_bytes(b"secret-token")
    path.chmod(0o600)
    return path


@pytest.fixture
def windows_filesystem(monkeypatch):
    real_open, posix_nofollow = os.open, os.O_NOFOLLOW

    class Filesystem:
        O_NOFOLLOW = 0x20000000  # Deliberately distinct from POSIX's flag.

        def __init__(self):
            self.opens = []

        def __getattr__(self, name):
            return getattr(os, name)

        def open(self, path, flags):
            self.opens.append(flags)
            assert flags & self.O_NOFOLLOW
            # Simulate the Windows helper's refusal of a reparse point, even
            # when the caller's earlier is_symlink check lost a rename race.
            return real_open(path, (flags & ~self.O_NOFOLLOW) | posix_nofollow)

    filesystem = Filesystem()
    monkeypatch.setattr(worker_owner, "filesystem", filesystem, raising=False)
    monkeypatch.delattr(os, "O_NOFOLLOW")
    return filesystem


def test_credential_read_without_o_nofollow(tmp_path, windows_filesystem):
    path = _credential(tmp_path)
    assert worker_owner._read_credential(str(path)) == b"secret-token"
    assert windows_filesystem.opens == [os.O_RDONLY | windows_filesystem.O_NOFOLLOW]


def test_credential_read_still_refuses_a_symlink_without_o_nofollow(tmp_path, windows_filesystem):
    target = _credential(tmp_path)
    link = tmp_path / "link.token"
    link.symlink_to(target)
    with pytest.raises(worker_owner.RunPodWorkerOwnerError):
        worker_owner._read_credential(str(link))
    assert windows_filesystem.opens == []


def test_windows_open_refuses_symlink_swapped_after_precheck(tmp_path, monkeypatch, windows_filesystem):
    target = _credential(tmp_path)
    link = tmp_path / "swapped.token"
    link.symlink_to(target)
    # The pathname looked safe at the pre-check; the open must still refuse it.
    monkeypatch.setattr(Path, "is_symlink", lambda self: False)
    with pytest.raises(worker_owner.RunPodWorkerOwnerError, match="source is unavailable"):
        worker_owner._read_credential(str(link))
    assert windows_filesystem.opens == [os.O_RDONLY | windows_filesystem.O_NOFOLLOW]


def test_posix_credential_read_keeps_o_nofollow(tmp_path, monkeypatch):
    path = _credential(tmp_path)
    flags = []
    real_open = os.open

    def recording_open(target, flag, *args, **kwargs):
        flags.append(flag)
        return real_open(target, flag, *args, **kwargs)

    monkeypatch.setattr(worker_owner.os, "open", recording_open)
    assert worker_owner._read_credential(str(path)) == b"secret-token"
    assert flags and all(flag & os.O_NOFOLLOW for flag in flags)
