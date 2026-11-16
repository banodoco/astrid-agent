"""Plan 55 D12: fixture setup preserves the Runtime's private-directory authority."""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests import conftest as shared
from tests.helpers import runtime as runtime_helper


class Owner:
    def __init__(self):
        self.cleanups = []

    def add_cleanup(self, callback):
        self.cleanups.append(callback)


@pytest.mark.parametrize("explicit", [False, True])
def test_windows_new_support_is_created_by_runtime_not_fixture(tmp_path, monkeypatch, explicit):
    support = tmp_path / "support"
    owner = Owner()
    observed = []
    daemon = SimpleNamespace(stop=lambda: None)
    def create(realm, **options):
        observed.append((Path(realm), options))
        assert not support.exists(), "fixture pre-created a Windows directory without a protected DACL"
        return SimpleNamespace(start=lambda: daemon)
    monkeypatch.setattr(runtime_helper, "new_runtime_daemon", create)
    monkeypatch.setattr(sys, "platform", "win32")
    start = shared.real_runtime.__wrapped__(tmp_path, owner)
    result = start(tmp_path / "realm", support_root=support) if explicit else start()
    assert result is daemon and owner.cleanups == [daemon.stop]
    assert observed == [(tmp_path / "realm", {"commit": None, "support_root": support})]
    assert not support.exists()


def test_windows_existing_unsafe_support_is_not_repaired(tmp_path, monkeypatch):
    support = tmp_path / "support"
    support.mkdir()
    marker = support / "existing.txt"
    marker.write_text("unchanged")
    owner = Owner()
    chmod_calls = []
    original = Path.chmod
    def chmod(path, mode, *args, **kwargs):
        if path == support:
            chmod_calls.append(mode)
        return original(path, mode, *args, **kwargs)
    def refuse():
        raise PermissionError("unsafe Windows owner/DACL")
    monkeypatch.setattr(Path, "chmod", chmod)
    monkeypatch.setattr(runtime_helper, "new_runtime_daemon",
                        lambda *args, **kwargs: SimpleNamespace(start=refuse))
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(PermissionError, match="unsafe Windows owner/DACL"):
        shared.real_runtime.__wrapped__(tmp_path, owner)()
    assert chmod_calls == []
    assert marker.read_text() == "unchanged"
    assert owner.cleanups == []


def test_posix_support_setup_still_requests_owner_only_mode(tmp_path, monkeypatch):
    support = tmp_path / "support"
    owner = Owner()
    chmod_calls = []
    original = Path.chmod
    daemon = SimpleNamespace(stop=lambda: None)
    def chmod(path, mode, *args, **kwargs):
        if path == support:
            chmod_calls.append(mode)
        return original(path, mode, *args, **kwargs)
    def create(realm, **options):
        assert support.is_dir()
        return SimpleNamespace(start=lambda: daemon)
    monkeypatch.setattr(Path, "chmod", chmod)
    monkeypatch.setattr(runtime_helper, "new_runtime_daemon", create)
    monkeypatch.setattr(sys, "platform", "linux")
    assert shared.real_runtime.__wrapped__(tmp_path, owner)() is daemon
    assert chmod_calls == [0o700]
    if os.name != "nt":
        assert stat.S_IMODE(support.stat().st_mode) == 0o700


def test_fixture_keeps_explicit_realm_commit_and_daemon_options(tmp_path, monkeypatch):
    owner = Owner()
    daemon = SimpleNamespace(stop=lambda: None)
    observed = []
    def create(realm, **options):
        observed.append((realm, options))
        return SimpleNamespace(start=lambda: daemon)
    monkeypatch.setattr(runtime_helper, "new_runtime_daemon", create)
    monkeypatch.setattr(sys, "platform", "win32")
    realm, support = tmp_path / "custom-realm", tmp_path / "nested" / "custom-support"
    result = shared.real_runtime.__wrapped__(tmp_path, owner)(
        realm, support_root=support, commit="recorded-commit", host="127.0.0.1")
    assert result is daemon
    assert observed == [(realm, {"commit": "recorded-commit", "host": "127.0.0.1",
                                 "support_root": support})]
    assert not support.exists() and owner.cleanups == [daemon.stop]


@pytest.mark.integration
def test_real_runtime_creates_and_reuses_private_support(real_runtime, tmp_path):
    support = tmp_path / "fresh-support"
    assert not support.exists()
    daemon = real_runtime(tmp_path / "first-realm", support_root=support)
    assert daemon.endpoint and support.is_dir()
    if sys.platform == "win32":
        from runtime_protocol.windows_security import private_directory
        private_directory(support)  # Existing valid directory must pass the real DACL validator.
    else:
        assert stat.S_IMODE(support.stat().st_mode) == 0o700
    daemon.stop()
    restarted = real_runtime(tmp_path / "first-realm", support_root=support)
    assert restarted.endpoint and support.is_dir()


@pytest.mark.integration
@pytest.mark.skipif(sys.platform != "win32", reason="requires actual Windows DACL enforcement")
def test_real_windows_runtime_refuses_existing_unprotected_support(real_runtime, tmp_path):
    support = tmp_path / "unprotected-support"
    support.mkdir()
    marker = support / "keep.txt"
    marker.write_text("do not repair or remove")
    with pytest.raises(PermissionError, match="unsafe Windows owner/DACL"):
        real_runtime(tmp_path / "refused-realm", support_root=support)
    assert marker.read_text() == "do not repair or remove"
