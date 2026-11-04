"""R0 stop boundary using captured service files and fake OS managers."""
from __future__ import annotations

import os
import plistlib
import shutil
from pathlib import Path

import pytest
from runtime_protocol import platform

from astrid.installer import r0_stop
from astrid.installer.r0_stop import R0StopError, stop_r0_and_wait
from astrid.installer.service import LABEL, Service


FIXTURES = Path(__file__).parent / "fixtures" / "r0_service"


@pytest.fixture(autouse=True)
def fake_launchd_uid(monkeypatch):
    monkeypatch.setattr(r0_stop, "_launchd_uid", lambda: 501)


class Manager:
    def __init__(self, system, unit):
        self.system, self.path = system, unit
        self.home = unit.parent
        self.root = unit.parent / "program"
        (self.home / ".local" / "share").mkdir(parents=True, exist_ok=True)
        self.calls = []
        self.mismatch = False
        self.fail = None

    def run(self, argv):
        argv = [str(v) for v in argv]
        self.calls.append(argv)
        if self.fail and self.fail in argv:
            raise RuntimeError("injected manager failure")
        if argv[:4] == ["systemctl", "--user", "show", "astrid.service"]:
            return "FragmentPath=" + ("/unrelated" if self.mismatch else str(self.path)) + "\n"
        if argv[:2] == ["launchctl", "print"]:
            path = "/unrelated" if self.mismatch else str(self.path)
            args = plistlib.loads(self.path.read_bytes())["ProgramArguments"]
            body = "".join("\t\t" + arg + "\n" for arg in args)
            return f"{argv[2]} = {{\n\tpath = {path}\n\targuments = {{\n{body}\t}}\n}}\n"
        return ""

    def powershell(self, script):
        self.calls.append(script)
        if self.fail and self.fail in script:
            raise RuntimeError("injected manager failure")
        return ""


def setup_case(tmp_path, system):
    realm = tmp_path / "data" / "realm"
    realm.mkdir(parents=True)
    (realm / "owner.lock").write_bytes(b"lock must stay unchanged")
    (realm / "realm.sqlite3").write_bytes(b"original database")
    (realm / "discovery.json").write_bytes(b"never inspect")
    unit = tmp_path / ("astrid.service" if system == "linux" else f"{LABEL}.plist")
    fixture = {"linux": "linux.txt", "darwin": "macos.txt", "win32": "windows.txt"}[system]
    shutil.copyfile(FIXTURES / fixture, unit)
    if system == "darwin":
        value = plistlib.loads(unit.read_bytes())
        old_root = "/Users/runner/work/_temp/astrid-r1-direct-macos/program"
        value["ProgramArguments"] = [arg.replace(old_root, str(tmp_path / "program")) for arg in value["ProgramArguments"]]
        unit.write_bytes(plistlib.dumps(value))
    return Manager(system, unit), realm


def snapshot(path):
    return {str(p.relative_to(path)): (p.read_bytes(), p.stat().st_mtime_ns, p.stat().st_size)
            for p in path.rglob("*") if p.is_file()}


@pytest.mark.parametrize("system", ["linux", "darwin"])
def test_stop_and_wait_with_captured_units(tmp_path, system):
    manager, realm = setup_case(tmp_path, system)
    data_before, unit_before = snapshot(realm), manager.path.read_bytes()
    if system == "linux":
        assert b"Restart=always" in unit_before and b"[Service]" in unit_before
    elif system == "darwin":
        assert plistlib.loads(unit_before)["Label"] == LABEL
    else:
        assert b"definition unavailable" in unit_before
    stop_r0_and_wait(manager, realm, timeout=0)
    assert snapshot(realm) == data_before
    assert manager.path.read_bytes() == unit_before
    if system == "linux":
        assert manager.calls[-1] == ["systemctl", "--user", "disable", "--now", "astrid.service"]
    elif system == "darwin":
        target = f"gui/501/{LABEL}"
        assert manager.calls[-2:] == [["launchctl", "disable", target], ["launchctl", "bootout", target]]
    else:
        assert "Disable-ScheduledTask" in manager.calls[0]
        assert "Stop-ScheduledTask" in manager.calls[1]


