"""Terminal commands in synced skills need neither PATH nor an activated venv."""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
import venv
from dataclasses import replace

import pytest

from astrid import omp_agent, skills
from astrid.skills import discovery, registry
from astrid.skills.commands import rewrite_commands
from astrid.skills.harnesses import CodexAdapter
from astrid.skills.links import iter_markdown_files
from astrid.skills.view import compose_view


@pytest.mark.parametrize("windows", [False, True], ids=["posix", "powershell"])
def test_view_quotes_python_path_and_only_rewrites_code(tmp_path, monkeypatch, windows):
    executable = r"C:\Astrid's tools\python.exe" if windows else "/opt/Astrid's tools/bin/python"
    prefix = "& 'C:\\Astrid''s tools\\python.exe'" if windows else "'/opt/Astrid'\"'\"'s tools/bin/python'"
    monkeypatch.setattr(sys, "executable", executable)
    monkeypatch.setattr("astrid.skills.commands.os", type("OS", (), {"name": "nt" if windows else "posix"}))
    core = next(d for d in discovery.list_skills(discovery.PACKS_DIR) if d.pack_id == "_core")
    source = tmp_path / "source"
    source.mkdir()
    text = (
        "# Astrid\n\nProse python3 -m astrid stays.\n"
        "`python3 -m astrid projects current` and ``python -m astrid.skills sync``.\n"
        "`python3 -m astrid.core.pack.cli\nvalidate`\n"
        "```sh\npython -m astrid.packs.vibecomfy.local scan\n```\n"
        "~~~~powershell\npython3 -m astrid tasks list\n~~~\n"
        "python3 -m astrid runs list\n~~~~\n"
        "Prose python -m astrid stays too.\n"
        "`python3 -m astridology` `python3 -m other`\n"
    )
    (source / "SKILL.md").write_text(text)
    core = replace(core, skill_dir=source, skill_md=source / "SKILL.md")
    compose_view(tmp_path / "view", core, [])
    rendered = (tmp_path / "view/SKILL.md").read_text()
    assert "Prose python3 -m astrid stays." in rendered
    assert "Prose python -m astrid stays too." in rendered
    assert "`python3 -m astridology` `python3 -m other`" in rendered
    for suffix in ("astrid projects current", "astrid.skills sync", "astrid.core.pack.cli\nvalidate",
                   "astrid.packs.vibecomfy.local scan", "astrid tasks list", "astrid runs list"):
        assert prefix + " -m " + suffix in rendered
    assert (source / "SKILL.md").read_text() == text
    if not windows:
        assert shlex.split(prefix) == [executable]


def test_generated_registry_blocks_rewrite_commands_but_keep_prose(tmp_path):
    descriptor = next(d for d in discovery.list_skills(discovery.PACKS_DIR) if d.pack_id != "_core")
    descriptor = replace(descriptor, short_description="python3 -m astrid prose; `python -m astrid tasks list`")
    for block in (registry.render_registry_block([descriptor]), CodexAdapter(home=tmp_path)._render_block([descriptor])):
        assert "python3 -m astrid prose" in block
        assert rewrite_commands("`python -m astrid tasks list`") in block


@pytest.mark.parametrize("family", ["projects", "documents", "preferences", "media", "tasks", "runs", "timelines"])
def test_console_script_families_delegate_to_package_gateway(monkeypatch, family):
    calls = []
    monkeypatch.setattr("astrid.core.gateway.main", lambda args: calls.append(args) or 0)
    assert omp_agent.main([family, "--help"]) == 0
    assert calls == [[family, "--help"]]


def test_pack_views_upgrade_symlinks_refresh_and_prune_without_touching_source(tmp_path, monkeypatch):
    descriptors = discovery.list_skills(discovery.PACKS_DIR)
    core = next(d for d in descriptors if d.pack_id == "_core")
    pack = next(d for d in descriptors if d.pack_id == "vibecomfy")
    original = pack.skill_md.read_bytes()
    root = tmp_path / "view"
    target = root / "packs" / pack.pack_id
    target.parent.mkdir(parents=True)
    target.symlink_to(pack.skill_dir)
    compose_view(root, core, [pack])
    assert not target.is_symlink()
    monkeypatch.setattr(sys, "executable", "/new install/bin/python")
    compose_view(root, core, [pack])
    assert "'/new install/bin/python' -m astrid.packs." in (target / "SKILL.md").read_text()
    foreign = root / "packs/foreign"
    foreign.mkdir()
    (foreign / "SKILL.md").write_text("keep me")
    compose_view(root, core, [], dry_run=True)
    assert target.exists()
    compose_view(root, core, [])
    assert not target.exists()
    assert (foreign / "SKILL.md").read_text() == "keep me"
    assert pack.skill_md.read_bytes() == original


