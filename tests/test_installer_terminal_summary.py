from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from runtime_protocol import platform

from astrid.installer import cli
from astrid.installer.backend import Backend
from astrid.installer.choices import RECOMMENDED_CHOICES
from astrid.installer.core import Installer
from astrid.installer.service import install_selection


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
        input_fn=iter(["c", "", "", "", ""]).__next__, output=output)
    assert choices == {name: RECOMMENDED_CHOICES[name] for name in RECOMMENDED_CHOICES}
    assert declined is False
    assert output.getvalue().count("? [") == 5
    assert output.getvalue().endswith("Use copies for skills? [y/N]\n")


def test_prompt_customizes_explicit_yes_and_no_choices():
    output = io.StringIO()
    choices, declined = cli._prompt_choices(
        input_fn=iter(["c", "y", "n", "y", "n"]).__next__, output=output)
    assert choices == {
        "autostart": True,
        "command": False,
        "skills": True,
        "skill_copies": False,
    }
    assert declined is False


def test_fresh_cli_custom_prompt_uses_common_choice_resolver(tmp_path, monkeypatch):
    root, data = tmp_path / "program", tmp_path / "workspace"
    seen = {}

    class FakeBackend:
        def __init__(self, *_args, **_kwargs):
            self.managed_by = None
            self.service = SimpleNamespace(recorded=lambda: {}, choices=None,
                                           selection=tmp_path / "selection.json",
                                           home=tmp_path, system="linux", path=tmp_path / "astrid.service")

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
                                           selection=tmp_path / "selection.json",
                                           home=tmp_path, system="linux", path=tmp_path / "astrid.service")

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

    assert cli.main(["--root", str(root), "--data-root", str(data)]) == 3
    output = capsys.readouterr().out
    assert "Astrid was NOT installed." in output
    assert "Why: you declined the setup, so nothing was changed." in output
    assert "Way back:" not in output  # the setup summary is in install.log, not the terminal
    assert "Way back:" in (tmp_path / "logs" / "install.log").read_text(encoding="utf-8")
    assert not (root / "install.json").exists()
    assert not (tmp_path / "selection.json").exists()


def test_fresh_unattended_cli_prints_plan_and_stops_before_install_writes(tmp_path, monkeypatch, capsys):
    root, data = tmp_path / "program", tmp_path / "workspace"
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(platform, "log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(platform, "private_directory", lambda path: Path(path).mkdir(
        parents=True, exist_ok=True))
    monkeypatch.setattr(platform, "data_root", lambda: data)
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: False))

    result = cli.main(["--root", str(root), "--data-root", str(data), "--command", "off"])

    output = capsys.readouterr().out
    assert result == 2
    assert "Astrid install plan" in output
    assert "autostart: on" in output
    assert "command: off" in output
    assert "Why: setup needs your consent; run the exact rerun command shown above." in output
    assert "Absolute Astrid CLI path" not in output
    assert not (root / "install.json").exists()
    assert not install_selection(tmp_path, cli.sys.platform).exists()


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
    assert "Failure: ValueError" in output
    assert "failed safely" not in output
    assert "running and ready" not in output


@pytest.mark.parametrize(("observed", "expected"), [
    (True, "registered"), (False, "not registered"), (None, "not confirmed"),
])
def test_summary_separates_observed_autostart_from_runtime_readiness(observed, expected, capsys):
    cli._print_run_summary(RECOMMENDED_CHOICES, mode="install", outcomes={
        "startup": "running and ready", "autostart_registered": observed,
    })
    output = capsys.readouterr().out
    assert "Startup: running and ready." in output
    assert f"Sign-in autostart: {expected}." in output


@pytest.mark.parametrize("observed", [True, False, None])
def test_activation_observes_registration_without_failing_on_observation_error(tmp_path, observed):
    calls = []

    def observe():
        calls.append("observe")
        if observed is None:
            raise OSError("observation unavailable")
        return observed

    backend = SimpleNamespace(
        service=SimpleNamespace(login_service_exists=observe),
        register=lambda *_: calls.append("register"),
        start=lambda *_: calls.append("start"),
        verify=lambda *_: calls.append("verify"),
    )
    installer = Installer(tmp_path / "root", tmp_path / "data", backend)
    installer._activate(object(), "realm")
    assert calls == ["register", "observe", "start", "verify"]
    assert installer.outcomes["autostart_registered"] is observed


