"""Manager-first stop against the unmodified R0 journey registrations."""
from __future__ import annotations

import hashlib
import importlib
import json
import os
import plistlib
import re
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
from runtime_protocol import platform

from astrid.installer.backend import Backend
from astrid.installer.core import RuntimeOperationError, Version, update_context, write_json
from astrid.installer.service import LABEL, Service, _release0_service_command, _release0_guarded_command
from tests.test_installer_busy_fence import realm_fixture

pytestmark = pytest.mark.posix_native

FIXTURES = Path(__file__).parent / "fixtures/release0/journey-services"


def installation(tmp_path, monkeypatch, system, *, busy=False, held=True):
    backend, _old, data, paths, realm, _calls = realm_fixture(
        tmp_path, monkeypatch, system=system, blocker="attempt" if busy else None)
    filename = system + (".plist" if system == "darwin" else ".service")
    raw = (FIXTURES / filename).read_bytes()
    provenance = json.loads((FIXTURES / "provenance.json").read_text())
    assert hashlib.sha256(raw).hexdigest() == provenance["files"][filename]["sha256"]
    prefix = ("/Users/runner/work/_temp/astrid-r1-direct-macos" if system == "darwin"
              else "/home/runner/work/_temp/astrid-r1-direct-linux")
    text = raw.decode().replace(prefix, str(tmp_path))
    if system == "darwin":
        document = plistlib.loads(text.encode())
        env = document["EnvironmentVariables"]
        log = Path(document["StandardOutPath"]).with_name("install.log")
        python = Path(document["ProgramArguments"][0])
    else:
        env = dict(json.loads(line.removeprefix("Environment=")).split("=", 1)
                   for line in text.splitlines() if line.startswith("Environment="))
        log = Path(json.loads(re.search(r'^StandardOutput=(.+)$', text, re.M)[1])
                   .removeprefix("append:")).with_name("install.log")
        python = Path(re.search(r'^ExecStart="([^"]+)"', text, re.M)[1])
    folder = python.parent.parent.parent
    folder.mkdir(parents=True)
    old = Version(folder, python, folder / "profile.json", env, release=0)
    write_json(folder / "version.json", old.document())
    backend.log = log
    backend.service = manager = Service(backend.root, log, None, home=tmp_path / "home", system=system)
    manager.path.parent.mkdir(parents=True, exist_ok=True)
    manager.path.write_text(text)
    write_json(manager.selection, {"owner": LABEL, "program_root": str(backend.root),
                                   "data_root": str(data), "realm_id": "selected", "realm_root": str(realm),
                                   "preferences": {"autostart": True}})
    lock = realm / "owner.lock"
    lock.write_bytes(b"opaque R0 lock: do not read or replace\n")
    descriptor = os.open(lock, os.O_RDONLY)
    if held:
        platform.lock_file(descriptor, blocking=False)
    calls = []
    action = _release0_service_command(old, data, windows=False, log=log, env_file=manager.env_file)
    state = {"loaded": True, "release": True, "action": action, "process_action": action,
             "birth": "proc-start-ticks:101;boot:test;pidns:55", "invocation": "a" * 32,
             "hooks": {}, "pid": 555, "cgroup": "/user.slice/astrid.service"}
    # Exercise the production read-only /proc observer against independent OS
    # evidence. The running process does not change on daemon-reload.
    python.parent.mkdir(parents=True)
    python.touch()
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "exe").symlink_to(python)
    monkeypatch.setattr(Service, "_linux_process_root", staticmethod(lambda _pid: proc))
    def identity(pid):
        (proc / "cmdline").write_bytes(b"\0".join(os.fsencode(arg) for arg in state["process_action"]) + b"\0")
        (proc / "cgroup").write_text("0::" + state["cgroup"] + "\n")
        return {"pid": pid, "uid": platform.current_user(), "birth_id": state["birth"]}
    monkeypatch.setattr(platform, "process_identity", identity)

    def run(argv, **kwargs):
        args = [str(arg) for arg in argv]
        calls.append(args)
        if args[:3] == ["systemctl", "--user", "show-environment"]:
            return ""
        if args[:3] == ["systemctl", "--user", "show"]:
            return (f"FragmentPath={manager.path}\nDropInPaths=\nLoadState=loaded\n"
                    f"MainPID={state['pid'] if state['loaded'] else 0}\nControlPID=0\n"
                    f"ActiveState={'active' if state['loaded'] else 'inactive'}\n"
                    f"SubState={'running' if state['loaded'] else 'dead'}\n"
                    f"InvocationID={state['invocation']}\nControlGroup=/user.slice/astrid.service\nKillMode=mixed")
        if args[0] == "busctl":
            rows = ([[state["action"][0], state["action"], False, 0, 0, 0, 0, 0, 0, 0]]
                    if args[-1] == "ExecStart" else state["hooks"].get(args[-1], []))
            return json.dumps({"type": "a(sasbttttuii)", "data": rows})
        if args[:3] == ["systemctl", "--user", "daemon-reload"]:
            registration = manager.release0_registration(data)
            if registration is not None:
                actions = manager._release0_actions(data, registration)
                state["action"] = actions[0] if manager.path.read_text() == registration["text"] else actions[1]
        if args[:2] == ["launchctl", "print"]:
            if not state["loaded"]:
                raise subprocess.CalledProcessError(113, args, output="Could not find service")
            action = plistlib.loads(manager.path.read_bytes())["ProgramArguments"]
            return (f"gui/{platform.current_user()}/{LABEL} = {{\n\tpath = {manager.path}\n"
                    f"\tstate = running\n\tprogram = {action[0]}\n\tpid = 555\n"
                    "\targuments = {\n" + "\n".join(action) + "\n\t}\n}")
        if args[:3] == ["systemctl", "--user", "stop"] or args[:2] == ["launchctl", "bootout"]:
            if state["loaded"] and state["release"]:
                platform.unlock_file(descriptor)
            state["loaded"] = False
        return ""

    manager.run = run
    for name in ("signal_process", "stop_tree"):
        monkeypatch.setattr(platform, name, Mock(side_effect=AssertionError("must not signal a PID")))
    monkeypatch.setattr(importlib.import_module("astrid_runtime.bootstrap"), "down", Mock(side_effect=AssertionError("R0 is already stopped")))
    # Stale/malformed publications must not be interpreted to signal a PID.
    paths.discovery_path.write_bytes(b"{stale R0 discovery}")
    paths.instance_lock_path.write_bytes(b"opaque instance marker")
    return backend, old, data, paths, realm, lock, descriptor, calls, state