@pytest.mark.parametrize("system", ["linux", "darwin"])
def test_busy_timeout_reenables_and_preserves_data(tmp_path, system):
    manager, realm = setup_case(tmp_path, system)
    before, unit_before = snapshot(realm), manager.path.read_bytes()
    fd = os.open(realm / "owner.lock", os.O_RDONLY)
    platform.lock_file(fd, blocking=False)
    try:
        with pytest.raises(R0StopError, match="Finish or cancel running tasks"):
            stop_r0_and_wait(manager, realm, timeout=0.02, poll_interval=0.005)
    finally:
        platform.unlock_file(fd)
        os.close(fd)
    assert snapshot(realm) == before
    assert manager.path.read_bytes() == unit_before
    if system == "linux":
        assert manager.calls[-1] == ["systemctl", "--user", "enable", "--now", "astrid.service"]
    elif system == "darwin":
        target = f"gui/501/{LABEL}"
        assert ["launchctl", "enable", target] in manager.calls
        assert manager.calls[-1] == ["launchctl", "kickstart", target]
    else:
        assert "Enable-ScheduledTask" in manager.calls[-2]
        assert "Start-ScheduledTask" in manager.calls[-1]


@pytest.mark.parametrize("system", ["linux", "darwin"])
def test_wrong_service_path_refuses_without_stopping(tmp_path, system):
    manager, realm = setup_case(tmp_path, system)
    manager.mismatch = True
    with pytest.raises(R0StopError, match="does not match"):
        stop_r0_and_wait(manager, realm)
    assert len(manager.calls) == 1


@pytest.mark.parametrize("system", ["linux", "darwin"])
def test_missing_lock_refuses_and_reenables_without_creating_it(tmp_path, system):
    manager, realm = setup_case(tmp_path, system)
    (realm / "owner.lock").unlink()
    before = snapshot(realm)
    with pytest.raises(R0StopError, match="owner-lock check failed"):
        stop_r0_and_wait(manager, realm, timeout=0)
    assert snapshot(realm) == before
    assert not (realm / "owner.lock").exists()
    assert any("enable" in str(call).lower() for call in manager.calls)


def test_manager_partial_failure_attempts_reenable(tmp_path):
    manager, realm = setup_case(tmp_path, "darwin")
    manager.fail = "bootout"
    with pytest.raises(R0StopError, match="injected manager failure"):
        stop_r0_and_wait(manager, realm)
    assert ["launchctl", "enable", f"gui/501/{LABEL}"] in manager.calls


def test_reenable_failure_is_actionable(tmp_path):
    manager, realm = setup_case(tmp_path, "linux")
    (realm / "owner.lock").unlink()
    manager.fail = "enable"
    with pytest.raises(R0StopError, match="automatic re-enable also failed.*Re-enable"):
        stop_r0_and_wait(manager, realm)


def test_real_service_uses_same_unit_identity(tmp_path):
    linux = Service(tmp_path, tmp_path / "log", lambda _: "", home=tmp_path, system="linux")
    mac = Service(tmp_path, tmp_path / "log", lambda _: "", home=tmp_path, system="darwin")
    assert linux.path.name == "astrid.service"
    assert mac.path.name == f"{LABEL}.plist"


def test_wait_succeeds_when_owner_releases(tmp_path):
    import threading
    import time

    manager, realm = setup_case(tmp_path, "linux")
    fd = os.open(realm / "owner.lock", os.O_RDONLY)
    platform.lock_file(fd, blocking=False)

    def release():
        time.sleep(0.03)
        platform.unlock_file(fd)

    worker = threading.Thread(target=release)
    worker.start()
    try:
        stop_r0_and_wait(manager, realm, timeout=1, poll_interval=0.005)
    finally:
        worker.join()
        os.close(fd)
    assert not any("enable" in call for call in manager.calls)


def test_symlink_lock_refused_and_left_intact(tmp_path):
    manager, realm = setup_case(tmp_path, "linux")
    lock = realm / "owner.lock"
    lock.unlink()
    try:
        lock.symlink_to(realm / "realm.sqlite3")
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")
    before = (realm / "realm.sqlite3").read_bytes()
    with pytest.raises(R0StopError, match="not a regular file"):
        stop_r0_and_wait(manager, realm)
    assert lock.is_symlink()
    assert (realm / "realm.sqlite3").read_bytes() == before
    assert manager.calls[-1][2:4] == ["enable", "--now"]


