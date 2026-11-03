import hashlib
import json
from types import SimpleNamespace
from pathlib import Path

import pytest

from astrid.packs.iteration.api import AssembleError, assemble_iteration


RUN_ID = "01ARZ3NDEKTSV4RRFFQ69G5FG0"


def test_assemble_emits_iteration_and_render_equivalent_hype_files(tmp_path: Path) -> None:
    prepare_dir = _write_prepare_outputs(tmp_path, data_quality=0.95)
    out_dir = tmp_path / "assembled"

    result = assemble_iteration(
        prepare_dir=prepare_dir,
        out_path=out_dir,
        repo_root=tmp_path,
        direction="gentle retrospective",
    )

    assert Path(result["timeline_path"]).is_file()
    assert Path(result["hype_timeline_path"]).is_file()
    assert Path(result["hype_assets_path"]).is_file()
    assert (out_dir / "iteration.report.html").is_file()
    assert _read_json(out_dir / "iteration.timeline.json") == _read_json(out_dir / "hype.timeline.json")
    manifest = _read_json(out_dir / "iteration.manifest.json")
    assert manifest["assembly"]["direction_label"] == "gentle retrospective"
    assert manifest["assembly"]["style_source"] == "direction-label"
    assert manifest["assembly"]["renderer_decisions"][0]["renderer"] == "image_grid"
    assert manifest["assembly"]["renderer_decisions"][1]["renderer"] == "audio_waveform"
    assert manifest["assembly"]["renderer_decisions"][2]["renderer"] == "generic_card"
    assert "renderer-fallback: no renderer for kind:model_3d" in result["diagnostics"]
    assert '<aside class="renderer-fallback">no renderer for kind:model_3d</aside>' in (out_dir / "iteration.report.html").read_text(encoding="utf-8")


def test_style_audio_and_mode_behavior(tmp_path: Path) -> None:
    prepare_dir = _write_prepare_outputs(tmp_path, data_quality=0.95)
    out_dir = tmp_path / "assembled"

    assemble_iteration(
        prepare_dir=prepare_dir,
        out_path=out_dir,
        repo_root=tmp_path,
        theme="banodoco-default",
        direction="kept as label",
    )

    manifest = _read_json(out_dir / "iteration.manifest.json")
    assert manifest["assembly"]["style_source"] == "theme"
    assert manifest["assembly"]["direction_label"] == "kept as label"
    assert manifest["assembly"]["audio_bed"] == "iterations-as-bed"
    with pytest.raises(AssembleError, match="only --mode chaptered"):
        assemble_iteration(prepare_dir=prepare_dir, out_path=tmp_path / "bad", repo_root=tmp_path, mode="parallel")
    with pytest.raises(AssembleError, match="never generates music"):
        assemble_iteration(prepare_dir=prepare_dir, out_path=tmp_path / "bad2", repo_root=tmp_path, audio_bed="generated_music")


def test_assemble_outputs_do_not_depend_on_deferred_preview_modes(tmp_path: Path) -> None:
    prepare_dir = _write_prepare_outputs(tmp_path, data_quality=0.95)
    out_dir = tmp_path / "assembled"

    assemble_iteration(prepare_dir=prepare_dir, out_path=out_dir, repo_root=tmp_path)

    combined = "\n".join(path.read_text(encoding="utf-8") for path in out_dir.glob("*.json"))
    assert "preview_modes" not in combined


def test_assemble_materializes_runtime_object_output_before_rendering(tmp_path: Path) -> None:
    payload = b"runtime-owned image bytes"
    digest = hashlib.sha256(payload).hexdigest()

    class Runtime:
        def __init__(self) -> None:
            self.requested: list[str] = []

        def get_object(self, object_id: str):
            self.requested.append(object_id)
            return SimpleNamespace(data=payload)

    runtime = Runtime()
    manifest = {
        "schema_version": 1,
        "target_run_id": RUN_ID,
        "thread_id": "01ARZ3NDEKTSV4RRFFQ69G5FG1",
        "runs": [{
            "run_id": RUN_ID,
            "output_artifacts": [{
                "kind": "image",
                "role": "result",
                "object_id": "object-image-1",
                "digest": f"sha256:{digest}",
                "size": len(payload),
            }],
        }],
    }
    quality = {"schema_version": 1, "target_run_id": RUN_ID, "data_quality": 1.0}

    result = assemble_iteration(
        out_path=tmp_path / "assembled",
        repo_root=tmp_path,
        input_manifest=manifest,
        input_quality=quality,
        runtime_client=runtime,
    )

    assert runtime.requested == ["object-image-1"]
    assets = _read_json(tmp_path / "assembled" / "hype.assets.json")["assets"]
    asset = next(iter(assets.values()))
    materialized = Path(asset["file"])
    assert materialized.read_bytes() == payload
    timeline = _read_json(tmp_path / "assembled" / "iteration.timeline.json")
    assert timeline["clips"][0]["clipType"] == "media"
    assert _read_json(tmp_path / "assembled" / "iteration.quality.json")["data_quality"] == 1.0
    assert result["diagnostics"] == []


