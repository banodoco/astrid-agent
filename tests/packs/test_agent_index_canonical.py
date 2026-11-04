"""The agent index projects public declarations without executing pack code."""
from __future__ import annotations

import json
import socket
import subprocess
from pathlib import Path

import yaml

from astrid.core.pack import agent_index


def _write_pack(parent: Path, pack_id: str, manifest: dict, files: dict[str, str]) -> Path:
    root = parent / pack_id
    root.mkdir(parents=True)
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (root / "pack.yaml").write_text(yaml.safe_dump({
        "schema_version": 3, "id": pack_id, "name": pack_id.title(),
        "version": "1.0.0", **manifest,
    }), encoding="utf-8")
    return root


def _fixture_packs(tmp_path: Path, monkeypatch) -> tuple[Path, Path, Path]:
    action = {"description": "Echo a request.",
              "invocation": {"kind": "python", "path": "actions/echo.py", "function": "run"},
              "inputs": [], "outputs": [{"name": "result", "type": "json", "mode": "create"}]}
    schemas = {"description": "Use authored schemas.",
               "invocation": {"kind": "python", "path": "actions/schema.py", "function": "run"},
               "inputs": "shared/input.schema.json", "outputs": {"type": "object", "properties": {"ok": {"default": False}}}}
    aliases = [{"kind": "action", "alias": "demo.old", "canonical_id": "demo.echo"},
               {"kind": "action", "alias": "demo.older", "canonical_id": "demo.old"},
               {"kind": "renderer", "alias": "demo.wrong-kind", "canonical_id": "demo.schema"}]
    demo = _write_pack(tmp_path, "demo", {
        "description": "Demonstration pack.",
        "agent": {"purpose": "Exercise canonical projection.", "normal_entrypoints": ["demo.older", "demo.wrong-kind"]},
        "actions": {"echo": action, "schema": schemas},
        "ui": {"editor": {"type": "editor", "entry": "ui/editor.tsx", "target": "video-editor",
                          "resources": [{"kind": "documentation", "path": "ui/guide.md"}]}},
        "rendering": {"animations/slide-left": {"type": "element", "path": "rendering/element.yaml"}},
        "documents": {"note": {"format_version": 1, "schema": "shared/note.schema.json"}},
        "aliases": aliases, "documentation": {"kind": "skill", "path": "docs/SKILL.md"},
    }, {
        "actions/echo.py": "raise AssertionError('discovery imported action')\n",
        "actions/schema.py": "raise AssertionError('discovery imported action')\n",
        "shared/input.schema.json": '{"type":"object"}',
        "shared/note.schema.json": '{"type":"string"}',
        "ui/editor.tsx": "export default editor;\n",
        "ui/guide.md": "# Editor guide\n",
        "rendering/element.yaml": yaml.safe_dump({"id": "slide-left", "kind": "animation", "pack_id": "demo", "schema_version": 1}),
        "docs/SKILL.md": "---\nname: demo\ndescription: Fixture guidance.\n---\n# Guidance\n",
    })
    support = _write_pack(tmp_path, "support", {
        "description": "Support documents only.",
        "documents": {"inline": {"format_version": "2026-10", "schema": False}},
        "documentation": {"kind": "none", "reason": "No guidance needed."},
    }, {})
    legacy = _write_pack(tmp_path, "legacy", {
        "schema_version": 2, "description": "Legacy pack.", "capabilities": ["legacy"],
        "agent": {"purpose": "Existing purpose.", "normal_entrypoints": ["legacy.run"]},
        "content": {"executors": "executors"},
    }, {
        "README.md": "# Legacy\n",
        "executors/run/executor.yaml": yaml.safe_dump({
            "schema_version": 1, "id": "legacy.run", "name": "Run", "kind": "built_in",
            "description": "Legacy executor.", "version": "1.0.0",
            "runtime": {"type": "python-cli", "entrypoint": "run.py"},
        }),
        "executors/run/run.py": "raise AssertionError('discovery executed legacy')\n",
        "executors/run/STAGE.md": "Legacy stage excerpt.\n## Details\nMore.\n",
    })
    monkeypatch.setattr(agent_index, "packs_root", lambda: tmp_path)
    return demo, support, legacy


