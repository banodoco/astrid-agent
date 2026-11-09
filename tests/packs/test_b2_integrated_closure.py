"""Integrated strict closure (schema_version 3) on the current capability catalog."""

from __future__ import annotations

from pathlib import Path

import yaml

from astrid.core.element.registry import load_default_registry as load_elements
from astrid.core.execution.executor.registry import (
    load_default_registry as load_executors,
)
from astrid.core.execution.orchestrator.registry import (
    load_default_registry as load_orchestrators,
)
from astrid.core.pack import discover_packs
from astrid.core.pack.canonical import BundledCatalog, validate_canonical_pack
from astrid.core.pack.validate_first_party import _FIRST_PARTY_PACK_IDS


ROOT = Path(__file__).resolve().parents[2]
PACKS = ROOT / "astrid/packs"
# One source of truth for the shipped pack ids: the first-party validator inventory.
PACK_IDS = tuple(sorted(_FIRST_PARTY_PACK_IDS))
RETIRED = {"builtin", "references", "astrid", "runaway", "shots", "timeline"}


def test_current_catalog_is_exact_strict_v3_capability_set() -> None:
    manifests = sorted(PACKS.glob("*/pack.yaml"))
    assert tuple(path.parent.name for path in manifests) == PACK_IDS
    assert not any((PACKS / pack_id / "pack.yaml").exists() for pack_id in RETIRED)
    assert not (PACKS / "_core/pack.yaml").exists()

    for manifest in manifests:
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        assert data["schema_version"] == 3
        assert data["id"] == manifest.parent.name
        assert "database" not in data
        assert "content" not in data
        assert data["documentation"] == {
            "kind": "skill",
            "path": "docs/SKILL.md",
        }
        assert (manifest.parent / "docs/SKILL.md").is_file()
        assert validate_canonical_pack(manifest.parent).id == data["id"]


def test_catalog_preserves_stage1_capability_census() -> None:
    packs = discover_packs()
    executors = load_executors()
    orchestrators = load_orchestrators(executor_registry=executors)
    elements = load_elements()
    assert tuple(pack.id for pack in packs) == PACK_IDS
    assert (len(packs), len(executors.list()), len(orchestrators.list()), len(elements.list())) == (
        28,
        107,
        1,
        44,
    )


def test_bundled_resources_are_confined_and_readable() -> None:
    catalog = BundledCatalog.from_root(PACKS)
    assert tuple(entry.id for entry in catalog.ordered_entries) == PACK_IDS
    for entry in catalog.entries:
        assert all(handle.resolved.is_relative_to(entry.root) for handle in entry.resource_handles)
        assert all(handle.resolved.exists() for handle in entry.resource_handles)


def test_examples_and_external_fixtures_use_canonical_schema_without_installed_overlay() -> None:
    candidates = [
        *sorted((ROOT / "examples/packs").glob("*/pack.yaml")),
        ROOT / "tests/fixtures/external_pack/pack.yaml",
        ROOT / "tests/fixtures/local_effect_smoke/astrid/packs/local/pack.yaml",
    ]
    for manifest in candidates:
        data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
        # Canonical admission accepts schema_version 2 and 3; examples and
        # fixtures intentionally cover both.
        assert data["schema_version"] in (2, 3), manifest
        assert "database" not in data, manifest
        assert validate_canonical_pack(manifest.parent).id == data["id"]

    installed_root = ROOT / "tests/fixtures/renderer_packs/discovery/installed"
    assert not installed_root.exists()
