"""The upgrade stop keeps the observed unit and its container bound together."""

import json
import time

import pytest

from astrid.installer.core import Version
from astrid.installer.service import LABEL, Service, render


@pytest.fixture
def unit(tmp_path):
    calls = []
    data = tmp_path / "data"
    manager = Service(tmp_path / "program", tmp_path / "log", lambda argv: calls.append(argv) or "",
                      home=tmp_path, system="linux")
    manager.path = tmp_path / "astrid.service"
    manager.selection = tmp_path / "selection.json"
    manager.selection.write_text(json.dumps({"owner": LABEL, "program_root": str(manager.root),
                                             "data_root": str(data)}))
    version = Version(manager.root / "versions" / "old", tmp_path / "python", tmp_path / "profile", {})
    manager.path.write_text(render("linux", version, data, manager.log, env_file=manager.env_file))
    manager.systemd = lambda: True
    info = {"LoadState": "loaded", "FragmentPath": str(manager.path), "ActiveState": "active",
            "UnitFileState": "enabled", "ControlGroup": "/user.slice/user-1000.slice/astrid.service"}
    manager._upgrade_systemd_state = lambda: info.copy()
    return manager, version, data, calls, info


def test_upgrade_service_stop_and_exact_restore(unit):
    manager, version, data, calls, _ = unit
    original = manager.path.read_bytes()
    manager.path.chmod(0o640)
    state = json.loads(json.dumps(manager.capture_upgrade_state(version, data)))
    manager.stop_for_upgrade(state)
    assert calls == [["systemctl", "--user", "disable", "--now", "astrid.service"]]
    manager.path.write_bytes(b"candidate")
    manager.restore_upgrade_definition(state, expected_current=b"candidate")
    assert manager.path.read_bytes() == original
    assert manager.path.stat().st_mode & 0o777 == 0o640
    manager.restore_upgrade_activation(state)
    assert calls[-3:] == [["systemctl", "--user", "daemon-reload"],
                          ["systemctl", "--user", "enable", "astrid.service"],
                          ["systemctl", "--user", "start", "astrid.service"]]


def test_upgrade_stop_refuses_changed_definition(unit):
    manager, version, data, calls, _ = unit
    state = manager.capture_upgrade_state(version, data)
    manager.path.write_text("replacement")
    with pytest.raises(ValueError, match="changed before stop"):
        manager.stop_for_upgrade(state)
    assert calls == []


def test_upgrade_restore_refuses_changed_definition(unit):
    manager, version, data, calls, _ = unit
    state = manager.capture_upgrade_state(version, data)
    manager.path.write_bytes(b"foreign")
    with pytest.raises(ValueError, match="changed before restore"):
        manager.restore_upgrade_definition(state, expected_current=b"candidate")
    assert manager.path.read_bytes() == b"foreign"
    assert calls == []


def test_upgrade_capture_refuses_unknown_action(unit):
    manager, version, data, calls, _ = unit
    manager.path.write_text('[Service]\nExecStart="/unrelated"\n')
    with pytest.raises(ValueError, match="unknown"):
        manager.capture_upgrade_state(version, data)
    assert calls == []


def test_upgrade_wait_checks_recursive_cgroup_and_times_out(unit, tmp_path):
    manager, version, data, _, _ = unit
    state = manager.capture_upgrade_state(version, data)
    root = tmp_path / "cgroup"
    root.mkdir()
    (root / "cgroup.controllers").write_text("cpu memory\n")
    group = root / state["cgroup"].lstrip("/")
    group.mkdir(parents=True)
    events = group / "cgroup.events"
    events.write_text("populated 1\nfrozen 0\n")
    with pytest.raises(RuntimeError, match="still has running processes"):
        manager.wait_upgrade_empty(state, time.monotonic(), cgroup_root=root)
    events.write_text("populated 0\nfrozen 0\n")
    manager.wait_upgrade_empty(state, time.monotonic(), cgroup_root=root)
    events.unlink()
    group.rmdir()
    manager.wait_upgrade_empty(state, time.monotonic(), cgroup_root=root)


@pytest.mark.parametrize("system", ["darwin", "win32"])
def test_unobservable_platform_refuses_before_mutation(tmp_path, system):
    calls = []
    manager = Service(tmp_path / "program", tmp_path / "log", lambda argv: calls.append(argv),
                      home=tmp_path, system=system)
    version = Version(tmp_path / "version", tmp_path / "python", tmp_path / "profile", {})
    with pytest.raises(RuntimeError, match="process container"):
        manager.capture_upgrade_state(version, tmp_path / "data")
    assert calls == []