def test_assemble_degrades_explicitly_when_runtime_object_cannot_be_materialized(tmp_path: Path) -> None:
    digest = hashlib.sha256(b"missing runtime bytes").hexdigest()
    manifest = {
        "schema_version": 1,
        "target_run_id": RUN_ID,
        "thread_id": "01ARZ3NDEKTSV4RRFFQ69G5FG1",
        "runs": [{
            "run_id": RUN_ID,
            "output_artifacts": [{
                "kind": "image",
                "object_id": "missing-object",
                "digest": digest,
                "size": 19,
            }],
        }],
    }
    quality = {"schema_version": 1, "target_run_id": RUN_ID, "data_quality": 1.0}

    result = assemble_iteration(
        out_path=tmp_path / "assembled",
        repo_root=tmp_path,
        input_manifest=manifest,
        input_quality=quality,
    )

    assert "cannot be fetched" in result["diagnostics"][0]
    timeline = _read_json(tmp_path / "assembled" / "iteration.timeline.json")
    assert timeline["clips"][0]["clipType"] == "text-card"
    assert timeline["clips"][0]["params"]["fallback"] is True
    assert _read_json(tmp_path / "assembled" / "iteration.quality.json")["data_quality"] == 0.5


def _write_prepare_outputs(tmp_path: Path, *, data_quality: float) -> Path:
    prepare_dir = tmp_path / "prepare"
    prepare_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1,
        "target_run_id": RUN_ID,
        "thread_id": "01ARZ3NDEKTSV4RRFFQ69G5FG1",
        "runs": [
            {
                "run_id": RUN_ID,
                "thread_id": "01ARZ3NDEKTSV4RRFFQ69G5FG1",
                "label": "in_thread",
                "causal_depth": 0,
                "output_artifacts": [
                    {"kind": "image", "role": "other", "path": "runs/source/image.png", "sha256": "a" * 64, "duration": 4},
                    {"kind": "audio", "role": "other", "path": "runs/source/audio.wav", "sha256": "b" * 64, "duration": 5},
                    {"kind": "model_3d", "role": "other", "path": "runs/source/model.glb", "sha256": "c" * 64, "duration": 2},
                ],
                "summary": {"summary": "prepared summary"},
            }
        ],
        "quality": {"data_quality": data_quality},
    }
    quality = {
        "schema_version": 1,
        "target_run_id": RUN_ID,
        "data_quality": data_quality,
        "valid_roots": [],
        "unresolved_producer_runs": [],
    }
    _write_json(prepare_dir / "iteration.manifest.json", manifest)
    _write_json(prepare_dir / "iteration.quality.json", quality)
    return prepare_dir


def test_assemble_writes_universal_result_manifest(tmp_path: Path) -> None:
    """iteration.assemble writes a universal {out}/manifest.json beside the domain iteration.manifest.json."""
    prepare_dir = _write_prepare_outputs(tmp_path, data_quality=0.95)
    out_dir = tmp_path / "assembled"

    result = assemble_iteration(
        prepare_dir=prepare_dir,
        out_path=out_dir,
        repo_root=tmp_path,
        force=True,
        direction="test-direction",
        mode="chaptered",
        theme="test-theme",
        style_preset="test-preset",
        audio_bed="silence-room-tone",
    )

    # Universal manifest exists
    universal_path = out_dir / "manifest.json"
    assert universal_path.is_file(), "universal manifest.json must exist"
    universal = _read_json(universal_path)

    # Core required fields
    assert universal["schema_version"] == 1
    assert universal["kind"] == "render"
    assert isinstance(universal["created"], str)
    assert isinstance(universal["warnings"], list)

    # Inputs echo prepare/out/options
    inputs = universal["inputs"]
    assert inputs["prepare_dir"] == str(prepare_dir)
    assert inputs["out"] == str(out_dir)
    assert inputs["force"] is True
    assert inputs["direction"] == "test-direction"
    assert inputs["mode"] == "chaptered"
    assert inputs["theme"] == "test-theme"
    assert inputs["style_preset"] == "test-preset"
    assert inputs["audio_bed"] == "silence-room-tone"

    # All six declared artifacts listed as outputs
    outputs = universal["outputs"]
    assert len(outputs) == 6
    output_paths = {item["path"] for item in outputs}
    assert str(out_dir / "iteration.timeline.json") in output_paths
    assert str(out_dir / "hype.timeline.json") in output_paths
    assert str(out_dir / "iteration.manifest.json") in output_paths
    assert str(out_dir / "iteration.quality.json") in output_paths
    assert str(out_dir / "iteration.report.html") in output_paths
    assert str(out_dir / "hype.assets.json") in output_paths

    # Each output has type "file" and enriched metadata
    for item in outputs:
        assert item["type"] == "file"
        assert "content_hash" in item
        assert "bytes" in item
        assert item["bytes"] > 0

    # Domain manifest (iteration.manifest.json) is still present and unchanged
    domain_manifest = _read_json(out_dir / "iteration.manifest.json")
    assert "assembly" in domain_manifest
    assert domain_manifest["assembly"]["direction_label"] == "test-direction"

    # Return dict includes universal_manifest_path
    assert "universal_manifest_path" in result
    assert Path(result["universal_manifest_path"]) == universal_path


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))
