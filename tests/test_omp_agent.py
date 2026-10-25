"""Focused contract tests for Astrid's installed OMP launcher."""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

import pytest

from astrid import omp_agent
from astrid import agent_context
from astrid.core.util import process as _process


@pytest.fixture(autouse=True)
def stub_preferences_context(monkeypatch):
    monkeypatch.setattr(agent_context, "current_context", lambda: "")


def test_installed_mode_uses_shipped_profile_despite_user_profile(monkeypatch) -> None:
    captured: dict[str, object] = {}
    reads = []
    monkeypatch.setenv("ASTRID_HOST_EXECUTION_PROFILE", "installed")
    monkeypatch.setattr(agent_context, "current_context", lambda: (reads.append("read") or "Saved prefs"))
    monkeypatch.setattr(omp_agent, "_has_named_agent_profile", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(omp_agent, "_find_launcher", lambda: (_ for _ in ()).throw(AssertionError("global launcher/cache must not run")))
    monkeypatch.setattr(omp_agent, "_resolve_omp_bin", lambda: "/private/installed/omp")
    monkeypatch.setattr(omp_agent, "_select_omp_bin", lambda: None)
    monkeypatch.setattr(omp_agent, "replace_process", lambda argv: captured.update(file=argv[0], argv=argv))

    assert omp_agent.main(["--mode=acp"]) == 1
    assert captured["file"] == "/private/installed/omp"
    argv = captured["argv"]
    assert argv[:3] == ["/private/installed/omp", "--system-prompt", omp_agent.ASTRID_SYSTEM_PROMPT]
    assert "--mode=acp" in argv
    assert argv[argv.index("--append-system-prompt") + 1].endswith("Saved prefs")
    assert reads == ["read"]


def test_main_uses_shipped_profile_without_materializing_a_user_agent(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.delenv("ASTRID_SYSTEM_PROMPT", raising=False)
    monkeypatch.delenv("ASTRID_SYSTEM_PROMPT_FILE", raising=False)

    monkeypatch.setattr(omp_agent, "_has_named_agent_profile", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(omp_agent, "_resolve_omp_bin", lambda: "/fake/omp")
    monkeypatch.setattr(omp_agent, "_select_omp_bin", lambda: None)
    monkeypatch.setattr(omp_agent, "replace_process", lambda argv: captured.update(file=argv[0], argv=argv))

    assert omp_agent.main(["make a shot list"]) == 1
    assert captured["file"] == "/fake/omp"
    argv = captured["argv"]
    assert argv[:2] == ["/fake/omp", "--system-prompt"]
    assert argv[2] == omp_agent.ASTRID_SYSTEM_PROMPT
    assert "--append-system-prompt" in argv
    assert "Hivemind contributor login status:" in argv[argv.index("--append-system-prompt") + 1]
    assert argv[-2:] == ["--print", "make a shot list"]


def test_existing_project_or_user_profile_keeps_the_omp_agent_route(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(omp_agent, "_has_named_agent_profile", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(omp_agent, "_find_launcher", lambda: Path("/fake/agent"))
    monkeypatch.setattr(omp_agent, "_select_omp_bin", lambda: None)
    monkeypatch.setattr(omp_agent, "replace_process", lambda argv: captured.update(file=argv[0], argv=argv))

    assert omp_agent.main([]) == 1
    assert captured["file"] == "/fake/agent"
    assert captured["argv"][:3] == ["/fake/agent", "run", "astrid"]


def test_explicit_astrid_prompt_override_is_appended(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setenv("ASTRID_SYSTEM_PROMPT", "extra Astrid guidance")
    monkeypatch.setattr(omp_agent, "_has_named_agent_profile", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(omp_agent, "_find_launcher", lambda: Path("/fake/agent"))
    monkeypatch.setattr(omp_agent, "_select_omp_bin", lambda: None)
    monkeypatch.setattr(omp_agent, "replace_process", lambda argv: captured.update(file=argv[0], argv=argv))

    omp_agent.main(["make a shot list"])

    argv = captured["argv"]
    prompt = argv[argv.index("--append-system-prompt") + 1]
    assert "extra Astrid guidance" in prompt
    assert "Hivemind contributor login status:" in prompt


def test_bundled_prompt_has_safe_startup_update_contract() -> None:
    prompt = omp_agent.ASTRID_SYSTEM_PROMPT
    assert "first action in each new session" in prompt
    assert "python3 -m astrid.skills sync" in prompt
    assert "bun run build" in prompt
    assert "never stash, reset, merge, rebase, or overwrite user work" in prompt
    assert "next `astrid` launch" in prompt


def test_installed_named_profile_has_safe_startup_update_contract() -> None:
    profile = omp_agent._AGENT_PROFILE_FILE
    text = profile.read_text(encoding="utf-8")
    assert text.startswith("---\nname: astrid\ndescription: ")
    assert "first action in each new session" in text
    assert "python3 -m astrid.skills sync" in text
    assert "bun run build" in text
    assert "/reload-plugins" in text
    assert "next `astrid` launch" in text
    assert omp_agent._read_agent_profile_body() == omp_agent.ASTRID_SYSTEM_PROMPT


def test_packaged_profile_resolves_skills_inside_selected_installation() -> None:
    package_root = Path(omp_agent.__file__).resolve().parent
    core = package_root / "packs" / "_core" / "docs" / "SKILL.md"
    media = package_root / "packs" / "media" / "docs" / "SKILL.md"
    assert core.is_file() and media.is_file()
    assert str(core) in omp_agent.ASTRID_SYSTEM_PROMPT
    assert str(media) in omp_agent.ASTRID_SYSTEM_PROMPT
    assert "{{ASTRID_PACKAGE_ROOT}}" not in omp_agent.ASTRID_SYSTEM_PROMPT
    assert "Read the discovered `astrid` skill first" not in omp_agent.ASTRID_SYSTEM_PROMPT


def test_installed_prompt_notice_preserves_overrides_and_names_packaged_skills() -> None:
    flags = omp_agent._prompt_args([], env={
        "ASTRID_HOST_EXECUTION_PROFILE": "installed", "ASTRID_SYSTEM_PROMPT": "Custom guidance",
    })
    assert flags[0] == "--append-system-prompt"
    assert "Custom guidance" in flags[1]
    assert str(Path(omp_agent.__file__).resolve().parent / "packs" / "_core" / "docs" / "SKILL.md") in flags[1]
    assert omp_agent._prompt_args([], agent="scout", env={"ASTRID_HOST_EXECUTION_PROFILE": "installed"}) == []


def test_prompt_args_appends_preferences_after_existing_prompt_text():
    result = omp_agent._prompt_args([], env={"ASTRID_SYSTEM_PROMPT": "Existing"}, preferences_context="Saved prefs")
    assert result[0] == "--append-system-prompt"
    assert result[1].startswith("Existing\n\nHivemind contributor login status:")
    assert result[1].endswith("Saved prefs")
    assert omp_agent._prompt_args(["--append-system-prompt", "custom"], preferences_context="Saved") == []


def test_profile_file_lookup_is_read_only_and_returns_installed_resource(capsys, monkeypatch) -> None:
    monkeypatch.setattr(omp_agent, "_find_launcher", lambda: (_ for _ in ()).throw(AssertionError("launcher lookup")))
    monkeypatch.setattr(omp_agent, "_select_omp_bin", lambda: (_ for _ in ()).throw(AssertionError("OMP lookup")))
    assert omp_agent.main(["--agent-profile-file"]) == 0
    assert capsys.readouterr().out.strip() == str(Path(omp_agent._AGENT_PROFILE_FILE))


def test_agent_context_flag_prints_block_and_always_succeeds(capsys, monkeypatch):
    monkeypatch.setattr(agent_context, "current_context", lambda: "Preferences block")
    assert omp_agent.main(["--agent-context"]) == 0
    assert capsys.readouterr().out == "Preferences block\n"
    monkeypatch.setattr(agent_context, "current_context", lambda: (_ for _ in ()).throw(RuntimeError()))
    assert omp_agent.main(["--agent-context"]) == 0
    assert capsys.readouterr().out == ""


def test_main_injects_context_for_default_agent_only_and_respects_overrides(monkeypatch):
    captured = []
    monkeypatch.setattr(agent_context, "current_context", lambda: "Saved prefs")
    monkeypatch.setattr(omp_agent, "_has_named_agent_profile", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(omp_agent, "_find_launcher", lambda: Path("/fake/agent"))
    monkeypatch.setattr(omp_agent, "_select_omp_bin", lambda: None)
    monkeypatch.setattr(omp_agent, "replace_process", lambda argv: captured.append(argv))

    omp_agent.main([])
    assert "Saved prefs" in captured[-1][captured[-1].index("--append-system-prompt") + 1]
    omp_agent.main(["--agent", "scout"])
    assert "--append-system-prompt" not in captured[-1]
    omp_agent.main(["--system-prompt", "custom"])
    assert "Saved prefs" not in captured[-1]


def test_gateway_console_script_is_astrid_tools_and_module_entry_is_unchanged() -> None:
    metadata = Path(__file__).parents[1] / "pyproject.toml"
    text = metadata.read_text(encoding="utf-8")
    assert 'astrid-tools = "astrid.core.gateway:main"' in text
    assert 'astrid = "astrid.omp_agent:main"' in text
    module_main = (metadata.parent / "astrid" / "__main__.py").read_text(encoding="utf-8")
    assert "from .core.gateway import main" in module_main


def test_omp_bin_override_and_astrid_identity(monkeypatch) -> None:
    monkeypatch.setenv("OMP_BIN", "/custom/omp")
    monkeypatch.delenv("ASTRID_STOCK_OMP", raising=False)
    assert omp_agent._resolve_omp_bin({"OMP_BIN": "/custom/omp"}) == "/custom/omp"

    captured: dict[str, object] = {}
    monkeypatch.setattr(omp_agent, "_has_named_agent_profile", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(omp_agent, "_find_launcher", lambda: Path("/fake/agent"))
    monkeypatch.setattr(omp_agent, "_select_omp_bin", lambda: None)
    monkeypatch.setattr(omp_agent, "replace_process", lambda argv: captured.update(file=argv[0], argv=argv))
    omp_agent.main([])
    assert os.environ["OMP_AGENT_IDENTITY"] == "astrid"
    assert os.environ["ASTRID_AGENT_IDENTITY"] == "astrid"
    assert captured["argv"][:3] == ["/fake/agent", "run", "astrid"]


def test_astrid_command_overrides_inherited_identity(monkeypatch) -> None:
    """A prior Arnold launch must not tint the next Astrid process."""
    monkeypatch.setenv("OMP_AGENT_IDENTITY", "arnold")
    monkeypatch.setenv("ASTRID_AGENT_IDENTITY", "arnold")
    captured: dict[str, object] = {}
    monkeypatch.setattr(omp_agent, "_has_named_agent_profile", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(omp_agent, "_find_launcher", lambda: Path("/fake/agent"))
    monkeypatch.setattr(omp_agent, "_select_omp_bin", lambda: None)
    monkeypatch.setattr(omp_agent, "replace_process", lambda argv: captured.update(file=argv[0], argv=argv))

    omp_agent.main([])

    assert os.environ["OMP_AGENT_IDENTITY"] == "astrid"
    assert os.environ["ASTRID_AGENT_IDENTITY"] == "astrid"
    assert captured["argv"][:3] == ["/fake/agent", "run", "astrid"]


def test_bare_agent_subcommand_is_an_explicit_interactive_agent_route(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(omp_agent, "_has_named_agent_profile", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(omp_agent, "_find_launcher", lambda: Path("/fake/agent"))
    monkeypatch.setattr(omp_agent, "_select_omp_bin", lambda: None)
    monkeypatch.setattr(omp_agent, "replace_process", lambda argv: captured.update(file=argv[0], argv=argv))

    assert omp_agent.main(["agent"]) == 1
    assert captured["argv"][:3] == ["/fake/agent", "run", "astrid"]
    assert "--print" not in captured["argv"]


def test_terminal_agent_uses_service_profile_and_explicit_profile_wins(monkeypatch) -> None:
    monkeypatch.setattr(omp_agent, "_service_profile", lambda: "app-profile")
    monkeypatch.setattr(omp_agent, "_has_named_agent_profile", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(omp_agent, "_find_launcher", lambda: Path("/fake/agent"))
    monkeypatch.setattr(omp_agent, "_select_omp_bin", lambda: None)
    calls = []
    monkeypatch.setattr(omp_agent, "replace_process", lambda argv: calls.append(argv))

    assert omp_agent.main(["agent"]) == 1
    assert omp_agent.main(["agent", "--profile", "user-profile"]) == 1
    assert calls[0][calls[0].index("--profile") + 1] == "app-profile"
    assert calls[1][calls[1].index("--profile") + 1] == "user-profile"


def test_model_commands_use_service_profile(monkeypatch) -> None:
    captured = []
    monkeypatch.setattr(omp_agent, "_resolve_omp_bin", lambda: "/fake/omp")
    monkeypatch.setattr(omp_agent, "_service_profile", lambda: "app-profile")
    monkeypatch.setattr(omp_agent, "_omp_model_snapshot", lambda _omp, profile: captured.append(profile) or {"providers": []})
    monkeypatch.setattr(omp_agent, "_merge_login_providers", lambda _omp, _snapshot, profile: captured.append(profile))
    monkeypatch.setattr(omp_agent, "_add_model_usage", lambda _omp, _snapshot, profile: captured.append(profile))

    assert omp_agent.main(["agent", "models", "--json"]) == 0
    assert omp_agent.main(["agent", "models", "--profile", "user-profile", "--json"]) == 0
    assert captured == [
        "app-profile", "app-profile", "app-profile",
        "user-profile", "user-profile", "user-profile",
    ]


def test_service_profile_is_read_from_install_selection(monkeypatch, tmp_path) -> None:
    from astrid.installer import service

    selection = tmp_path / "astrid-install.json"
    selection.write_text('{"chat_settings":{"ASTRID_ACP_PROFILE":"app-profile"}}')
    monkeypatch.setattr(service, "install_selection", lambda _home, _system: selection)
    assert omp_agent._service_profile() == "app-profile"


def test_toolkit_families_and_help_delegate_to_gateway(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(omp_agent, "_dispatch_toolkit", lambda argv: calls.append(argv) or 7)

    assert omp_agent.main(["projects", "list", "--json"]) == 7
    assert omp_agent.main(["help"]) == 7
    assert omp_agent.main(["install", "--help"]) == 7
    assert omp_agent.main(["skills", "sync"]) == 7
    assert calls == [["projects", "list", "--json"], ["help"], ["install", "--help"], ["skills", "sync"]]


def test_setup_is_a_conversational_message(monkeypatch) -> None:
    captured = {}
    monkeypatch.setattr(omp_agent, "_dispatch_toolkit", lambda _argv: pytest.fail("setup has no gateway route"))
    monkeypatch.setattr(omp_agent, "_installed_pack_families", lambda: frozenset())
    monkeypatch.setattr(omp_agent, "_has_named_agent_profile", lambda *_a, **_kw: True)
    monkeypatch.setattr(omp_agent, "_find_launcher", lambda: Path("/fake/agent"))
    monkeypatch.setattr(omp_agent, "_select_omp_bin", lambda: None)
    monkeypatch.setattr(omp_agent, "replace_process", lambda argv: captured.update(argv=argv))

    assert omp_agent.main(["setup"]) == 1
    assert captured["argv"][:3] == ["/fake/agent", "run", "astrid"]
    assert captured["argv"][-2:] == ["--print", "setup"]


def test_top_level_help_describes_unified_agent_and_toolkit_surface(capsys) -> None:
    assert omp_agent.main(["--help"]) == 0
    output = capsys.readouterr().out
    assert "interactive Astrid agent" in output
    assert "astrid agent" in output
    for family in ("projects", "timelines", "media", "tasks", "runs", "doctor", "backup"):
        assert family in output
    for command in ("astrid login", "astrid status", "astrid logout", "astrid revoke"):
        assert command in output


def test_alternate_agent_does_not_receive_astrid_packaged_prompt(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(omp_agent, "_find_launcher", lambda: Path("/fake/agent"))
    monkeypatch.setattr(omp_agent, "_select_omp_bin", lambda: None)
    monkeypatch.setattr(omp_agent, "replace_process", lambda argv: captured.update(file=argv[0], argv=argv))

    assert omp_agent.main(["--agent", "scout", "inspect this scene"]) == 1
    argv = captured["argv"]
    assert argv[:3] == ["/fake/agent", "run", "scout"]
    assert "--system-prompt" not in argv
    assert "--append-system-prompt" not in argv


@pytest.mark.parametrize("route", ["packaged", "named", "onboard"])
def test_windows_handoffs_wait_and_propagate_child_exit(monkeypatch, route):
    captured = {}
    executable = "/selected program/omp.exe" if route != "named" else "/selected program/agent.exe"
    monkeypatch.setattr(omp_agent, "_has_named_agent_profile", lambda *_a, **_kw: route == "named")
    monkeypatch.setattr(omp_agent, "_find_launcher", lambda: Path(executable))
    monkeypatch.setattr(omp_agent, "_resolve_omp_bin", lambda: executable)
    monkeypatch.setattr(omp_agent, "_select_omp_bin", lambda: None)
    monkeypatch.setattr(omp_agent, "_omp_supports_onboard", lambda _bin: True)
    monkeypatch.setenv("ASTRID_HOST_EXECUTION_PROFILE", "source")
    monkeypatch.setenv("ASTRID_SYSTEM_PROMPT", 'extra "quoted" guidance')
    monkeypatch.delenv("ASTRID_SYSTEM_PROMPT_FILE", raising=False)
    monkeypatch.setattr(_process.sys, "platform", "win32")
    for name in ("execvpe", "execvp", "execve"):
        monkeypatch.setattr(_process.os, name, lambda *_a: pytest.fail("Windows must wait for its child"))

    def run(argv, **kwargs):
        captured.update(argv=argv, kwargs=kwargs)
        signal.raise_signal(signal.SIGINT)
        return subprocess.CompletedProcess(argv, 23)

    monkeypatch.setattr(_process.subprocess, "run", run)
    message = 'inspect "this scene" together'
    args = ["--onboard"] if route == "onboard" else ["--mode=acp", message]
    previous_handler = signal.getsignal(signal.SIGINT)
    with pytest.raises(SystemExit) as exc:
        omp_agent.main(args)
    assert exc.value.code == 23
    assert captured["argv"][0] == executable
    assert captured["kwargs"] == {"env": None, "check": False}
    assert signal.getsignal(signal.SIGINT) is previous_handler
    if route == "onboard":
        assert captured["argv"] == [executable, "onboard"]
    else:
        assert captured["argv"][-2:] == ["--print", message]
        if route == "packaged":
            assert captured["argv"][:3] == [executable, "--system-prompt", omp_agent.ASTRID_SYSTEM_PROMPT]
        else:
            assert captured["argv"][:3] == [executable, "run", "astrid"]
        prompt = captured["argv"][captured["argv"].index("--append-system-prompt") + 1]
        assert 'extra "quoted" guidance' in prompt


@pytest.mark.parametrize("output,returncode,supported", [
    (b"detect-first provider onboarding", 0, True),
    (b"detect-first provider onboarding", 1, False),
    (b"Install optional Python dependencies", 0, False),
    (b"Unknown command: onboard", 0, False),
])
def test_onboard_checks_for_the_provider_setup_command(monkeypatch, output, returncode, supported):
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, returncode, stdout=output, stderr=b"")

    monkeypatch.setattr(omp_agent.subprocess, "run", run)
    assert omp_agent._omp_supports_onboard("/selected/omp") is supported
    assert calls == [["/selected/omp", "onboard", "--help"]]


@pytest.mark.parametrize("help_flag", ["--help", "-h"])
def test_onboard_forwards_help_without_an_interactive_terminal(monkeypatch, help_flag):
    monkeypatch.setattr(omp_agent, "_resolve_omp_bin", lambda: "/selected/omp")
    monkeypatch.setattr(omp_agent, "_omp_supports_onboard", lambda _: True)
    calls = []
    monkeypatch.setattr(omp_agent, "replace_process", lambda argv: calls.append(argv))
    assert omp_agent.main(["--onboard", help_flag]) == 1
    assert calls == [["/selected/omp", "onboard", help_flag]]
