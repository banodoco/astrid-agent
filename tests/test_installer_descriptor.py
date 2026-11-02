"""Focused fixtures for the bounded read-only installer descriptor."""

from __future__ import annotations

import hashlib
import inspect
import json
from pathlib import Path

import pytest

from astrid.installer import descriptor
from astrid.installer.core import Version, write_json
from astrid.installer.service import LABEL, Service, install_selection, render


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {str(path.relative_to(root)): path.read_bytes() for path in root.rglob("*") if path.is_file()}


def _selected_installation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path, Path, Version, Service]:
    home, root, data = tmp_path / "home", tmp_path / "program", tmp_path / "data"
    home.mkdir(exist_ok=True)
    data.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    version_root = root / "versions" / "v1"
    python = version_root / "py" / "bin" / "python"
    profile = version_root / "profile.json"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python.chmod(0o700)
    profile.write_text("{}\n", encoding="utf-8")
    version = Version(version_root, python, profile, {}, release=36)
    write_json(version_root / "version.json", version.document())
    (root / "current").write_text("v1\n", encoding="utf-8")
    write_json(root / "install.json", {"owner": LABEL, "data_root": str(data)})
    selection = install_selection(home, "linux")
    selection.parent.mkdir(parents=True)
    write_json(selection, {
        "owner": LABEL,
        "program_root": str(root),
        "data_root": str(data),
        "identity": version.document()["identity"],
    })
    service = Service(root, root / "logs" / "install.log", lambda *_args, **_kwargs: "",
                      home=home, system="linux", say=lambda _line: None)
    write_json(service.env_file, {})
    service.path.parent.mkdir(parents=True)
    service.path.write_text(render("linux", version, data, service.log, env_file=service.env_file), encoding="utf-8")
    return home, root, data, version, service


def test_public_descriptor_accepts_no_caller_selected_targets():
    assert tuple(inspect.signature(descriptor.describe_current_user_installation).parameters) == ()


def test_descriptor_reads_only_selected_owned_installation(tmp_path, monkeypatch):
    home, root, data, version, service = _selected_installation(tmp_path, monkeypatch)
    before = _tree_bytes(tmp_path)

    value = descriptor._describe(home, "linux")

    assert value.program_root == str(root)
    assert value.data_root == str(data)
    assert value.selected_version_root == str(version.root)
    assert value.definition_path == str(service.path)
    assert value.definition_sha256 == hashlib.sha256(service.path.read_bytes()).hexdigest()
    assert value.action[0] == str(version.python)
    assert value.principal
    assert value.definition_owner == "product-installer"
    assert value.working_directory == ""
    assert value.removal_plan.operation == "product-uninstall"
    assert value.removal_plan.keep_data is True
    assert value.removal_plan.remove_paths == (str(root),)
    assert value.removal_plan.preserve_paths == (str(data),)
    assert value.removal_plan.expected_postconditions == (
        "owner-target-absent",
        "all-authorized-incarnations-absent",
        "no-owned-action-processes",
        "preserved-data-identity-unchanged",
    )
    assert _tree_bytes(tmp_path) == before


@pytest.mark.parametrize("mutation, message", [
    ("foreign-selection", "not a login-managed"),
    ("pinokio", "not a login-managed"),
    ("changed-definition", "foreign or changed"),
    ("pending-update", "pending update"),
    ("symlink-action", "unsupported or changed"),
])
def test_descriptor_refuses_untrusted_or_unstable_selection_before_emitting_a_removal_plan(
        tmp_path, monkeypatch, mutation, message):
    home, root, _data, version, service = _selected_installation(tmp_path, monkeypatch)
    selection = install_selection(home, "linux")
    if mutation == "foreign-selection":
        value = json.loads(selection.read_text(encoding="utf-8"))
        write_json(selection, {**value, "owner": "foreign-installer"})
    elif mutation == "pinokio":
        value = json.loads(selection.read_text(encoding="utf-8"))
        write_json(selection, {**value, "managed_by": "pinokio"})
    elif mutation == "changed-definition":
        service.path.write_bytes(service.path.read_bytes() + b"# changed\n")
    elif mutation == "pending-update":
        write_json(root / "update-transaction.json", {"step": "stopping"})
    else:
        version.python.unlink()
        version.python.symlink_to("/bin/sh")
    before = _tree_bytes(tmp_path)

    with pytest.raises(descriptor.InstallationDescriptorError, match=message):
        descriptor._describe(home, "linux")

    assert _tree_bytes(tmp_path) == before


def test_descriptor_rejects_an_overlapping_keep_data_plan(tmp_path, monkeypatch):
    home, root, _data, version, service = _selected_installation(tmp_path, monkeypatch)
    selection = install_selection(home, "linux")
    selection_value = json.loads(selection.read_text(encoding="utf-8"))
    write_json(selection, {**selection_value, "data_root": str(root)})
    write_json(root / "install.json", {"owner": LABEL, "data_root": str(root)})
    service.path.write_text(render("linux", version, root, service.log, env_file=service.env_file), encoding="utf-8")

    with pytest.raises(descriptor.InstallationDescriptorError, match="overlaps"):
        descriptor._describe(home, "linux")
