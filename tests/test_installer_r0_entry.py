"""Piece A through the public install path; OS managers are explicit simulations.

The subprocess tests use the real CLI, Backend, Service, captured R0 unit, and
POSIX owner locks. Only systemctl is replaced with a persistent test executable.
These tests do not establish native systemd/launchd or Windows behavior.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from runtime_protocol import platform

from astrid.installer import cli, r0_stop
from astrid.installer.backend import Backend
from astrid.installer.core import Installer, Version, current, write_json
from astrid.installer.service import LABEL, Service, installer_lock_path
from tests.test_installer_r0_stop import FIXTURES, setup_case, snapshot


def catalog(data, realm, realm_id="r0"):
    write_json(data / "runtime" / "catalog.json", {
        "version": 1, "selected_realm_id": realm_id,
        "realms": [{"realm_id": realm_id, "data_root": str(realm)}],
    })


def public_case(tmp_path, monkeypatch):
    home, root, data = tmp_path / "home", tmp_path / "program", tmp_path / "data"
    home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
    folder = root / "versions" / "release0"
    python = folder / "py" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("This interpreter must never be executed by Piece A.")
    version = Version(folder, python, folder / "profile.json", {}, release=0)
    write_json(folder / "version.json", version.document())
    (root / "current").write_text("release0\n")
    write_json(root / "install.json", {"owner": LABEL, "data_root": str(data)})
    realm = data / "runtime" / "realms" / "r0"
    realm.mkdir(parents=True)
    (realm / "owner.lock").write_bytes(b"unchanged R0 lock")
    (realm / "realm.sqlite3").write_bytes(b"unchanged R0 database")
    (realm / "discovery.json").write_text("must not be parsed")
    catalog(data, realm)
    service = Service(root, home / "log", None)
    write_json(service.selection, {"owner": LABEL, "program_root": str(root),
                                  "data_root": str(data), "realm_root": str(realm)})
    service.path.parent.mkdir(parents=True)
    service.path.write_bytes((FIXTURES / "linux.txt").read_bytes())
    state_path = tmp_path / "manager.json"
    write_json(state_path, {"unit": str(service.path), "enabled": True, "running": True,
                            "r1": "absent", "calls": [], "kill_after_disable": False})
    shim = tmp_path / "commands" / "systemctl"
    shim.parent.mkdir()
    # A process boundary exercises Backend.run's actual arguments, timeout and
    # CalledProcessError behavior. This executable is a simulation, not systemd.
    shim.write_text(f"#!{sys.executable}\n" + '''import json, os, signal, sys
from pathlib import Path
path = Path(os.environ["R0_TEST_MANAGER_STATE"])
state = json.loads(path.read_text())
args = sys.argv[1:]
state["calls"].append(args)
path.write_text(json.dumps(state))
if args[:3] == ["--user", "show", "astrid.service"]:
    print("FragmentPath=" + state["unit"])
elif args[:3] == ["--user", "show", "astrid-r1.service"]:
    if state["r1"] == "unknown":
        print("manager unavailable")
        sys.exit(1)
    if state["r1"] == "absent":
        print("LoadState=not-found\\nActiveState=inactive")
        sys.exit(1)  # real systemctl can return nonzero for a missing unit
    print("LoadState=loaded\\nActiveState=" + state["r1"])
elif args == ["--user", "disable", "--now", "astrid.service"]:
    state.update(enabled=False, running=False)
    path.write_text(json.dumps(state))
    if state["kill_after_disable"]:
        os.kill(os.getppid(), signal.SIGKILL)
elif args == ["--user", "enable", "--now", "astrid.service"]:
    state.update(enabled=True, running=True)
    path.write_text(json.dumps(state))
else:
    raise SystemExit("unexpected manager mutation: " + repr(args))
''')
    shim.chmod(0o755)
    monkeypatch.setenv("R0_TEST_MANAGER_STATE", str(state_path))
    monkeypatch.setenv("PATH", str(shim.parent) + os.pathsep + os.environ["PATH"])
    return root, data, realm, service, state_path


def invoke(root):
    return subprocess.run([sys.executable, "-m", "astrid", "install", "--root", str(root)],
                          capture_output=True, text=True, timeout=30)


def state(path, **changes):
    value = json.loads(path.read_text())
    if changes:
        value.update(changes)
        path.write_text(json.dumps(value))
    return value


def move_selected_tree(root, data, service, destination, *, program=False):
    """Exercise recorded selection, including the CLI's default-root override."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(root if program else data), destination)
    if program:
        root = destination
        folder = root / "versions" / "release0"
        write_json(folder / "version.json", Version(
            folder, folder / "py" / "bin" / "python", folder / "profile.json", {}, release=0,
        ).document())
    else:
        data = destination
    realm = data / "runtime" / "realms" / "r0"
    catalog(data, realm)
    marker = json.loads((root / "install.json").read_text())
    write_json(root / "install.json", {**marker, "data_root": str(data)})
    selected = json.loads(service.selection.read_text())
    write_json(service.selection, {**selected, "program_root": str(root),
                                  "data_root": str(data), "realm_root": str(realm)})
    return root, data, realm


pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="Linux executable manager simulation")


@pytest.mark.parametrize("program", [False, True], ids=["data", "program"])
@pytest.mark.parametrize("explicit_root", [False, True], ids=["recorded-root", "explicit-root"])
@pytest.mark.parametrize("existing_lock", [False, True], ids=["0755-missing-lock", "0555-existing-lock"])
def test_public_cli_installer_state_overlap_preserves_roots(
    tmp_path, monkeypatch, program, explicit_root, existing_lock,
):
    root, data, _, service, manager = public_case(tmp_path, monkeypatch)
    lock = installer_lock_path(Path.home(), "linux")
    # The selection is already in this directory. Move the selected contents
    # alongside it, preserving the coherent catalog and real owner.lock.
    source = root if program else data
    for child in source.iterdir():
        shutil.move(str(child), lock.parent / child.name)
    source.rmdir()
    if program:
        root = lock.parent
    else:
        data = lock.parent
    realm = data / "runtime" / "realms" / "r0"
    catalog(data, realm)
    if program:
        folder = root / "versions" / "release0"
        write_json(folder / "version.json", Version(
            folder, folder / "py" / "bin" / "python", folder / "profile.json", {}, release=0,
        ).document())
    write_json(root / "install.json", {"owner": LABEL, "data_root": str(data)})
    selected = json.loads(service.selection.read_text())
    write_json(service.selection, {**selected, "program_root": str(root),
                                  "data_root": str(data), "realm_root": str(realm)})
    if existing_lock:
        lock.write_bytes(b"retained installer lock")
    lock.parent.chmod(0o555 if existing_lock else 0o755)
    fd = os.open(realm / "owner.lock", os.O_RDONLY)
    platform.lock_file(fd, blocking=False)
    before = snapshot(root), snapshot(data)
    try:
        command = [sys.executable, "-m", "astrid", "install"]
        if explicit_root:
            command.extend(["--root", str(root)])
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "recovery marker must be outside" in result.stdout
        assert state(manager)["enabled"] and state(manager)["running"]
        assert state(manager)["calls"] == []
        assert (snapshot(root), snapshot(data)) == before
    finally:
        platform.unlock_file(fd)
        os.close(fd)
        lock.parent.chmod(0o755)


@pytest.mark.parametrize("system", ["darwin", "win32"])
def test_cli_refuses_installer_state_overlap_for_other_platform_layouts(tmp_path, monkeypatch, system):
    root, data, _, service, manager = public_case(tmp_path, monkeypatch)
    # Exercise each CLI state layout and the common early guard on Linux. The
    # real Linux Service fixture remains; no native manager call is expected.
    monkeypatch.setattr(cli, "sys", SimpleNamespace(platform=system))
    lock = installer_lock_path(Path.home(), system)
    lock.parent.mkdir(parents=True, exist_ok=True)
    for child in data.iterdir():
        shutil.move(str(child), lock.parent / child.name)
    data.rmdir()
    data = lock.parent
    realm = data / "runtime" / "realms" / "r0"
    catalog(data, realm)
    write_json(root / "install.json", {"owner": LABEL, "data_root": str(data)})
    selected = json.loads(service.selection.read_text())
    write_json(service.selection, {**selected, "data_root": str(data), "realm_root": str(realm)})
    data.chmod(0o555)
    before = snapshot(root), snapshot(data)
    try:
        assert cli.main(["--root", str(root)]) == 1
        assert state(manager)["calls"] == []
        assert (snapshot(root), snapshot(data)) == before
    finally:
        data.chmod(0o755)