@pytest.fixture
def launchd(tmp_path, monkeypatch):
    from astrid.installer import service
    from astrid.installer.core import write_json
    from astrid.installer.service import command

    monkeypatch.setattr(service.platform, "current_user", lambda: 501)
    data, calls = tmp_path / "data", []
    manager = Service(tmp_path / "program", tmp_path / "log", None, home=tmp_path, system="darwin")
    folder = manager.root / "versions" / "old"
    folder.mkdir(parents=True)
    version = Version(folder, folder / "bin" / "python", folder / "profile", {})
    write_json(folder / "version.json", version.document())
    write_json(manager.selection, {"owner": LABEL, "program_root": str(manager.root), "data_root": str(data)})
    manager.path.parent.mkdir(parents=True)
    manager.path.write_text(render("darwin", version, data, manager.log, env_file=manager.env_file))
    target = f"gui/501/{LABEL}"
    argv = command(version, data, log=manager.log, env_file=manager.env_file)
    job = {"pid": 123, "override": "enabled", "action": argv, "loaded": True}

    def run(args):
        calls.append(list(args))
        if args[:2] == ["launchctl", "print"]:
            if not job["loaded"]:
                import subprocess
                raise subprocess.CalledProcessError(113, args, output="Could not find service")
            action = job["action"]
            return (f"{target} = {{\n\tpath = {manager.path}\n\tstate = running\n"
                    f"\tpid = {job['pid']}\n\tprogram = {action[0]}\n"
                    f"\targuments = {{\n" + "\n".join(action) + "\n\t}\n}")
        if args[:2] == ["launchctl", "print-disabled"]:
            return f'{{\n\t"{LABEL}" => {job["override"]}\n}}'
        if args[1] in {"enable", "disable"}:
            job["override"] = "enabled" if args[1] == "enable" else "disabled"
            return ""
        if args[1] == "bootout":
            job["loaded"] = False
            return ""
        raise AssertionError(args)

    manager.run = run
    return manager, version, data, calls, job, target


@pytest.mark.parametrize("override,restore_command", [("enabled", "enable"), ("disabled", "disable")])
def test_launchd_upgrade_stop_orders_disable_bootout_and_restores_override(launchd, override, restore_command):
    manager, version, data, calls, job, target = launchd
    job["override"] = override
    state = json.loads(json.dumps(manager.capture_launchd_upgrade_stop(version, data, owner_pid=123)))
    manager.stop_launchd_for_upgrade(state)
    mutations = [call for call in calls if call[1] in {"enable", "disable", "bootout"}]
    assert mutations == [["launchctl", "disable", target], ["launchctl", "bootout", target]]
    manager.restore_launchd_upgrade_override(state)
    assert calls[-1] == ["launchctl", restore_command, target]


def test_launchd_upgrade_refuses_unknown_override_and_owner_before_mutation(launchd):
    manager, version, data, calls, job, _ = launchd
    with pytest.raises(ValueError, match="does not match"):
        manager.capture_launchd_upgrade_stop(version, data, owner_pid=999)
    job["override"] = "unknown"
    with pytest.raises(ValueError, match="override is unknown"):
        manager.capture_launchd_upgrade_stop(version, data, owner_pid=123)
    assert all(call[1] not in {"enable", "disable", "bootout"} for call in calls)


def test_launchd_upgrade_revalidates_before_disable_and_bootout(launchd):
    manager, version, data, calls, job, target = launchd
    state = manager.capture_launchd_upgrade_stop(version, data, owner_pid=123)
    job["pid"] = 456
    with pytest.raises(ValueError, match="does not match"):
        manager.stop_launchd_for_upgrade(state)
    assert ["launchctl", "disable", target] not in calls
    job["pid"] = 123
    run = manager.run
    def change_after_disable(args):
        result = run(args)
        if args[1] == "disable":
            job["pid"] = 456
        return result
    manager.run = change_after_disable
    with pytest.raises(ValueError, match="does not match"):
        manager.stop_launchd_for_upgrade(state)
    assert ["launchctl", "bootout", target] not in calls
    job["pid"] = 123
    manager.restore_launchd_upgrade_override(state)
    assert calls[-1] == ["launchctl", "enable", target]


def test_launchd_inactive_registration_does_not_load_job(launchd):
    manager, version, data, calls, _job, _ = launchd
    manager.register(version, data, activate=False)
    assert manager.path.read_text() == render("darwin", version, data, manager.log, env_file=manager.env_file)
    assert calls == []