def test_summary_preserves_pinokio_startup_wording(capsys):
    cli._print_run_summary(RECOMMENDED_CHOICES, mode="install", outcomes={
        "startup": "Pinokio owns startup",
    })
    output = capsys.readouterr().out
    assert "Startup: Pinokio owns startup." in output
    assert "Sign-in autostart: not confirmed." in output


def test_summary_reports_command_off_effects_and_keeps_way_back(capsys):
    choices = {**RECOMMENDED_CHOICES, "command": False}
    cli._print_run_summary(choices, mode="install", outcomes={
        "startup": "registered; readiness not confirmed",
        "command": {
            "action": "disabled", "path": None, "path_hint": None, "error": None,
            "removed_shim": True, "removed_path": True,
        },
        "skills": {"ok": True, "destinations": []},
    })
    output = capsys.readouterr().out
    assert "Command/PATH: off; Astrid's command shim removed, Astrid's PATH entry removed." in output
    assert "Way back:" in output and "--command off" in output


def test_summary_explains_all_off_choices_and_recovery_paths(tmp_path, capsys):
    choices = {name: False for name in RECOMMENDED_CHOICES}
    install_root = tmp_path / "Astrid"
    install_root.mkdir()
    (install_root / "run-astrid").touch()
    cli._print_run_summary(choices, mode="update", install_root=install_root)
    output = capsys.readouterr().out

    assert "Startup recovery:" in output and "Astrid will not start at sign-in" in output
    assert "--autostart off with --autostart on" in output
    assert "Command recovery:" in output and "--command off with --command on" in output
    assert "Absolute Astrid CLI path while the command is off: " + str(tmp_path / "Astrid" / "run-astrid") in output
    assert "Skills recovery:" in output and "--skills on" in output
    assert "Skill copies recovery:" in output and "--skill-copies on" in output
    assert "rerun the install line to repair or restart" in output
    assert "add --update to update" in output and "--uninstall" in output
    assert "--delete-data" in output
    assert ("Way back: curl -fsSL https://astrid.haus/install | sh -s -- --yes --update "
            "--autostart off --command off --skills off --skill-copies off") in output


def test_summary_uses_absolute_windows_cli_path_when_command_is_off(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "platform", "win32")
    install_root = tmp_path / "Astrid"
    install_root.mkdir()
    (install_root / "run-astrid.cmd").touch()
    cli._print_run_summary({**RECOMMENDED_CHOICES, "command": False}, mode="install",
                           install_root=install_root)
    output = capsys.readouterr().out
    assert "Absolute Astrid CLI path while the command is off: " + str(tmp_path / "Astrid" / "run-astrid.cmd") in output


def test_summary_reports_added_and_absent_windows_path_truthfully(capsys):
    cli._print_run_summary(RECOMMENDED_CHOICES, mode="install", outcomes={
        "startup": "registered; readiness not confirmed",
        "command": {
            "action": "ready", "path": r"C:\\Astrid\\astrid.cmd", "path_hint": None,
            "error": None, "path_added": r"C:\\Astrid", "path_present": True,
            "shim_change": "created",
        },
        "skills": {"ok": True, "destinations": []},
    })
    output = capsys.readouterr().out
    assert "user PATH entry added" in output
    assert "Way back:" in output and "--command on" in output


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


def test_backend_skill_sync_passes_saved_skills_and_copy_policy(tmp_path, monkeypatch):
    backend = Backend(tmp_path / "program", tmp_path / "install.log", tmp_path / "uv")
    backend.service.choices = {"skills": False, "skill_copies": True}
    calls = []
    monkeypatch.setattr(backend, "run", lambda argv, **_kwargs: calls.append(argv) or '{"actions": []}')
    backend.sync_skills(type("Version", (), {"python": tmp_path / "python", "env": {}})())
    assert calls[0][-4:] == ["--skills", "off", "--skill-copies", "on"]


def test_backend_reports_preserved_skill_conflicts(tmp_path, monkeypatch):
    backend = Backend(tmp_path / "program", tmp_path / "install.log", tmp_path / "uv")
    monkeypatch.setattr(backend, "run", lambda *_args, **_kwargs: json.dumps({
        "actions": [{"harness": "claude", "removed": ["/agents/astrid-owned"]}],
        "conflicts": [{"path": "/agents/astrid-copy", "reason": "edited copy"}],
    }))
    result = backend.sync_skills(type("Version", (), {"python": tmp_path / "python", "env": {}})())
    assert result["conflicts"] == [{"path": "/agents/astrid-copy", "reason": "edited copy"}]
    assert result["removed"] == ["/agents/astrid-owned"]
    assert "edited copy" in (tmp_path / "install.log").read_text(encoding="utf-8")


