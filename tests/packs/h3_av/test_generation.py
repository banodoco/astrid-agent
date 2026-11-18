from __future__ import annotations

from astrid.packs.h3_av.shared.generation import generation_inputs_from_bindings

import hashlib
import json
import os
import shutil
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from astrid.packs.h3_av.shared.request import H3RequestError, normalize_request
from astrid.packs.h3_av.shared.prepare import prepare_request
from astrid.packs.h3_av.shared.compile import CompilationError, compile_preparation
from astrid.packs.h3_av.shared.generation import generation_timing
from astrid.packs.h3_av.shared.stages import _generation_intent


def _locked_model_workflow(**kwargs):
    from astrid.packs.h3_av.shared.generation import build_generation_workflow

    return build_generation_workflow(generation_inputs_from_bindings(
        references=["reference.png"], prompt="A scene.", frames=24,
        model="minimax_h3_ref2va_pruned_int8_convrot.safetensors", steps=8, seed=1,
        **kwargs,
    ))


def test_generation_assets_use_recipe_lock_revisions():
    from astrid.core.generation.engine_recipe import load_recipe_model_lock
    from astrid.packs.h3_av.shared import generation

    lock = load_recipe_model_lock("comfyui", "linux-x86_64-cu130")
    workflow = _locked_model_workflow(adapter_strength=0.7)
    assets = {f'{row["subdir"]}/{row["name"]}': row for row in workflow.metadata["model_assets"]}
    assert len(lock.models) == 5
    assert set(assets) == {model.destination for model in lock.models} | {
        f"loras/{generation.CHARACTER_SWAP_FILENAME}"
    }
    for model in lock.models:
        assert assets[model.destination]["url"] == (
            f"https://huggingface.co/{model.repository}/resolve/{model.revision}/{model.path}"
        )
    character = assets[f"loras/{generation.CHARACTER_SWAP_FILENAME}"]
    assert character["url"] == (
        f"https://huggingface.co/{generation.CHARACTER_SWAP_REPOSITORY}/resolve/"
        f"{generation.CHARACTER_SWAP_REVISION}/{generation.CHARACTER_SWAP_FILENAME}"
    )
    assert character["hf_revision"] == generation.CHARACTER_SWAP_REVISION
    assert f"loras/{generation.TURBO_FILENAME}" not in {
        f'{row["subdir"]}/{row["name"]}'
        for row in _locked_model_workflow(turbo_enabled=False).metadata["model_assets"]
    }


def test_generation_binds_standalone_audio_to_audio_typed_reference_port():
    workflow = _locked_model_workflow(audio_references=[{"binding": "audio_reference_0"}])
    target = workflow.nodes["target"]
    ports = dict(zip(target.native_input_names, target.native_input_types, strict=True))
    assert ports["ref_audios.ref_audio_0"] == "AUDIO"
    assert workflow.inputs["audio_reference_0"].media_semantics == "audio"


def test_builder_reference_port_counts_match_the_limits_table():
    from astrid.packs.h3_av.shared.generation import LIMITS, build_generation_workflow

    capacities = LIMITS["builder_slots"]["value"]
    workflow = build_generation_workflow(generation_inputs_from_bindings(
        references=[f"image-{index}" for index in range(capacities["image"])],
        video_references=[{"binding": f"video-{index}"} for index in range(capacities["video"])],
        audio_references=[{"binding": f"audio-{index}"} for index in range(capacities["audio"])],
        prompt="A scene", frames=24, model="minimax_h3_ref2va_pruned_int8_convrot.safetensors", steps=8, seed=1,
    ))
    target = workflow.nodes["target"]
    ports = dict(zip(target.native_input_names, target.native_input_types, strict=True))
    assert sum(name.startswith("ref_images.") for name in ports) == capacities["image"]
    assert sum(name.startswith("ref_videos.") for name in ports) == capacities["video"]
    assert sum(name.startswith("ref_audios.") for name in ports) == capacities["audio"]
    assert all(ports[f"ref_audios.ref_audio_{index}"] == "AUDIO" for index in range(capacities["audio"]))
    dimensions = LIMITS["canvas_dimension"]["value"]
    assert (dimensions["min"], dimensions["max"], dimensions["multiple"]) == (32, 16384, 32)


