from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

from astrid.installer import cli
from astrid.installer.backend import Backend
from astrid.installer.choices import RECOMMENDED_CHOICES
from runtime_protocol import platform


def test_prompt_accepts_recommended_setup_with_enter_and_y():
    for answer in ("", "Y"):
        output = io.StringIO()
        choices, declined = cli._prompt_choices(
            input_fn=iter([answer]).__next__, output=output)
        assert choices == RECOMMENDED_CHOICES
        assert declined is False
        assert "Continue with this setup? [Y/n/c]" in output.getvalue()
        assert "autostart: on" in output.getvalue()


def test_prompt_declines_without_choices():
    choices, declined = cli._prompt_choices(
        input_fn=iter(["n"]).__next__, output=io.StringIO())
    assert choices is None
    assert declined is True


def test_prompt_customizes_each_supported_choice():
    output = io.StringIO()
    choices, declined = cli._prompt_choices(
        input_fn=iter(["c", "n", "", "n", "y"]).__next__, output=output)
    assert choices == {
        "autostart": False,
        "command": True,
        "skills": False,
        "skill_copies": True,
    }
    assert declined is False
    assert output.getvalue().count("? [") == 5


def test_fresh_cli_custom_prompt_uses_common_choice_resolver(tmp_path, monkeypatch):
    root, data = tmp_path / "program", tmp_path / "workspace"
    seen = {}

    class FakeBackend:
        def __init__(self, *_args, **_kwargs):
            self.managed_by = None
            self.service = SimpleNamespace(recorded=lambda: {}, choices=None,
                                           selection=tmp_path / "selection.json")

        def source_checkout_ownership(self, *_args):
            return False

        def preflight_stop(self, _data):
            seen["choices"] = self.service.choices
            raise RuntimeError("test boundary")

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(platform, "log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(platform, "private_directory", lambda path: Path(path).mkdir(
        parents=True, exist_ok=True))
    monkeypatch.setattr(platform, "data_root", lambda: data)
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(cli, "Backend", FakeBackend)
    custom = {"autostart": False, "command": True, "skills": False, "skill_copies": True}
    monkeypatch.setattr(cli, "_prompt_choices", lambda: (custom, False))

    assert cli.main(["--root", str(root), "--data-root", str(data)]) == 1
    assert seen["choices"] == custom


def test_fresh_cli_decline_returns_before_setup_effects(tmp_path, monkeypatch, capsys):
    root, data = tmp_path / "program", tmp_path / "workspace"

    class FakeBackend:
        def __init__(self, *_args, **_kwargs):
            self.managed_by = None
            self.service = SimpleNamespace(recorded=lambda: {}, choices=None,
                                           selection=tmp_path / "selection.json")

        def preflight_stop(self, *_args):
            raise AssertionError("declining must happen before setup preflight")

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(platform, "log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(platform, "private_directory", lambda path: Path(path).mkdir(
        parents=True, exist_ok=True))
    monkeypatch.setattr(platform, "data_root", lambda: data)
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(cli, "Backend", FakeBackend)
    monkeypatch.setattr(cli, "_prompt_choices", lambda: (None, True))

    assert cli.main(["--root", str(root), "--data-root", str(data)]) == 0
    output = capsys.readouterr().out
    assert "Result: setup declined" in output
    assert "Way back:" in output
    assert not (root / "install.json").exists()
    assert not (tmp_path / "selection.json").exists()


def test_summary_reports_apply_outcomes_and_way_back(capsys):
    choices = {**RECOMMENDED_CHOICES, "autostart": False}
    cli._print_run_summary(choices, mode="install", outcomes={
        "startup": "registered; readiness not confirmed",
        "command": {"path": "/home/alice/.local/bin/astrid", "path_hint": "add PATH", "error": None},
        "skills": {"ok": True, "destinations": ["/home/alice/.claude/skills/astrid", "/home/alice/.codex/skills/astrid"]},
    })
    output = capsys.readouterr().out
    assert "Startup: registered; readiness not confirmed." in output
    assert "Command/PATH: shim created at /home/alice/.local/bin/astrid;" in output
    assert "Skill destinations: /home/alice/.claude/skills/astrid, /home/alice/.codex/skills/astrid" in output
    assert "--autostart off" in output
    assert "--skill-copies off" in output


def test_summary_does_not_claim_failed_or_unreached_work(capsys):
    cli._print_run_summary(RECOMMENDED_CHOICES, mode="install", outcomes={
        "startup": "registered; readiness not confirmed",
        "command": {"path": None, "path_hint": None, "error": "OSError"},
        "skills": {"ok": False, "destinations": []},
    }, failure=ValueError("failed safely"))
    output = capsys.readouterr().out
    assert "Command/PATH: setup failed (OSError)" in output
    assert "Skill destinations: sync failed" in output
    assert "Failure: ValueError: failed safely" in output
    assert "running and ready" not in output


def test_backend_skill_result_exposes_actual_targets(tmp_path, monkeypatch):
    backend = Backend(tmp_path / "program", tmp_path / "install.log", tmp_path / "uv")
    target = str(tmp_path / "agents" / "skills" / "astrid")
    monkeypatch.setattr(backend, "run", lambda *_args, **_kwargs: json.dumps({
        "actions": [{"harness": "claude", "steps": [{"target": target}]}],
    }))
    result = backend.sync_skills(type("Version", (), {
        "python": tmp_path / "python", "env": {},
    })())
    assert result == {"ok": True, "destinations": [target]}


def test_backend_skill_result_marks_subprocess_failure(tmp_path, monkeypatch):
    backend = Backend(tmp_path / "program", tmp_path / "install.log", tmp_path / "uv")
    monkeypatch.setattr(backend, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        subprocess.CalledProcessError(1, ["python"], output="sync failed")))
    result = backend.sync_skills(type("Version", (), {
        "python": tmp_path / "python", "env": {},
    })())
    assert result == {"ok": False, "destinations": []}