def test_public_cli_marker_data_is_protected_without_selection(tmp_path, monkeypatch):
    root, data, _, service, manager = public_case(tmp_path, monkeypatch)
    service.selection.unlink()
    # Keep the actual source in place and put installer state inside it using
    # the supported XDG_DATA_HOME selection, with no per-user record to help.
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    before = snapshot(root), snapshot(data)
    result = invoke(root)
    assert result.returncode == 1
    assert "protected installer state" in result.stdout
    assert state(manager)["calls"] == []
    assert (snapshot(root), snapshot(data)) == before


@pytest.mark.parametrize("key", ["program_root", "data_root"])
def test_public_cli_retained_stop_roots_protect_installer_state(tmp_path, monkeypatch, key):
    root, data, realm, service, manager = public_case(tmp_path, monkeypatch)
    backend = Backend(root, service.home / "log", Path(sys.executable))
    r0_stop.stop_r0_and_wait(backend.service, realm, data_root=data)
    intent_path = r0_stop._marker_path(service)
    intent = json.loads(intent_path.read_text())
    # An older/changed layout can leave intent naming a source that now
    # contains installer state, even though current selections are disjoint.
    write_json(intent_path, {**intent, key: str(intent_path.parent)})
    state(manager, calls=[])
    intent_path.parent.chmod(0o555)
    before = snapshot(root), snapshot(data), snapshot(intent_path.parent)
    try:
        result = invoke(root)
        assert result.returncode == 1
        assert "protected installer state" in result.stdout
        assert intent_path.exists()
        assert state(manager)["calls"] == []
        assert not state(manager)["enabled"]  # intent retained for safe recovery
        assert (snapshot(root), snapshot(data), snapshot(intent_path.parent)) == before
    finally:
        intent_path.parent.chmod(0o755)


@pytest.mark.parametrize("changed", ["selection", "intent"])
def test_cli_rechecks_roots_under_installer_lock_before_manager_calls(tmp_path, monkeypatch, changed):
    root, data, _, service, manager = public_case(tmp_path, monkeypatch)
    lock_path = installer_lock_path(Path.home(), "linux")
    acquire = platform.lock_file
    under_lock = []

    def change_after_acquisition(fd, **options):
        acquire(fd, **options)
        if changed == "selection":
            selected = json.loads(service.selection.read_text())
            write_json(service.selection, {**selected, "data_root": str(lock_path.parent)})
        else:
            write_json(r0_stop._marker_path(service), {
                "program_root": str(root), "data_root": str(lock_path.parent),
            })
        under_lock.append(snapshot(lock_path.parent))

    monkeypatch.setattr(platform, "lock_file", change_after_acquisition)
    before = snapshot(root), snapshot(data)
    assert cli.main(["--root", str(root)]) == 1
    assert len(under_lock) == 1
    assert snapshot(lock_path.parent) == under_lock[0]
    assert state(manager)["calls"] == []
    assert state(manager)["enabled"] and state(manager)["running"]
    assert (snapshot(root), snapshot(data)) == before


@pytest.mark.parametrize("alias", ["symlink", "hardlink"])
def test_public_cli_installer_lock_alias_preserves_source(tmp_path, monkeypatch, alias):
    root, data, realm, _, manager = public_case(tmp_path, monkeypatch)
    lock = installer_lock_path(Path.home(), "linux")
    if alias == "symlink":
        lock.symlink_to(realm / "owner.lock")
    else:
        os.link(realm / "owner.lock", lock)
    before = snapshot(root), snapshot(data), snapshot(lock.parent)
    result = invoke(root)
    assert result.returncode == 1
    assert state(manager)["calls"] == []
    assert (snapshot(root), snapshot(data), snapshot(lock.parent)) == before