def test_builder_rejects_one_past_standalone_audio_slots():
    from astrid.packs.h3_av.shared.generation import LIMITS, build_generation_workflow

    with pytest.raises(ValueError, match="at most three standalone audio references"):
        build_generation_workflow(generation_inputs_from_bindings(references=["image-0"], audio_references=[
            {"binding": f"audio-{index}"} for index in range(LIMITS["builder_slots"]["value"]["audio"] + 1)
        ], prompt="A scene", frames=24, model="minimax_h3_ref2va_pruned_int8_convrot.safetensors", steps=8, seed=1))


def test_limits_match_the_pinned_cached_h3_node_schema():
    from astrid.packs.h3_av.shared.generation import LIMITS

    cache = Path(os.environ["VIBECOMFY_SOURCE"]) / "vibecomfy/porting/cache/object_info/comfy_core@cpu-runtime.json"
    classes = json.loads(cache.read_text())
    schema = classes["MiniMaxH3ReferenceToVideo"]
    assert schema["git_commit"] == "6bfaacc67c2103481e5f0c84d75257cd0581d86a"
    optional = schema["inputs"]["optional"]
    slots = LIMITS["builder_slots"]["value"]
    assert optional["ref_images"][1]["template"]["max"] == slots["image"]
    assert optional["ref_videos"][1]["template"]["max"] == slots["video"]
    assert optional["ref_audios"][1]["template"]["max"] == slots["audio"]
    width = schema["inputs"]["required"]["width"][1]
    height = schema["inputs"]["required"]["height"][1]
    for dim in (width, height):
        assert (dim["min"], dim["max"], dim["step"]) == (
            LIMITS["canvas_dimension"]["value"]["min"],
            LIMITS["canvas_dimension"]["value"]["max"],
            LIMITS["canvas_dimension"]["value"]["multiple"],
        )


@pytest.mark.parametrize("invalid", [
    {"revision": None}, {"revision": "main"}, {"repository": None},
    {"path": None}, {"url": "https://huggingface.co/owner/repo/resolve/main/model"},
])
def test_generation_rejects_incomplete_or_unpinned_lock_entry(monkeypatch, invalid):
    from astrid.packs.h3_av.shared import generation

    from astrid.core.generation.engine_recipe import load_recipe_model_lock
    lock = load_recipe_model_lock("comfyui", "linux-x86_64-cu130")
    broken = replace(lock.models[0], **invalid)
    _install_lock(replace(lock, models=(broken, *lock.models[1:])))
    with pytest.raises(ValueError, match="pinned Hugging Face source"):
        _locked_model_workflow()


@pytest.mark.parametrize("index", range(5))
def test_generation_rejects_missing_required_lock_destination(monkeypatch, index):
    from astrid.packs.h3_av.shared import generation

    from astrid.core.generation.engine_recipe import load_recipe_model_lock
    lock = load_recipe_model_lock("comfyui", "linux-x86_64-cu130")
    missing = lock.models[index]
    _install_lock(replace(lock, models=tuple(model for model in lock.models if model is not missing)))
    with pytest.raises(ValueError, match="missing destination") as error:
        _locked_model_workflow()
    assert missing.destination in str(error.value)


def _install_lock(lock):
    """Hand the pack a modified model lock through its engine facts (the autouse fixture restores them)."""
    from astrid.core.generation.engine_facts import comfyui_facts
    from astrid.packs.h3_av.shared import engine_facts
    engine_facts.install(replace(comfyui_facts(), locked_models=lambda: {m.destination: m for m in lock.models}))


def request(count=4, **updates):
    raw = {"version": 1, "operation": "generate", "output": {"duration": 15},
           "content": {"prompt": "A subject crosses the scene in <Picture 1>."},
           "changes": {"video": [], "audio": []},
           "references": [{"asset": f"image-{i}", "purpose": "appearance"} for i in range(count)],
           "overrides": {"steps": 8, "seed": 42}}
    raw.update(updates)
    return normalize_request(raw)




