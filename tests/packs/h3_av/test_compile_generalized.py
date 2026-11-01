from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrid.packs.h3_av.actions.transform.run import _generation_intent
from astrid.packs.h3_av.actions.compile.compile import CompilationError, compile_preparation
from astrid.packs.h3_av.actions.prepare.prepare import prepare_request
from astrid.packs.h3_av.shared.request import normalize_request


def _assert_single_public_video(compiled: dict, *, internal_outputs: list[str]) -> None:
    persisted = json.loads(Path(compiled["manifest_path"]).read_text())
    public = persisted["capabilities"]["public_generation"]
    assert public == {
        "modality": "video",
        "selectors": [{"selector": "main-0", "ordinal": 0, "variant_key": "original", "required": True}],
        "internal_outputs": internal_outputs,
    }
    intent = _generation_intent(persisted)
    assert intent["groups"] == [{"group_key": "main", "selectors": public["selectors"]}]


def _request(**overrides):
    value = {
        "version": 1,
        "operation": "edit",
        "source": {"asset": "source.mp4", "range": [0, 4]},
        "output": {"duration": 4},
        "content": {"prompt": "Keep the shot and replace the spoken line."},
        "changes": {
            "video": [{"during": [1, 3], "area": {"full_frame": True}, "action": "generate"}],
            "audio": [{"during": [0.5, 1.25], "action": "generate", "dialogue": "Say hello."}],
        },
        "references": [],
        "overrides": {"steps": 12, "seed": 7},
    }
    value.update(overrides)
    return normalize_request(value)


def _prepared(tmp_path: Path, request, *, masks: bool = False, references: bool = False):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source-video")
    asset_map = {"source.mp4": str(source)}
    if masks:
        mask = tmp_path / "mask.png"
        mask.write_bytes(b"mask-image")
        asset_map["mask.png"] = str(mask)
    if references:
        ref = tmp_path / "reference.png"
        ref.write_bytes(b"reference-image")
        asset_map["reference.png"] = str(ref)
    return prepare_request(request, asset_map=asset_map)


def test_lanpaint_bindings_preserve_prompt_duration_masks_and_audio_intervals(tmp_path: Path) -> None:
    compiled = compile_preparation(_prepared(tmp_path, _request()), out_dir=tmp_path / "compiled")
    inputs = compiled["workflow_inputs"]

    frozen_workflow = Path(compiled["workflow"]["workflow.py"]["path"])
    assert "LanPaint_VideoMaskEditor" in frozen_workflow.read_text(encoding="utf-8")
    assert inputs["duration"] == 4.0
    assert "Keep the shot" in inputs["prompt"]
    assert "Say hello." in inputs["prompt"]
    assert json.loads(inputs["audio_intervals"]) == [{"start": 0.5, "end": 1.25}]
    keyframes = json.loads(inputs["mask_keyframes"])
    assert keyframes["24"].endswith("mask_full_frame.png")
    assert keyframes["72"].endswith("mask_preserve.png")
    assert compiled["capabilities"]["audio_mask"] == "independent sample intervals"
    assert compiled["capabilities"]["output_contract"] == "separate_av_full_timeline"
    _assert_single_public_video(compiled, internal_outputs=["audio"])


def test_lanpaint_regional_change_requires_and_binds_real_mask_asset(tmp_path: Path) -> None:
    request = _request(
        changes={
            "video": [{"during": [1, 3], "area": {"rectangle": [0, 0, 100, 100]}, "mask_asset": "mask.png", "action": "generate"}],
            "audio": [],
        }
    )
    compiled = compile_preparation(_prepared(tmp_path, request, masks=True), out_dir=tmp_path / "compiled")
    keyframes = json.loads(compiled["workflow_inputs"]["mask_keyframes"])
    assert keyframes["24"].endswith("mask.png")


@pytest.mark.parametrize("count", [0, 1, 2])
def test_native_continuation_binds_zero_one_or_two_actual_reference_slots(tmp_path: Path, count: int) -> None:
    request = normalize_request(
        {
            "version": 1,
            "operation": "continue",
            "source": {"asset": "source.mp4", "range": [0, 4]},
            "output": {"duration": 7},
            "content": {"prompt": "Continue the action without a reset."},
            "changes": {"video": [{"during": [4, 7], "area": {"full_frame": True}, "action": "generate"}], "audio": [{"during": [4, 7], "action": "generate"}]},
            "references": [{"asset": "reference.png", "purpose": "appearance"} for _ in range(count)],
            "overrides": {"seed": 9},
        }
    )
    compiled = compile_preparation(_prepared(tmp_path, request, references=True), out_dir=tmp_path / f"compiled-{count}")
    inputs = compiled["workflow_inputs"]
    assert inputs["prompt"] == "Continue the action without a reset."
    assert inputs["duration"] == 124 / 24
    assert inputs["source_frames"] == 96
    assert compiled["continuation_timing"]["requested_new_frames"] == 72
    assert compiled["continuation_timing"]["generated_capacity_frames"] == 85
    assert compiled["continuation_timing"]["trim_tail_frames"] == 13
    assert compiled["capabilities"]["output_contract"] == "muxed_av_full_timeline"
    _assert_single_public_video(compiled, internal_outputs=[])
    if count == 0:
        assert "reference_0" not in inputs
        assert Path(compiled["workflow"]["workflow.py"]["path"]).parent.name == "workflow-bundle"
    else:
        assert inputs["reference_0"].endswith("reference.png")
        assert inputs["reference_1"].endswith("reference.png")
        assert compiled["capabilities"]["references"] == count


def test_unsupported_capabilities_fail_before_graph_submission(tmp_path: Path) -> None:
    too_many = normalize_request(
        {
            "version": 1,
            "operation": "continue",
            "source": {"asset": "source.mp4", "range": [0, 4]},
            "output": {"duration": 7},
            "content": {"prompt": "continue"},
            "changes": {"video": [{"during": [4, 7], "area": {"full_frame": True}, "action": "generate"}], "audio": [{"during": [4, 7], "action": "generate"}]},
            "references": [{"asset": "reference.png", "purpose": "appearance"} for _ in range(3)],
            "overrides": {},
        }
    )
    with pytest.raises(CompilationError, match="two image-reference slots"):
        compile_preparation(_prepared(tmp_path, too_many, references=True), out_dir=tmp_path / "too-many")

    unsupported_audio = _request(changes={"video": [], "audio": [{"during": [1, 2], "action": "generate", "stem": "voice"}]})
    with pytest.raises(CompilationError, match="stem"):
        compile_preparation(_prepared(tmp_path, unsupported_audio), out_dir=tmp_path / "stem")

    edit_with_refs = _request(references=[{"asset": "reference.png", "purpose": "appearance"}])
    with pytest.raises(CompilationError, match="no reference-image input"):
        compile_preparation(_prepared(tmp_path, edit_with_refs, references=True), out_dir=tmp_path / "edit-refs")
