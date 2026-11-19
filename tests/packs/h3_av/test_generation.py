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
    caps = compiled["capabilities"]
    assert caps["references"] == 1 and caps["reference_count_modality"] == "image"
    assert caps["effective_reference_count"] == 3
    assert caps["reference_modality_counts"] == {"image": 1, "video": 1, "audio": 1}
    assert caps["effective_audio_tags"] == ["<Audio 1>", "<Audio 2>"]
    assert caps["effective_reference_order"] == [row["occurrence"] for row in rows]
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
    allowed = normalize_v2({"version": 2, "prompt": "A scene with <ordinary text>", "duration": 6, "media": [
        {"asset": "image", "role": "reference", "modality": "image"},
    ]})
    assert _compile_native_v2(allowed, {"image": image}, tmp_path / "allowed")["status"] == "compiled"


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


@pytest.mark.parametrize("media", [[],
    [{"asset": "video", "role": "reference", "modality": "video", "audio": False}],
    [{"asset": "audio", "role": "reference", "modality": "audio"}],
    [{"asset": "video", "role": "reference", "modality": "video", "audio": False},
     {"asset": "audio", "role": "reference", "modality": "audio"}],
])
def test_source_free_without_image_compiles_with_correct_documented_use_note(monkeypatch, tmp_path, media):
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
    notes = [row for row in dry_run["warnings"] if row["limit"] == "text_only_documented_use"]
    assert bool(notes) is (not media)
    if notes:
        assert "outside the documented use of the Ref2VA checkpoint" in notes[0]["message"]
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
    assert ("ref_videos.ref_video_0" in target["inputs"]) == any(item["modality"] == "video" for item in media)
    assert ("ref_audios.ref_audio_0" in target["inputs"]) == any(item["modality"] == "audio" for item in media)


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
        assert any(row["limit"] == "text_only_documented_use" and "outside the documented use" in row["message"] for row in preparation["warnings"])
        results.append((compilation["request_digest"], compilation["compilation_digest"]))
    assert results[0] == results[1]


def test_same_mixed_occurrences_bind_identically_on_source_free_and_masked_routes(tmp_path):
    from vibecomfy.cli_loader import load_workflow_any
    from astrid.packs.h3_av.shared.compile import _compile_native_v2
    from astrid.packs.h3_av.shared.request_v2 import normalize_v2

    references = [
        {"id": "voice", "asset": "voice", "role": "reference", "modality": "audio"},
        {"id": "performance", "asset": "performance", "role": "reference", "modality": "video", "range": [1, 3], "audio": True},
        {"id": "picture", "asset": "picture", "role": "reference", "modality": "image"},
    ]
    image = tmp_path / "image.png"
    Image.new("RGB", (32, 32)).save(image)
    assets = {"picture": image}
    for name in ("voice", "performance", "source"):
        assets[name] = tmp_path / name
        assets[name].write_bytes(b"asset fixture")
    timeline = {"id": "source", "asset": "source", "role": "timeline", "modality": "video", "at": {"frame": 0}, "range": [0, 6],
                "edit": [{"stream": "video", "during": [0, 6], "mask": {"full_frame": True}}]}
    prompt = "<Picture 1> <Video 1> <Audio 1> <Audio 2>"
    outputs = []
    for name, media in (("free", references), ("masked", [*references, timeline])):
        request = normalize_v2({"version": 2, "prompt": prompt, "duration": 6, "media": media})
        compilation = _compile_native_v2(request, assets, tmp_path / name)
        rows = compilation["capabilities"]["conditioning"]
        workflow = load_workflow_any(compilation["workflow"]["workflow.py"]["path"])
        api = workflow.compile("api", run_inputs=compilation["workflow_inputs"])
        target = next(node for node in api.values() if node["class_type"] == "MiniMaxH3ReferenceToVideo")
        assert target["inputs"]["prompt"] == prompt
        for row in rows:
            assert row["input_binding"]["port"] in target["inputs"]
            if "soundtrack" in row["input_binding"]:
                assert row["input_binding"]["soundtrack"]["port"] in target["inputs"]
        outputs.append(rows)
    assert outputs[0] == outputs[1]