@pytest.mark.parametrize("updates,match", [
    ({"source": {"asset": "video"}}, "source must be omitted"),
    ({"references": []}, "one to nine"),
    ({"references": [{"asset": "a", "purpose": "audio"}]}, "image guidance"),
    ({"references": [{"asset": "a", "purpose": "keyframe"}]}, "timed keyframes"),
    ({"output": {"duration": 15.01}}, "24 fps"),
    ({"output": {"duration": 4 / 24}}, "job length frames"),
    ({"output": {"duration": 3593 / 24}}, "job length frames"),
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






def _runpod_request():
    return {
        "target": {"kind": "runpod", "pod_id": "original-pod",
                   "provider_account_ref": "account-1",
                   "storage": {"network_volume_id": "original-volume"}},
        "remote_activation_required": True,
        "lifecycle": {"mode": "leave_running"},
    }


def _canonical_result(**changes):
    from astrid.sdk.results import InvocationResult
    return InvocationResult(
        capability_id="vibecomfy.run", capability_type="executor", native_kind="executor",
        kernel_task_id="task-1", kernel_run_id="run-1", kernel_attempt_id="attempt-1",
        **{"ok": True, **changes},
    )


@pytest.fixture
def prepared_client(tmp_path):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from astrid.packs.runpod.prepared_task import PreparedWorkerSelection
    final = _canonical_result()
    underlying = SimpleNamespace(invoke_result=Mock(side_effect=[_canonical_result(), final]))

    def show(task_id):
        assert task_id == "task-1"
        return {"task_id": task_id, "run_id": "run-1", "attempt_id": "attempt-1",
                "capability_id": "vibecomfy.run",
                "state": "queued" if underlying.invoke_result.call_count == 1 else "succeeded",
                "execution_binding": {"effective_target": _runpod_request()["target"]}}

    underlying.tasks = SimpleNamespace(show=show)
    owner = SimpleNamespace(ensure_for_task=Mock(), retire_after_task=Mock())
    factory = Mock(return_value=owner)
    selection = PreparedWorkerSelection(
        claim_handle_path=tmp_path / "claim.json", deployment_profile_path=tmp_path / "profile.json",
        staging_inputs_path=tmp_path / "staging.json", journal_dir=tmp_path / "owner-journal",
        target=_runpod_request()["target"], deployment_profile={}, staging_inputs={},
        composition_factory=factory,
    )
    prepared = selection.bind_client(
        underlying, workflow_path=tmp_path / "workflow.py",
        workflow_bundle_path=tmp_path, workflow_inputs={"seed": 0},
    )
    prepared.invoke_result = Mock(wraps=prepared.invoke_result)
    return prepared, underlying, owner, factory, final
















def test_source_free_audio_trim_compiles_to_bound_audio_graph(tmp_path):
    import subprocess
    from vibecomfy.cli_loader import load_workflow_any
    from astrid.packs.h3_av.shared.request_v2 import normalize_v2

    image = tmp_path / "image.png"
    audio = tmp_path / "audio.wav"
    Image.new("RGB", (32, 32)).save(image)
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "sine=sample_rate=48000", "-t", "5", str(audio)], check=True)
    raw = {"version": 2, "prompt": "A scene", "duration": 6, "media": [
        {"asset": "image", "role": "reference", "modality": "image"},
        {"asset": "audio", "role": "reference", "modality": "audio", "range": [1, 4]},
    ]}
    prepared = prepare_request(normalize_v2(raw), asset_map={"image": str(image), "audio": str(audio)})
    compiled = compile_preparation(prepared, out_dir=tmp_path / "compiled")
    capabilities = compiled["capabilities"]
    assert capabilities["references"] == 1
    assert capabilities["reference_order"] == ["image"]
    assert capabilities["audio_references"] == 1
    assert capabilities["audio_reference_order"] == ["audio"]
    assert not any("image references only" in text for text in compiled["limitations"])
    assert prepared["resource_estimate"]["requested_target_rows"] == 144
    assert prepared["resource_estimate"]["target_rows"] == 158
    assert prepared["resource_estimate"]["target_raw_rgb_bytes"] == 158 * 1024 * 576 * 3
    workflow = load_workflow_any(compiled["workflow"]["workflow.py"]["path"])
    api = workflow.compile("api", run_inputs=compiled["workflow_inputs"])
    target = next(row for row in api.values() if row["class_type"] == "MiniMaxH3ReferenceToVideo")
    trim = api[target["inputs"]["ref_audios.ref_audio_0"][0]]
    assert trim["class_type"] == "TrimAudioDuration"
    assert trim["inputs"]["start_index"] == 1
    assert trim["inputs"]["duration"] == 3
    assert api[trim["inputs"]["audio"][0]]["class_type"] == "LoadAudio"


