"""HM2 R05: doctor diagnostics agree with component loaders and quarantine."""

from __future__ import annotations

import json

import pytest

from astrid.core.execution import generic_host
from astrid.core.execution.executor import folder as executor_folder
from astrid.core.execution.orchestrator import folder as orchestrator_folder
from astrid.core.pack.loader import scan_pack_roots
from astrid.pack_api import VERSION as PACK_API_VERSION


def _invalid_component(root, kind):
    folder = root / f"{kind}s" / "broken"
    folder.mkdir(parents=True)
    manifest = folder / f"{kind}.json"
    manifest.write_text("{}", encoding="utf-8")
    return folder, manifest


def test_r05_doctor_reports_executor_and_orchestrator_loader_errors_without_writing(tmp_path):
    """R05: the public diagnostic includes each malformed component and its fix."""
    roots = [_invalid_component(tmp_path, kind) for kind in ("executor", "orchestrator")]
    errors = []
    for (folder, _), loader in zip(roots, (
        executor_folder.load_folder_executor, orchestrator_folder.load_folder_orchestrator,
    ), strict=True):
        with pytest.raises(ValueError) as error:
            loader(folder)
        errors.append(str(error.value))
    section = generic_host.executor_skip_section([tmp_path])
    assert section["error"] is None
    assert section["count"] == 2
    assert section["skipped"] == [
        {"kind": kind, "folder": str(folder), "error": error,
         "fix": f"fix the {kind} manifest, then run: python3 -m astrid.core.pack.cli validate {folder.parents[1]}"}
        for kind, (folder, _), error in zip(
            ("executor", "orchestrator"), roots, errors, strict=True,
        )
    ]
    assert [manifest.read_text(encoding="utf-8") for _, manifest in roots] == ["{}", "{}"]


def test_r05_quarantined_pack_is_not_reported_as_individually_broken_components(tmp_path):
    """R05: a quarantined pack stays excluded while a sibling diagnostic survives."""
    collection = tmp_path / "packs"
    quarantine = collection / "hm2_quarantined"
    _invalid_component(quarantine, "executor")
    _invalid_component(quarantine, "orchestrator")
    (quarantine / "pack.yaml").write_text(json.dumps({
        "schema_version": 3, "id": "hm2_quarantined", "name": "HM2 quarantined",
        "version": "1.0.0", "pack_api": f"{PACK_API_VERSION[0] + 1}.0", "actions": {},
    }), encoding="utf-8")
    outside, _ = _invalid_component(collection / "loose", "orchestrator")
    scan = scan_pack_roots((collection,))
    assert [record.pack_id for record in scan.quarantined] == ["hm2_quarantined"]
    section = generic_host.executor_skip_section([collection])
    assert section["error"] is None
    assert section["count"] == 1
    assert section["skipped"][0]["folder"] == str(outside)
    assert section["skipped"][0]["kind"] == "orchestrator"


def test_r05_doctor_exposes_top_level_scan_failure_as_a_section_error(tmp_path, monkeypatch):
    """R05: an unreadable scan does not become an empty successful diagnostic."""
    def unavailable(_root):
        raise OSError("fixture component scan unavailable")

    monkeypatch.setattr(executor_folder, "discover_folder_executor_roots", unavailable)
    assert generic_host.executor_skip_section([tmp_path]) == {
        "count": 0, "skipped": [], "error": "fixture component scan unavailable",
    }