def test_sealed_reference_view_uses_compiled_rows_and_labels_video_counts_as_estimates(tmp_path):
    from astrid.packs.h3_av.shared.compile import _compile_native_v2
    from astrid.packs.h3_av.shared.request_v2 import normalize_v2
    from astrid.packs.h3_av.shared.stages import _sealed_plan

    media = [
        {"asset": "voice", "role": "reference", "modality": "audio"},
        {"asset": "performance", "role": "reference", "modality": "video", "range": [1, 3], "audio": True},
        {"asset": "picture", "role": "reference", "modality": "image"},
    ]
    image = tmp_path / "picture.png"
    Image.new("RGB", (32, 32)).save(image)
    assets = {"picture": image}
    for name in ("voice", "performance"):
        assets[name] = tmp_path / name
        assets[name].write_bytes(b"asset fixture")
    request = normalize_v2({"version": 2, "prompt": "<Picture 1> <Video 1> <Audio 1> <Audio 2>", "duration": 6, "media": media})
    compilation = _compile_native_v2(request, assets, tmp_path / "out")
    rows = compilation["capabilities"]["conditioning"]
    preparation = {"conditioning_measurements": [{
        "occurrence": "occurrence-2", "retained_picture_frames_estimate": {
            "value": 47, "kind": "metadata_upper_bound", "observed": False,
        }, "effective_size": {"width": 1024, "height": 576}, "bound_file": "/scratch/video.mp4",
    }]}
    view = _sealed_plan(compilation, preparation=preparation)["reference_view"]

    assert [{key: row[key] for key in ("occurrence", "modality", "trim", "input_binding", "tag")} for row in view["references"]] == rows
    video = next(row for row in view["references"] if row["modality"] == "video")
    assert video["retained_picture_frames_estimate"] == {
        "value": 47, "kind": "metadata_upper_bound", "observed": False,
    }
    assert video["effective_size"] == {"width": 1024, "height": 576}
    assert "/scratch" not in str(view)
    assert "retained_picture_frames" not in video

    # Character Swap's compiler places timing at compilation top level.
    character_swap = dict(compilation)
    character_swap["generation_timing"] = {"raw_frames": 3592}
    character_swap["capabilities"] = dict(compilation["capabilities"])
    character_swap["capabilities"].pop("generation_timing", None)
    assert _sealed_plan(character_swap, preparation=preparation)["reference_view"]["target_frame_cap"] == 3592


def test_character_swap_sealed_view_uses_compilation_level_timing(tmp_path, monkeypatch):
    import subprocess
    from astrid.packs.h3_av.shared import generation
    from astrid.packs.h3_av.shared.compile import compile_preparation
    from astrid.packs.h3_av.shared.prepare import prepare_request
    from astrid.packs.h3_av.shared.request import normalize_request
    from astrid.packs.h3_av.shared.stages import _sealed_plan

    monkeypatch.setattr(generation, "character_swap_asset_sha256", lambda: "a1" * 32)
    video, image = tmp_path / "performance.mp4", tmp_path / "replacement.png"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=c=green:s=64x64:r=24:d=4",
                    "-an", "-c:v", "mpeg4", "-y", str(video)], check=True, capture_output=True)
    Image.new("RGB", (32, 32), color="red").save(image)
    request = normalize_request({"version": 2, "duration": 1,
        "prompt": "Replace the person in <Video 1> with <Picture 1>.",
        "media": [{"id": "performance", "asset": "performance", "role": "reference",
                   "modality": "video", "range": [1, 3], "audio": False},
                  {"id": "replacement", "asset": "replacement", "role": "reference", "modality": "image"}],
        "settings": {"adapter": {"id": "character_swap_v1"}}})
    preparation = prepare_request(request, asset_map={"performance": str(video), "replacement": str(image)})
    compilation = compile_preparation(preparation, out_dir=tmp_path / "character-swap")
    assert "generation_timing" not in compilation["capabilities"]
    assert _sealed_plan(compilation, preparation=preparation)["reference_view"]["target_frame_cap"] == \
        compilation["generation_timing"]["raw_frames"]