def test_source_free_video_reference_binds_occurrence_and_prompt_tag(tmp_path):
    from vibecomfy.cli_loader import load_workflow_any
    from astrid.packs.h3_av.shared.compile import _compile_native_v2
    from astrid.packs.h3_av.shared.request_v2 import normalize_v2

    image = tmp_path / "image.png"
    video = tmp_path / "video.mp4"
    Image.new("RGB", (32, 32)).save(image)
    video.write_bytes(b"video binding fixture")
    request = normalize_v2({"version": 2, "prompt": "A scene with <Video 1>", "duration": 6, "media": [
        {"asset": "image", "role": "reference", "modality": "image"},
        {"asset": "video", "role": "reference", "modality": "video", "audio": False},
    ]})
    compiled = _compile_native_v2(request, {"image": image, "video": video}, tmp_path / "out")
    row = compiled["capabilities"]["conditioning"][1]
    assert row["occurrence"] == request.value["media"][1]["occurrence_id"]
    assert row["input_binding"]["binding"] == "video_reference_0"
    assert row["tag"] == "<Video 1>"
    workflow = load_workflow_any(compiled["workflow"]["workflow.py"]["path"])
    api = workflow.compile("api", run_inputs=compiled["workflow_inputs"])
    target = next(node for node in api.values() if node["class_type"] == "MiniMaxH3ReferenceToVideo")
    assert target["inputs"]["prompt"] == "A scene with <Video 1>"
    loader = api[target["inputs"]["ref_videos.ref_video_0"][0]]
    assert loader["class_type"] == "VHS_LoadVideoFFmpeg"
    assert loader["inputs"]["video"] == compiled["workflow_inputs"]["video_reference_0"]


def test_sixteen_seconds_compiles_and_warns(tmp_path):
    from vibecomfy.cli_loader import load_workflow_any
    image = tmp_path / "reference.png"
    Image.new("RGB", (32, 32)).save(image)
    prepared = prepare_request(request(1, output={"duration": 16}), asset_map={"image-0": str(image)})
    assert prepared["warnings"][0]["limit"] == "job_duration_seconds"
    compiled = compile_preparation(prepared, out_dir=tmp_path / "compiled-16")
    workflow = load_workflow_any(compiled["workflow"]["workflow.py"]["path"])
    api = workflow.compile("api", run_inputs=compiled["workflow_inputs"])
    target = next(row for row in api.values() if row["class_type"] == "MiniMaxH3ReferenceToVideo")
    assert target["inputs"]["length"] == 396


def test_node_frame_grid_maximum():
    assert generation_timing(3592 / 24)["raw_frames"] == 3592
    with pytest.raises(ValueError, match="job length frames"):
        generation_timing(3593 / 24)
    with pytest.raises(ValueError, match="job length frames"):
        generation_timing(4 / 24)
    from astrid.packs.h3_av.shared.generation import build_generation_workflow
    with pytest.raises(ValueError, match="job length frames"):
        build_generation_workflow(generation_inputs_from_bindings(references=["i"], prompt="scene", frames=3593, model="minimax_h3_ref2va_pruned_int8_convrot.safetensors", steps=8, seed=1))


