"""Windows has no os.O_NOFOLLOW; reading the Runtime credential must still work."""
from __future__ import annotations

import os

import pytest

from astrid.packs.runpod import worker_owner


def _credential(tmp_path):
    path = tmp_path / "executor.token"
    path.write_bytes(b"secret-token")
    path.chmod(0o600)
    return path


def test_credential_read_without_o_nofollow(tmp_path, monkeypatch):
    path = _credential(tmp_path)
    monkeypatch.delattr(os, "O_NOFOLLOW")
    assert worker_owner._read_credential(str(path)) == b"secret-token"


def test_credential_read_still_refuses_a_symlink_without_o_nofollow(tmp_path, monkeypatch):
    target = _credential(tmp_path)
    link = tmp_path / "link.token"
    link.symlink_to(target)
    monkeypatch.delattr(os, "O_NOFOLLOW")
    with pytest.raises(worker_owner.RunPodWorkerOwnerError):
        worker_owner._read_credential(str(link))


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