@pytest.mark.parametrize("program", [False, True], ids=["data", "program"])
@pytest.mark.parametrize("existing", [False, True], ids=["missing-log", "existing-log"])
def test_public_cli_nested_default_log_preserves_selected_roots(tmp_path, monkeypatch, program, existing):
    root, data, _, service, manager = public_case(tmp_path, monkeypatch)
    root, data, _ = move_selected_tree(root, data, service, platform.log_dir().parent, program=program)
    if existing:
        platform.log_dir().mkdir(mode=0o755)
        log = platform.log_dir() / "install.log"
        log.write_text("keep the old installer log\n")
        log.chmod(0o644)
    before = snapshot(root), snapshot(data)
    # No --root: the recorded program/data selection must be resolved before
    # touching the default log, including when it is inside the program tree.
    result = subprocess.run([sys.executable, "-m", "astrid", "install"],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "pinned user-local uv" in result.stdout
    assert state(manager)["enabled"] and state(manager)["running"]
    assert not r0_stop._marker_path(service).exists()
    assert (snapshot(root), snapshot(data)) == before


@pytest.mark.parametrize("program", [False, True], ids=["data", "program"])
def test_public_cli_busy_refusal_with_nested_log(tmp_path, monkeypatch, capsys, program):
    root, data, realm, service, manager = public_case(tmp_path, monkeypatch)
    monkeypatch.setattr(platform, "log_dir", lambda: (root if program else data) / "logs")
    before = snapshot(root), snapshot(data)
    wait = r0_stop._wait_for_owner_lock
    monkeypatch.setattr(r0_stop, "_wait_for_owner_lock", lambda path, *_: wait(path, .02, .005))
    fd = os.open(realm / "owner.lock", os.O_RDONLY)
    platform.lock_file(fd, blocking=False)
    try:
        assert cli.main(["--update", "--root", str(root)]) == 1
    finally:
        platform.unlock_file(fd)
        os.close(fd)
    output = capsys.readouterr()
    assert "Finish or cancel running tasks" in output.out
    assert "Finish or cancel running tasks" in output.err  # console traceback
    assert not any(line.startswith("Details: /") for line in output.out.splitlines())
    assert state(manager)["enabled"] and state(manager)["running"]
    assert not r0_stop._marker_path(service).exists()
    assert (snapshot(root), snapshot(data)) == before


@pytest.mark.parametrize("program", [False, True], ids=["data", "program"])
def test_public_cli_recovers_interrupted_stop_with_nested_read_only_source(tmp_path, monkeypatch, program):
    root, data, _, service, manager = public_case(tmp_path, monkeypatch)
    root, data, _ = move_selected_tree(root, data, service, platform.log_dir().parent, program=program)
    try:
        for tree in (root, data):
            for path in sorted(tree.rglob("*"), reverse=True):
                path.chmod(0o555 if path.is_dir() else 0o444)
            tree.chmod(0o555)
        before = snapshot(root), snapshot(data)
        state(manager, kill_after_disable=True)
        killed = invoke(root)
        assert killed.returncode == -signal.SIGKILL, killed.stdout + killed.stderr
        assert r0_stop._marker_path(service).exists()
        assert not state(manager)["enabled"]
        state(manager, kill_after_disable=False)
        recovered = invoke(root)
        assert recovered.returncode == 3, recovered.stdout + recovered.stderr  # recovery-only run: the receipt says not updated (exit 3)
        assert "Recovered the previous R0 service" in recovered.stdout
        assert state(manager)["enabled"] and state(manager)["running"]
        assert not r0_stop._marker_path(service).exists()
        assert (snapshot(root), snapshot(data)) == before
    finally:
        for tree in (root, data):
            tree.chmod(0o755)
            for path in tree.rglob("*"):
                if not path.is_symlink():
                    path.chmod(0o755 if path.is_dir() else 0o644)


def test_public_cli_log_becoming_unwritable_cannot_block_manager_recovery(tmp_path, monkeypatch):
    root, data, _, service, manager = public_case(tmp_path, monkeypatch)
    log = platform.log_dir() / "install.log"
    shim = tmp_path / "commands" / "systemctl"
    shim.write_text(shim.read_text().replace(
        'state.update(enabled=False, running=False)',
        f'log = Path({str(log)!r})\n    log.unlink()\n    log.mkdir()\n'
        '    print("R0 stopped; diagnostic destination unavailable")\n'
        '    state.update(enabled=False, running=False)',
    ).replace('state.update(enabled=True, running=True)',
              'print("R0 restored")\n    state.update(enabled=True, running=True)'))
    before = snapshot(root), snapshot(data)
    result = invoke(root)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "pinned user-local uv" in result.stdout
    assert "diagnostic destination unavailable" in result.stdout
    assert "R0 restored" in result.stdout
    assert "pinned user-local uv" in result.stderr  # failure handler also falls back
    assert not any(line.startswith("Details: /") for line in result.stdout.splitlines())
    assert state(manager)["enabled"] and state(manager)["running"]
    assert not r0_stop._marker_path(service).exists()
    assert (snapshot(root), snapshot(data)) == before


@pytest.mark.parametrize("alias", ["file-symlink", "parent-symlink", "hardlink"])
def test_public_cli_log_alias_into_source_is_console_only(tmp_path, monkeypatch, alias):
    root, data, _, service, manager = public_case(tmp_path, monkeypatch)
    source_log = data / "logs" / "install.log"
    source_log.parent.mkdir()
    source_log.write_text("retained log\n")
    source_log.chmod(0o644)
    log_dir = platform.log_dir()
    log_dir.parent.mkdir(parents=True)
    if alias == "parent-symlink":
        log_dir.symlink_to(source_log.parent, target_is_directory=True)
    else:
        log_dir.mkdir()
        if alias == "file-symlink":
            (log_dir / "install.log").symlink_to(source_log)
        else:
            os.link(source_log, log_dir / "install.log")
    before = snapshot(root), snapshot(data)
    result = invoke(root)
    assert result.returncode == 1 and "pinned user-local uv" in result.stdout
    assert state(manager)["enabled"] and state(manager)["running"]
    assert not r0_stop._marker_path(service).exists()
    assert (snapshot(root), snapshot(data)) == before


def test_real_cli_stops_restores_and_refuses_without_legacy_writes(tmp_path, monkeypatch):
    root, data, _, service, manager = public_case(tmp_path, monkeypatch)
    # Source permissions and complete manifests include the catalog and program
    # metadata. Root-run CI can override chmod, so the manifest is also asserted.
    try:
        for tree in (root, data):
            for path in sorted(tree.rglob("*"), reverse=True):
                path.chmod(0o555 if path.is_dir() else 0o444)
            tree.chmod(0o555)
        before = snapshot(root), snapshot(data), service.path.read_bytes()
        result = invoke(root)
        assert result.returncode == 1, result.stdout + result.stderr
        assert "Astrid will restart to update" in result.stdout
        assert "pinned user-local uv" in result.stdout
        assert "Open https://astrid.haus" not in result.stdout
        assert (snapshot(root), snapshot(data), service.path.read_bytes()) == before
        assert ["--user", "disable", "--now", "astrid.service"] in state(manager)["calls"]
        assert state(manager)["enabled"] and state(manager)["running"]
        assert not r0_stop._marker_path(service).exists()
    finally:
        # Make this test's read-only source disposable after the upgrade and
        # manifest assertions, even on hosts without CAP_DAC_OVERRIDE.
        for tree in (root, data):
            tree.chmod(0o755)
            for path in tree.rglob("*"):
                if not path.is_symlink():
                    path.chmod(0o755 if path.is_dir() else 0o644)


def test_public_cli_busy_lock_refusal_reenables_and_preserves_both_roots(tmp_path, monkeypatch, capsys):
    root, data, realm, service, manager = public_case(tmp_path, monkeypatch)
    before = snapshot(root), snapshot(data)
    wait = r0_stop._wait_for_owner_lock
    monkeypatch.setattr(r0_stop, "_wait_for_owner_lock", lambda path, *_: wait(path, .02, .005))
    fd = os.open(realm / "owner.lock", os.O_RDONLY)
    platform.lock_file(fd, blocking=False)
    try:
        assert cli.main(["--update", "--root", str(root)]) == 1
    finally:
        platform.unlock_file(fd)
        os.close(fd)
    assert "Finish or cancel running tasks" in capsys.readouterr().out
    assert (snapshot(root), snapshot(data)) == before
    assert state(manager)["enabled"] and state(manager)["running"]
    assert not r0_stop._marker_path(service).exists()


def test_sigkill_in_manager_stop_real_cli_rerun_recovers(tmp_path, monkeypatch):
    root, data, _, service, manager = public_case(tmp_path, monkeypatch)
    before = snapshot(root), snapshot(data)
    state(manager, kill_after_disable=True)
    killed = invoke(root)
    assert killed.returncode == -signal.SIGKILL, killed.stdout + killed.stderr
    assert r0_stop._marker_path(service).is_file()
    assert not state(manager)["enabled"]
    state(manager, kill_after_disable=False)
    recovered = invoke(root)
    assert recovered.returncode == 3  # recovery-only run: the receipt says not updated (exit 3)
    assert "Recovered the previous R0 service" in recovered.stdout
    assert state(manager)["enabled"] and state(manager)["running"]
    assert not r0_stop._marker_path(service).exists()
    assert (snapshot(root), snapshot(data)) == before


@pytest.mark.parametrize("r1", ["active", "inactive", "unknown"])
def test_rerun_preserves_marker_and_does_not_restart_r0_with_r1(tmp_path, monkeypatch, r1):
    root, data, _, service, manager = public_case(tmp_path, monkeypatch)
    before = snapshot(root), snapshot(data)
    backend = Backend(root, service.home / "log", Path(sys.executable))
    r0_stop.stop_r0_and_wait(backend.service, data / "runtime" / "realms" / "r0", data_root=data)
    state(manager, r1=r1, calls=[])
    result = invoke(root)
    assert result.returncode == 1
    assert r0_stop._marker_path(service).exists()
    assert not state(manager)["enabled"]
    assert all("enable" not in call and "disable" not in call for call in state(manager)["calls"])
    assert (snapshot(root), snapshot(data)) == before


@pytest.mark.parametrize("r1", ["absent", "active", "inactive", "unknown"])
def test_interrupted_registration_refuses_without_losing_roots(tmp_path, monkeypatch, r1):
    root, data, realm, service, manager = public_case(tmp_path, monkeypatch)
    backend = Backend(root, service.home / "log", Path(sys.executable))
    r0_stop.stop_r0_and_wait(backend.service, realm, data_root=data)
    # A definition may be incomplete or complete when the installer dies.
    # Its existence is not evidence that the manager did or did not start it.
    unit = service.path.with_name(r0_stop.R1_UNIT)
    unit.write_text("[Unit]\nDescription=interrupted registration\n")
    destination = Path(str(data) + "-r1")
    destination.mkdir()
    (destination / "retained-data").write_text("keep")
    before = snapshot(root), snapshot(data), snapshot(destination), unit.read_bytes()
    state(manager, r1=r1, calls=[])
    result = invoke(root)
    assert result.returncode == 1
    if r1 == "absent":
        assert "Recovered the interrupted R0 stop" in result.stdout
        assert state(manager)["enabled"] and state(manager)["running"]
        assert not r0_stop._marker_path(service).exists()
        retry = invoke(root)
        assert retry.returncode == 1 and "R1 handoff is already present" in retry.stdout
    else:
        assert not state(manager)["enabled"]
        assert r0_stop._marker_path(service).exists()
        assert not any("enable" in call for call in state(manager)["calls"])
    assert (snapshot(root), snapshot(data), snapshot(destination), unit.read_bytes()) == before


def test_public_cli_collision_refuses_before_stop(tmp_path, monkeypatch):
    root, data, _, _, manager = public_case(tmp_path, monkeypatch)
    destination = Path(str(data) + "-r1")
    destination.mkdir()
    (destination / "user-file").write_text("keep")
    before = snapshot(root), snapshot(data), snapshot(destination)
    result = invoke(root)
    assert result.returncode == 1 and "already exists" in result.stdout
    assert state(manager)["calls"] == []
    assert (snapshot(root), snapshot(data), snapshot(destination)) == before


def test_public_cli_wrong_manager_binding_refuses_before_stop(tmp_path, monkeypatch):
    root, data, _, _, manager = public_case(tmp_path, monkeypatch)
    state(manager, unit="/foreign/astrid.service")
    before = snapshot(root), snapshot(data)
    result = invoke(root)
    assert result.returncode == 1 and "FragmentPath does not match" in result.stdout
    assert not any("enable" in call or "disable" in call for call in state(manager)["calls"])
    assert (snapshot(root), snapshot(data)) == before


def test_legacy_journal_is_retained_without_replay(tmp_path, monkeypatch):
    root, data, _, _, manager = public_case(tmp_path, monkeypatch)
    write_json(root / "update-transaction.json", {"step": "restoring_realm", "transaction_kind": "migration"})
    before = snapshot(root), snapshot(data)
    result = invoke(root)
    assert result.returncode == 1 and "interrupted legacy R0 update is retained" in result.stdout
    assert state(manager)["calls"] == []
    assert (snapshot(root), snapshot(data)) == before


@pytest.mark.parametrize("evidence", ["selection", "new-unit"])
def test_completed_r1_handoff_cannot_fall_back_to_r0_data_or_label(tmp_path, monkeypatch, evidence):
    root, data, _, service, manager = public_case(tmp_path, monkeypatch)
    old = current(root)
    # Future C retains data_root=R0 as provenance and adds r1_data_root. This
    # candidate must not mistake it for the legacy R1-to-R1 update path.
    write_json(old.root / "version.json", Version(old.root, old.python, old.profile, {}, release=1).document())
    if evidence == "selection":
        selected = json.loads(service.selection.read_text())
        selected["r1_data_root"] = str(data) + "-r1"
        write_json(service.selection, selected)
    else:
        service.path.with_name(r0_stop.R1_UNIT).write_text("R1 job may already be active")
    before = snapshot(root), snapshot(data)
    result = invoke(root)
    assert result.returncode == 1 and "R1 handoff is already present" in result.stdout
    assert state(manager)["calls"] == []
    assert (snapshot(root), snapshot(data)) == before


def test_installer_api_cannot_bypass_entry_boundary(tmp_path, monkeypatch):
    root, data, _, _, _ = public_case(tmp_path, monkeypatch)
    before = snapshot(root), snapshot(data)
    with pytest.raises(r0_stop.R0StopError, match="copy-only upgrade boundary"):
        Installer(root, data, object()).run("update", {"release": 1})
    with pytest.raises(r0_stop.R0StopError, match="cannot enter the legacy in-place update"):
        Installer(root, data, object())._run_update_transaction(
            current(root), {}, prepare=lambda: pytest.fail("R0 must not stage in the legacy transaction"),
            stop=None, switch_candidate=None, switch_old=None, activate=None, activate_old=None,
        )
    assert (snapshot(root), snapshot(data)) == before


def test_legacy_adopt_journal_never_authorizes_destination_reuse(tmp_path):
    manager, realm = setup_case(tmp_path, "linux")
    data = realm.parent
    catalog(data, realm)
    destination = Path(str(data) + "-r1")
    adopted = destination / "runtime" / "realms" / "r0"
    adopted.mkdir(parents=True)
    catalog(destination, adopted)
    write_json(destination / "runtime/adopt-journal.json", {
        "state": "complete", "realm_id": "r0", "source": str(realm),
        "destination": str(adopted), "backup_identity": None})
    before = snapshot(data), snapshot(destination)
    with pytest.raises(r0_stop.R0StopError, match="already exists"):
        r0_stop.stop_r0_and_wait(manager, realm, data_root=data)
    assert manager.calls == []
    assert (snapshot(data), snapshot(destination)) == before


def test_captures_match_real_r0_evidence():
    evidence = Path("/home/user/merge-docs-fresh/docs/merge-plan/evidence/actions/41625801442")
    if not evidence.is_dir():
        pytest.skip("local R0 evidence is not present")
    for system in ("linux", "macos", "windows"):
        actual = evidence / f"journey-receipt-direct-{system}" / "before-cleanup" / "unit-file-before-cleanup.txt"
        assert (FIXTURES / f"{system}.txt").read_text() == actual.read_text()


@pytest.mark.parametrize("mode", [["--update"], []], ids=["update", "install"])
def test_public_cli_windows_r0_refuses_with_exact_line_before_any_change(tmp_path, monkeypatch, capsys, mode):
    from astrid.installer import boundary_handoff, boundary_registration

    root, data, _, service, manager = public_case(tmp_path, monkeypatch)
    made = []

    def windows_backend(*args, **kwargs):
        backend = Backend(*args, **kwargs)
        # Only the manager identity is Windows; every Windows mutation is forbidden.
        backend.service.system = "win32"
        backend.service.powershell = lambda script: pytest.fail("Task Scheduler must not be called")
        for name in ("stage", "runtime", "preflight_stop", "register", "recover_r1_registration"):
            setattr(backend, name, lambda *a, _name=name, **kw: pytest.fail(f"{_name} ran before refusal"))
        made.append(backend)
        return backend

    def forbidden(*args, **kwargs):
        pytest.fail("a stop, registration or copy ran before the Windows refusal")

    monkeypatch.setattr(cli, "Backend", windows_backend)
    monkeypatch.setattr(boundary_handoff, "BoundaryHandoff", forbidden)
    monkeypatch.setattr(boundary_registration, "BoundaryRegistration", forbidden)
    monkeypatch.setattr(r0_stop, "stop_r0_and_wait", forbidden)
    monkeypatch.setattr(Installer, "run", forbidden)
    before = snapshot(root), snapshot(data), service.selection.read_bytes(), service.path.read_bytes()
    assert cli.main([*mode, "--root", str(root), "--uv", sys.executable]) == 1
    output = capsys.readouterr().out.splitlines()
    assert boundary_handoff.WINDOWS_UPGRADE_REFUSAL in output
    assert ("Upgrading from an earlier Windows version isn't supported yet. "
            "Your data was kept; reinstall to get the new version.") in output
    assert made and all(backend.service.system == "win32" for backend in made)
    assert (snapshot(root), snapshot(data), service.selection.read_bytes(), service.path.read_bytes()) == before
    assert state(manager)["calls"] == []
    assert not r0_stop._marker_path(made[-1].service).exists()


def test_public_cli_windows_r0_refusal_keeps_the_legacy_selection_r0_reads(tmp_path, monkeypatch, capsys):
    """Run 42406972807: migration moved R0's live record before the refusal."""
    from astrid.installer import boundary_handoff
    from astrid.installer.service import install_selection

    root, data, _, service, manager = public_case(tmp_path, monkeypatch)
    local = tmp_path / "local"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    legacy = local / "astrid-install.json"
    write_json(legacy, {"owner": LABEL, "program_root": str(root), "data_root": str(data)})
    before = legacy.read_bytes()
    # On Windows the R1 selection is the private record, which R0 never wrote.
    service.selection.unlink()

    def windows_backend(*args, **kwargs):
        backend = Backend(*args, **kwargs)
        backend.service.system = "win32"
        backend.service.powershell = lambda script: pytest.fail("Task Scheduler must not be called")
        return backend

    monkeypatch.setattr(cli, "Backend", windows_backend)
    monkeypatch.setattr(Installer, "run", lambda *a, **kw: pytest.fail("installer ran before refusal"))
    assert cli.main(["--update", "--root", str(root), "--uv", sys.executable]) == 1
    assert boundary_handoff.WINDOWS_UPGRADE_REFUSAL in capsys.readouterr().out.splitlines()
    assert legacy.read_bytes() == before
    assert not os.path.lexists(install_selection(Path.home(), "win32"))
    assert state(manager)["calls"] == []