def test_source_free_mixed_reference_tags_follow_connected_soundtracks(tmp_path):
    from vibecomfy.cli_loader import load_workflow_any
    from astrid.packs.h3_av.shared.compile import _compile_native_v2
    from astrid.packs.h3_av.shared.request_v2 import normalize_v2

    image, video, audio = (tmp_path / name for name in ("image.png", "video.mp4", "voice.wav"))
    Image.new("RGB", (32, 32)).save(image)
    video.write_bytes(b"video fixture")
    audio.write_bytes(b"audio fixture")
    request = normalize_v2({"version": 2, "prompt": "<Picture 1> <Video 1> <Audio 1> <Audio 2>", "duration": 6, "media": [
        {"asset": "voice", "role": "reference", "modality": "audio"},
        {"asset": "video", "role": "reference", "modality": "video", "audio": True},
        {"asset": "image", "role": "reference", "modality": "image"},
    ]})
    compiled = _compile_native_v2(request, {"image": image, "video": video, "voice": audio}, tmp_path / "out")
    rows = compiled["capabilities"]["conditioning"]
    assert [(row["occurrence"], row["tag"]) for row in rows] == [
        (request.value["media"][2]["occurrence_id"], "<Picture 1>"),
        (request.value["media"][1]["occurrence_id"], "<Video 1>"),
        (request.value["media"][0]["occurrence_id"], "<Audio 2>"),
    ]
    assert rows[1]["input_binding"]["soundtrack"]["tag"] == "<Audio 1>"
    workflow = load_workflow_any(compiled["workflow"]["workflow.py"]["path"])
    api = workflow.compile("api", run_inputs=compiled["workflow_inputs"])
    target = next(node for node in api.values() if node["class_type"] == "MiniMaxH3ReferenceToVideo")
    video_port = target["inputs"]["ref_videos.ref_video_0"]
    assert target["inputs"]["ref_video_audios.ref_video_audio_0"] == [video_port[0], 2]
    audio_port = target["inputs"]["ref_audios.ref_audio_0"]
    assert api[audio_port[0]]["inputs"]["audio"] == compiled["workflow_inputs"]["audio_reference_0"]


def test_unbound_generated_reference_tag_is_refused(tmp_path):
    from astrid.packs.h3_av.shared.compile import _compile_native_v2
    from astrid.packs.h3_av.shared.request_v2 import normalize_v2
    image = tmp_path / "image.png"
    Image.new("RGB", (32, 32)).save(image)
    request = normalize_v2({"version": 2, "prompt": "<Audio 1> and <ordinary text>", "duration": 6, "media": [
        {"asset": "image", "role": "reference", "modality": "image"},
    ]})
    with pytest.raises(CompilationError, match="prompt reference tag <Audio 1> has no effective binding"):
        _compile_native_v2(request, {"image": image}, tmp_path / "out")


@pytest.mark.parametrize("location", ["request", "occurrence"])
def test_forged_request_reference_tags_are_refused(location):
    raw = {"version": 2, "prompt": "A scene", "duration": 6, "media": [
        {"asset": "image", "role": "reference", "modality": "image"},
    ]}
    if location == "request":
        raw["model_tags"] = {"image": "<Picture 42>"}
    else:
        raw["media"][0]["model_tag"] = "<Picture 42>"
    with pytest.raises(H3RequestError, match="unsupported field"):
        normalize_request(raw)


def test_graph_custom_node_requirements_follow_used_lowerings_and_pins():
    from astrid.core.generation.engine_facts import comfyui_facts
    pins = comfyui_facts().custom_node_sources()
    plain = _locked_model_workflow()
    with_video = _locked_model_workflow(video_references=[{"binding": "video_reference_0", "audio": False}])
    def rows(workflow):
        return {row["slug"]: row for row in workflow.requirements.custom_node_refs}
    plain_rows, video_rows = rows(plain), rows(with_video)
    for name, pin in pins.items():
        if name in plain_rows:
            assert plain_rows[name]["url"] == pin.repository
            assert plain_rows[name]["commit"] == pin.revision
    assert "VHS_LoadVideoFFmpeg" not in plain_rows["ComfyUI-VideoHelperSuite"]["classes"]
    assert "VHS_LoadVideoFFmpeg" in video_rows["ComfyUI-VideoHelperSuite"]["classes"]