def test_collision_refuses_before_manager_mutation(tmp_path):
    manager, realm = setup_case(tmp_path, "linux")
    destination = Path(str(realm) + "-r1")
    destination.mkdir()
    (destination / "user-file").write_text("preserve")
    with pytest.raises(R0StopError, match="already exists"):
        stop_r0_and_wait(manager, realm)
    assert manager.calls == []
    assert (destination / "user-file").read_text() == "preserve"


def test_launchd_foreign_loaded_arguments_refused(tmp_path):
    manager, realm = setup_case(tmp_path, "darwin")
    original = manager.run

    def foreign(argv):
        output = original(argv)
        if argv[:2] == ["launchctl", "print"]:
            return output.replace(str(manager.root / "versions"), "/foreign/versions")
        return output

    manager.run = foreign
    with pytest.raises(R0StopError, match="ProgramArguments"):
        stop_r0_and_wait(manager, realm)
    assert len(manager.calls) == 1


def test_windows_unverified_task_refused_before_mutation(tmp_path):
    manager, realm = setup_case(tmp_path, "win32")
    with pytest.raises(R0StopError, match="cannot be verified"):
        stop_r0_and_wait(manager, realm)
    assert manager.calls == []


def test_killed_after_disable_next_invocation_recovers(tmp_path):
    import signal

    if not hasattr(os, "fork"):
        pytest.skip("POSIX process test")
    manager, realm = setup_case(tmp_path, "linux")
    before = snapshot(realm)
    state = tmp_path / "manager-state"
    state.write_text("enabled")
    read_fd, write_fd = os.pipe()
    original = manager.run

    child = os.fork()
    if child == 0:
        os.close(read_fd)
        # Block inside the manager call after its persistent side effect.
        def child_run(argv):
            output = original(argv)
            if argv[2:4] == ["disable", "--now"]:
                state.write_text("disabled")
                os.write(write_fd, b"1")
                signal.pause()
            return output
        manager.run = child_run
        stop_r0_and_wait(manager, realm)
        os._exit(2)
    os.close(write_fd)
    try:
        assert os.read(read_fd, 1) == b"1"
        os.kill(child, signal.SIGKILL)
        _, status = os.waitpid(child, 0)
        assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
        assert state.read_text() == "disabled"
        def recover_run(argv):
            output = original(argv)
            if argv[2:4] == ["enable", "--now"]:
                state.write_text("enabled")
            return output
        manager.run = recover_run
        assert r0_stop.recover_r0(manager)
        assert state.read_text() == "enabled"
        assert snapshot(realm) == before
        assert not r0_stop._marker_path(manager).exists()
    finally:
        os.close(read_fd)


def test_recovery_refuses_changed_unit(tmp_path):
    manager, realm = setup_case(tmp_path, "linux")
    stop_r0_and_wait(manager, realm)
    manager.path.write_text("foreign replacement")
    before = len(manager.calls)
    with pytest.raises(R0StopError, match="marker was retained"):
        r0_stop.recover_r0(manager)
    assert len(manager.calls) == before
    assert r0_stop._marker_path(manager).exists()


def test_install_entry_recovers_marker_before_update_preflight(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace
    from astrid.installer import cli
    from astrid.installer.core import write_json

    if sys.platform != "linux":
        pytest.skip("Linux manager model")
    manager, realm = setup_case(tmp_path, "linux")
    manager.root.mkdir()
    write_json(manager.root / "install.json", {"owner": LABEL, "data_root": str(realm)})
    manager.recorded = lambda: {"owner": LABEL, "program_root": str(manager.root), "data_root": str(realm)}
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    stop_r0_and_wait(manager, realm)
    manager.calls.clear()
    backend = SimpleNamespace(service=manager, managed_by=None)
    monkeypatch.setattr(cli, "Backend", lambda *_a, **_k: backend)
    monkeypatch.setattr(platform, "log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(platform, "private_directory", lambda path: Path(path).mkdir(parents=True, exist_ok=True))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    assert cli.main(["--root", str(manager.root)]) == 0
    assert ["systemctl", "--user", "enable", "--now", "astrid.service"] in manager.calls
    assert not r0_stop._marker_path(manager).exists()
