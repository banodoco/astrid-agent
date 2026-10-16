"""Focused contract tests for Astrid's installed OMP launcher."""

from __future__ import annotations

import os
from pathlib import Path

from astrid import omp_agent


def test_installed_mode_uses_shipped_profile_despite_user_profile(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setenv("ASTRID_HOST_EXECUTION_PROFILE", "installed")
    monkeypatch.setattr(omp_agent, "_has_named_agent_profile", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(omp_agent, "_find_launcher", lambda: (_ for _ in ()).throw(AssertionError("global launcher/cache must not run")))
    monkeypatch.setattr(omp_agent, "_resolve_omp_bin", lambda: "/private/installed/omp")
    monkeypatch.setattr(omp_agent, "_select_omp_bin", lambda: None)
    monkeypatch.setattr(omp_agent, "_run_confined_omp", lambda argv, **_kwargs: captured.update(file=argv[0], argv=argv) or 1)

    assert omp_agent.main(["--mode=acp"]) == 1
    assert captured["file"] == "/private/installed/omp"
    argv = captured["argv"]
    assert argv[:3] == ["/private/installed/omp", "--system-prompt", omp_agent.ASTRID_SYSTEM_PROMPT]
    assert "--mode=acp" in argv


def test_main_uses_shipped_profile_without_materializing_a_user_agent(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.delenv("ASTRID_SYSTEM_PROMPT", raising=False)
    monkeypatch.delenv("ASTRID_SYSTEM_PROMPT_FILE", raising=False)

    monkeypatch.setattr(omp_agent, "_has_named_agent_profile", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(omp_agent, "_resolve_omp_bin", lambda: "/fake/omp")
    monkeypatch.setattr(omp_agent, "_select_omp_bin", lambda: None)
    monkeypatch.setattr(omp_agent, "_run_confined_omp", lambda argv, **_kwargs: captured.update(file=argv[0], argv=argv) or 1)

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
    monkeypatch.setattr(omp_agent, "_run_confined_omp", lambda argv, **_kwargs: captured.update(file=argv[0], argv=argv) or 1)

    assert omp_agent.main([]) == 1
    assert captured["file"] == "/fake/agent"
    assert captured["argv"][:3] == ["/fake/agent", "run", "astrid"]


def test_caller_environment_prompt_override_is_not_appended(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setenv("ASTRID_SYSTEM_PROMPT", "extra Astrid guidance")
    monkeypatch.setattr(omp_agent, "_has_named_agent_profile", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(omp_agent, "_find_launcher", lambda: Path("/fake/agent"))
    monkeypatch.setattr(omp_agent, "_select_omp_bin", lambda: None)
    monkeypatch.setattr(omp_agent, "_run_confined_omp", lambda argv, **_kwargs: captured.update(file=argv[0], argv=argv) or 1)

    omp_agent.main(["make a shot list"])

    argv = captured["argv"]
    prompt = argv[argv.index("--append-system-prompt") + 1]
    assert "extra Astrid guidance" not in prompt
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


def test_profile_file_lookup_is_read_only_and_returns_installed_resource(capsys, monkeypatch) -> None:
    monkeypatch.setattr(omp_agent, "_find_launcher", lambda: (_ for _ in ()).throw(AssertionError("launcher lookup")))
    monkeypatch.setattr(omp_agent, "_select_omp_bin", lambda: (_ for _ in ()).throw(AssertionError("OMP lookup")))
    assert omp_agent.main(["--agent-profile-file"]) == 0
    assert capsys.readouterr().out.strip() == str(Path(omp_agent._AGENT_PROFILE_FILE))


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
    monkeypatch.setattr(omp_agent, "_run_confined_omp", lambda argv, **_kwargs: captured.update(file=argv[0], argv=argv) or 1)
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
    monkeypatch.setattr(omp_agent, "_run_confined_omp", lambda argv, **_kwargs: captured.update(file=argv[0], argv=argv) or 1)

    omp_agent.main([])

    assert os.environ["OMP_AGENT_IDENTITY"] == "astrid"
    assert os.environ["ASTRID_AGENT_IDENTITY"] == "astrid"
    assert captured["argv"][:3] == ["/fake/agent", "run", "astrid"]


def test_bare_agent_subcommand_is_an_explicit_interactive_agent_route(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(omp_agent, "_has_named_agent_profile", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(omp_agent, "_find_launcher", lambda: Path("/fake/agent"))
    monkeypatch.setattr(omp_agent, "_select_omp_bin", lambda: None)
    monkeypatch.setattr(omp_agent, "_run_confined_omp", lambda argv, **_kwargs: captured.update(file=argv[0], argv=argv) or 1)

    assert omp_agent.main(["agent"]) == 1
    assert captured["argv"][:3] == ["/fake/agent", "run", "astrid"]
    assert "--print" not in captured["argv"]


def test_toolkit_families_and_help_delegate_to_gateway(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(omp_agent, "_dispatch_toolkit", lambda argv: calls.append(argv) or 7)

    assert omp_agent.main(["projects", "list", "--json"]) == 7
    assert omp_agent.main(["help"]) == 7
    assert calls == [["projects", "list", "--json"], ["help"]]


def test_status_and_dev_dispatch_to_gateway_but_auth_stays_namespaced(monkeypatch) -> None:
    gateway_calls: list[list[str]] = []
    auth_calls: list[list[str]] = []
    monkeypatch.setattr(omp_agent, "_dispatch_toolkit", lambda argv: gateway_calls.append(argv) or 7)
    monkeypatch.setattr(omp_agent, "run_auth", lambda argv: auth_calls.append(argv) or 9)

    assert omp_agent.main(["status", "--json"]) == 7
    assert omp_agent.main(["dev", "status"]) == 7
    assert omp_agent.main(["auth", "status"]) == 9
    assert gateway_calls == [["status", "--json"], ["dev", "status"]]
    assert auth_calls == [["status"]]


def test_top_level_help_describes_unified_agent_and_toolkit_surface(capsys) -> None:
    assert omp_agent.main(["--help"]) == 0
    output = capsys.readouterr().out
    assert "interactive Astrid agent" in output
    assert "astrid agent" in output
    for family in ("projects", "timelines", "media", "tasks", "runs", "doctor", "backup"):
        assert family in output
    assert "astrid status [--json]" in output
    assert "astrid dev" in output
    assert "astrid auth login|status|logout|revoke" in output


def test_alternate_agent_does_not_receive_astrid_packaged_prompt(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(omp_agent, "_find_launcher", lambda: Path("/fake/agent"))
    monkeypatch.setattr(omp_agent, "_select_omp_bin", lambda: None)
    monkeypatch.setattr(omp_agent, "_run_confined_omp", lambda argv, **_kwargs: captured.update(file=argv[0], argv=argv) or 1)

    assert omp_agent.main(["--agent", "scout", "inspect this scene"]) == 1
    argv = captured["argv"]
    assert argv[:3] == ["/fake/agent", "run", "scout"]
    assert "--system-prompt" not in argv
    assert "--append-system-prompt" not in argv
