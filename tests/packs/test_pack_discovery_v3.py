"""F03: canonical declarations and the existing read-only discovery layers."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from astrid.core.pack import PackValidationError, discover_packs, load_pack_manifest
from astrid.core.pack.canonical import validate_canonical_pack
from astrid.core.pack.discovery import (
    DiscoveredPack,
    discover_canonical_pack_metadata,
    discover_pack_metadata,
)


SKILL = "---\nname: directly-authored\ndescription: Use the declared demo pack.\n---\nSee [guide](references/guide.md).\n"


def write_pack(parent: Path, pack_id: str = "demo", *, version: int = 3,
               files: dict[str, str] | None = None, **sections) -> Path:
    root = parent / pack_id
    root.mkdir(parents=True)
    for name, text in (files or {}).items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    manifest = {"schema_version": version, "id": pack_id, "name": "Demo", "version": "1.0.0",
                **({"capabilities": ["testing"]} if version == 2 else
                   {"documents": {"note": {"format_version": 1, "schema": {"type": "object"}}}}),
                **sections}
    (root / "pack.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return root


def inventory(monkeypatch, *roots: Path):
    sources = tuple(SimpleNamespace(pack_root=root, pack_id=root.name, revision="a" * 40,
                                   manifest_sha256="b" * 64, tree_sha256="c" * 64) for root in roots)
    result = SimpleNamespace(sources=sources, identity="fixture-inventory")
    monkeypatch.setattr("astrid.core.pack.source_setup.active_source_inventory", lambda **kwargs: result)
    monkeypatch.delenv("ASTRID_PACKS_PATH", raising=False)
    return result


def test_v3_loader_populates_exact_canonical_declarations_without_import(tmp_path: Path) -> None:
    actions = {"echo": {"description": "Echo a message.",
                         "invocation": {"kind": "python", "path": "actions/echo.py", "function": "echo"},
                         "inputs": [{"name": "message", "type": "string"}],
                         "outputs": [{"name": "result", "type": "json", "mode": "create"}],
                         "metadata": {"tags": ["fixture"]}}}
    ui = {"editor": {"type": "editor", "entry": "ui/editor/extension.tsx", "target": "video-editor"}}
    rendering = {"backend": {"type": "renderer", "path": "rendering/backend/renderer.yaml"}}
    documents = {"note": {"format_version": "2026-10", "schema": "shared/note.json",
                           "resources": [{"kind": "example", "path": "shared/example.json"}]}}
    pointer = {"kind": "skill", "path": "docs/SKILL.md"}
    root = write_pack(tmp_path, actions=actions, ui=ui, rendering=rendering, documents=documents,
                      documentation=pointer, files={
                          "actions/echo.py": "raise AssertionError('discovery must not import')\n",
                          "ui/editor/extension.tsx": "export default editor;\n",
                          "rendering/backend/renderer.yaml": yaml.safe_dump({"id": "demo.backend", "schema_version": 1}),
                          "shared/note.json": '{"type":"object"}', "shared/example.json": "{}",
                          "docs/SKILL.md": SKILL, "docs/references/guide.md": "# Guide\n",
                          "actions/echo/skill/SKILL.md": SKILL,
                          "skill/SKILL.md": SKILL,
                      })
    canonical = validate_canonical_pack(root).definition.to_dict()
    pack = load_pack_manifest(root / "pack.yaml")
    assert pack.schema_version == "3"
    assert pack.content == pack.extensions == {}
    for name, expected in (("actions", actions), ("ui", ui), ("rendering", rendering),
                           ("documents", documents), ("documentation", pointer)):
        assert getattr(pack, name) == canonical[name] == expected
        assert pack.to_dict()[name] == expected
    discovered = DiscoveredPack(pack, "extra", 0)
    assert discovered.documentation_path == root / "docs/SKILL.md"
    assert discovered.skill_roots() == (root / "docs",)
    assert (root / "docs/SKILL.md").read_text() == SKILL
    assert set(pack.documentation) == {"kind", "path"}
    pack.actions["echo"]["metadata"]["tags"].append("mutable-dto")
    assert validate_canonical_pack(root).definition.to_dict()["actions"] == actions


@pytest.mark.parametrize("version", [2, 3])
def test_v2_and_v3_discover_one_declared_skill(tmp_path: Path, version: int) -> None:
    path = "skill/SKILL.md" if version == 2 else "docs/SKILL.md"
    root = write_pack(tmp_path, version=version, files={path: SKILL},
                      documentation={"kind": "skill", "path": path})
    pack, = discover_packs(tmp_path)
    assert pack.schema_version == str(version)
    assert pack.documentation == {"kind": "skill", "path": path}
    discovered = DiscoveredPack(pack, "source", 0)
    assert discovered.documentation_path == root / path
    assert discovered.skill_roots() == ((root / path).parent,)
    if version == 2:
        assert pack.actions == pack.ui == pack.rendering == pack.documents == {}


@pytest.mark.parametrize("pointer,files,expected", [
    ({}, {"skill/SKILL.md": SKILL, "actions/echo/skill/SKILL.md": SKILL}, None),
    ({"kind": "none", "reason": "No agent guidance"}, {}, None),
    ({"kind": "agents", "path": "AGENTS.md"}, {"AGENTS.md": "# Instructions\n"}, "AGENTS.md"),
])
def test_documentation_dispositions_never_infer_skills(tmp_path: Path, pointer, files, expected) -> None:
    root = write_pack(tmp_path, files=files, **({"documentation": pointer} if pointer else {}))
    pack = load_pack_manifest(root / "pack.yaml")
    discovered = DiscoveredPack(pack, "source", 0)
    assert discovered.documentation_path == (root / expected if expected else None)
    assert discovered.skill_roots() == ()


def test_layer_priority_and_managed_identity_are_preserved(tmp_path: Path, monkeypatch) -> None:
    source = write_pack(tmp_path / "source", version=2)
    project = tmp_path / "project"
    local = write_pack(project / "astrid/packs", "local")
    managed = write_pack(tmp_path / "managed")
    extra = write_pack(tmp_path / "extra", version=2)
    env = write_pack(tmp_path / "env")
    inventory(monkeypatch, managed)
    monkeypatch.setenv("ASTRID_PACKS_PATH", str(env))
    def scan(root=None):
        return discover_packs(source.parent if root is None else root)
    rows = discover_pack_metadata(project_root=project, discover_packs_fn=scan,
                                  extra_pack_roots=(str(managed), str(extra)))
    assert [r.source_kind for r in rows] == ["source", "local", "managed", "extra", "env"]
    assert [r.pack_dir for r in rows] == [source, local, managed, extra, env]
    assert [r.priority_index for r in rows] == list(range(5))
    assert rows[2].source_revision == "a" * 40
    assert rows[2].source_manifest_sha256 == "b" * 64
    assert rows[2].source_tree_sha256 == "c" * 64
    assert rows[2].source_inventory_identity == "fixture-inventory"
    canonical = discover_canonical_pack_metadata(project_root=project, extra_pack_roots=(str(managed), str(extra)))
    assert [r.source_kind for r in canonical] == ["local", "managed", "extra", "env"]
    assert [r.pack_dir for r in canonical] == [local, managed, extra, env]
    assert [r.priority_index for r in canonical] == list(range(4))
    assert canonical[1].source_inventory_identity == "fixture-inventory"


@pytest.mark.parametrize("api", [discover_pack_metadata, discover_canonical_pack_metadata])
def test_external_broken_hidden_and_duplicate_roots_are_isolated(tmp_path: Path, monkeypatch, caplog, api) -> None:
    extras = tmp_path / "extra"
    good_v2 = write_pack(extras, "good_v2", version=2)
    good_v3 = write_pack(extras, "good_v3")
    write_pack(extras, "hidden", visibility="hidden")
    write_pack(extras, "local")
    broken = write_pack(extras, "broken")
    (broken / "pack.yaml").write_text("schema_version: 999\n")
    alternate = extras / "alternate"
    alternate.mkdir()
    (alternate / "pack.yml").write_text("schema_version: 3\n")
    broken_managed = write_pack(tmp_path / "managed", "managed_broken")
    (broken_managed / "pack.yaml").write_text("schema_version: 999\n")
    inventory(monkeypatch, broken_managed)
    monkeypatch.setenv("ASTRID_PACKS_PATH", str(extras))
    kwargs = {"project_root": tmp_path / "project", "extra_pack_roots": (str(extras), str(extras))}
    if api is discover_pack_metadata:
        kwargs["discover_packs_fn"] = lambda *args: ()
    rows = api(**kwargs)
    assert [(r.id, r.source_kind, r.priority_index) for r in rows] == [
        ("good_v2", "extra", 0), ("good_v3", "extra", 1)]
    assert [r.pack_dir for r in rows] == [good_v2, good_v3]
    assert "broken" in caplog.text and "managed_broken" in caplog.text


def test_broken_source_remains_strict_and_core_shell_is_not_a_pack(tmp_path: Path) -> None:
    core = tmp_path / "_core/docs"
    core.mkdir(parents=True)
    (core / "SKILL.md").write_text(SKILL)
    assert discover_packs(tmp_path) == ()
    assert (core / "SKILL.md").read_text() == SKILL
    broken = write_pack(tmp_path, "broken")
    (broken / "pack.yaml").write_text("schema_version: 999\n")
    with pytest.raises(PackValidationError, match="schema_version"):
        discover_pack_metadata(project_root=tmp_path, discover_packs_fn=lambda *args: discover_packs(tmp_path))
