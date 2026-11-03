"""The R0 checkpoint is diagnostic only and reports independent unknowns."""
import json
import os
from unittest.mock import Mock

import pytest

from astrid.installer import service
from astrid.installer.core import Version, write_json


@pytest.fixture
def snapshot(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    manager = service.Service(tmp_path / "program", tmp_path / "log", Mock(), home=tmp_path, system="linux")
    data = tmp_path / "data"
    root = manager.root / "versions" / "r0"
    version = Version(root, root / "py/bin/python", root / "profile.json", {"FOO": "bar"})
    write_json(manager.selection, {"owner": service.LABEL, "program_root": str(manager.root), "data_root": str(data)})
    root.mkdir(parents=True, exist_ok=True)
    write_json(root / "version.json", version.document())
    (manager.root / "current").write_text("r0\n")
    manager.path.parent.mkdir(parents=True, exist_ok=True)
    manager.path.write_bytes(service._r0_systemd_fragment(version, data, manager.log, manager.env_file))
    fields = dict.fromkeys(service._TRANSFER_UNIT_PROPERTIES, "")
    fields.update(Id="astrid.service", LoadState="loaded", FragmentPath=str(manager.path),
                  ExecStart="{ path=/python ; argv[]=/python -c line1\nline2 ; }",
                  NeedDaemonReload="no", ActiveState="inactive", SubState="dead",
                  MainPID="0", ControlPID="0", UnitFileState="enabled", ControlGroup="/test/astrid.service")
    monkeypatch.setattr(service, "_transfer_systemctl_show", lambda: "\n".join(f"{k}={v}" for k, v in fields.items()))
    monkeypatch.setattr(service, "_snapshot_proc_census", lambda *_: ((), True, "complete scan"))
    return manager, version, data, fields


def values(result):
    return {name: predicate.result for name, predicate in result.predicates.items()}


def test_exact_binding_is_stable_and_non_authoritative(snapshot):
    manager, version, data, _ = snapshot
    first = manager._snapshot_linux_r0(version, data, known_children=())
    second = manager._snapshot_linux_r0(version, data, known_children=())
    assert first.binding_digest == second.binding_digest
    assert first.started_ns <= first.ended_ns
    assert first.authority is False
    assert all(values(first)[key] == "match" for key in ("selection", "current", "version_record", "fragment", "sample_stability"))
    assert all(values(first)[key] == "unknown" for key in ("manager_identity", "effective_action", "effective_principal", "loaded_disk_equivalence", "historical_containment", "current_descendant_completeness"))
    assert values(first)["selected_processes"] == "unknown"
    assert values(first)["known_children"] == "unknown"
    manager.run.assert_not_called()
    with pytest.raises(RuntimeError):
        manager._prepare_lifecycle_transfer()


def test_foreign_and_replaced_definition(snapshot, monkeypatch):
    manager, version, data, fields = snapshot
    original_binding = manager._snapshot_linux_r0(version, data).binding_digest
    manager.path.write_text("[Service]\nExecStart=/foreign\n")
    fields["FragmentPath"] = "/foreign/astrid.service"
    result = manager._snapshot_linux_r0(version, data)
    assert result.binding_digest != original_binding
    assert values(result)["fragment"] == values(result)["unit_target"] == "mismatch"
    original = service._transfer_systemctl_show
    calls = 0
    def changing():
        nonlocal calls
        calls += 1
        if calls == 2:
            manager.path.write_text("replacement")
            fields["MainPID"] = "42"
        return original()
    monkeypatch.setattr(service, "_transfer_systemctl_show", changing)
    assert values(manager._snapshot_linux_r0(version, data))["sample_stability"] == "unknown"


def test_unreadable_census_is_unknown_independently(snapshot, monkeypatch):
    manager, version, data, _ = snapshot
    monkeypatch.setattr(service, "_snapshot_proc_census", lambda *_: ((), False, "unreadable"))
    result = values(manager._snapshot_linux_r0(version, data, known_children=()))
    assert result["selected_processes"] == result["cgroup_members"] == result["known_children"] == "unknown"
    assert result["fragment"] == result["active_state"] == "match"


def test_live_or_unknown_child_never_yields_authority(snapshot, monkeypatch):
    manager, version, data, _ = snapshot
    monkeypatch.setattr(service, "_snapshot_proc_census", lambda *_: (((123, 45, 1000, 1, "known-pid"),), True, "complete scan"))
    result = manager._snapshot_linux_r0(version, data, known_children=((123, 45),))
    assert values(result)["known_children"] == "present"
    assert not result.authority
    monkeypatch.setattr(service, "_snapshot_proc_census", lambda *_: ((), True, "complete scan"))
    assert values(manager._snapshot_linux_r0(version, data))["known_children"] == "unknown"


def test_inactive_and_running_unit_are_independently_reported(snapshot):
    manager, version, data, fields = snapshot
    assert values(manager._snapshot_linux_r0(version, data))["active_state"] == "match"
    fields.update(ActiveState="active", SubState="running", MainPID="3256")
    result = values(manager._snapshot_linux_r0(version, data))
    assert result["active_state"] == result["sub_state"] == result["main_pid"] == "mismatch"
    assert result["control_pid"] == "match"


def test_proc_census_detects_live_reused_and_unreadable(tmp_path):
    def process(pid, ticks, cmd, cgroup):
        p = tmp_path / str(pid)
        p.mkdir()
        (p / "stat").write_text(f"{pid} (a name) S " + "0 " * 18 + f"{ticks} 0")
        (p / "status").write_text("Uid:\t1000\t1000\t1000\t1000\n")
        (p / "cmdline").write_bytes(cmd)
        (p / "cgroup").write_text("0::" + cgroup + "\n")
    process(10, 9, b"/selected/r0/py\0", "/unit/child")
    candidates, complete, _ = service._snapshot_proc_census(tmp_path / "unused", "/unit", ((10, 9),), tmp_path)
    assert complete and candidates == ((10, 9, 1000, 0, "unit-cgroup,known-pid"),)
    _, complete, _ = service._snapshot_proc_census(tmp_path / "unused", "/unit", ((10, 8),), tmp_path)
    assert not complete
    (tmp_path / "10" / "cmdline").unlink()
    _, complete, _ = service._snapshot_proc_census(tmp_path / "unused", "/unit", (), tmp_path)
    assert not complete


def test_frozen_r0_fragment_contains_exact_legacy_action_and_policy(snapshot):
    manager, version, data, _ = snapshot
    fragment = manager.path.read_text()
    script = ("import json,os,sys\nfrom pathlib import Path\n"
              f"path=Path({str(manager.env_file)!r})\n"
              "settings=json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}\n"
              "if (not isinstance(settings,dict) or set(settings)-set(['ASTRID_ALLOW_UNQUALIFIED_GPU', 'ASTRID_WARM_IDLE_SECONDS']) or any(not isinstance(value,str) for value in settings.values())):\n"
              "    raise ValueError('Invalid Astrid service-env.json; use the documented string settings.')\n"
              "os.environ.update(settings)\nfrom astrid_runtime.cli import main\n"
              f"sys.exit(main(['run', '--profile', 'astrid', '--data-root', {str(data)!r}, '--source-manifest', {str(version.profile)!r}]))")
    expected = " ".join(service.systemd_quote(x).replace("$", "$$") for x in
                        [str(version.python), "-X", "utf8", "-I", "-c", script])
    assert f"ExecStart={expected}\n" in fragment
    assert 'Environment="FOO=bar"\nRestart=always\nRestartSec=5\nKillMode=mixed\n' in fragment
    assert "ASTRID_INSTALL_RELEASE" not in fragment


def test_dropin_and_unreadable_manager_are_independent(snapshot, monkeypatch):
    manager, version, data, fields = snapshot
    dropin = manager.path.parent / "astrid.service.d" / "override.conf"
    dropin.parent.mkdir()
    dropin.write_text("[Service]\nRestart=no\n")
    fields["DropInPaths"] = str(dropin)
    result = values(manager._snapshot_linux_r0(version, data))
    assert result["dropins"] == "present" and result["dropin_files"] == "match"
    assert result["effective_action"] == "unknown"
    dropin.unlink()
    assert values(manager._snapshot_linux_r0(version, data))["dropin_files"] == "unknown"
    monkeypatch.setattr(service, "_transfer_systemctl_show", Mock(side_effect=OSError("no bus")))
    result = values(manager._snapshot_linux_r0(version, data))
    assert result["fragment"] == "match" and result["unit_target"] == "unknown"
    assert result["active_state"] == "unknown" and result["sample_stability"] == "unknown"
