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