def _commands(text):
    """Extract commands independently from fenced code and inline code spans."""
    code = []
    fence = None
    prose = []
    for line in text.splitlines(keepends=True):
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            if fence is None:
                fence = marker[1]
            elif marker[1][0] == fence[0] and len(marker[1]) >= len(fence):
                fence = None
            continue
        if fence:
            code.append(line)
        else:
            prose.append(line)
    code.extend(m[2] for m in re.finditer(r"(`+)(.*?)\1", "".join(prose), re.DOTALL))
    for snippet in code:
        for line in snippet.splitlines():
            quoted = line.lstrip().startswith("'") and "' -m astrid" in line
            if quoted or re.match(r"\s*python3? -m astrid\b", line):
                yield line.strip().removesuffix("\\").rstrip()


@pytest.mark.parametrize("harness", ["claude", "codex", "hermes"])
@pytest.mark.posix_native
def test_every_synced_command_has_help_in_a_clean_terminal(tmp_path, monkeypatch, harness, record_property):
    # Model an installed environment, with dependencies from the test runner.
    # The source path is in this venv's .pth, never in the subprocess environment
    # or cwd; the machine's editable install may point at a different worktree.
    env_root = tmp_path / "Astrid's environment"
    venv.EnvBuilder(with_pip=False).create(env_root)
    site = next(env_root.glob("lib/python*/site-packages"))
    repo = discovery.PACKS_DIR.parents[1]
    dependencies = [p for p in sys.path if p.endswith("site-packages")]
    (site / "astrid-test.pth").write_text("\n".join([
        "import sys; sys.dont_write_bytecode = True", str(repo),
        *(f"import site; site.addsitedir({path!r})" for path in dependencies),
    ]) + "\n")
    python = env_root / "bin/python"
    monkeypatch.setattr(sys, "executable", str(python))
    home = tmp_path / "terminal-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    (home / f".{harness}").mkdir()
    state_path = tmp_path / "state.json"
    sources = {p: p.read_bytes() for p in discovery.PACKS_DIR.rglob("SKILL.md")}
    skills.sync(harness_names=[harness], deep=True, state_path=state_path)
    view = tmp_path / "skills" / harness
    rendered = {p.relative_to(view): p.read_bytes() for p in iter_markdown_files(view)}
    skills.sync(harness_names=[harness], deep=True, state_path=state_path)
    assert {p.relative_to(view): p.read_bytes() for p in iter_markdown_files(view)} == rendered
    assert all(p.read_bytes() == before for p, before in sources.items())
    assert not (view / "packs/video_editing").is_symlink()
    assert (home / f".{harness}/skills/astrid").resolve() == view
    commands = [command for p in iter_markdown_files(view) for command in _commands(p.read_text())]
    assert len(commands) >= 90
    assert not any("python3 -m astrid" in command for command in commands)
    record_property("commands_checked", len(commands))
    for command in commands:
        argv = shlex.split(command, comments=True)
        assert argv[:2] == [str(python), "-m"], command
        module = argv[2]
        if module == "astrid":
            # Family help handles examples with placeholders without admitting work.
            help_argv = argv[:3] + ([argv[3]] if len(argv) > 3 and not argv[3].startswith("-") else [])
        elif module == "astrid.omp_invoke":
            # This JSON-only boundary has no help parser; use task family help.
            help_argv = [str(python), "-m", "astrid", "tasks"]
        else:
            help_argv = argv[:3]
        result = subprocess.run(["/usr/bin/env", "-i", f"HOME={home}", "PATH=/usr/bin:/bin",
                                 *help_argv, "--help"], cwd=home, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, (command, result.stdout, result.stderr)