@pytest.mark.parametrize("system", ["linux", "darwin"])
@pytest.mark.parametrize("held", [True, False])
def test_real_r0_manager_stop_guards_unit_and_preserves_lock(tmp_path, monkeypatch, system, held):
    backend, old, data, paths, realm, lock, fd, calls, state = installation(
        tmp_path, monkeypatch, system, held=held)
    before = {p: (p.read_bytes(), p.stat().st_ino) for p in
              (lock, paths.discovery_path, paths.instance_lock_path)}
    try:
        # Guard the opaque owner lock against reads as well as PID signaling.
        read_bytes = Path.read_bytes
        def checked_read(path):
            assert path != lock, "owner.lock contents are not evidence"
            return read_bytes(path)
        with monkeypatch.context() as guard:
            guard.setattr(Path, "read_bytes", checked_read)
            backend.publish_launch_admission(old, data)
            result = backend.stop(old, data)
            assert result["status"] == "stopped"
            backend.stop(old, data)  # Idempotent after manager stop / bootout.
        assert {p: (p.read_bytes(), p.stat().st_ino) for p in before} == before
        assert "launch_admission(Version.load(" in backend.service.path.read_text()
        assert not state["loaded"]
        stop = (["systemctl", "--user", "stop", "astrid.service"] if system == "linux" else
                ["launchctl", "bootout", f"gui/{platform.current_user()}/{LABEL}"])
        assert stop in calls
        assert all("disable" not in call for call in calls)
    finally:
        os.close(fd)


@pytest.mark.parametrize("system", ["linux", "darwin"])
def test_busy_r0_stays_running(tmp_path, monkeypatch, system):
    backend, old, data, _paths, _realm, _lock, fd, calls, state = installation(
        tmp_path, monkeypatch, system, busy=True)
    try:
        with pytest.raises(RuntimeOperationError) as error:
            backend.stop(old, data)
        assert error.value.details["safe"] is False
        assert calls == [] and state["loaded"]
    finally:
        os.close(fd)


@pytest.mark.parametrize("system", ["linux", "darwin"])
def test_held_lock_times_out_without_down_or_signal(tmp_path, monkeypatch, system):
    backend, old, data, _paths, _realm, _lock, fd, _calls, state = installation(tmp_path, monkeypatch, system)
    state["release"] = False
    monkeypatch.setattr(backend, "_wait_owner_lock_free",
                        lambda realm: Backend._wait_owner_lock_free(realm, timeout=0))
    try:
        with pytest.raises(RuntimeOperationError, match="lock is still held"):
            backend.stop(old, data)
    finally:
        os.close(fd)


