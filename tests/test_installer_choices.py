"""Saved setup choice parsing, resolution and selection persistence."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from runtime_protocol import platform

from astrid.installer import cli
from astrid.installer.backend import Backend
from astrid.installer.choices import (
    CHOICE_NAMES,
    RECOMMENDED_CHOICES,
    parse_choice,
    resolve_choices,
    unattended_consent_required,
)
from astrid.installer.core import Version, read_json, write_json
from astrid.installer.service import LABEL, Service, install_selection


@pytest.mark.parametrize("value,expected", [("on", True), ("off", False)])
def test_documented_choice_values_parse(value, expected):
    assert parse_choice(value) is expected


@pytest.mark.parametrize("name", CHOICE_NAMES)
@pytest.mark.parametrize("value,expected", [("on", True), ("off", False)])
def test_each_public_flag_accepts_on_and_off(name, value, expected):
    flag = "--skill-copies" if name == "skill_copies" else f"--{name}"
    parsed = cli.parser().parse_args([flag, value])
    assert parsed.choices == {name: expected}


@pytest.mark.parametrize("argv,reason", [
    (["--unknown-choice", "on"], "unrecognized arguments"),
    (["--skills", "maybe"], "Expected 'on' or 'off'"),
    (["--skills", "on", "--skills", "off"], "may only be supplied once"),
])
def test_invalid_or_duplicate_choice_flags_fail_during_parsing(argv, reason, capsys):
    with pytest.raises(SystemExit):
        cli.parser().parse_args(argv)
    assert reason in capsys.readouterr().err


@pytest.mark.parametrize("explicit,saved,prompt,yes,expected", [
    ({"autostart": False}, {"autostart": True}, {"autostart": True}, True, False),
    ({"autostart": True}, {"autostart": False}, {"autostart": False}, False, True),
    (None, {"autostart": False}, {"autostart": True}, True, False),
    (None, {"autostart": True}, {"autostart": False}, False, True),
    (None, None, {"autostart": False}, True, False),
    (None, None, {"autostart": True}, False, True),
    (None, None, None, True, True),
    (None, None, None, False, True),
])
def test_precedence_resolves_true_and_false_at_each_edge(explicit, saved, prompt, yes, expected):
    result = resolve_choices(explicit=explicit, saved=saved, prompt=prompt,
                             accept_recommended=yes)
    assert result["autostart"] is expected


@pytest.mark.parametrize("saved", [
    {"unknown": True}, {"skills": "on"}, {"skills": 1},
])
def test_invalid_saved_values_are_rejected(saved):
    with pytest.raises(ValueError, match="choice|Unknown"):
        resolve_choices(saved=saved)


def test_legacy_install_resolves_all_effects_on_without_artifact_input():
    choices = resolve_choices(existing_install=True)
    assert choices == {
        "autostart": True,
        "command": True,
        "skills": True,
        "skill_copies": False,
    }
    assert resolve_choices(existing_install=True, explicit={"skills": False})["skills"] is False


def test_unattended_consent_only_blocks_fresh_without_yes_or_tty():
    assert unattended_consent_required(existing_install=False, accept_recommended=False,
                                       interactive=False)
    assert not unattended_consent_required(existing_install=False, accept_recommended=True,
                                           interactive=False)
    assert not unattended_consent_required(existing_install=True, accept_recommended=False,
                                           interactive=False)


def test_fresh_unattended_cli_prints_plan_before_system_writes(tmp_path, monkeypatch, capsys):
    root, data = tmp_path / "program", tmp_path / "workspace"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(platform, "log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(platform, "private_directory", lambda path: Path(path).mkdir(
        parents=True, exist_ok=True))
    monkeypatch.setattr(platform, "data_root", lambda: data)
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: False))

    result = cli.main(["--root", str(root), "--data-root", str(data)])

    output = capsys.readouterr().out
    assert result == 2
    assert "Astrid install plan" in output
    assert "autostart: on" in output
    assert not (root / "install.json").exists()
    assert not install_selection(tmp_path, cli.sys.platform).exists()


@pytest.mark.parametrize("yes,existing,saved", [
    (True, False, None),
    (False, True, {"autostart": False, "skills": False}),
])
def test_yes_or_saved_existing_choices_proceed_to_preflight(
        tmp_path, monkeypatch, yes, existing, saved):
    root, data = tmp_path / "program", tmp_path / "workspace"
    root.mkdir()
    record = ({"owner": LABEL, "program_root": str(root), "data_root": str(data),
               "choices": saved} if existing else {})
    observed = {}
    monkeypatch.setenv("HOME", str(tmp_path))
    selection = install_selection(tmp_path, cli.sys.platform)
    if record:
        write_json(selection, record)
    real_backend = cli.Backend

    def backend_factory(*args, **kwargs):
        backend = real_backend(*args, **kwargs)

        def preflight(_data):
            observed["choices"] = backend.service.choices
            raise RuntimeError("reached OS lifecycle boundary")

        backend.preflight_stop = preflight
        return backend

    monkeypatch.setattr(cli, "Backend", backend_factory)
    monkeypatch.setattr(platform, "log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(platform, "private_directory", lambda path: Path(path).mkdir(
        parents=True, exist_ok=True))
    monkeypatch.setattr(platform, "data_root", lambda: data)
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    args = ["--root", str(root), "--data-root", str(data)] + (["--yes"] if yes else [])

    assert cli.main(args) == 1
    assert observed["choices"] == (
        RECOMMENDED_CHOICES if yes else {
            "autostart": False, "command": True, "skills": False, "skill_copies": False,
        }
    )


def test_service_registration_persists_choices_and_preserves_selection_fields(tmp_path):
    root = tmp_path / "program"
    manager = Service(root, tmp_path / "install.log", Mock(return_value=""),
                      home=tmp_path, system="linux", say=lambda _line: None)
    version_root = root / "versions" / "candidate"
    version_root.mkdir(parents=True)
    version = Version(version_root, tmp_path / "python", version_root / "profile.json", {}, 4)
    write_json(manager.selection, {
        "owner": LABEL,
        "program_root": str(root),
        "data_root": str(tmp_path / "workspace"),
        "foreground": {"pid": 15, "birth_id": "kept"},
        "uninstall_receipt": {"state": "retained"},
        "unknown_field": {"keep": True},
    })
    manager.choices = {**RECOMMENDED_CHOICES, "command": False, "skills": False}

    manager.register(version, tmp_path / "workspace")
    record = read_json(manager.selection)

    assert record["choices"] == manager.choices
    assert record["foreground"] == {"pid": 15, "birth_id": "kept"}
    assert record["uninstall_receipt"] == {"state": "retained"}
    assert record["unknown_field"] == {"keep": True}
    assert record["identity"] == version.document()["identity"]


def test_maintenance_identity_write_preserves_choices_and_owned_fields(tmp_path):
    root = tmp_path / "program"
    backend = Backend(root, tmp_path / "install.log", tmp_path / "uv")
    version_root = root / "versions" / "candidate"
    version_root.mkdir(parents=True)
    version = Version(version_root, tmp_path / "python", version_root / "profile.json", {}, 7)
    choices = {**RECOMMENDED_CHOICES, "autostart": False}
    backend.service.choices = choices
    write_json(backend.service.selection, {
        "owner": LABEL,
        "program_root": str(root),
        "data_root": str(tmp_path / "workspace"),
        "foreground": {"pid": 42, "birth_id": "same-process"},
        "uninstall_receipt": {"state": "retained"},
        "unknown_field": ["retained"],
    })

    backend.persist_identity(version)
    record = read_json(backend.service.selection)

    assert record["choices"] == choices
    assert record["foreground"] == {"pid": 42, "birth_id": "same-process"}
    assert record["uninstall_receipt"] == {"state": "retained"}
    assert record["unknown_field"] == ["retained"]
    assert record["identity"] == version.document()["identity"]