def test_backend_skill_result_marks_subprocess_failure(tmp_path, monkeypatch):
    backend = Backend(tmp_path / "program", tmp_path / "install.log", tmp_path / "uv")
    monkeypatch.setattr(backend, "run", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        subprocess.CalledProcessError(1, ["python"], output="sync failed")))
    result = backend.sync_skills(type("Version", (), {
        "python": tmp_path / "python", "env": {},
    })())
    assert result == {"ok": False, "destinations": []}


# Card 2 (Plan 29 R11): point to detail, never dump it. The receipt's outcome lines
# are printed last; the setup summary and the failure's JSON details go to install.log.

def _failing_cli(tmp_path, monkeypatch, custom=None, failure=None):
    from astrid.installer.core import RuntimeOperationError

    root, data = tmp_path / "program", tmp_path / "workspace"
    failure = failure or RuntimeOperationError(
        "The Runtime refused to stop.", details={"safe": True, "probe": "secret-detail-xyz"})

    class FakeBackend:
        def __init__(self, *_args, **_kwargs):
            self.managed_by = None
            self.service = SimpleNamespace(recorded=lambda: {}, choices=None,
                                           selection=tmp_path / "selection.json",
                                           home=tmp_path, system="linux", path=tmp_path / "astrid.service")

        def source_checkout_ownership(self, *_args):
            return False

        def preflight_stop(self, _data):
            raise failure

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(platform, "log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(platform, "private_directory", lambda path: Path(path).mkdir(
        parents=True, exist_ok=True))
    monkeypatch.setattr(platform, "data_root", lambda: data)
    monkeypatch.setattr(cli.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(cli, "Backend", FakeBackend)
    monkeypatch.setattr(cli, "_prompt_choices", lambda: (dict(custom or RECOMMENDED_CHOICES), False))
    code = cli.main(["--root", str(root), "--data-root", str(data)])
    return code, tmp_path / "logs" / "install.log"


def test_failing_run_ends_with_the_outcome_lines_and_details_last(tmp_path, monkeypatch, capsys):
    code, log = _failing_cli(tmp_path, monkeypatch)
    assert code == 1
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    assert lines[-1] == f"Details: {log.absolute()}"
    assert lines[-2].startswith("Next: ")
    assert lines[-4].startswith("Astrid was NOT installed: ")
    assert lines[-3] == "Nothing was installed."


def test_setup_summary_and_failure_json_go_to_install_log_not_the_terminal(tmp_path, monkeypatch, capsys):
    _failing_cli(tmp_path, monkeypatch)
    out = capsys.readouterr().out
    for text in ("Astrid setup summary:", "Way back:", "Maintenance:", "Startup:",
                 "Command/PATH:", "Skill destinations:", "Failure: RuntimeOperationError",
                 "secret-detail-xyz"):
        assert text not in out
    logged = (tmp_path / "logs" / "install.log").read_text(encoding="utf-8")
    for text in ("Astrid setup summary:", "Way back:", "Maintenance:",
                 "Failure: RuntimeOperationError", "secret-detail-xyz"):
        assert text in logged, text


def test_recommended_setup_prints_no_choice_line(tmp_path, monkeypatch, capsys):
    _failing_cli(tmp_path, monkeypatch)
    assert "Your choice:" not in capsys.readouterr().out


def test_a_changed_choice_still_shows_its_choice_line(tmp_path, monkeypatch, capsys):
    custom = {**RECOMMENDED_CHOICES, "autostart": False, "skill_copies": True}
    _failing_cli(tmp_path, monkeypatch, custom=custom)
    out = capsys.readouterr().out
    assert ("Your choice: start at sign-in is off. To turn it on, rerun the install line "
            "with --autostart on.") in out
    assert "Your choice: skills are copies." in out
    assert "Your choice: the astrid command is off" not in out
    lines = [line for line in out.splitlines() if line.strip()]
    assert lines[-1].startswith("Details: ")  # the choice lines come before the receipt
