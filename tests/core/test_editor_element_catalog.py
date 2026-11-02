"""Coverage for the deterministic editor-facing Astrid element projection."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from astrid.core.element import catalog
from astrid.core.element.registry import load_default_registry

ROOT = Path(__file__).resolve().parents[2]
GENERATOR = ROOT / "scripts" / "gen_element_catalog.py"


def test_runtime_projection_uses_registry_and_shipped_projection_is_core_only() -> None:
    descriptors = catalog.list_element_descriptors()
    by_kind = {
        kind: {descriptor["id"] for descriptor in descriptors if descriptor["kind"] == kind}
        for kind in ("effect", "animation", "transition")
    }
    registry = load_default_registry()

    assert by_kind["effect"] == {item.id for item in registry.list(kind="effects")}
    assert by_kind["animation"] == {item.id for item in registry.list(kind="animations")}
    assert by_kind["transition"] == {item.id for item in registry.list(kind="transitions")}
    assert {"scrolling-guide", "audio-reactive-colour"}.issubset(by_kind["effect"])
    shipped = (ROOT / "astrid/packs/rendering/elements/catalog.ts").read_text(encoding="utf-8")
    assert all(item["packId"] == "rendering" for item in json.loads(
        shipped.split("Object.freeze(\n", 1)[1].split("\n);", 1)[0]
    ))


def test_projection_is_path_free_and_revision_stable() -> None:
    first = catalog.list_element_descriptors()
    second = catalog.list_element_descriptors()

    assert first == second
    assert all(descriptor["revision"].startswith("sha256:") for descriptor in first)
    assert all("/Users/" not in repr(descriptor) for descriptor in first)
    duration = next(item for item in first if item["id"] == "cross-fade")
    assert duration["parameters"] == [
        {
            "name": "durationFrames",
            "label": "Duration Frames",
            "description": "",
            "type": "number",
            "default": 8,
            "min": 1,
        }
    ]


def _install_fake_public_composition(tmp_path: Path, monkeypatch) -> None:
    descriptors = catalog.list_element_descriptors(pack_id="rendering")
    public = {
        kind: sorted(
            item["id"] for item in descriptors
            if item["kind"] == kind[:-1]
            and item["source"] == "pack:rendering"
            and item["renderability"]["preview"] == "unknown"
        )
        for kind in ("effects", "animations", "transitions")
    }
    package = tmp_path / "remotion" / "node_modules" / "@banodoco" / "timeline-composition"
    package.mkdir(parents=True)
    (package / "package.json").write_text(
        json.dumps({
            "name": "@banodoco/timeline-composition",
            "type": "module",
            "exports": {".": "./index.js", "./registries": "./registries.js"},
        }),
        encoding="utf-8",
    )
    (package / "index.js").write_text("export const DEFAULT_THEME = {};\n", encoding="utf-8")
    (package / "registries.js").write_text(
        "export function resolveElementRegistries() { return "
        + json.dumps({kind: {item_id: True for item_id in ids} for kind, ids in public.items()})
        + "; }\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ASTRID_REMOTION_PROJECT_DIR", str(package.parent.parent.parent))


def test_checked_in_typescript_projection_is_current(tmp_path: Path, monkeypatch) -> None:
    _install_fake_public_composition(tmp_path, monkeypatch)
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--check"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def _write_installed_catalog_fixture(pack_root: Path) -> str:
    pack_id = "catalog_fixture"
    declarations = {}
    for kind, element_id, singular in (
        ("animations", "fixture-animation", "animation"),
        ("transitions", "fixture-transition", "transition"),
    ):
        relative = f"rendering/elements/{kind}/{element_id}"
        element_root = pack_root / relative
        element_root.mkdir(parents=True)
        (element_root / "component.tsx").write_text(
            "export default function Element() { return null; }\n", encoding="utf-8"
        )
        (element_root / "element.yaml").write_text(
            json.dumps({
                "schema_version": 1,
                "id": element_id,
                "kind": singular,
                "pack_id": pack_id,
                "metadata": {"label": element_id},
                "schema": {"type": "object"},
            }),
            encoding="utf-8",
        )
        declarations[f"{kind}/{element_id}"] = relative

    manifest = [
        "schema_version: 3",
        f"id: {pack_id}",
        "name: Catalog Fixture",
        "version: 1.0.0",
        "status: active",
        "rendering:",
    ]
    for key, relative in declarations.items():
        manifest.extend((
            f"  {key}:",
            "    type: element",
            f"    path: {relative}/element.yaml",
            "    resources:",
            "      - kind: implementation",
            f"        path: {relative}/component.tsx",
        ))
    (pack_root / "pack.yaml").write_text("\n".join(manifest) + "\n", encoding="utf-8")
    return pack_id


def test_installed_animation_and_transition_fixture_remain_in_live_projection(
    tmp_path: Path, monkeypatch
) -> None:
    pack_root = tmp_path / "external" / "catalog_fixture"
    pack_root.mkdir(parents=True)
    pack_id = _write_installed_catalog_fixture(pack_root)
    monkeypatch.setenv("ASTRID_PACKS_PATH", str(pack_root.parent))
    catalog._clear_registry_cache()

    installed = catalog.list_element_descriptors(pack_id=pack_id)
    assert {(item["kind"], item["id"]) for item in installed} == {
        ("animation", "fixture-animation"),
        ("transition", "fixture-transition"),
    }


def test_shipped_catalog_check_ignores_absent_or_malformed_optional_pack(
    tmp_path: Path, monkeypatch
) -> None:
    from scripts.gen_element_catalog import generate

    _install_fake_public_composition(tmp_path, monkeypatch)
    catalog_path = ROOT / "astrid/packs/rendering/elements/catalog.ts"
    shipped = catalog_path.read_bytes()

    monkeypatch.setenv("ASTRID_PACKS_PATH", "")
    absent_generation = generate().encode("utf-8")
    absent_check = subprocess.run(
        [sys.executable, str(GENERATOR), "--check"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert absent_check.returncode == 0, absent_check.stderr

    malformed_root = tmp_path / "malformed-optional"
    malformed_pack = malformed_root / "broken-pack"
    malformed_pack.mkdir(parents=True)
    (malformed_pack / "pack.yaml").write_text("id: [broken\n", encoding="utf-8")
    monkeypatch.setenv("ASTRID_PACKS_PATH", str(malformed_root))
    malformed_generation = generate().encode("utf-8")
    malformed_check = subprocess.run(
        [sys.executable, str(GENERATOR), "--check"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert malformed_check.returncode == 0, malformed_check.stderr

    assert absent_generation == malformed_generation == shipped
