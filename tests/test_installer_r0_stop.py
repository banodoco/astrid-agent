"""R0 stop boundary using captured service files and fake OS managers."""
from __future__ import annotations

import os
import hashlib
import json
import plistlib
import shutil
import subprocess
from pathlib import Path

import pytest
from runtime_protocol import platform

from astrid.installer import r0_stop
from astrid.installer.r0_stop import R0StopError, stop_r0_and_wait
from astrid.installer.service import LABEL, Service


FIXTURES = Path(__file__).parent / "r0_service_data"


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
        self.loaded = True
        self.enabled = True
        self.r1_registered = False

    def run(self, argv, *, timeout):
        assert timeout == 105
        argv = [str(v) for v in argv]
        self.calls.append(argv)
        if self.fail and self.fail in argv:
            raise RuntimeError("injected manager failure")
        if argv[:4] == ["systemctl", "--user", "show", r0_stop.R1_UNIT]:
            return ("LoadState=loaded\nActiveState=active" if self.r1_registered
                    else "LoadState=not-found\nActiveState=inactive")
        if argv[:4] == ["systemctl", "--user", "disable", "--now"]:
            self.enabled = self.loaded = False
        if argv[:4] == ["systemctl", "--user", "enable", "--now"]:
            self.enabled = self.loaded = True
        if argv[:2] == ["launchctl", "disable"]:
            self.enabled = False
        if argv[:2] == ["launchctl", "bootout"]:
            self.loaded = False
        if argv[:2] == ["launchctl", "enable"]:
            self.enabled = True
        if argv[:2] == ["launchctl", "bootstrap"]:
            assert self.enabled
            self.loaded = True
        if argv[:4] == ["systemctl", "--user", "show", "astrid.service"]:
            return "FragmentPath=" + ("/unrelated" if self.mismatch else str(self.path)) + "\n"
        if argv[:2] == ["launchctl", "print"]:
            if argv[2].endswith(r0_stop.R1_LABEL):
                if self.r1_registered:
                    return "registered R1"
                raise subprocess.CalledProcessError(113, argv, output=f'Could not find service "{r0_stop.R1_LABEL}"')
            if not self.loaded:
                raise subprocess.CalledProcessError(113, argv, output=f'Could not find service "{LABEL}"')
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
    result = {}
    for p in [path, *path.rglob("*")]:
        info = p.lstat()
        digest = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
        result[str(p.relative_to(path))] = (info.st_mode, info.st_size, info.st_mtime_ns, digest)
    return result


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
    assert r0_stop._marker_path(manager).exists()
    manager.fail = None
    assert r0_stop.recover_r0(manager)
    assert manager.enabled
    assert not (realm / "owner.lock").exists()


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


def test_claimed_completed_adoption_still_refuses_without_verified_contract(tmp_path):
    manager, realm = setup_case(tmp_path, "linux")
    destination = Path(str(realm) + "-r1")
    destination.mkdir()
    (destination / "receipt.json").write_text(json.dumps({"ok": True, "source": str(realm)}))
    before = snapshot(realm.parent)
    with pytest.raises(R0StopError, match="already exists"):
        stop_r0_and_wait(manager, realm)
    assert manager.calls == []
    assert snapshot(realm.parent) == before


def test_launchd_foreign_loaded_arguments_refused(tmp_path):
    manager, realm = setup_case(tmp_path, "darwin")
    original = manager.run

    def foreign(argv, **kwargs):
        output = original(argv, **kwargs)
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
        def child_run(argv, **kwargs):
            output = original(argv, **kwargs)
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
        def recover_run(argv, **kwargs):
            output = original(argv, **kwargs)
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


@pytest.mark.parametrize("system", ["linux", "darwin"])
def test_recovery_refuses_registered_r1(tmp_path, system):
    manager, realm = setup_case(tmp_path, system)
    before = snapshot(realm)
    stop_r0_and_wait(manager, realm)
    manager.r1_registered = True
    manager.calls.clear()
    with pytest.raises(R0StopError, match="R1 is registered"):
        r0_stop.recover_r0(manager)
    assert all("enable" not in call and "bootstrap" not in call for call in manager.calls)
    assert r0_stop._marker_path(manager).exists()
    assert snapshot(realm) == before


@pytest.mark.parametrize("system", ["linux", "darwin"])
def test_registered_r1_refuses_before_r0_stop(tmp_path, system):
    manager, realm = setup_case(tmp_path, system)
    manager.r1_registered = True
    before = snapshot(realm)
    with pytest.raises(R0StopError, match="R1 is registered"):
        stop_r0_and_wait(manager, realm)
    assert manager.enabled and manager.loaded
    assert snapshot(realm) == before
    assert not r0_stop._marker_path(manager).exists()


@pytest.mark.parametrize("cut", ["disable", "bootout"])
def test_launchd_sigkill_at_each_stop_step_then_recover(tmp_path, cut):
    import select
    import signal

    if not hasattr(os, "fork"):
        pytest.skip("POSIX SIGKILL test")
    manager, realm = setup_case(tmp_path, "darwin")
    before = snapshot(realm)
    state = tmp_path / "launchd-state.json"
    read_fd, write_fd = os.pipe()
    original = manager.run
    child = os.fork()
    if child == 0:
        os.close(read_fd)

        def interrupted(argv, **kwargs):
            result = original(argv, **kwargs)
            if argv[:2] == ["launchctl", cut]:
                state.write_text(json.dumps({"enabled": manager.enabled, "loaded": manager.loaded}))
                os.write(write_fd, b"1")
                signal.pause()
            return result

        manager.run = interrupted
        stop_r0_and_wait(manager, realm)
        os._exit(2)
    os.close(write_fd)
    try:
        assert select.select([read_fd], [], [], 5)[0], "child never reached stop step"
        assert os.read(read_fd, 1) == b"1"
        os.kill(child, signal.SIGKILL)
        _, status = os.waitpid(child, 0)
        child = None
        assert os.WIFSIGNALED(status) and os.WTERMSIG(status) == signal.SIGKILL
        persistent = json.loads(state.read_text())
        manager.enabled, manager.loaded = persistent["enabled"], persistent["loaded"]
        assert r0_stop.recover_r0(manager)
        assert manager.enabled and manager.loaded
        assert not r0_stop._marker_path(manager).exists()
        assert snapshot(realm) == before
        if cut == "bootout":
            commands = [call[1] for call in manager.calls if call[0] == "launchctl"]
            assert commands.index("enable") < commands.index("bootstrap")
    finally:
        if child is not None:
            os.kill(child, signal.SIGKILL)
            os.waitpid(child, 0)
        os.close(read_fd)


def test_launchd_query_failure_is_not_treated_as_missing_job(tmp_path):
    manager, realm = setup_case(tmp_path, "darwin")
    stop_r0_and_wait(manager, realm)
    original = manager.run

    def unavailable(argv, **kwargs):
        if argv[:2] == ["launchctl", "print"]:
            raise subprocess.CalledProcessError(1, argv, output="permission denied")
        return original(argv, **kwargs)

    manager.run = unavailable
    manager.calls.clear()
    with pytest.raises(R0StopError, match="marker was retained"):
        r0_stop.recover_r0(manager)
    assert manager.calls == []
    assert r0_stop._marker_path(manager).exists()