@pytest.mark.parametrize("media", [[], [
    {"asset": "video", "role": "reference", "modality": "video", "audio": False},
    {"asset": "audio", "role": "reference", "modality": "audio"},
]])
def test_source_free_without_image_compiles_and_warns(monkeypatch, tmp_path, media):
    from vibecomfy.cli_loader import load_workflow_any
    from astrid.packs.h3_av.shared import generation, prepare
    from astrid.packs.h3_av.shared.compile import _compile_native_v2
    from astrid.packs.h3_av.shared.request_v2 import normalize_v2

    request = normalize_v2({"version": 2, "prompt": "A scene", "duration": 6, "media": media})
    monkeypatch.setattr(prepare, "_resolve_assets", lambda *_: [])
    monkeypatch.setattr(prepare, "_admit_conditioning", lambda *_: [])
    monkeypatch.setattr(prepare, "_validate_mask_assets", lambda *_: None)
    monkeypatch.setattr(prepare, "_check_source_av_rates", lambda *_: None)
    monkeypatch.setattr(prepare, "_v2_mask_schedule", lambda *_args, **_kwargs: {"status": "ready"})
    dry_run = prepare.prepare_request(request)
    assert any(row["limit"] == "no_image_reference" for row in dry_run["warnings"])
    assets = {}
    for item in media:
        path = tmp_path / item["asset"]
        path.write_bytes(b"asset fixture")
        assets[item["asset"]] = path
    seen = []
    original = generation.build_generation_workflow
    def capture(inputs):
        seen.append(inputs)
        return original(inputs)
    monkeypatch.setattr(generation, "build_generation_workflow", capture)
    compiled = _compile_native_v2(request, assets, tmp_path / "out")
    assert seen[0].canvas == {"width": 1024, "height": 576, "frames": 158}
    assert seen[0].placed == ()
    assert seen[0].grants[0]["stream"] == "av"
    assert seen[0].grants[0]["interval"] == [0, 6]
    assert seen[0].model["base"] == request.value["settings"]["model"]
    assert seen[0].model["adapter"] is None
    assert seen[0].sampling["seed"] == request.value["settings"]["seed"]
    assert all(set(row) == {"occurrence", "modality", "trim", "input_binding", "tag"} for row in seen[0].conditioning)
    workflow = load_workflow_any(compiled["workflow"]["workflow.py"]["path"])
    api = workflow.compile("api", run_inputs=compiled["workflow_inputs"])
    target = next(node for node in api.values() if node["class_type"] == "MiniMaxH3ReferenceToVideo")
    assert not any(key.startswith("ref_images.") for key in target["inputs"])
    assert ("ref_videos.ref_video_0" in target["inputs"]) == bool(media)
    assert ("ref_audios.ref_audio_0" in target["inputs"]) == bool(media)


def test_text_only_request_runs_public_hook_without_fabricated_assets(tmp_path):
    import importlib.util
    from astrid.core.generation.engine_facts import comfyui_facts

    path = Path(__file__).parents[3] / "astrid/packs/h3_av/extensions/vibecomfy.workflows/hooks.py"
    spec = importlib.util.spec_from_file_location("h3_text_only_test_hooks", path)
    hooks = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hooks)
    request_file = tmp_path / "request.json"
    request_file.write_text(json.dumps({"version": 2, "prompt": "A scene", "duration": 6, "media": []}))
    results = []
    for name in ("one", "two"):
        out = tmp_path / name
        result = hooks.build({"request": str(request_file)}, {}, out, engine=comfyui_facts())
        preparation = json.loads((out / "h3/preparation.json").read_text())
        compilation = json.loads((out / "h3/plan/compile/compilation.json").read_text())
        assert result["assets"] == {}
        assert "managed_assets" not in compilation
        assert not (out / "h3/plan/compile/managed-assets.zip").exists()
        assert any(row["limit"] == "no_image_reference" for row in preparation["warnings"])
        results.append((compilation["request_digest"], compilation["compilation_digest"]))
    assert results[0] == results[1]
