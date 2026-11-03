"""Focused coverage for the bounded current-user keep-data uninstall."""

from __future__ import annotations

import inspect
from pathlib import Path

import pytest

from astrid.installer import cli, keep_data
from astrid.installer.backend import Backend
from astrid.installer.core import read_json, write_json
from astrid.installer.descriptor import InstallationDescriptor, KeepDataRemovalPlan
from astrid.installer.service import LABEL


def _descriptor(root: Path, data: Path) -> InstallationDescriptor:
    return InstallationDescriptor(
        program_root=str(root),
        data_root=str(data),
        selected_version_root=str(root / "versions" / "v1"),
        definition_path=str(root / "astrid.service"),
        definition_sha256="0" * 64,
        action=("astrid-runtime",),
        principal="current-user",
        definition_owner="product-installer",
        working_directory="",
        removal_plan=KeepDataRemovalPlan(
            operation="product-uninstall",
            keep_data=True,
            remove_paths=(str(root),),
            preserve_paths=(str(data),),
            expected_postconditions=("preserved-data-identity-unchanged",),
        ),
    )


class _Service:
    def __init__(self, home: Path, selection: Path):
        self.home = home
        self.system = "linux"
        self.selection = selection

    def recorded(self) -> dict:
        return read_json(self.selection)


def _backend(root: Path, data: Path, home: Path, selection: Path, events: list[tuple]):
    """A real remove/preflight Backend with only manager actions replaced."""
    value = Backend.__new__(Backend)
    value.root = root
    value.uv = home / "unrelated-uv"
    value.service = _Service(home, selection)
    value.managed_by = None
    value.source_checkout_ownership = lambda *_args: False
    value.existing = lambda _data: None

    def preflight_stop(_data):
        events.append(("preflight-stop",))

    def stop(_old, _data):
        events.append(("stop",))

    def unregister():
        events.append(("unregister",))
        write_json(selection, {**read_json(selection), "uninstalled": True})

    value.preflight_stop = preflight_stop
    value.stop = stop
    value.unregister = unregister
    return value


def _install_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home, root, data = tmp_path / "home", tmp_path / "program", tmp_path / "data"
    home.mkdir(exist_ok=True)
    root.mkdir()
    data.mkdir()
    (root / "owned-program-file").write_text("owned", encoding="utf-8")
    sentinel = data / "unrelated-sentinel"
    sentinel.write_text("keep", encoding="utf-8")
    selection = home / ".local" / "share" / "astrid-install.json"
    write_json(root / "install.json", {"owner": LABEL, "data_root": str(data)})
    write_json(selection, {"owner": LABEL, "program_root": str(root), "data_root": str(data)})
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    monkeypatch.setattr("runtime_protocol.platform.log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(cli, "installer_lock_path", lambda *_args: tmp_path / "installer.lock")
    return home, root, data, selection, sentinel


def test_public_keep_data_uninstall_accepts_no_caller_targets():
    assert tuple(inspect.signature(keep_data.uninstall_current_user_keep_data).parameters) == ()


def test_keep_data_uninstall_binds_only_descriptor_roots(monkeypatch, tmp_path):
    root, data = tmp_path / "program", tmp_path / "data"
    value = _descriptor(root, data)
    calls = []
    monkeypatch.setattr(keep_data, "describe_current_user_installation", lambda: value)
    monkeypatch.setattr(keep_data.cli, "main", lambda argv: calls.append(argv) or 7)

    result = keep_data.uninstall_current_user_keep_data()

    assert calls == [["--uninstall", "--root", str(root), "--data-root", str(data)]]
    assert "--delete-data" not in calls[0]
    assert result == keep_data.KeepDataUninstallResult(
        exit_code=7, requested=True, keep_data=True, program_root=str(root), data_root=str(data)
    )


def test_keep_data_uninstall_refuses_rebound_login_selection(tmp_path, monkeypatch):
    home, root, data, selection, _sentinel = _install_fixture(tmp_path, monkeypatch)
    other_root = tmp_path / "other-program"
    other_root.mkdir()
    events: list[tuple] = []
    backend = _backend(root, data, home, selection, events)
    write_json(selection, {"owner": LABEL, "program_root": str(other_root), "data_root": str(data)})
    monkeypatch.setattr(keep_data, "describe_current_user_installation", lambda: _descriptor(root, data))
    monkeypatch.setattr(cli, "Backend", lambda *_args, **_kwargs: backend)

    result = keep_data.uninstall_current_user_keep_data()

    assert result.exit_code == 1
    assert events == []
    assert root.exists()


def test_keep_data_uninstall_holds_existing_lock_and_preserves_temp_data(tmp_path, monkeypatch):
    home, root, data, selection, sentinel = _install_fixture(tmp_path, monkeypatch)
    events: list[tuple] = []
    backend = _backend(root, data, home, selection, events)
    held = False

    def lock_file(_descriptor, *, blocking):
        nonlocal held
        assert blocking is False
        assert not held
        held = True

    def unlock_file(_descriptor):
        nonlocal held
        assert held
        held = False

    original_preflight = Backend.preflight_remove

    def preflight(actual_root, actual_data, *, delete_data):
        events.append(("preflight-remove", held, actual_root, actual_data, delete_data))
        return original_preflight(backend, actual_root, actual_data, delete_data=delete_data)

    backend.preflight_remove = preflight
    monkeypatch.setattr(keep_data, "describe_current_user_installation", lambda: _descriptor(root, data))
    monkeypatch.setattr(cli, "Backend", lambda *_args, **_kwargs: backend)
    monkeypatch.setattr("runtime_protocol.platform.lock_file", lock_file)
    monkeypatch.setattr("runtime_protocol.platform.unlock_file", unlock_file)
    monkeypatch.setattr("astrid.installer.backend.remove_shim", lambda _service: None)

    result = keep_data.uninstall_current_user_keep_data()

    assert result.exit_code == 0
    assert result.requested is True and result.keep_data is True
    assert not root.exists()
    assert read_json(selection)["uninstalled"] is True
    assert sentinel.read_text(encoding="utf-8") == "keep"
    assert [event[0] for event in events] == [
        "preflight-stop", "preflight-remove", "stop", "unregister", "preflight-remove",
    ]
    assert all(event[1] is True for event in events if event[0] == "preflight-remove")
    assert all(event[4] is False for event in events if event[0] == "preflight-remove")
    assert held is False


def test_keep_data_uninstall_refuses_real_overlap_preflight_before_stop(tmp_path, monkeypatch):
    home, root, data, selection, _sentinel = _install_fixture(tmp_path, monkeypatch)
    (data / "runtime").mkdir()
    (data / "runtime" / "catalog.json").write_text("{}", encoding="utf-8")
    events: list[tuple] = []
    backend = _backend(root, data, home, selection, events)
    backend.workspace = lambda *_args: {"realm_root": str(root / "attached")}
    original_preflight = Backend.preflight_remove

    def preflight(actual_root, actual_data, *, delete_data):
        events.append(("preflight-remove", delete_data))
        return original_preflight(backend, actual_root, actual_data, delete_data=delete_data)

    backend.preflight_remove = preflight
    monkeypatch.setattr(keep_data, "describe_current_user_installation", lambda: _descriptor(root, data))
    monkeypatch.setattr(cli, "Backend", lambda *_args, **_kwargs: backend)
    monkeypatch.setattr("astrid.installer.backend.remove_shim", lambda _service: None)

    result = keep_data.uninstall_current_user_keep_data()

    assert result.exit_code == 1
    assert events == [("preflight-stop",), ("preflight-remove", False)]
    assert root.exists()
    assert read_json(selection).get("uninstalled") is not True
