"""Windows claim setup stays local and uses the existing OS boundary."""

import errno
import hashlib
import json
import os
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from runtime_protocol import platform, windows_security

from astrid.core.execution.warm_hold import WarmHolds
from astrid.packs.runpod import provider, release_control, worker_owner


@pytest.fixture(params=["zero", "absent"])
def windows_filesystem(monkeypatch, request):
    """Model missing CRT operations; retain real POSIX locks on this host."""
    monkeypatch.setattr("random.uniform", lambda *_: 0.0)
    monkeypatch.delattr(os, "fchmod")
    if request.param == "zero":
        monkeypatch.setattr(os, "O_DIRECTORY", 0)
    else:
        monkeypatch.delattr(os, "O_DIRECTORY")
    opened, restricted = [], []
    real_open = os.open
    owner_only, fsync_directory = platform.owner_only, platform.fsync_directory

    def crt_open(path, *args, **kwargs):
        if not isinstance(path, int) and Path(path).is_dir():
            opened.append(path)
            raise PermissionError(errno.EACCES, "CRT cannot open a directory", str(path))
        return real_open(path, *args, **kwargs)

    def on_windows(function, *args, **kwargs):
        # Scope the OS selection to the helper, so pytest and the real local
        # account lock continue to use the Linux host's platform.
        with monkeypatch.context() as native:
            native.setattr(platform, "sys", SimpleNamespace(platform="win32"))
            return function(*args, **kwargs)

    def restrict(fd):
        assert stat.S_ISREG(os.fstat(fd).st_mode)
        restricted.append(fd)

    monkeypatch.setattr(os, "open", crt_open)
    monkeypatch.setattr(windows_security, "restrict", restrict)
    monkeypatch.setattr(platform, "owner_only", lambda *a, **kw: on_windows(owner_only, *a, **kw))
    monkeypatch.setattr(provider, "fsync_directory", lambda path: on_windows(fsync_directory, path), raising=False)
    return SimpleNamespace(opened=opened, restricted=restricted)


@pytest.fixture
def local_budget(monkeypatch):
    calls = []

    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        pytest.fail("claim state setup must start zero processes and make zero provider calls")

    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(provider, "provider_call", forbidden)
    monkeypatch.setattr(provider, "async_provider_call", forbidden)
    return calls


def test_windows_lease_owner_setup_budget(tmp_path, windows_filesystem, local_budget):
    selection = SimpleNamespace(compatibility=object(), path=tmp_path / "claim.json", _read=lambda: None)
    owner = worker_owner.RunPodLeaseOwner(selection, holds=WarmHolds(), quiesce=lambda _: None)
    try:
        descriptor = json.loads(release_control.descriptor_path(selection.path).read_text())
        assert descriptor == {
            "schema": release_control._SCHEMA, "host": "127.0.0.1",
            "port": owner.release_control._socket.getsockname()[1],
            "owner_epoch": owner.epoch, "nonce": owner.release_control.nonce,
        }
        assert len(windows_filesystem.restricted) == 1
        assert local_budget == []
        assert windows_filesystem.opened == []
    finally:
        owner.close()
    assert not release_control.descriptor_path(selection.path).exists()


def test_windows_account_binding_and_pacing_budget(tmp_path, monkeypatch, windows_filesystem, local_budget):
    monkeypatch.setenv("ASTRID_RUNTIME_DATA_ROOT", str(tmp_path))
    gate = provider.account_gate("fake-only", account_ref="account-a")
    account = hashlib.sha256(b"account:account-a").hexdigest()
    credential = hashlib.sha256(b"fake-only").hexdigest()
    assert gate.path.name == account + ".json"
    assert gate.alias_path.name == credential + ".account.json"
    assert json.loads(gate.alias_path.read_text()) == {"account": account}
    assert json.loads(gate.path.read_text()) == {"next": 0, "cooldown": 0, "attempt": 0}
    gate.clock = lambda: 1000.
    gate.before_call(1001.)
    gate.after_response(429, "120")
    assert json.loads(gate.path.read_text()) == {"next": 1000.05, "cooldown": 1120., "attempt": 1}
    assert provider.account_gate("fake-only").path == gate.path
    assert windows_filesystem.opened == []
    assert local_budget == []