def _no_execution(*args, **kwargs):
    raise AssertionError("metadata discovery attempted execution or network access")


def test_canonical_public_contributions_aliases_and_docs_are_deterministic(tmp_path: Path, monkeypatch) -> None:
    demo, support, _legacy = _fixture_packs(tmp_path, monkeypatch)
    monkeypatch.setattr(subprocess, "Popen", _no_execution)
    monkeypatch.setattr(socket, "socket", _no_execution)

    first = agent_index.build_agent_index()
    second = agent_index.build_agent_index()
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
    packs = {pack["pack_id"]: pack for pack in first["packs"]}
    projected = packs["demo"]
    assert projected["purpose"] == "Exercise canonical projection."
    assert projected["documentation"] == {"kind": "skill", "path": "docs/SKILL.md"}
    assert projected["docs_paths"]["documentation"] == str(demo / "docs/SKILL.md")
    components = {item["id"]: item for item in projected["components"]}
    assert set(components) == {"demo.echo", "demo.schema", "demo.editor", "animations/slide-left", "demo.note"}
    assert [(item["id"], item["section"], item["kind"]) for item in projected["components"]] == sorted([
        ("demo.echo", "actions", "action"), ("demo.schema", "actions", "action"),
        ("demo.editor", "ui", "editor"), ("animations/slide-left", "rendering", "element"),
        ("demo.note", "documents", "document"),
    ])
    assert components["demo.echo"]["description"] == "Echo a request."
    assert components["demo.echo"]["inputs"] == []
    assert components["demo.echo"]["outputs"] == [{"name": "result", "type": "json", "mode": "create"}]
    assert components["demo.schema"]["inputs"] == "shared/input.schema.json"
    assert components["demo.schema"]["outputs"] == {"type": "object", "properties": {"ok": {"default": False}}}
    assert components["demo.note"]["schema"] == "shared/note.schema.json"
    assert components["demo.editor"]["declaration"] == {
        "type": "editor", "entry": "ui/editor.tsx", "target": "video-editor",
        "resources": [{"kind": "documentation", "path": "ui/guide.md"}],
    }
    assert components["demo.editor"]["docs_paths"]["documentation"] == str(demo / "ui/guide.md")
    assert components["demo.echo"]["is_entrypoint"] is True
    assert components["demo.echo"]["aliases"] == ["demo.old", "demo.older"]
    assert components["demo.schema"]["is_entrypoint"] is False
    assert components["demo.schema"]["aliases"] == []
    assert agent_index.build_agent_index(pack_id="demo")["components"] == projected["components"]
    assert projected["aliases"] == [
        {"kind": "action", "alias": "demo.old", "canonical_id": "demo.echo"},
        {"kind": "action", "alias": "demo.older", "canonical_id": "demo.old"},
        {"kind": "renderer", "alias": "demo.wrong-kind", "canonical_id": "demo.schema"},
    ]
    only_support = packs["support"]
    assert only_support["purpose"] == "Support documents only."
    assert only_support["documentation"] == {"kind": "none", "reason": "No guidance needed."}
    assert only_support["components"][0]["id"] == "support.inline"
    assert only_support["components"][0]["schema"] is False
    assert agent_index.build_agent_index(pack_id="missing") is None


def test_legacy_component_projection_stays_compatible(tmp_path: Path, monkeypatch) -> None:
    _demo, _support, legacy = _fixture_packs(tmp_path, monkeypatch)
    projected = agent_index.build_agent_index(pack_id="legacy")
    assert projected["purpose"] == "Existing purpose."
    assert projected["capabilities"] == ["legacy"]
    assert projected["normal_entrypoints"] == ["legacy.run"]
    assert projected["docs_paths"]["readme"] == str(legacy / "README.md")
    assert projected["components"] == [{
        "id": "legacy.run", "name": "Run", "kind": "built_in", "description": "Legacy executor.",
        "runtime": {"type": "python-cli", "entrypoint": "run.py", "callable": None},
        "is_entrypoint": True, "docs_paths": {"stage": str(legacy / "executors/run/STAGE.md")},
        "stage_excerpt": "Legacy stage excerpt.",
    }]
