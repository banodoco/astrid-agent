from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrid.installer import cli
from astrid.installer.choices import (
    CHOICES_KEY,
    DEFAULT_CHOICES,
    is_existing_install,
    resolve_choices,
    save_choices,
)
from astrid.installer.core import Version, persist_install_identity, read_json
from astrid.installer.service import LABEL, default_program_root, install_selection


def test_choice_precedence_is_flag_then_saved_then_selection_then_default():
    defaults = resolve_choices()
    assert defaults == DEFAULT_CHOICES

    selected = resolve_choices(selected={"autostart": False, "skills": False})
    assert selected["autostart"] is False
    assert selected["skills"] is False
    assert selected["terminal_command"] is True

    saved = resolve_choices(
        saved={"autostart": True, "terminal_command": False, "skills_mechanism": "copy"},
        selected={"autostart": False, "terminal_command": True, "skills": False},
    )
    assert saved == {
        "autostart": True,
        "terminal_command": False,
        "skills": False,
        "skills_mechanism": "copy",
    }

    explicit = resolve_choices(
        explicit={"autostart": False, "terminal_command": True, "skills_mechanism": "symlink"},
        saved={"autostart": True, "terminal_command": False, "skills": False,
               "skills_mechanism": "copy"},
        selected={"autostart": True, "skills": True},
    )
    assert explicit == {
        "autostart": False,
        "terminal_command": True,
        "skills": False,
        "skills_mechanism": "symlink",
    }


@pytest.mark.parametrize("overrides,expected", [
    ({}, DEFAULT_CHOICES),
    ({"saved": {"skills": False}}, {**DEFAULT_CHOICES, "skills": False}),
    ({"explicit": {"terminal_command": False}}, {**DEFAULT_CHOICES, "terminal_command": False}),
])
def test_existing_install_defaults_all_on_except_explicit_saved_or_flag(overrides, expected):
    assert resolve_choices(existing_install=True, **overrides) == expected


def test_choice_merge_and_installer_identity_writer_preserve_unknown_state(tmp_path):
    selection = tmp_path / "astrid-install.json"
    selection.write_text(json.dumps({
        "owner": LABEL,
        "future_installer_field": {"keep": True},
        CHOICES_KEY: {"future_choice": "keep-too", "skills": False},
    }), encoding="utf-8")

    save_choices(selection, {"autostart": False, "skills_mechanism": "copy"})
    version = Version(
        root=tmp_path / "versions" / "v1",
        python=tmp_path / "versions" / "v1" / "bin" / "python",
        profile=tmp_path / "versions" / "v1" / "profile.json",
        env={},
        release=3,
    )
    persist_install_identity(version, tmp_path / "program", selection=selection,
                             fields={"data_root": str(tmp_path / "data")})

    record = read_json(selection)
    assert record["future_installer_field"] == {"keep": True}
    assert record[CHOICES_KEY] == {
        "future_choice": "keep-too",
        "skills": False,
        "autostart": False,
        "skills_mechanism": "copy",
    }
    assert record["identity"] == {"release": 3, "managed_by": None}
    assert record["data_root"] == str(tmp_path / "data")


def test_existing_detection_uses_only_explicit_install_identity(tmp_path, monkeypatch):
    root = tmp_path / "program"
    assert not is_existing_install({}, {}, root)
    # Artifacts and empty service state do not imply an existing install.
    root.mkdir()
    (root / "astrid").mkdir()
    assert not is_existing_install({}, {"identity": {}}, root)
    assert is_existing_install({"owner": LABEL}, {}, root)
    assert is_existing_install({}, {"owner": LABEL, "program_root": str(root)}, root)
    assert not is_existing_install({}, {"owner": LABEL, "program_root": str(tmp_path / "other")}, root)
    assert not is_existing_install({}, {"owner": LABEL, "uninstalled": True}, root)
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    default_root = default_program_root(home, sys.platform)
    assert is_existing_install({}, {"owner": LABEL}, default_root)
    assert not is_existing_install({}, {"owner": LABEL}, root)


def test_fresh_unattended_cli_prints_plan_and_stops_before_backend(tmp_path, monkeypatch, capsys):
    home = tmp_path / "home"
    root = tmp_path / "program"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "user-data"))
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(cli, "Backend", lambda *_args, **_kwargs: pytest.fail(
        "fresh unattended consent must stop before backend/service operations"))

    result = cli.main(["--root", str(root), "--no-autostart", "--no-skills",
                       "--skills-mechanism", "copy"])

    assert result == 2
    output = capsys.readouterr().out
    assert "Astrid setup plan:" in output
    assert "Start at sign-in: no" in output
    assert "Install the astrid command: yes" in output
    assert "Install coding-agent skills: no" in output
    assert "rerun with --yes" in output
    assert not root.exists()
    assert not install_selection(home, sys.platform).exists()


def test_yes_accepts_without_prompt_and_persists_resolved_flags(tmp_path, monkeypatch):
    home, root, data = tmp_path / "home", tmp_path / "program", tmp_path / "workspace"
    manifest = tmp_path / "manifest.json"
    manifest.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "user-data"))
    selection = install_selection(home, sys.platform)
    from runtime_protocol import platform

    monkeypatch.setattr(platform, "private_directory", lambda path: Path(path).mkdir(
        parents=True, exist_ok=True))
    monkeypatch.setattr(platform, "data_root", lambda: data)
    monkeypatch.setattr(platform, "log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(platform, "lock_file", lambda fd, blocking=False: None)
    monkeypatch.setattr(platform, "unlock_file", lambda _fd: None)
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(cli, "default_uv", lambda: tmp_path / "uv")
    monkeypatch.setattr(cli, "validate_manifest", lambda _value: {"release": 1})

    class FakeBackend:
        def __init__(self, *_args, **_kwargs):
            self.managed_by = None
            self.service = SimpleNamespace(selection=selection, recorded=lambda: {})

        def source_checkout_ownership(self, *_args):
            return False

        def preflight_stop(self, *_args):
            return None

    ran = []

    class FakeInstaller:
        def __init__(self, *_args, **_kwargs):
            pass

        def run(self, *_args, **_kwargs):
            ran.append(True)

    monkeypatch.setattr(cli, "Backend", FakeBackend)
    monkeypatch.setattr(cli, "Installer", FakeInstaller)

    assert cli.main(["--yes", "--no-skills", "--root", str(root), "--data-root", str(data),
                     "--manifest", str(manifest), "--uv", str(tmp_path / "uv")]) == 0
    assert ran == [True]
    assert read_json(selection)[CHOICES_KEY] == {
        "autostart": True,
        "terminal_command": True,
        "skills": False,
        "skills_mechanism": "symlink",
    }
