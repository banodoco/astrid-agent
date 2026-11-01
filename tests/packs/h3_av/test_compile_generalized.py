from __future__ import annotations

import json
from pathlib import Path

import pytest

from astrid.packs.h3_av.actions.transform.run import _generation_intent
from astrid.packs.h3_av.actions.compile.compile import CompilationError, compile_preparation
from astrid.packs.h3_av.actions.prepare.prepare import prepare_request
from astrid.packs.h3_av.shared.request import normalize_request
from astrid.packs.vibecomfy.invocation_preflight import InvocationPreflightError, preflight_invocation


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
        from PIL import Image
        Image.new("L", (1024, 576), 255).save(mask)
        asset_map["mask.png"] = str(mask)
    if references:
        ref = tmp_path / "reference.png"
        ref.write_bytes(b"reference-image")
        asset_map["reference.png"] = str(ref)
    return prepare_request(request, asset_map=asset_map)


def test_audio_regeneration_fails_closed_without_legacy_edit_dispatch(tmp_path: Path) -> None:
    with pytest.raises(CompilationError, match="Advanced legacy changes are outside SPEC-MIN"):
        compile_preparation(_prepared(tmp_path, _request()), out_dir=tmp_path / "compiled")


def test_imported_v1_regional_change_binds_native_red_mask(tmp_path: Path) -> None:
    from vibecomfy.cli_loader import load_workflow_any

    request = _request(changes={
        "video": [{"during": [1, 3], "area": {"rectangle": [0, 0, 100, 100]},
                   "mask_asset": "mask.png", "action": "generate"}], "audio": [],
    })
    compiled = compile_preparation(_prepared(tmp_path, request, masks=True), out_dir=tmp_path / "compiled")
    workflow = load_workflow_any(compiled["workflow"]["workflow.py"]["path"])
    api = workflow.compile("api", run_inputs=compiled["workflow_inputs"])
    mask = next(node for node in api.values() if node["class_type"] == "ImageToMask")
    assert mask["inputs"]["channel"] == "red"
    edit = next(node for node in api.values() if node["class_type"] == "H3V2VGranularFractionalDenoise")
    assert edit["inputs"]["mode"] == "spatial"
    assert edit["inputs"]["crop"] == "disabled"
    assert compiled["capabilities"]["edit_binding"]["during_frames"] == [24, 72]
    _assert_single_public_video(compiled, internal_outputs=[])


def test_native_masked_edit_source_binding_matches_worker_contract(tmp_path: Path) -> None:
    request = _request(changes={
        "video": [{"during": [0, 4], "area": {"rectangle": [0, 0, 100, 100]},
                   "mask_asset": "mask.png", "action": "generate"}], "audio": [],
    })
    compiled = compile_preparation(_prepared(tmp_path, request, masks=True), out_dir=tmp_path / "compiled")
    workflow_path = compiled["workflow"]["workflow.py"]["path"]
    kwargs = {"run_inputs": compiled["workflow_inputs"], "expected_source_node": "99", "expected_source_field": "video"}

    receipt = preflight_invocation(workflow_path, **kwargs)
    assert receipt["run_inputs"]["source_video"] == compiled["workflow_inputs"]["source_video"]

    with pytest.raises(InvocationPreflightError, match="SOURCE_BINDING"):
        preflight_invocation(workflow_path, **{**kwargs, "expected_source_node": "wrong-node"})
    with pytest.raises(InvocationPreflightError, match="SOURCE_BINDING"):
        preflight_invocation(workflow_path, **{**kwargs, "expected_source_field": "wrong-field"})


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
    with pytest.raises(CompilationError, match="Advanced legacy changes are outside SPEC-MIN"):
        compile_preparation(_prepared(tmp_path, unsupported_audio), out_dir=tmp_path / "stem")

    edit_with_refs = _request(
        changes={"video": [{"during": [0, 4], "area": {"full_frame": True}, "action": "generate"}], "audio": []},
        references=[{"asset": "reference.png", "purpose": "appearance"}],
    )
    compiled = compile_preparation(_prepared(tmp_path, edit_with_refs, references=True), out_dir=tmp_path / "edit-refs")
    assert compiled["capabilities"]["reference_order"] == ["reference.png"]
    assert compiled["workflow_inputs"]["reference_0"].endswith("reference.png")
