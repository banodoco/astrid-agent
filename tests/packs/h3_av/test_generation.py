from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from PIL import Image

from astrid.packs.h3_av.shared.request import H3RequestError, normalize_request
from astrid.packs.h3_av.actions.prepare.prepare import prepare_request
from astrid.packs.h3_av.actions.compile.compile import CompilationError, compile_preparation
from astrid.packs.h3_av.shared.generation import generation_timing
from astrid.packs.h3_av.actions.transform.run import _generation_intent


def request(count=4, **updates):
    raw = {"version": 1, "operation": "generate", "output": {"duration": 15},
           "content": {"prompt": "A subject crosses the scene in <Picture 1>."},
           "changes": {"video": [], "audio": []},
           "references": [{"asset": f"image-{i}", "purpose": "appearance"} for i in range(count)],
           "overrides": {"steps": 8, "seed": 42}}
    raw.update(updates)
    return normalize_request(raw)


@pytest.mark.parametrize("count", [1, 4, 9])
@pytest.mark.integration
def test_dynamic_references_emit_one_portable_sampling_graph(tmp_path: Path, count: int, monkeypatch):
    from vibecomfy.cli_loader import load_bundle
    from vibecomfy.security.provenance import Provenance
    paths = {}
    for i in range(count):
        path = tmp_path / f"image-{i}.png"
        Image.new("RGB", (32, 32), color=(i * 20, 0, 0)).save(path)
        paths[f"image-{i}"] = str(path)
    req = request(count, overrides={"steps": 12, "seed": 7, "sampler": "euler", "guidance": 0.5})
    prepared = prepare_request(req, asset_map=paths)
    assert prepared["mask_schedule"]["video"]["generated_intervals"] == [[0, 15]]
    assert prepared["mask_schedule"]["audio"]["protected_intervals"] == []
    compiled = compile_preparation(prepared, out_dir=tmp_path / "compiled")
    portable = tmp_path / "relocated"
    shutil.copytree(tmp_path / "compiled/workflow-bundle", portable)
    shutil.rmtree(tmp_path / "compiled")
    bundle = load_bundle(portable, trust=Provenance.USER_CONFIRMED)
    api = bundle.workflow.compile("api", run_inputs=compiled["workflow_inputs"])
    target = next(n for n in api.values() if n["class_type"] == "MiniMaxH3ReferenceToVideo")
    refs = {k: v for k, v in target["inputs"].items() if k.startswith("ref_images.")}
    assert len(refs) == count
    for i in range(count):
        loader = api[refs[f"ref_images.ref_image_{i}"][0]]
        assert loader["inputs"]["image"] == compiled["workflow_inputs"][f"reference_{i}"]
    assert target["inputs"]["prompt"] == req.value["content"]["prompt"]
    assert target["inputs"]["length"] == 362
    by_class = {node["class_type"]: node["inputs"] for node in api.values()}
    assert by_class["BasicScheduler"]["steps"] == 12
    assert by_class["RandomNoise"]["noise_seed"] == 7
    assert by_class["KSamplerSelect"]["sampler_name"] == "euler"
    assert by_class["LoraLoaderModelOnly"]["strength_model"] == 0.5
    assert sum(n["class_type"] == "SamplerCustomAdvanced" for n in api.values()) == 1
    assert sum(n["class_type"] == "VHS_VideoCombine" for n in api.values()) == 1
    assert not any(n["class_type"] in {"MiniMaxH3CustomKeyframes", "MiniMaxH3GeneratedAVMaskedContext"} for n in api.values())
    assert len(bundle.workflow.outputs) == 1
    assert bundle.workflow.outputs[0].expected_cardinality == "one"
    assert len(_generation_intent(compiled)["groups"][0]["selectors"]) == 1
    assert compiled["capabilities"]["reference_order"] == [f"image-{i}" for i in range(count)]
    source = (portable / "workflow.py").read_text()
    assert "mink" not in source.lower() and "runpy" not in source and "/workspace/" not in source
    assert all(bundle.workflow.inputs[f"reference_{i}"].default == "" for i in range(count))
    if count == 4:
        import importlib
        monkeypatch.setenv("ASTRID_INTERNAL_INVOCATION", "1")
        validator = importlib.import_module("astrid.packs.vibecomfy.actions.validate.run")
        report = validator._canonical_bundle_validation(portable / "workflow.py", python_execution_consent="confirmed")
        assert report["ok"] is True
        assert report["validation_mode"] == "canonical_bundle_structural"
        from astrid.packs.vibecomfy.invocation_preflight import preflight_invocation
        preflight_invocation(portable / "workflow.py", run_inputs=compiled["workflow_inputs"],
                             expected_prompt=req.value["content"]["prompt"])


@pytest.mark.parametrize("updates,match", [
    ({"source": {"asset": "video"}}, "source must be omitted"),
    ({"references": []}, "one to nine"),
    ({"references": [{"asset": "a", "purpose": "audio"}]}, "image guidance"),
    ({"references": [{"asset": "a", "purpose": "keyframe"}]}, "timed keyframes"),
    ({"output": {"duration": 15.01}}, "24 fps"),
    ({"output": {"duration": 16}}, "5..362"),
    ({"output": {"duration": float("inf")}}, "finite"),
    ({"references": [{"asset": "a", "purpose": "style"}] * 10}, "nine"),
    ({"changes": {"video": [], "audio": [{"during": [0, 1], "action": "preserve"}]}}, "changes/masks"),
])
def test_unsupported_generation_requests_fail_at_admission(updates, match):
    with pytest.raises(H3RequestError, match=match):
        request(**updates)


def test_generation_timing_covers_original_fifteen_second_request():
    assert generation_timing(15) == {"fps": 24, "requested_frames": 360,
                                   "raw_frames": 362, "trim_tail_frames": 2, "duration": 15}


@pytest.mark.integration
def test_reference_must_decode_as_still_image(tmp_path: Path):
    path = tmp_path / "fake.png"
    path.write_bytes(b"not an image")
    with pytest.raises(CompilationError, match="decodable still image"):
        compile_preparation(prepare_request(request(1), asset_map={"image-0": str(path)}), out_dir=tmp_path / "compiled")