def test_settled_reference_count_redecodes_pinned_vhs_path_and_keeps_failures_unqualified(tmp_path):
    import hashlib
    import shutil
    import subprocess
    from astrid.packs.h3_av.shared.stages import _settled_reference_view

    ffmpeg = shutil.which("ffmpeg")
    assert ffmpeg
    video = tmp_path / "managed/assets/clip.mp4"
    video.parent.mkdir(parents=True)
    subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i",
                    "testsrc=size=64x48:rate=12:duration=6", "-t", "6", "-an",
                    "-c:v", "mpeg4", "-y", str(video)], check=True, capture_output=True)
    digest = hashlib.sha256(video.read_bytes()).hexdigest()
    view = {"target_frame_cap": 120, "references": [{
        "occurrence": "performance", "modality": "video", "tag": "<Video 1>",
        "input_binding": {"binding": "video_reference_0", "start_time": 1.0, "frame_load_cap": 65},
        "retained_picture_frames_estimate": {"value": 56, "kind": "metadata_upper_bound", "observed": False},
    }]}
    assets = {"video_reference_0": {"member": "assets/clip.mp4", "sha256": digest}}

    settled = _settled_reference_view(view, reference_assets=assets, workdir=tmp_path)
    derived = settled["references"][0]["retained_picture_frames_derived"]
    assert derived["input_frame_count"] == 65
    assert derived["value"] == 56  # min(65, 120), then the greatest 5+17k value
    assert derived["kind"] == "derived_pinned_node_rule"
    assert derived["conformance"] == "not_conformed"
    assert derived["input_sha256"] == digest
    assert "/managed/" not in str(settled)
    assert settled["references"][0]["retained_picture_frames_estimate"] == view["references"][0]["retained_picture_frames_estimate"]

    mutated = _settled_reference_view(view, reference_assets={
        "video_reference_0": {"member": "assets/clip.mp4", "sha256": "0" * 64},
    }, workdir=tmp_path)
    assert mutated["references"][0]["retained_picture_frames_derived"]["status"] == "unqualified"
    assert mutated["references"][0]["retained_picture_frames_derived"]["reason"] == "bound_reference_redecode_unavailable"
    assert "/managed/" not in str(mutated)

    unavailable = _settled_reference_view(view, reference_assets=assets, workdir=tmp_path,
                                          ffmpeg="/missing/ffmpeg")
    assert unavailable["references"][0]["retained_picture_frames_derived"]["status"] == "unqualified"
    assert "ffmpeg" in unavailable["references"][0]["retained_picture_frames_derived"]["reason"]

    missing = _settled_reference_view(view, reference_assets={
        "video_reference_0": {"member": "assets/missing.mp4", "sha256": digest},
    }, workdir=tmp_path)
    assert missing["references"][0]["retained_picture_frames_derived"]["reason"] == "managed_reference_asset_missing"

    no_vhs_bindings = {**view, "references": [{**view["references"][0], "input_binding": {"binding": "video_reference_0"}}]}
    unbound = _settled_reference_view(no_vhs_bindings, reference_assets=assets, workdir=tmp_path)
    assert unbound["references"][0]["retained_picture_frames_derived"]["reason"] == "vhs_binding_parameters_unavailable"

    outside = tmp_path.parent / (tmp_path.name + "-managed")
    outside.mkdir()
    (tmp_path / "managed").rename(tmp_path / "managed-original")
    (tmp_path / "managed").symlink_to(outside, target_is_directory=True)
    escaped = _settled_reference_view(view, reference_assets=assets, workdir=tmp_path)
    assert escaped["references"][0]["retained_picture_frames_derived"]["reason"] == "managed_reference_asset_root_symlink"


