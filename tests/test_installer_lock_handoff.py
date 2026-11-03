"""The CLI may lend its lifetime lock only to a Runtime migration child."""

from __future__ import annotations

import os
import stat
import sys
import textwrap
from pathlib import Path

import pytest

from runtime_protocol import platform

from astrid.installer import cli
from astrid.installer.backend import Backend, RUNTIME_INSTALLER_LOCK_FD_ENV
from astrid.installer.core import RuntimeOperationError, Version


POSIX_ONLY = pytest.mark.skipif(os.name != "posix", reason="POSIX descriptor inheritance")


def _backend(tmp_path: Path) -> Backend:
    log = tmp_path / "logs" / "install.log"
    log.parent.mkdir()
    return Backend(tmp_path / "program", log, tmp_path / "uv", say=lambda _message: None)


def _private_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a+b")
    platform.owner_only(stream.fileno())
    platform.lock_file(stream.fileno(), blocking=False)
    return stream


def _runtime_probe(path: Path, lock_path: Path) -> Path:
    probe = path / "runtime-upgrade-probe"
    probe.write_text(textwrap.dedent(f"""\
        #!/usr/bin/env python3
        import fcntl
        import os
        import subprocess
        import sys

        variable = {RUNTIME_INSTALLER_LOCK_FD_ENV!r}
        if sys.argv[1:6] != ["-X", "utf8", "-I", "-m", "astrid_runtime"] or "upgrade" not in sys.argv:
            raise SystemExit("not the Runtime upgrade invocation")
        fd = int(os.environ[variable])
        os.fstat(fd)
        # The inherited descriptor shares the CLI's open-file description. A
        # fresh lock descriptor must still observe that lifetime custody.
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        competitor = chr(10).join([
            "import fcntl,sys",
            "lock=open(sys.argv[1], 'a+b')",
            "try:",
            "    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)",
            "except BlockingIOError:",
            "    raise SystemExit(0)",
            "else:",
            "    raise SystemExit(23)",
        ])
        result = subprocess.run([sys.executable, "-c", competitor,
                                 {str(lock_path)!r}])
        if result.returncode != 0:
            raise SystemExit("a competing installer acquired the lock")
        print('{{"ok": true}}')
    """), encoding="utf-8")
    probe.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    return probe


@POSIX_ONLY
def test_cli_lends_actual_lifetime_lock_only_to_runtime_upgrade(tmp_path, monkeypatch):
    """The real CLI holder keeps competitors out through its Runtime child."""
    lock_path = tmp_path / "locks" / "astrid-install.lock"
    data = tmp_path / "workspace"
    root = tmp_path / "program"
    backend = _backend(tmp_path)
    backend.service.recorded = lambda: {}
    backend.source_checkout_ownership = lambda *_args: False
    backend.preflight_stop = lambda *_args: None
    probe = _runtime_probe(tmp_path, lock_path)
    events = []

    class HandoffInstaller:
        def __init__(self, _root, _data, actual_backend, **_options):
            assert actual_backend is backend

        def run(self, _mode, _manifest, **_options):
            fd = backend._runtime_upgrade_lock_fd
            assert type(fd) is int
            # A normal subprocess gets neither a forged environment variable
            # nor the locked descriptor, even while the CLI owns it.
            ordinary = (
                "import os,sys; fd=int(sys.argv[1]); "
                f"assert {RUNTIME_INSTALLER_LOCK_FD_ENV!r} not in os.environ; "
                "\ntry: os.fstat(fd)\nexcept OSError: pass\nelse: raise SystemExit(19)"
            )
            backend.run([sys.executable, "-c", ordinary, str(fd)],
                        extra_env={RUNTIME_INSTALLER_LOCK_FD_ENV: "forged"})
            version = Version(tmp_path / "candidate", probe, tmp_path / "profile.json", {})
            backend.upgrade(version, data)
            events.append("upgrade-complete")

    monkeypatch.setattr(cli, "Backend", lambda *_args, **_kwargs: backend)
    monkeypatch.setattr(cli, "Installer", HandoffInstaller)
    monkeypatch.setattr(cli, "installer_lock_path", lambda *_args: lock_path)
    monkeypatch.setattr(platform, "log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(platform, "data_root", lambda: data)
    monkeypatch.setattr(cli, "validate_manifest", lambda _value: {})
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}\n", encoding="utf-8")

    assert cli.main(["--update", "--root", str(root), "--manifest", str(manifest),
                     "--uv", str(tmp_path / "unused-uv")]) == 0
    assert events == ["upgrade-complete"]
    # The CLI is the sole release authority: only after it returns can a new
    # process acquire the file lock.
    with lock_path.open("a+b") as contender:
        platform.lock_file(contender.fileno(), blocking=False)
        platform.unlock_file(contender.fileno())


@POSIX_ONLY
def test_runtime_handoff_is_not_routed_to_non_upgrade_commands(tmp_path, monkeypatch):
    backend = _backend(tmp_path)
    lock_path = tmp_path / "locks" / "astrid-install.lock"
    with _private_lock(lock_path) as lock:
        fd = lock.fileno()
        backend.bind_runtime_upgrade_lock(lock.fileno(), lock_path)
        calls = []

        def capture(_argv, **kwargs):
            calls.append(kwargs)
            return "{}"

        monkeypatch.setattr(backend, "run", capture)
        version = Version(tmp_path / "candidate", Path(sys.executable), tmp_path / "profile.json", {
            RUNTIME_INSTALLER_LOCK_FD_ENV: "forged",
        })
        backend.runtime(version, tmp_path / "workspace", "workspace", "inspect")
        backend.runtime(version, tmp_path / "workspace", "upgrade")

    assert RUNTIME_INSTALLER_LOCK_FD_ENV not in calls[0]["extra_env"]
    assert calls[0]["handoff_fd"] is None
    assert RUNTIME_INSTALLER_LOCK_FD_ENV not in calls[1]["extra_env"]
    assert calls[1]["handoff_fd"] == fd


@POSIX_ONLY
def test_handoff_binding_refuses_a_foreign_or_unsafe_descriptor(tmp_path):
    backend = _backend(tmp_path)
    lock_path = tmp_path / "locks" / "astrid-install.lock"
    with _private_lock(lock_path), (tmp_path / "foreign").open("a+b") as foreign:
        platform.owner_only(foreign.fileno())
        with pytest.raises(RuntimeOperationError, match="handoff is unsafe"):
            backend.bind_runtime_upgrade_lock(foreign.fileno(), lock_path)


def test_runtime_upgrade_never_requests_a_handoff_on_windows(tmp_path, monkeypatch):
    """Windows retains its existing subprocess behavior and receives no FD."""
    import astrid.installer.backend as backend_module

    backend = _backend(tmp_path)
    backend._runtime_upgrade_lock_fd = 42
    calls = []
    monkeypatch.setattr(backend, "run", lambda _argv, **kwargs: calls.append(kwargs) or "{}")
    monkeypatch.setattr(backend_module.os, "name", "nt")
    backend.runtime(Version(tmp_path / "candidate", Path(sys.executable), tmp_path / "profile.json", {}),
                    tmp_path / "workspace", "upgrade")

    assert calls[0]["handoff_fd"] is None