@pytest.mark.parametrize("kind", ["symlink", "directory", "replacement"])
def test_lock_probe_refuses_unsafe_inode(tmp_path, monkeypatch, kind):
    realm = tmp_path / "realm"
    realm.mkdir()
    lock = realm / "owner.lock"
    if kind == "symlink":
        lock.symlink_to(tmp_path / "other")
    elif kind == "directory":
        lock.mkdir()
    else:
        lock.touch()
        original = platform.lock_file
        def replace(fd, **kwargs):
            original(fd, **kwargs)
            # Test deliberately models an external attacker replacing the inode.
            lock.rename(realm / "retired")
            lock.touch()
        monkeypatch.setattr(platform, "lock_file", replace)
    with pytest.raises((ValueError, OSError)):
        Backend._wait_owner_lock_free(realm, timeout=0)


@pytest.mark.parametrize("system", ["linux", "darwin"])
@pytest.mark.parametrize("mode", [0o600, 0o644])
def test_rollback_restores_guarded_captured_r0_unit(tmp_path, monkeypatch, system, mode):
    backend, old, data, _paths, _realm, _lock, fd, calls, _state = installation(tmp_path, monkeypatch, system)
    backend.service.path.chmod(mode)
    before = backend.service.path.read_bytes()
    transaction = {"format": "astrid-install-update/v1", "data_root": str(data),
                   "step": "stopping", "transaction_context": {}}
    try:
        with update_context(backend, transaction):
            backend.publish_launch_admission(old, data)
            assert backend.service.path.read_bytes() == before
            backend.stop(old, data)
        saved = transaction["transaction_context"]["release0_service"]
        candidate = Version(backend.root / "versions/candidate", backend.root / "versions/candidate/py/bin/python",
                            backend.root / "versions/candidate/profile.json", {}, release=1)
        candidate.root.mkdir()
        write_json(candidate.root / "version.json", candidate.document())
        # Candidate registration can replace the unit. Rollback uses the
        # durable transaction copy even through a fresh Service controller.
        backend.service.register(candidate, data)
        backend.service.path.chmod(0o644)
        manager = Service(backend.root, backend.log, backend.service.run,
                          home=backend.service.home, system=system)
        manager.restore_release0(old, data, saved)
        assert manager.path.read_bytes() != before
        assert manager.path.stat().st_mode & 0o777 == mode
        assert manager.release0_registration(data)["text"].encode() == before
        assert "launch_admission(Version.load(" in manager.path.read_text()
        assert manager.recorded()["preferences"]["autostart"] is True
        assert any("bootstrap" in call if system == "darwin" else "daemon-reload" in call for call in calls)
    finally:
        os.close(fd)


@pytest.mark.parametrize("system", ["linux", "darwin"])
@pytest.mark.parametrize("mutation", ["action", "environment", "selection"])
def test_changed_r0_registration_refuses_pid_fallback(tmp_path, monkeypatch, system, mutation):
    backend, old, data, _paths, _realm, _lock, fd, calls, _state = installation(tmp_path, monkeypatch, system)
    try:
        if mutation == "selection":
            record = backend.service.recorded()
            record["data_root"] = str(tmp_path / "other")
            write_json(backend.service.selection, record)
        else:
            text = backend.service.path.read_text()
            text = (text.replace("from astrid_runtime.cli import main", "import unknown") if mutation == "action"
                    else text.replace("ASTRID_HOST_EXECUTION_PROFILE", "UNKNOWN_EXECUTION_PROFILE"))
            backend.service.path.write_text(text)
        with pytest.raises(RuntimeOperationError):
            backend.stop(old, data)
        assert calls == []
    finally:
        os.close(fd)


@pytest.mark.parametrize("system", ["linux", "darwin"])
@pytest.mark.parametrize("mutation", ["loaded-action", "file-race", "command-failure"])
def test_changed_manager_or_failed_stop_never_uses_down(tmp_path, monkeypatch, system, mutation):
    backend, old, data, _paths, _realm, _lock, fd, calls, _state = installation(tmp_path, monkeypatch, system)
    original = backend.service.run
    def run(argv, **kwargs):
        result = original(argv, **kwargs)
        if "show" in argv or "print" in argv:
            if mutation == "loaded-action":
                result = (result.replace("DropInPaths=", "DropInPaths=/other.conf") if system == "linux"
                          else result.replace("from astrid_runtime.cli import main", "import unknown"))
            if mutation == "file-race":
                backend.service.path.write_text("external replacement")
        if mutation == "command-failure" and ("stop" in argv or "bootout" in argv):
            raise subprocess.CalledProcessError(1, argv, output="manager refused")
        return result
    backend.service.run = run
    try:
        with pytest.raises(RuntimeOperationError):
            backend.stop(old, data)
        if mutation != "command-failure":
            assert not any("stop" in call or "bootout" in call for call in calls)
    finally:
        os.close(fd)