def test_pinned_vhs_vp9_decoder_override_is_used_for_probe_and_redecode(monkeypatch, tmp_path):
    import subprocess
    from types import SimpleNamespace
    from astrid.packs.h3_av.shared import stages

    calls = []
    def fake_run(command, **kwargs):
        calls.append(command)
        if "null" in command:
            return SimpleNamespace(stderr=b"Stream #0:0: Video: vp9 , 64x48")
        return SimpleNamespace(stdout="# framehash\n0, 0, 1, 1, 0, 0, 0, 0, 0, 0, 0\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert stages._vhs_frame_count(tmp_path / "bound.webm", start_time=0, frame_load_cap=0,
                                   ffmpeg="/bin/ffmpeg") == 1
    assert len(calls) == 3
    assert all(any(command[index:index + 2] == ["-c:v", "libvpx-vp9"]
                   for index in range(len(command) - 1)) for command in calls[1:])


def test_settled_reference_count_is_unqualified_for_multiple_video_streams(monkeypatch, tmp_path):
    import hashlib
    from types import SimpleNamespace
    from astrid.packs.h3_av.shared import stages

    video = tmp_path / "managed/assets/multi.mkv"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"multi-stream bytes")
    digest = hashlib.sha256(video.read_bytes()).hexdigest()
    view = {"target_frame_cap": 120, "references": [{
        "occurrence": "performance", "modality": "video",
        "input_binding": {"binding": "video_reference_0", "start_time": 0, "frame_load_cap": 0},
    }]}
    monkeypatch.setattr(stages.subprocess, "run", lambda *a, **k: SimpleNamespace(stderr=(
        b"Stream #0:0: Video: h264, 64x48\nStream #0:1: Video: h264, 128x72\n")))
    settled = stages._settled_reference_view(
        view, reference_assets={"video_reference_0": {"member": "assets/multi.mkv", "sha256": digest}},
        workdir=tmp_path, ffmpeg="/fake/ffmpeg",
    )
    derived = settled["references"][0]["retained_picture_frames_derived"]
    assert derived["status"] == "unqualified"
    assert derived["reason"] == "multiple_video_streams_unqualified"


def test_fractional_reference_trim_survives_compilation_manifest(tmp_path):
    from astrid.packs.h3_av.shared.compile import _compile_native_v2
    from astrid.packs.h3_av.shared.request_v2 import normalize_v2

    video = tmp_path / "video.mp4"
    video.write_bytes(b"video fixture")
    request = normalize_v2({"version": 2, "prompt": "<Video 1>", "duration": 6, "media": [
        {"asset": "video", "role": "reference", "modality": "video", "range": ["1/24", "49/24"], "audio": False},
    ]})
    compilation = _compile_native_v2(request, {"video": video}, tmp_path / "out")
    manifest = json.loads(Path(compilation["manifest_path"]).read_text())
    assert manifest["capabilities"]["conditioning"][0]["trim"] == ["1/24", "49/24"]


@pytest.mark.parametrize("with_typed", [False, True])
def test_public_hook_refuses_untyped_image_occurrence_before_claim(tmp_path, with_typed):
    import importlib.util
    from astrid.core.generation.engine_facts import comfyui_facts

    hook_path = Path(__file__).parents[3] / "astrid/packs/h3_av/extensions/vibecomfy.workflows/hooks.py"
    spec = importlib.util.spec_from_file_location("h3_untyped_test_hooks", hook_path)
    hooks = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hooks)
    image = tmp_path / "image.png"
    Image.new("RGB", (32, 32)).save(image)
    media = [{"asset": "untyped", "role": "reference"}]
    asset_map = {"untyped": str(image)}
    if with_typed:
        media.append({"asset": "typed", "role": "reference", "modality": "image"})
        asset_map["typed"] = str(image)
    request_file = tmp_path / "request.json"
    request_file.write_text(json.dumps({"version": 2, "prompt": "A scene", "duration": 6, "media": media}))
    with pytest.raises(ValueError, match=r"occurrence-1.*unresolved modality.*declare image, video, or audio"):
        hooks.build({"request": str(request_file), "asset_map": asset_map}, {}, tmp_path / "out", engine=comfyui_facts())
    assert not (tmp_path / "out/h3/plan/compile/compilation.json").exists()
