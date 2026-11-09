from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrid.packs.vibecomfy.asset_manifest import (
    AssetManifestError,
    build_asset_manifest,
    read_archive,
    read_archive_bytes,
    resolve_inputs,
)
from astrid.packs.vibecomfy.actions.run.run import _stage_managed_assets


def _write_archive(path: Path, source: Path) -> dict:
    from astrid.packs.h3_av.shared.compile import _write_asset_bundle

    return _write_asset_bundle(
        path,
        {"source_video": source},
        workflow_inputs={"seed": 7, "prompt": "task-specific prompt"},
        lineage={
            "source_video": {
                "kind": "derived",
                "original_asset": "matrix-prefix",
                "operation": "normalize_h3_source_av",
            }
        },
    )


def test_bundled_only_manifest_resolves_media_and_task_inputs(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source-bytes")
    archive = tmp_path / "managed-assets.zip"
    manifest = _write_archive(archive, source)

    resolved = read_archive(archive)
    assert resolved.bindings["source_video"].endswith("source.mp4")
    assert resolved.bindings["seed"] == 7
    assert resolved.bindings["prompt"] == "task-specific prompt"
    assert resolved.manifest["assets"][0]["lineage"]["original_asset"] == "matrix-prefix"

    staged = _stage_managed_assets(
        str(archive), readiness_profile=None, output_root=tmp_path / "run"
    )
    assert staged == {"source_video": resolved.bindings["source_video"]}
    assert (tmp_path / "run" / "engine-input" / staged["source_video"]).read_bytes() == b"source-bytes"
    assert manifest["workflow_inputs"]["seed"] == 7


def test_matching_duplicate_input_is_accepted_and_conflict_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source-bytes")
    manifest = build_asset_manifest(
        {"source_video": source}, workflow_inputs={"seed": 7}
    )
    member = Path(manifest["assets"][0]["member"]).name

    assert resolve_inputs(manifest, {"seed": 7, "source_video": member}) == {
        "seed": 7,
        "source_video": member,
    }
    with pytest.raises(AssetManifestError, match="managed assets conflict.*seed"):
        resolve_inputs(manifest, {"seed": 8})


def test_archive_bytes_and_manifest_are_deterministic(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source-bytes")
    archive = tmp_path / "managed-assets.zip"
    _write_archive(archive, source)

    from_bytes = read_archive_bytes(archive.read_bytes())
    from_path = read_archive(archive)
    assert from_bytes.manifest == from_path.manifest
    assert from_bytes.members == from_path.members
    assert json.dumps(from_bytes.manifest, sort_keys=True)