def test_absent_lock_is_not_created(tmp_path):
    realm = tmp_path / "realm"
    realm.mkdir()
    Backend._wait_owner_lock_free(realm, timeout=0)
    assert list(realm.iterdir()) == []


@pytest.mark.parametrize("system", ["linux", "darwin"])
def test_shared_transaction_restarts_untouched_r0_after_backup_failure(tmp_path, monkeypatch, system):
    from astrid.installer.core import Installer, current, switch

    backend, old, data, _paths, realm, _lock, fd, calls, _state = installation(tmp_path, monkeypatch, system)
    before = backend.service.path.read_bytes()
    switch(backend.root, old)
    candidate = Version(backend.root / "versions/candidate", backend.root / "versions/candidate/py/bin/python",
                        backend.root / "versions/candidate/profile.json", {}, release=1)
    candidate.root.mkdir()
    write_json(candidate.root / "version.json", candidate.document())
    backend.register = Backend.register.__get__(backend)
    backend.start = Backend.start.__get__(backend)
    backend.verify = lambda *_args: None
    installer = Installer(backend.root, data, backend, say=lambda *_: None)
    def fail_backup(*_args):
        raise ValueError("simulated backup failure")
    try:
        with pytest.raises(ValueError, match="simulated backup failure"):
            installer._run_update_transaction(
                old, {"realm_id": "selected", "realm_root": str(realm)}, prepare=lambda: candidate,
                stop=backend.stop, switch_candidate=lambda version: switch(backend.root, version),
                switch_old=lambda version, _candidate: switch(backend.root, version),
                activate=lambda *_: pytest.fail("backup failed before candidate activation"),
                activate_old=lambda version, _data, selected: installer._activate(version, selected["realm_id"]),
                backup=fail_backup, restore_workspace=False,
            )
        assert current(backend.root) == old
        assert backend.service.path.read_bytes() != before
        assert backend.service.release0_registration(data)["text"].encode() == before
        journal = json.loads((backend.root / "update-transaction.json").read_text())
        assert journal["step"] == "rolled_back"
        assert journal["transaction_context"]["release0_service"]["text"].encode() == before
        start = (["systemctl", "--user", "start", "astrid.service"] if system == "linux" else
                 ["launchctl", "kickstart", f"gui/{platform.current_user()}/{LABEL}"])
        assert start in calls
    finally:
        os.close(fd)


@pytest.mark.parametrize("system", ["linux", "darwin"])
def test_work_admitted_after_preflight_still_refuses_manager_stop(tmp_path, monkeypatch, system):
    import sqlite3

    backend, old, data, _paths, realm, _lock, fd, calls, state = installation(tmp_path, monkeypatch, system)
    original = backend.preflight_stop
    def preflight(data):
        original(data)
        with sqlite3.connect(realm / "realm.sqlite3") as connection:
            connection.execute("INSERT INTO tasks VALUES ('late', 'render', 'running', '2000-01-01T00:00:00Z')")
    monkeypatch.setattr(backend, "preflight_stop", preflight)
    try:
        with pytest.raises(RuntimeOperationError) as error:
            backend.stop(old, data)
        assert error.value.details["safe"] is False
        assert calls == [] and state["loaded"]
    finally:
        os.close(fd)


def test_lock_probe_keeps_one_inode_until_delayed_release(tmp_path, monkeypatch):
    realm = tmp_path / "realm"
    realm.mkdir()
    lock = realm / "owner.lock"
    lock.touch()
    fd = os.open(lock, os.O_RDONLY)
    platform.lock_file(fd, blocking=False)
    try:
        monkeypatch.setattr("astrid.installer.backend.time.sleep", lambda _delay: platform.unlock_file(fd))
        Backend._wait_owner_lock_free(realm, timeout=1)
    finally:
        os.close(fd)


def test_replacement_while_waiting_cannot_hide_held_lock(tmp_path, monkeypatch):
    realm = tmp_path / "realm"
    realm.mkdir()
    lock = realm / "owner.lock"
    lock.touch()
    fd = os.open(lock, os.O_RDONLY)
    platform.lock_file(fd, blocking=False)
    def replace(_delay):
        lock.rename(realm / "retired")
        lock.touch()
    monkeypatch.setattr("astrid.installer.backend.time.sleep", replace)
    try:
        with pytest.raises(ValueError, match="lock changed"):
            Backend._wait_owner_lock_free(realm, timeout=1)
    finally:
        os.close(fd)
