"""Read-only systemd observations are not lifecycle transfer authority."""

import subprocess
from unittest.mock import Mock

import pytest

from astrid.installer import service
from astrid.installer.core import write_json


@pytest.fixture
def observed(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    manager = service.Service(tmp_path / "program", tmp_path / "log", Mock(),
                              home=tmp_path, system="linux")
    data = tmp_path / "data"
    write_json(manager.selection, {"owner": service.LABEL,
                                  "program_root": str(manager.root), "data_root": str(data)})
    manager.path.parent.mkdir(parents=True, exist_ok=True)
    manager.path.write_text("[Service]\nExecStart=/selected\n")
    dropin = manager.path.parent / "astrid.service.d" / "override.conf"
    dropin.parent.mkdir()
    dropin.write_text("[Service]\nRestart=no\n")
    monkeypatch.setattr(service, "_typed_environment_files", lambda: ())
    fields = dict.fromkeys(service._TRANSFER_UNIT_PROPERTIES, "")
    fields.update(Id="astrid.service", LoadState="loaded", FragmentPath=str(manager.path),
                  DropInPaths=str(dropin), ExecStart="{ path=/selected ; argv[]=/selected ; }",
                  User="1001", Environment="SECRET=private", NeedDaemonReload="no")
    def show():
        return "\n".join(f"{key}={value}" for key, value in fields.items())
    monkeypatch.setattr(service, "_transfer_systemctl_show", show)
    return manager, data, dropin, fields


def test_observation_captures_effective_action_principal_and_all_files_without_authority(observed):
    manager, data, dropin, fields = observed
    result = manager._observe_systemd_transfer_definition(data)
    assert dict(result.properties)["ExecStart"] == fields["ExecStart"]
    assert dict(result.properties)["User"] == "1001"
    assert [entry.path for entry in result.files] == [str(manager.path), str(dropin)]
    assert result.files[1].content == dropin.read_bytes()
    assert "private" not in repr(result)
    with pytest.raises(RuntimeError, match="unsupported"):
        manager._prepare_lifecycle_transfer()
    manager.run.assert_not_called()


def test_transfer_observation_binds_real_uid_when_effective_uid_differs(observed, monkeypatch):
    manager, data, _, _ = observed
    monkeypatch.setattr(service, "owned_by_current_user", lambda *_: True)
    monkeypatch.setattr(service.platform, "current_user", lambda: 101)
    monkeypatch.setattr(service.os, "geteuid", lambda: 202)
    assert manager._observe_systemd_transfer_definition(data).uid == 101
    with pytest.raises(RuntimeError, match="unsupported"):
        manager._prepare_lifecycle_transfer()
    manager.run.assert_not_called()


@pytest.mark.parametrize("unit_file_state", ["enabled", "disabled", "static"])
def test_stopped_observation_preserves_state_but_cannot_authorize_transfer(observed, unit_file_state):
    manager, data, _dropin, fields = observed
    fields.update(ActiveState="inactive", SubState="dead", MainPID="0", ControlPID="0",
                  Job="", UnitFileState=unit_file_state)
    result = manager._observe_stopped_systemd_transfer_definition(data)
    assert dict(result.properties)["UnitFileState"] == unit_file_state
    with pytest.raises(RuntimeError, match="historical containment"):
        manager._prepare_lifecycle_transfer()
    manager.run.assert_not_called()


@pytest.mark.parametrize("key,value", [
    ("ActiveState", "active"), ("SubState", "failed"), ("MainPID", "123"),
    ("ControlPID", "456"), ("Job", "42"), ("UnitFileState", "masked"),
    ("UnitFileState", ""),
])
def test_stopped_observation_refuses_live_pending_or_unknown_state(observed, key, value):
    manager, data, _dropin, fields = observed
    fields.update(ActiveState="inactive", SubState="dead", MainPID="0", ControlPID="0",
                  Job="", UnitFileState="enabled")
    fields[key] = value
    with pytest.raises(ValueError, match="supported stopped state"):
        manager._observe_stopped_systemd_transfer_definition(data)
    manager.run.assert_not_called()


@pytest.mark.parametrize("key,value", [
    ("Id", "other.service"), ("LoadState", "not-found"),
    ("FragmentPath", "/other/astrid.service"), ("ExecStart", ""),
    ("NeedDaemonReload", "yes"), ("DropInPaths", r"/path/with\\x20spaces.conf"),
])
def test_unknown_definition_refuses(observed, key, value):
    manager, data, _dropin, fields = observed
    fields[key] = value
    with pytest.raises(ValueError):
        manager._observe_systemd_transfer_definition(data)
    manager.run.assert_not_called()


@pytest.mark.parametrize("change", ["principal", "invocation", "file", "selection"])
def test_replacement_during_collection_refuses(observed, monkeypatch, change):
    manager, data, dropin, fields = observed
    original = service._transfer_systemctl_show
    calls = 0
    def show():
        nonlocal calls
        calls += 1
        if calls == 2:
            if change == "principal":
                fields["User"] = "1002"
            elif change == "invocation":
                fields["ExecStart"] = "{ path=/replacement ; }"
            elif change == "file":
                dropin.write_text("[Service]\nRestart=always\n")
            else:
                write_json(manager.selection, {"owner": service.LABEL,
                                              "program_root": str(manager.root), "data_root": "/replacement"})
        return original()
    monkeypatch.setattr(service, "_transfer_systemctl_show", show)
    with pytest.raises(ValueError):
        manager._observe_systemd_transfer_definition(data)
    manager.run.assert_not_called()


def test_symlink_file_and_incomplete_or_failed_manager_query_refuse(observed, monkeypatch):
    manager, data, dropin, fields = observed
    dropin.unlink()
    dropin.symlink_to(manager.path)
    with pytest.raises(OSError):
        manager._observe_systemd_transfer_definition(data)
    dropin.unlink()
    dropin.write_text("[Service]\nRestart=no\n")
    del fields["ControlGroup"]
    with pytest.raises(ValueError, match="incomplete"):
        manager._observe_systemd_transfer_definition(data)
    monkeypatch.setattr(service, "_transfer_systemctl_show", Mock(side_effect=subprocess.CalledProcessError(1, "systemctl")))
    with pytest.raises(subprocess.CalledProcessError):
        manager._observe_systemd_transfer_definition(data)
    manager.run.assert_not_called()


@pytest.mark.parametrize("system", ["darwin", "win32", "freebsd"])
def test_other_managers_explicitly_refuse_transfer(tmp_path, system):
    manager = service.Service(tmp_path, tmp_path / "log", Mock(), home=tmp_path, system=system)
    with pytest.raises(RuntimeError, match="unsupported"):
        manager._observe_systemd_transfer_definition(tmp_path)
    with pytest.raises(RuntimeError, match="unsupported"):
        manager._prepare_lifecycle_transfer()
    manager.run.assert_not_called()


def test_typed_environment_is_private_and_changes_refuse(observed, monkeypatch, caplog):
    manager, data, _, _ = observed
    entries = (("/secret/*", True), ("/secret/*", False))
    monkeypatch.setattr(service, "_typed_environment_files", lambda: entries)
    result = manager._observe_systemd_transfer_definition(data)
    assert result.environment_files == entries
    assert "/secret" not in repr(result) and "/secret" not in caplog.text
    monkeypatch.setattr(service, "_typed_environment_files", Mock(side_effect=[(), entries]))
    with pytest.raises(ValueError, match="changed"):
        manager._observe_systemd_transfer_definition(data)
    monkeypatch.setattr(service, "_typed_environment_files", Mock(side_effect=ValueError("unsupported")))
    with pytest.raises(ValueError, match="unsupported"):
        manager._observe_systemd_transfer_definition(data)
    with pytest.raises(RuntimeError):
        manager._prepare_lifecycle_transfer()
    manager.run.assert_not_called()
