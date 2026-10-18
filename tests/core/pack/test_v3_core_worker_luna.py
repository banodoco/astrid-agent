"""Focused checks for the V3 core source-worker seams.

These tests deliberately avoid the full pack corpus and runtime adapters.  The
union lane can run them after the shared source and dependency state settles.
"""

from pathlib import Path

from astrid.core.element import registry as element_registry
from astrid.core.element.schema import ElementDefinition, ElementValidationError
from astrid.core.execution.capability_ledger import capability_execution_admission
from astrid.core.pack.definition import PackDefinition
from astrid.core.pack.loader import PackScan, QuarantinedPack, discover_packs, scan_packs
from astrid.core.pack._common import PackValidationError


def _pack(root: Path, pack_id: str) -> PackDefinition:
    return PackDefinition(
        id=pack_id,
        name=pack_id,
        version="1.0.0",
        root=root,
        manifest_path=root / "pack.yaml",
        metadata={},
        schema_version="3",
    )


def test_non_strict_pack_scan_quarantines_optional_failure_without_downgrading_discovery(monkeypatch, tmp_path):
    packs_root = tmp_path / "packs"
    good_root = packs_root / "good"
    bad_root = packs_root / "bad"
    packs_root.mkdir()
    good_root.mkdir()
    bad_root.mkdir()
    (good_root / "pack.yaml").touch()
    (bad_root / "pack.yaml").touch()

    def fake_manifest_path(root):
        return Path(root) / "pack.yaml"

    def fake_load(path):
        if Path(path).parent.name == "bad":
            raise PackValidationError("invalid optional pack")
        return _pack(Path(path).parent, Path(path).parent.name)

    monkeypatch.setattr("astrid.core.pack.loader.pack_manifest_path", fake_manifest_path)
    monkeypatch.setattr("astrid.core.pack.loader.load_pack_manifest", fake_load)

    observed = scan_packs(packs_root, strict=False)
    assert isinstance(observed, PackScan)
    assert [pack.id for pack in observed.packs] == ["good"]
    assert [record.pack_id for record in observed.quarantined] == ["bad"]
    assert isinstance(observed.quarantined[0], QuarantinedPack)

    try:
        discover_packs(packs_root)
    except PackValidationError as exc:
        assert "invalid optional pack" in str(exc)
    else:
        raise AssertionError("strict discovery admitted an invalid pack")


def test_element_loading_isolates_invalid_sibling_and_keeps_diagnostic(monkeypatch, tmp_path):
    pack_root = tmp_path / "demo"
    good_manifest = pack_root / "elements" / "effects" / "good" / "element.yaml"
    bad_manifest = pack_root / "elements" / "effects" / "bad" / "element.yaml"
    good_manifest.parent.mkdir(parents=True)
    bad_manifest.parent.mkdir(parents=True)
    pack = _pack(pack_root, "demo")
    pack = PackDefinition(**{**pack.__dict__, "schema_version": "2"})

    def fake_paths(*_args, **_kwargs):
        return (("effects", good_manifest), ("effects", bad_manifest))

    def fake_load(root, **kwargs):
        if Path(root).name == "bad":
            raise ElementValidationError("invalid sibling")
        return ElementDefinition(
            id="good",
            kind="effects",
            root=Path(root),
            source=kwargs["source"],
            editable=False,
            priority=30,
            component=Path(root) / "component.tsx",
            schema={},
            defaults={},
            metadata={"pack_id": "demo"},
        )

    monkeypatch.setattr(element_registry, "iter_element_manifest_paths", fake_paths)
    monkeypatch.setattr(element_registry, "load_element_definition", fake_load)

    elements, skipped = element_registry._load_pack_elements_with_diagnostics(
        (pack,),
        element_kind_registry=element_registry.ELEMENT_KIND_REGISTRY,
    )
    assert [element.id for element in elements] == ["good"]
    assert len(skipped) == 1
    assert skipped[0].pack_id == "demo"
    assert skipped[0].path == bad_manifest
    assert "invalid sibling" in skipped[0].error


def test_capability_execution_admission_fails_closed_for_unapproved_ids():
    ledger = {
        "capabilities": [
            {"id": "approved.run", "disposition": "required", "evidence_reason": "shipped"},
            {"id": "retired.run", "disposition": "retired", "evidence_reason": "removed"},
        ],
        "sources": {
            "coverage": {"source_labels": {"complete": True}},
            "executor_inventory": [],
        },
    }

    assert capability_execution_admission(ledger, "approved.run")["executable"] is True
    assert capability_execution_admission(ledger, "retired.run")["executable"] is False
    assert capability_execution_admission(ledger, "not-declared")["executable"] is False