def test_windows_unbound_pacing_state(tmp_path, windows_filesystem, local_budget):
    gate = provider.ProviderGate(tmp_path / "gate.json", clock=lambda: 1000.)
    gate.before_call(1001.)
    gate.after_error("120")
    assert json.loads(gate.path.read_text()) == {"next": 1000.05, "cooldown": 1120., "attempt": 1}
    assert gate.path.with_suffix(".json.lock").read_text() == "1"
    assert windows_filesystem.opened == []
    assert local_budget == []


def test_posix_release_modes_and_nofollow_are_unchanged(tmp_path, monkeypatch):
    modes, flags, flushes = [], [], []
    fchmod, open_file, fsync = os.fchmod, os.open, os.fsync

    def chmod(fd, mode):
        modes.append(mode)
        return fchmod(fd, mode)

    def opened(path, flag, *args, **kwargs):
        flags.append((path, flag))
        return open_file(path, flag, *args, **kwargs)

    def sync(fd):
        flushes.append(stat.S_ISREG(os.fstat(fd).st_mode))
        return fsync(fd)

    monkeypatch.setattr(os, "fchmod", chmod)
    monkeypatch.setattr(os, "open", opened)
    monkeypatch.setattr(os, "fsync", sync)
    claim = tmp_path / "private" / "claim.json"
    server = release_control.ReleaseControlServer(claim, "epoch", lambda *_: {"state": "cleanup_complete"})
    try:
        path = release_control.descriptor_path(claim)
        assert stat.S_IMODE(claim.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert modes == [0o600]
        assert flushes == [True]
        assert release_control.request_release(claim, "a" * 32, "pod-exact") == {"state": "cleanup_complete"}
        assert (path, os.O_RDONLY | os.O_NOFOLLOW) in flags
    finally:
        server.close()


def test_posix_pacing_modes_and_directory_fsync_are_unchanged(tmp_path, monkeypatch):
    events = []
    fsync, replace = os.fsync, os.replace

    def sync(fd):
        events.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        return fsync(fd)

    def publish(*args):
        events.append("replace")
        return replace(*args)

    monkeypatch.setattr(os, "fsync", sync)
    monkeypatch.setattr(os, "replace", publish)
    gate = provider.ProviderGate(tmp_path / "gate.json", clock=lambda: 1000.)
    gate.after_error("120")
    assert events == ["file", "replace", "directory", "file"]
    assert stat.S_IMODE(gate.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(gate.path.with_suffix(".json.lock").stat().st_mode) == 0o600


def test_windows_terminal_request_persistence_needs_no_fchmod(tmp_path, monkeypatch):
    """R12 generic host: terminal-request persistence ran os.fchmod unconditionally."""
    import json

    from astrid.core.execution import generic_host

    monkeypatch.delattr(os, "fchmod")
    restricted = []

    def restrict(fd):
        assert stat.S_ISREG(os.fstat(fd).st_mode)
        restricted.append(fd)

    monkeypatch.setattr(platform, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(windows_security, "restrict", restrict)
    path = tmp_path / "terminal-request.json"
    generic_host._persist_terminal_request(path, {"operation": "settle", "lease_token": "t"})
    assert json.loads(path.read_text()) == {"operation": "settle", "lease_token": "t"}
    assert len(restricted) == 1


def test_posix_terminal_request_persistence_stays_private(tmp_path):
    from astrid.core.execution import generic_host

    path = tmp_path / "terminal-request.json"
    generic_host._persist_terminal_request(path, {"operation": "fail"})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
