"""Developer skills: developing-astrid by default, the rest only with ``--developer``."""

from __future__ import annotations

import io
import json
import os
import re
from pathlib import Path

import pytest

from astrid import mcp_server, skills
from astrid.skills import cli as skills_cli
from astrid.skills import discovery, registry, state

DEVELOPER_IDS = ["developer", "developer-fast-iteration", "developer-releasing"]
EXTRA_IDS = ["developer-fast-iteration", "developer-releasing"]


@pytest.fixture()
def fx(tmp_path, monkeypatch):
    home = tmp_path / "home"
    for harness in (".claude", ".codex", ".hermes"):
        (home / harness).mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("ASTRID_STATE_HOME", str(tmp_path / "_state"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(Path, "home", lambda: home)

    class Fixture:
        root = tmp_path
        state_path = tmp_path / "state.json"
        claude = home / ".claude" / "skills"
        view = tmp_path / "skills" / "claude"

    return Fixture()


def _links(text: str) -> list[str]:
    return re.findall(r"\]\(([^)]+)\)", text)


def test_developer_pack_declares_developing_astrid_and_developer_only_extras():
    mains = discovery.list_skills()
    extras = discovery.list_extras(mains)
    main = next(d for d in mains if d.pack_id == "developer")
    assert main.name == "developing-astrid"
    assert not main.developer_only
    by_id = {d.skill_id: d for d in extras if d.pack_id == "developer"}
    assert {by_id[sid].name for sid in EXTRA_IDS} == {"fast-iteration", "releasing"}
    assert all(by_id[sid].developer_only for sid in EXTRA_IDS)
    # Every other shipped skill keeps the default audience.
    assert [d.skill_id for d in [*mains, *extras] if d.developer_only] == EXTRA_IDS
    assert [d.skill_id for d in skills.developer_descriptors(mains, extras)] == DEVELOPER_IDS
    assert "developer" in skills.default_pack_ids(mains)


def test_the_three_skills_reference_each_other_in_source():
    docs = discovery.PACKS_DIR / "developer" / "docs"
    main = _links((docs / "SKILL.md").read_text(encoding="utf-8"))
    fast = _links((docs / "fast-iteration" / "SKILL.md").read_text(encoding="utf-8"))
    release = _links((docs / "releasing" / "SKILL.md").read_text(encoding="utf-8"))
    assert {"fast-iteration/SKILL.md", "releasing/SKILL.md"} <= set(main)
    assert {"../SKILL.md", "../releasing/SKILL.md"} <= set(fast)
    assert {"../SKILL.md", "../fast-iteration/SKILL.md"} <= set(release)
    for source, targets in ((docs / "SKILL.md", main), (docs / "fast-iteration/SKILL.md", fast),
                            (docs / "releasing/SKILL.md", release)):
        for target in targets:
            if "://" not in target:
                assert (source.parent / target.split("#")[0]).is_file(), (source, target)
    gateway = (discovery.PACKS_DIR / "_core" / "docs" / "SKILL.md").read_text(encoding="utf-8")
    assert "../../developer/docs/SKILL.md" in _links(gateway)
    assert "astrid skills sync --developer" in (docs / "SKILL.md").read_text(encoding="utf-8")


def test_default_sync_installs_developing_astrid_but_not_the_developer_skills(fx):
    report = skills.sync(harness_names=["claude"], state_path=fx.state_path)
    assert report["broken_links"] == []
    # developing-astrid is composed with the other defaults and routed from the gateway.
    main = fx.view / "packs" / "developer" / "SKILL.md"
    assert main.is_file()
    assert "packs/developer/SKILL.md" in _links((fx.view / "SKILL.md").read_text(encoding="utf-8"))
    # The developer-only skills are neither copied nor linked, and their links keep only text.
    assert not (fx.view / "packs" / "developer" / "fast-iteration").exists()
    assert not (fx.view / "packs" / "developer" / "releasing").exists()
    text = main.read_text(encoding="utf-8")
    assert "Fast iteration" in text and "Releasing" in text
    assert not any("fast-iteration/SKILL.md" in link or "releasing/SKILL.md" in link for link in _links(text))
    assert not any(path.name.startswith("astrid-developer") for path in fx.claude.iterdir())
    selected = state.load(fx.state_path)["skills"]["claude"]["skills"]
    assert not any(selected.get(sid, {}).get("selected") for sid in DEVELOPER_IDS)

    # Even --deep (link every skill) leaves the developer-only skills out.
    deep = skills.sync(harness_names=["claude"], state_path=fx.state_path, deep=True)
    assert deep["broken_links"] == []
    assert (fx.claude / "astrid-developer").is_symlink()
    assert not (fx.claude / "astrid-developer-fast-iteration").exists()
    assert not (fx.claude / "astrid-developer-releasing").exists()


def test_developer_sync_installs_resolves_survives_resync_and_uninstalls(fx):
    dry = skills.sync(harness_names=["claude"], state_path=fx.state_path, developer=True, dry_run=True)
    assert dry["actions"][0]["harness"] == "claude"
    assert not fx.state_path.exists() or not any(
        state.load(fx.state_path)["skills"]["claude"]["skills"].get(sid, {}).get("selected") for sid in DEVELOPER_IDS
    )

    report = skills.sync(harness_names=["claude"], state_path=fx.state_path, developer=True)
    assert report["broken_links"] == []
    for skill_id, name in zip(DEVELOPER_IDS, ("developing-astrid", "fast-iteration", "releasing")):
        link = fx.claude / f"astrid-{skill_id}"
        assert link.is_symlink(), link
        assert f"name: {name}" in (link / "SKILL.md").read_text(encoding="utf-8")
    # The references are live links in the installed view and all resolve.
    main = (fx.claude / "astrid-developer" / "SKILL.md").read_text(encoding="utf-8")
    assert {"fast-iteration/SKILL.md", "releasing/SKILL.md"} <= set(_links(main))
    for skill_id, wanted in (("developer-fast-iteration", "releasing/SKILL.md"),
                             ("developer-releasing", "fast-iteration/SKILL.md")):
        skill_md = fx.claude / f"astrid-{skill_id}" / "SKILL.md"
        targets = [link for link in _links(skill_md.read_text(encoding="utf-8")) if link.endswith(wanted)]
        assert targets, (skill_id, wanted)
        assert all(Path(os.path.join(skill_md.parent, target)).is_file() for target in targets)
    packs_md = (fx.view / "creative-work" / "references" / "packs.md").read_text(encoding="utf-8")
    assert "developer / fast-iteration" in packs_md and "selected" in packs_md

    # A later plain sync (as the installer runs) keeps them.
    again = skills.sync(harness_names=["claude"], state_path=fx.state_path)
    assert again["broken_links"] == []
    assert all((fx.claude / f"astrid-{sid}").is_symlink() for sid in DEVELOPER_IDS)

    # uninstall --developer removes them and leaves developing-astrid composed.
    removed = skills.uninstall(None, ["claude"], state_path=fx.state_path, developer=True)
    assert removed["resync"]["broken_links"] == []
    assert not any((fx.claude / f"astrid-{sid}").exists() for sid in DEVELOPER_IDS)
    assert (fx.view / "packs" / "developer" / "SKILL.md").is_file()
    assert not (fx.view / "packs" / "developer" / "fast-iteration").exists()
    data = state.load(fx.state_path)
    assert "developer" not in data["disabled_defaults"]["claude"]
    assert not any(data["skills"]["claude"]["skills"].get(sid, {}).get("selected") for sid in DEVELOPER_IDS)


def test_install_developer_selects_the_same_units(fx):
    skills.install(None, ["claude"], state_path=fx.state_path, developer=True)
    selected = state.load(fx.state_path)["skills"]["claude"]["skills"]
    assert all(selected[sid]["selected"] for sid in DEVELOPER_IDS)
    assert all((fx.claude / f"astrid-{sid}" / "SKILL.md").is_file() for sid in DEVELOPER_IDS)
    main = (fx.view / "packs" / "developer" / "SKILL.md").read_text(encoding="utf-8")
    assert "fast-iteration/SKILL.md" in _links(main)
    with pytest.raises(ValueError, match="--developer"):
        skills.install(["developer"], ["claude"], state_path=fx.state_path, developer=True)


def test_cli_exposes_developer_on_sync_install_and_uninstall(fx, monkeypatch, capsys):
    parser = skills_cli.build_parser()
    for argv in (["sync", "--developer"], ["update", "--developer"], ["install", "--developer"],
                 ["uninstall", "--developer"]):
        assert parser.parse_args(argv).developer is True
    assert parser.parse_args(["sync"]).developer is False

    calls = {}
    monkeypatch.setattr(skills_cli, "sync_fn", lambda **kwargs: calls.setdefault("sync", kwargs) and
                        {"actions": [], "registry": {"changed": False, "by_harness": {}}, "links": {}, "warnings": []})
    monkeypatch.setattr(skills_cli, "install_fn", lambda **kwargs: calls.setdefault("install", kwargs) and {"actions": []})
    monkeypatch.setattr(skills_cli, "uninstall_fn", lambda **kwargs: calls.setdefault("uninstall", kwargs) and {"actions": []})
    assert skills_cli.main(["sync", "--developer", "--harness", "claude"]) == 0
    assert calls["sync"]["developer"] is True and calls["sync"]["harness_names"] == ["claude"]
    assert skills_cli.main(["install", "--developer"]) == 0
    assert calls["install"]["developer"] is True and calls["install"]["pack_ids"] is None
    assert skills_cli.main(["uninstall", "--developer"]) == 0
    assert calls["uninstall"]["developer"] is True and calls["uninstall"]["pack_ids"] is None
    capsys.readouterr()
    for argv in (["sync", "timeline", "--developer"], ["install", "timeline", "--developer"],
                 ["uninstall", "--all", "--developer"]):
        with pytest.raises(Exception, match="--developer"):
            skills_cli.main(argv)


def test_registry_advertises_the_developer_flag_for_unselected_developer_skills():
    mains = discovery.list_skills()
    block = registry.render_registry_block([*mains, *discovery.list_extras(mains)])
    assert "developer / releasing" in block
    assert "developer skill: `astrid skills sync --developer`" in block
    assert "astrid skills install developer --skill releasing" not in block


def test_mcp_serves_developer_skills_only_with_developer_flag(monkeypatch, capsys):
    default = mcp_server.build_prompts()
    developer = mcp_server.build_prompts(developer=True)
    assert "developing-astrid" in default
    assert "fast-iteration" not in default and "releasing" not in default
    assert {"developing-astrid", "fast-iteration", "releasing"} <= set(developer)
    assert set(developer) - set(default) == {"fast-iteration", "releasing"}

    monkeypatch.setattr(mcp_server, "build_tools", lambda: {})
    monkeypatch.setattr(mcp_server, "_extra_tools", lambda: {})
    request = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "prompts/list"}) + "\n"
    for argv, expected in (([], False), (["--developer"], True)):
        monkeypatch.setattr(mcp_server.sys, "stdin", io.StringIO(request))
        assert mcp_server.main(argv) == 0
        [line] = capsys.readouterr().out.splitlines()
        names = {prompt["name"] for prompt in json.loads(line)["result"]["prompts"]}
        assert ("releasing" in names) is expected
        assert "developing-astrid" in names
