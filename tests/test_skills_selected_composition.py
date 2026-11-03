"""Selected declarations drive core resources and MCP without acquisition.

Source patterns: tests/test_skills_package_data.py::
test_default_composition_preserves_opt_outs_without_acquisition and
 tests/test_skills.py::test_view_sync_retains_selection_and_migrates_old_installs.
Installed proof uses the separately proven Timeline installed-wheel pattern.
"""
from __future__ import annotations

import json
from pathlib import Path

from astrid import mcp_server, skills
from astrid.skills import discovery, registry, state
from astrid.skills.view import compose_view
from scripts import gen_capability_index, gen_tool_catalog


def _empty_selection(tmp_path):
    descriptors = discovery.list_skills()
    current = state.load(tmp_path / "skills.json")
    current["disabled_defaults"]["claude"] = list(skills.default_pack_ids(descriptors))
    return descriptors, current


def test_selected_declarations_drive_views_and_mcp(tmp_path):
    descriptors, current = _empty_selection(tmp_path)
    core = next(d for d in descriptors if d.pack_id == "_core")
    assert "core/resources/skills" in core.skill_md.as_posix()
    zero = skills._sync_descriptors_for_harness(descriptors, current, "claude", deep=False)
    assert [d.pack_id for d in zero] == ["_core"]
    one = skills._sync_descriptors_for_harness(
        descriptors, current, "claude", deep=False, selected_pack_ids=("pixel",),
    )
    pixel = next(d for d in one if d.pack_id == "pixel")
    manifest = skills.discovery.load_pack_manifest(pixel.skill_dir.parent / "pack.yaml")
    assert "sprites" in manifest.actions
    assert pixel.skill_md == manifest.root / manifest.documentation["path"]
    for selected, expected in ((zero, set()), (one, {"pixel"})):
        root = tmp_path / ("one" if expected else "zero")
        packs = [d for d in selected if d.pack_id != "_core"]
        _, _, view_packs = compose_view(root, core, packs)
        registry.regenerate(skill_md_path=root / "creative-work/references/packs.md", descriptors=view_packs, view_root=root)
        index = (root / "references/capabilities.md").read_text()
        directory = (root / "creative-work/references/packs.md").read_text()
        assert ("pixel.sprites" in index) is bool(expected)
        assert ("packs/pixel/SKILL.md" in directory) is bool(expected)
        assert "vibecomfy.run" not in index
        prompts = mcp_server.build_prompts(selected_pack_ids=expected)
        assert ("pixel" in prompts) is bool(expected)
        assert ("pixel.sprites" in prompts["astrid"]["text"]) is bool(expected)
        assert "vibecomfy" not in prompts
        reply = mcp_server.handle({"id": 1, "method": "prompts/list"}, {"prompts": prompts})
        assert ("pixel" in {row["name"] for row in reply["result"]["prompts"]}) is bool(expected)
    compose_view(tmp_path / "one", core, [])
    assert not (tmp_path / "one/packs/pixel").exists()
    assert "pixel.sprites" not in (tmp_path / "one/references/capabilities.md").read_text()


def test_missing_capability_refuses_before_process_or_acquisition(monkeypatch, tmp_path):
    descriptors, current = _empty_selection(tmp_path)
    monkeypatch.setattr(skills, "list_skills", lambda: descriptors)
    monkeypatch.setattr(state, "load", lambda *args: current)
    def forbidden(*args, **kwargs):
        raise AssertionError("unselected capability attempted acquisition")
    monkeypatch.setattr(mcp_server, "_run", forbidden)
    monkeypatch.setattr("astrid.core.pack.source_setup.provision", forbidden)
    from astrid.sdk import AstridClient
    monkeypatch.setattr(AstridClient, "open_from_launcher", forbidden)
    before = json.dumps(current, sort_keys=True)
    result = mcp_server.call_tool(mcp_server._extra_tools()["invoke"], {
        "capability_id": "pixel.sprites", "kind": "executor", "project": "demo",
    })
    assert result["isError"] is True
    assert result["structuredContent"]["error"]["code"] == "capability_unavailable"
    assert json.dumps(current, sort_keys=True) == before


def test_capability_index_check_is_non_mutating(monkeypatch, tmp_path):
    target = tmp_path / "capabilities.md"
    target.write_text("stale\n")
    monkeypatch.setattr("sys.argv", ["gen_capability_index.py", "--check", "--target", str(target)])
    assert gen_capability_index.main() == 1
    assert target.read_text() == "stale\n"
    target.write_text(gen_capability_index.update_skill("stale\n", gen_capability_index.render_block()))
    before = target.read_bytes()
    assert gen_capability_index.main() == 0
    assert target.read_bytes() == before
    assert "pixel.sprites" not in before.decode()


def test_tool_catalog_selection_preserves_declared_bindings():
    all_tools = json.loads(gen_tool_catalog.generate())["tools"]
    assert all_tools
    assert json.loads(gen_tool_catalog.generate(selected_pack_ids=()))["tools"] == []
    pack_id = all_tools[0]["pack_id"]
    selected = json.loads(gen_tool_catalog.generate(selected_pack_ids=(pack_id,)))["tools"]
    assert selected == [tool for tool in all_tools if tool["pack_id"] == pack_id]
    assert all(tool["manifest"]["sha256"] and tool["entry"]["sha256"] for tool in selected)
