from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator, ValidationError

from astrid.packs.h3_av.shared.compile import CompilationError, compile_preparation
from astrid.packs.h3_av.shared.generation import (
    CHARACTER_SWAP_FILENAME,
    CHARACTER_SWAP_REVISION,
    CHARACTER_SWAP_SHA256,
    TURBO_FILENAME,
)
from astrid.packs.h3_av.shared.request import H3RequestError, load_request_document, load_request, normalize_request
from astrid.packs.h3_av.shared.request_v2 import REF2VA_MODEL, effective_settings

PACK = Path(__file__).resolve().parents[3] / "astrid/packs/h3_av"
SCHEMA = Draft202012Validator(json.loads((PACK / "schemas/request.v2.json").read_text()))


def _request(*, adapter: bool = True, turbo: bool = True, source: bool = False) -> dict:
    media = [
        {"id": "performance", "asset": "performance.mp4", "role": "reference", "modality": "video", "range": [1, 3], "audio": False},
        {"id": "replacement", "asset": "replacement.png", "role": "reference", "modality": "image"},
    ]
    if source:
        media.insert(0, {
            "id": "baseline", "asset": "baseline.mp4", "role": "timeline", "modality": "video",
            "at": {"frame": 0}, "range": [0, 1],
            "edit": [{"stream": "video", "during": [0, 1], "mask": {"full_frame": True}}],
        })
    settings: dict = {"turbo": {"enabled": turbo}}
    if adapter:
        settings["adapter"] = {"id": "character_swap_v1"}
    return {
        "version": 2,
        "prompt": "Replace the person in <Video 1> with the person in <Picture 1>.",
        "duration": 1,
        "media": media,
        "settings": settings,
    }


def _preparation(raw: dict, files: dict[str, Path]) -> dict:
    request = normalize_request(raw)
    assets = []
    for asset, path in files.items():
        assets.append({
            "asset": asset,
            "status": "resolved",
            "path": str(path.resolve()),
            "size": path.stat().st_size,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        })
    return {
        "kind": "h3_av_preparation",
        "status": "prepared",
        "runtime_submission": "eligible",
        "request": request.value,
        "request_digest": request.digest,
        "assets": assets,
    }


def _files(tmp_path: Path, *, source: bool = False) -> dict[str, Path]:
    files = {
        "performance.mp4": tmp_path / "performance.mp4",
        "replacement.png": tmp_path / "replacement.png",
    }
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "color=c=green:s=64x64:r=24:d=4", "-an", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", str(files["performance.mp4"])], check=True, capture_output=True)
    from PIL import Image
    Image.new("RGB", (64, 64), color="red").save(files["replacement.png"])
    if source:
        files["baseline.mp4"] = tmp_path / "baseline.mp4"
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                        "-i", "color=c=blue:s=1024x576:r=24:d=1", "-f", "lavfi",
                        "-i", "sine=frequency=440:sample_rate=48000:duration=1", "-map", "0:v:0",
                        "-map", "1:a:0", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-shortest", str(files["baseline.mp4"])], check=True, capture_output=True)
    return files


def test_adapter_defaults_aliases_and_legacy_replay_are_canonical_without_mutation() -> None:
    raw = _request()
    normalized = normalize_request(raw)
    assert normalized.value["settings"] == {
        "model": REF2VA_MODEL,
        "steps": 8,
        "sampler": "res_multistep",
        "scheduler": "simple",
        "seed": 123456789,
        "turbo": {"enabled": True, "strength": 0.95},
        "adapter": {"id": "character_swap_v1", "strength": 1.0},
    }
    assert [(row["id"], row["occurrence_id"], row["modality"]) for row in normalized.value["media"]] == [
        ("performance", "occurrence-1", "video"), ("replacement", "occurrence-2", "image"),
    ]
    assert "model_tags" not in normalized.value
    assert all("model_tag" not in row for row in normalized.value["media"])
    SCHEMA.validate(raw)

    alias = copy.deepcopy(raw)
    alias["settings"]["guidance_scale"] = 0.4
    canonical = copy.deepcopy(alias)
    canonical["settings"].pop("guidance_scale")
    canonical["settings"]["turbo"]["strength"] = 0.4
    assert normalize_request(alias) == normalize_request(canonical)
    assert effective_settings(normalized)["adapter"]["strength"] == 1.0

    # Legacy guidance spellings remain valid without an explicit Turbo object;
    # the normalizer supplies the default Turbo projection.
    for alias_name in ("guidance", "guidance_scale"):
        legacy_alias = _request()
        legacy_alias["settings"] = {"adapter": {"id": "character_swap_v1"}, alias_name: 0.4}
        SCHEMA.validate(legacy_alias)
        assert normalize_request(legacy_alias).value["settings"]["turbo"]["strength"] == 0.4

    # Already sealed pre-adapter requests retain their original identity while
    # the effective projection supplies current defaults for compilation.
    legacy = {
        "version": 2, "profile": "h3_av.native.v2", "output_count": 1,
        "prompt": "Legacy source-free request.", "duration": 1,
        "media": [{"occurrence_id": "image", "asset": "look.png", "role": "reference", "modality": "image", "model_tag": "<Picture 1>"}],
        "settings": {"model": REF2VA_MODEL, "steps": 8, "sampler": "res_multistep", "seed": 123456789, "guidance_scale": 0.35},
        "continuation": False,
        "model_tags": {"image": "<Picture 1>"},
    }
    payload = json.dumps(legacy, sort_keys=True, separators=(",", ":")).encode()
    digest = hashlib.sha256(payload).hexdigest()
    from astrid.packs.h3_av.shared.request_v2 import read_prepared_request

    sealed = read_prepared_request(legacy, digest, require_normalized_v2=True)
    assert sealed.digest == digest
    assert sealed.value == legacy
    assert effective_settings(sealed)["turbo"] == {"enabled": True, "strength": 0.35}
    assert "turbo" not in sealed.value["settings"]

    tampered_replay = copy.deepcopy(normalize_request(raw).value)
    tampered_replay["continuation"] = True
    # Construct the identity document from this fixture's authored intent and
    # normalized sampling defaults, independently of the identity helper.
    tampered_identity = {
        "version": 2, "prompt": raw["prompt"], "duration": 1,
        "media": [
            {"occurrence_id": "occurrence-1", "id": "performance", "asset": "performance.mp4",
             "role": "reference", "modality": "video", "range": [1, 3], "audio": False},
            {"occurrence_id": "occurrence-2", "id": "replacement", "asset": "replacement.png",
             "role": "reference", "modality": "image"},
        ],
        "settings": {
            "model": REF2VA_MODEL, "steps": 8, "sampler": "res_multistep", "scheduler": "simple",
            "seed": 123456789, "turbo": {"enabled": True, "strength": 0.95},
            "adapter": {"id": "character_swap_v1", "strength": 1.0},
        },
        "continuation": True,
    }
    tampered_payload = json.dumps(tampered_identity, sort_keys=True, separators=(",", ":")).encode()
    with pytest.raises(H3RequestError, match="digest does not match"):
        read_prepared_request(tampered_replay, "0" * 64, require_normalized_v2=True)
    with pytest.raises(H3RequestError, match="does not support continuation"):
        read_prepared_request(
            tampered_replay,
            hashlib.sha256(tampered_payload).hexdigest(),
            require_normalized_v2=True,
        )


def test_request_document_loader_preserves_raw_v2_payload_for_child_handoff(tmp_path: Path) -> None:
    raw = _request()
    path = tmp_path / "request.json"
    path.write_text(json.dumps(raw), encoding="utf-8")

    original = load_request_document(path)
    normalized = load_request(path)

    assert original == raw
    assert normalized == normalize_request(raw)
    assert "model_tags" not in original
    assert [(row["id"], row["occurrence_id"], row["modality"]) for row in normalized.value["media"]] == [
        ("performance", "occurrence-1", "video"), ("replacement", "occurrence-2", "image"),
    ]
    assert "model_tags" not in normalized.value
    assert all("model_tag" not in row for row in normalized.value["media"])
    # A child reading the handed-off document re-normalizes the original input
    # instead of rejecting derived fields from the parent's normalized value.
    assert normalize_request(original) == normalized


@pytest.mark.parametrize("strength", [-0.1, 2.001, float("nan"), float("inf"), -float("inf"), True, "1"])
def test_adapter_strength_must_be_finite_numeric_and_bounded(strength: object) -> None:
    raw = _request()
    raw["settings"]["adapter"]["strength"] = strength
    with pytest.raises(H3RequestError, match="adapter.strength"):
        normalize_request(raw)


@pytest.mark.parametrize("settings", [
    {"adapter": {"id": "other"}},
    {"adapter": {}},
    {"adapter": {"id": "character_swap_v1", "url": "https://example.invalid/lora"}},
    {"adapters": [{"id": "character_swap_v1"}]},
    {"model": "another-base.safetensors", "adapter": {"id": "character_swap_v1"}},
    {"model": "another-base.safetensors", "turbo": {"enabled": False}},
    {"guidance": 0.4, "guidance_scale": 0.4},
    {"guidance": 0.4, "turbo": {"strength": 0.4}},
    {"turbo": {"enabled": 1}},
])
def test_invalid_adapter_and_turbo_contract_fails_in_schema_or_normalizer(settings: dict) -> None:
    raw = _request(adapter=False)
    raw["settings"] = settings
    with pytest.raises(H3RequestError):
        normalize_request(raw)
    with pytest.raises(ValidationError):
        SCHEMA.validate(raw)


@pytest.mark.parametrize("case", [
    "no_video", "no_picture", "extra_picture", "extra_video", "audio_reference",
    "untyped", "continuation", "implicit_continuation", "audio_only", "separate_audio",
])
def test_adapter_media_and_branch_limits_fail_before_compile(case: str) -> None:
    raw = _request()
    if case == "no_video":
        raw["media"].pop(0)
    elif case == "no_picture":
        raw["media"].pop(1)
    elif case == "extra_picture":
        raw["media"].append({"asset": "extra.png", "role": "reference", "modality": "image"})
    elif case == "extra_video":
        raw["media"].append({"asset": "extra.mp4", "role": "reference", "modality": "video"})
    elif case == "audio_reference":
        raw["media"].append({"asset": "voice.wav", "role": "reference", "modality": "audio"})
    elif case == "untyped":
        raw["media"][0].pop("modality")
        raw["media"][0].pop("audio")
        raw["media"][0].pop("range")
        raw["media"][0]["asset"] = {"id": "performance.mp4"}
    elif case == "continuation":
        raw["continuation"] = True
    elif case == "implicit_continuation":
        raw = _request(source=True)
        raw["media"][0]["edit"] = []
    elif case == "audio_only":
        raw["media"] = [row for row in raw["media"] if row["role"] == "reference"]
        raw["media"].insert(0, {"asset": "voice.wav", "role": "timeline", "modality": "audio", "at": {"frame": 0}, "range": [0, 1]})
    elif case == "separate_audio":
        raw["media"].insert(0, {"asset": "voice.wav", "role": "timeline", "modality": "audio", "at": {"frame": 1}, "range": [0, 1]})
    if case in {"extra_picture", "extra_video", "audio_reference"}:
        # The shared limits table allows further references but marks them untested.
        normalize_request(raw)
    else:
        with pytest.raises(H3RequestError, match="character_swap_v1"):
            normalize_request(raw)


@pytest.mark.parametrize("turbo_enabled", [True, False])
@pytest.mark.parametrize("source", [False, True])
@pytest.mark.integration
def test_native_v2_compile_binds_exact_references_and_connected_lora_chain(
    tmp_path: Path, turbo_enabled: bool, source: bool,
) -> None:
    from vibecomfy.cli_loader import load_workflow_any
    raw = _request(turbo=turbo_enabled, source=source)
    raw["settings"].update({"steps": 13, "seed": 731, "sampler": "heun", "scheduler": "karras"})
    raw["settings"]["turbo"]["strength"] = 0.4
    raw["settings"]["adapter"]["strength"] = 0.7
    files = _files(tmp_path, source=source)
    prepared = _preparation(raw, files)
    compiled = compile_preparation(prepared, out_dir=tmp_path / "compiled")
    assert compiled["branch"] == ("source_backed_v2v" if source else "source_free")
    assert compiled["workflow_inputs"]["scheduler"] == "karras"
    assert {key: compiled["workflow_inputs"][key] for key in ("steps", "seed", "sampler")} == {
        "steps": 13, "seed": 731, "sampler": "heun",
    }
    assert compiled["workflow_inputs"].get("guidance") == (0.4 if turbo_enabled else None)
    assert ("guidance" in compiled["workflow_inputs"]) is turbo_enabled
    assert compiled["effective_configuration"]["adapter"] == {
        "id": "character_swap_v1",
        "repository": "akatz-ai/MiniMax-H3-Character-Swap-LoRA",
        "revision": CHARACTER_SWAP_REVISION,
        "filename": CHARACTER_SWAP_FILENAME,
        "sha256": CHARACTER_SWAP_SHA256,
        "strength": 0.7,
        "asset": compiled["effective_configuration"]["adapter"]["asset"],
    }
    assert {
        key: compiled["effective_configuration"]["adapter"]["asset"].get(key)
        for key in ("name", "url", "subdir", "sha256", "hf_revision")
    } == {
        "name": CHARACTER_SWAP_FILENAME,
        "url": "https://huggingface.co/akatz-ai/MiniMax-H3-Character-Swap-LoRA/resolve/"
              "62407e0cc8089c363abd9ce4b0b27662abb237af/h3_character_swap_pro4500_1000.safetensors",
        "subdir": "loras",
        "sha256": CHARACTER_SWAP_SHA256,
        "hf_revision": CHARACTER_SWAP_REVISION,
    }
    connected = {row["name"]: row for row in compiled["effective_configuration"]["connected_model_assets"]}
    assert CHARACTER_SWAP_FILENAME in connected
    assert (TURBO_FILENAME in connected) is turbo_enabled
    assert compiled["effective_configuration"]["turbo"]["enabled"] is turbo_enabled
    assert (compiled["effective_configuration"]["turbo"]["asset"] is not None) is turbo_enabled


    assert compiled["capabilities"]["reference_tags"] == {"video": "<Video 1>", "image": "<Picture 1>"}
    assert compiled["capabilities"]["source_audio"].startswith("preserved by existing composition")
    assert compiled["managed_assets"]["manifest"]["workflow_inputs"]["video_reference_0"]

    workflow = load_workflow_any(compiled["workflow"]["workflow.py"]["path"])
    api = workflow.compile("api", run_inputs=compiled["workflow_inputs"])
    target = next(row for row in api.values() if row["class_type"] == "MiniMaxH3ReferenceToVideo")
    assert target["inputs"]["ref_videos.ref_video_0"]
    assert target["inputs"]["ref_images.ref_image_0"]
    video_loader = api[target["inputs"]["ref_videos.ref_video_0"][0]]
    assert video_loader["class_type"] == "VHS_LoadVideoFFmpeg"
    assert video_loader["inputs"]["start_time"] == 1
    assert video_loader["inputs"]["frame_load_cap"] == 48
    by_class = {row["class_type"]: row["inputs"] for row in api.values()}
    assert by_class["ModelAttentionBackend"]["attention"] == "comfy kitchen attention"
    loras = [row["inputs"]["lora_name"] for row in api.values() if row["class_type"] == "LoraLoaderModelOnly"]
    assert loras == ([CHARACTER_SWAP_FILENAME, TURBO_FILENAME] if turbo_enabled else [CHARACTER_SWAP_FILENAME])
    character = next(row for row in api.values() if row["inputs"].get("lora_name") == CHARACTER_SWAP_FILENAME)
    assert character["inputs"]["strength_model"] == 0.7
    assert by_class["BasicScheduler"]["scheduler"] == "karras"
    assert by_class["BasicScheduler"]["steps"] == 13
    assert by_class["RandomNoise"]["noise_seed"] == 731
    assert by_class["KSamplerSelect"]["sampler_name"] == "heun"
    if turbo_enabled:
        turbo_node = next(row for row in api.values() if row["inputs"].get("lora_name") == TURBO_FILENAME)
        assert turbo_node["inputs"]["strength_model"] == 0.4
    assert workflow.validate().ok


@pytest.mark.integration
def test_native_v2_compile_uses_only_the_selected_synthetic_boundary_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import vibecomfy  # noqa: F401  (a sibling checkout; compile loads it lazily)
    from astrid.packs.h3_av.shared import generation

    synthetic_digest = "a1" * 32
    monkeypatch.setattr(generation, "character_swap_asset_sha256", lambda: synthetic_digest)
    raw = _request(turbo=False, source=False)
    raw["settings"]["adapter"]["strength"] = 0.7
    compiled = compile_preparation(
        _preparation(raw, _files(tmp_path)),
        out_dir=tmp_path / "compiled-synthetic-model-boundary",
    )

    assert compiled["effective_configuration"]["adapter"]["sha256"] == synthetic_digest
    assert compiled["effective_configuration"]["adapter"]["asset"]["sha256"] == synthetic_digest
    connected = {
        row["name"]: row
        for row in compiled["effective_configuration"]["connected_model_assets"]
    }
    assert connected[CHARACTER_SWAP_FILENAME]["sha256"] == synthetic_digest
    assert connected[CHARACTER_SWAP_FILENAME]["hf_revision"] == CHARACTER_SWAP_REVISION




@pytest.mark.integration
def test_character_swap_rejects_spatial_and_channel_specific_composition_before_execution(tmp_path: Path) -> None:
    import vibecomfy  # noqa: F401  (a sibling checkout; compile loads it lazily)
    raw = _request(source=True)
    raw["media"][0]["edit"][0]["mask"] = {"rectangle": [0, 0, 0.5, 0.5]}
    with pytest.raises(CompilationError, match="integer pixel coordinates"):
        compile_preparation(_preparation(raw, _files(tmp_path, source=True)), out_dir=tmp_path / "regional")

    raw = _request(source=True)
    raw["media"][0]["edit"].append({"stream": "audio", "during": [0, 1], "channels": "voice", "action": "generate"})
    with pytest.raises(CompilationError, match="channel-specific audio"):
        compile_preparation(_preparation(raw, _files(tmp_path, source=True)), out_dir=tmp_path / "voice-only")


@pytest.mark.ffmpeg
def test_character_swap_preserve_only_publishes_authoritative_source_bytes(tmp_path: Path) -> None:

    from astrid.packs.h3_av.shared.compose import compose_candidate
    from astrid.packs.h3_av.shared.prepare import prepare_request
    from astrid.packs.h3_av.shared.verify import verify_candidate

    files = _files(tmp_path, source=True)
    source = tmp_path / "source.mkv"
    files["baseline.mp4"] = source
    generated = tmp_path / "model-output.mkv"
    for path, color, tone in ((source, "blue", 440), (generated, "red", 880)):
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", f"color=c={color}:s=1024x576:r=24:d=1",
                "-f", "lavfi", "-i", f"sine=frequency={tone}:sample_rate=48000:duration=1",
                "-map", "0:v:0", "-map", "1:a:0", "-c:v", "ffv1", "-pix_fmt", "yuv420p",
                "-c:a", "pcm_s16le", "-shortest", str(path),
            ],
            check=True,
            capture_output=True,
        )

    raw = _request(source=True)
    raw["media"][0]["edit"][0]["action"] = "preserve"
    request = normalize_request(raw)
    preparation = prepare_request(
        request,
        asset_map={asset: str(path) for asset, path in files.items()},
    )
    assert preparation["mask_schedule"]["source_protected"] is True
    assert preparation["mask_schedule"]["video"]["generated_intervals"] == []
    assert preparation["mask_schedule"]["audio"]["generated_intervals"] == []

    composition = compose_candidate(
        preparation=preparation,
        generated=generated,
        source=source,
        out_dir=tmp_path / "preserve-only",
    )
    verification = verify_candidate(
        preparation=preparation,
        composition=composition,
        source=source,
    )

    candidate = Path(composition["candidate"]["path"])
    assert composition["composition"]["method"] == "h3-source-preservation-v1"
    assert candidate.read_bytes() == source.read_bytes()
    assert verification["preservation"]["status"] == "exact_whole_file_match"


@pytest.mark.integration
def test_character_swap_compilation_accepts_resolved_image_mask_asset(tmp_path: Path) -> None:
    import vibecomfy  # noqa: F401  (a sibling checkout; compile loads it lazily)
    from PIL import Image

    raw = _request(source=True)
    raw["media"][0]["edit"][0]["mask"] = {"asset": "region.png"}
    files = _files(tmp_path, source=True)
    mask = tmp_path / "region.png"
    Image.new("L", (64, 64), 255).save(mask)
    files["region.png"] = mask

    compiled = compile_preparation(
        _preparation(raw, files),
        out_dir=tmp_path / "regional-mask-asset",
    )

    assert compiled["branch"] == "source_backed_v2v"
    assert compiled["workflow_inputs"]["reference_0"]


def test_silent_timeline_source_does_not_claim_an_audio_baseline(monkeypatch, tmp_path: Path) -> None:
    from astrid.packs.h3_av.shared import prepare as prepare_module

    raw = _request(source=True)
    files = _files(tmp_path, source=True)
    request = normalize_request(raw)
    monkeypatch.setattr(prepare_module, "_video_stream_types", lambda _path: {"video"})
    asset_map = {asset: str(path) for asset, path in files.items()}
    prepared = prepare_module.prepare_request(request, asset_map=asset_map)
    audio = prepared["mask_schedule"]["audio"]
    assert audio["baseline_intervals"] == []
    assert audio["generated_intervals"] == [[0.0, 1.0]]
    assert audio["protected_intervals"] == []

    raw["media"][0]["edit"].append({
        "stream": "audio", "during": [0, 1], "action": "preserve",
    })
    with pytest.raises(prepare_module.PreparationError, match="preservation requires an authoritative audio baseline"):
        prepare_module.prepare_request(normalize_request(raw), asset_map=asset_map)
















@pytest.mark.parametrize("streams", [set(), {"audio"}])
def test_character_swap_timeline_without_video_fails_before_permissions(monkeypatch, tmp_path: Path, streams: set[str]) -> None:
    from astrid.packs.h3_av.shared import prepare as prepare_module
    from astrid.packs.h3_av.shared.errors import PreparationError
    monkeypatch.setattr(prepare_module, "_video_stream_types", lambda _path: streams)
    with pytest.raises(PreparationError, match="contains no video stream"):
        prepare_module.prepare_request(normalize_request(_request(source=True)), asset_map={name: str(path) for name, path in _files(tmp_path, source=True).items()})


def test_character_swap_inspection_requires_available_probe(monkeypatch, tmp_path: Path) -> None:
    from astrid.packs.h3_av.shared import prepare as prepare_module
    from astrid.packs.h3_av.shared.errors import PreparationError
    monkeypatch.setattr(prepare_module.shutil, "which", lambda _name: None)
    with pytest.raises(PreparationError, match="requires ffprobe"):
        prepare_module._video_stream_types(tmp_path / "source.mp4")


def test_masked_route_binds_mixed_reference_occurrences(tmp_path):
    from vibecomfy.cli_loader import load_workflow_any
    from astrid.packs.h3_av.shared.compile import _compile_native_v2
    raw = _request(adapter=False, source=True)
    raw["prompt"] = "Use <Picture 1>, <Video 1>, <Audio 1>, and <Audio 2>."
    raw["media"][1]["audio"] = True
    raw["media"].append({"id": "voice", "asset": "voice.wav", "role": "reference", "modality": "audio"})
    request = normalize_request(raw)
    files = {}
    for item in raw["media"]:
        path = tmp_path / item["asset"]
        path.write_bytes(b"reference fixture")
        files[item["asset"]] = path
    compiled = _compile_native_v2(request, files, tmp_path / "out")
    rows = compiled["capabilities"]["conditioning"]
    assert [(row["occurrence"], row["tag"]) for row in rows] == [
        (request.value["media"][2]["occurrence_id"], "<Picture 1>"),
        (request.value["media"][1]["occurrence_id"], "<Video 1>"),
        (request.value["media"][3]["occurrence_id"], "<Audio 2>"),
    ]
    assert rows[1]["input_binding"]["soundtrack"]["tag"] == "<Audio 1>"
    workflow = load_workflow_any(compiled["workflow"]["workflow.py"]["path"])
    api = workflow.compile("api", run_inputs=compiled["workflow_inputs"])
    target = next(node for node in api.values() if node["class_type"] == "MiniMaxH3ReferenceToVideo")
    assert target["inputs"]["ref_video_audios.ref_video_audio_0"] == [target["inputs"]["ref_videos.ref_video_0"][0], 2]
    for port, cls, binding, field in (("ref_images.ref_image_0", "LoadImage", "reference_0", "image"),
                                      ("ref_videos.ref_video_0", "VHS_LoadVideoFFmpeg", "video_reference_0", "video"),
                                      ("ref_audios.ref_audio_0", "LoadAudio", "audio_reference_0", "audio")):
        loader = api[target["inputs"][port][0]]
        assert loader["class_type"] == cls
        assert loader["inputs"][field] == compiled["workflow_inputs"][binding]
    assert target["inputs"]["prompt"] == raw["prompt"]


def test_masked_route_missing_reference_fails_closed(tmp_path):
    from astrid.packs.h3_av.shared.compile import _compile_native_v2
    request = normalize_request(_request(adapter=False, source=True))
    with pytest.raises(CompilationError, match="missing from preparation"):
        _compile_native_v2(request, {"baseline.mp4": tmp_path / "source"}, tmp_path)


def test_character_swap_with_pixel_rectangle_binds_adapter_and_mask(tmp_path, monkeypatch):
    from vibecomfy.cli_loader import load_workflow_any
    from astrid.packs.h3_av.shared import generation
    from astrid.packs.h3_av.shared.compile import _compile_native_v2
    raw = _request(source=True)
    raw["media"][0]["edit"][0]["mask"] = {"rectangle": [0, 0, 128, 128]}
    request = normalize_request(raw)
    files = {}
    for item in raw["media"]:
        path = tmp_path / item["asset"]
        path.write_bytes(b"asset fixture")
        files[item["asset"]] = path
    seen = []
    original = generation.build_generation_workflow
    def capture(inputs):
        seen.append(inputs)
        return original(inputs)
    monkeypatch.setattr(generation, "build_generation_workflow", capture)
    compiled = _compile_native_v2(request, files, tmp_path / "out")
    job = seen[0]
    assert job.model["adapter"]["id"] == "character_swap_v1"
    assert job.model["adapter"]["strength"] == 1
    assert job.placed[0]["occurrence_id"] == request.value["media"][0]["occurrence_id"]
    assert job.grants[0]["stream"] == "video"
    assert job.grants[0]["interval"] == [0, 1]
    assert job.grants[0]["source_occurrence"] == job.placed[0]["occurrence_id"]
    assert job.grants[0]["region_raster"]["input_binding"] == "mask_0"
    assert job.grants[0]["strength"] == 1
    assert job.grants[0]["seed"] == request.value["settings"]["seed"]
    assert set(job.grants[0]["references"]) == {request.value["media"][1]["occurrence_id"], request.value["media"][2]["occurrence_id"]}
    workflow = load_workflow_any(compiled["workflow"]["workflow.py"]["path"])
    api = workflow.compile("api", run_inputs=compiled["workflow_inputs"])
    adapter = next(node for node in api.values() if node["class_type"] == "LoraLoaderModelOnly" and node["inputs"]["lora_name"] == CHARACTER_SWAP_FILENAME)
    mask = next(node for node in api.values() if node["class_type"] == "H3V2VGranularFractionalDenoise")
    assert adapter["inputs"]["strength_model"] == 1.0
    assert api[mask["inputs"]["source_mask"][0]]["class_type"] == "LoadImageMask"
    assert compiled["workflow_inputs"]["mask_0"]


def test_full_frame_character_swap_uses_only_selected_guides(tmp_path):
    from vibecomfy.cli_loader import load_workflow_any
    from astrid.packs.h3_av.shared.compile import _compile_native_v2
    raw = _request(source=True)
    raw["media"][0]["edit"][0]["guides"] = ["performance", "replacement"]
    raw["media"].append({"id": "unused", "asset": "unused.png", "role": "reference", "modality": "image"})
    request = normalize_request(raw)
    assets = {}
    for item in raw["media"]:
        path = tmp_path / item["asset"]
        path.write_bytes(b"asset fixture")
        assets[item["asset"]] = path
    compiled = _compile_native_v2(request, assets, tmp_path / "out")
    caps = compiled["capabilities"]
    assert caps["references"] == 2
    assert caps["reference_modality_counts"] == {"image": 1, "video": 1, "audio": 0}
    assert [row["occurrence"] for row in caps["conditioning"]] == [request.value["media"][2]["occurrence_id"], request.value["media"][1]["occurrence_id"]]
    assert "reference_1" not in compiled["workflow_inputs"]
    workflow = load_workflow_any(compiled["workflow"]["workflow.py"]["path"])
    api = workflow.compile("api", run_inputs=compiled["workflow_inputs"])
    target = next(node for node in api.values() if node["class_type"] == "MiniMaxH3ReferenceToVideo")
    assert "ref_images.ref_image_0" in target["inputs"]
    assert "ref_images.ref_image_1" not in target["inputs"]
    assert "ref_videos.ref_video_0" in target["inputs"]


def test_character_swap_refuses_differing_full_frame_guide_sets(tmp_path):
    from astrid.packs.h3_av.shared.compile import _compile_native_v2
    raw = _request(source=True)
    raw["media"][0]["edit"][0]["guides"] = ["performance", "replacement"]
    raw["media"].append({"id": "unused", "asset": "unused.png", "role": "reference", "modality": "image"})
    raw["media"][0]["edit"].append({"stream": "video", "during": [0, 1], "mask": {"full_frame": True}, "guides": ["performance", "unused"]})
    request = normalize_request(raw)
    with pytest.raises(CompilationError, match="different reference sets"):
        _compile_native_v2(request, {"baseline.mp4": tmp_path / "source"}, tmp_path / "out")


def test_masked_character_swap_requires_bound_video_not_merely_declared(tmp_path):
    from astrid.packs.h3_av.shared.compile import _compile_native_v2
    raw = _request(source=True)
    raw["media"][0]["edit"][0].update(mask={"rectangle": [0, 0, 128, 128]}, guides=["replacement"])
    raw["prompt"] = "Replace the character using <Picture 1>."
    request = normalize_request(raw)
    assets = {}
    for item in raw["media"]:
        path = tmp_path / item["asset"]
        path.write_bytes(b"asset fixture")
        assets[item["asset"]] = path
    with pytest.raises(CompilationError, match="effective references.*video.*image"):
        _compile_native_v2(request, assets, tmp_path / "out")


def test_character_swap_refuses_differing_full_frame_grant_seeds(tmp_path):
    from astrid.packs.h3_av.shared.compile import _compile_native_v2
    raw = _request(source=True)
    raw["media"][0]["edit"][0]["seed"] = 1
    raw["media"][0]["edit"].append({"stream": "video", "during": [0, 1], "mask": {"full_frame": True}, "seed": 2})
    request = normalize_request(raw)
    with pytest.raises(CompilationError, match="different seeds"):
        _compile_native_v2(request, {"baseline.mp4": tmp_path / "source"}, tmp_path / "out")


def test_character_swap_explicit_default_strength_preserves_default_graph(tmp_path):
    from astrid.packs.h3_av.shared.compile import _compile_native_v2
    raw = _request(source=True)
    assets = {}
    for item in raw["media"]:
        path = tmp_path / item["asset"]
        path.write_bytes(b"asset fixture")
        assets[item["asset"]] = path
    omitted = _compile_native_v2(normalize_request(raw), assets, tmp_path / "omitted")
    raw["media"][0]["edit"][0]["strength"] = 1
    explicit = _compile_native_v2(normalize_request(raw), assets, tmp_path / "explicit")
    assert omitted["workflow"]["workflow.py"]["sha256"] == explicit["workflow"]["workflow.py"]["sha256"]
    assert omitted["workflow_inputs"] == explicit["workflow_inputs"]


def test_masked_character_swap_refuses_prompt_tag_for_unselected_video(tmp_path):
    from astrid.packs.h3_av.shared.compile import _compile_native_v2
    raw = _request(source=True)
    raw["media"][0]["edit"][0].update(mask={"rectangle": [0, 0, 128, 128]}, guides=["replacement"])
    request = normalize_request(raw)
    assets = {}
    for item in raw["media"]:
        path = tmp_path / item["asset"]
        path.write_bytes(b"asset fixture")
        assets[item["asset"]] = path
    with pytest.raises(CompilationError, match="prompt reference tag <Video 1> has no effective binding"):
        _compile_native_v2(request, assets, tmp_path / "out")

@pytest.mark.parametrize("size", [(1024, 576), (512, 288), (768, 448)])
def test_character_swap_canvas_is_emitted(tmp_path, size):
    from vibecomfy.cli_loader import load_workflow_any
    raw = _request()
    raw["output"] = {"size": list(size)}
    compiled = compile_preparation(_preparation(raw, _files(tmp_path)), out_dir=tmp_path / "out")
    api = load_workflow_any(compiled["workflow"]["workflow.py"]["path"]).compile("api", run_inputs=compiled["workflow_inputs"])
    sized = [(row["class_type"], key, value) for row in api.values() for key, value in row["inputs"].items() if key in ("width", "height")]
    assert sorted(sized) == sorted([("MiniMaxH3ReferenceToVideo", "width", size[0]), ("MiniMaxH3ReferenceToVideo", "height", size[1])])
    assert compiled["size"] == {"width": size[0], "height": size[1]}



def test_source_backed_selected_guide_order_is_provisional_until_compiled(tmp_path):
    from PIL import Image
    from astrid.packs.h3_av.shared.prepare import prepare_request

    raw = _request(source=True)
    raw["media"].append({"id": "other", "asset": "other.png", "role": "reference", "modality": "image"})
    raw["media"][0]["edit"][0]["guides"] = ["other", "replacement", "performance"]
    files = _files(tmp_path, source=True)
    files["other.png"] = tmp_path / "other.png"
    Image.new("RGB", (64, 64), color="blue").save(files["other.png"])
    request = normalize_request(raw)
    prepared = prepare_request(request, asset_map={name: str(path) for name, path in files.items()})
    assert all(row["tag"] is None and row["slot"] is None and
               row["binding_status"] == "provisional_until_lowering"
               for row in prepared["conditioning_measurements"])
    compiled = compile_preparation(prepared, out_dir=tmp_path / "compiled")
    rows = compiled["capabilities"]["conditioning"]
    assert [(row["occurrence"], row["tag"]) for row in rows] == [
        (request.value["media"][3]["occurrence_id"], "<Picture 1>"),
        (request.value["media"][2]["occurrence_id"], "<Picture 2>"),
        (request.value["media"][1]["occurrence_id"], "<Video 1>"),
    ]


def test_character_swap_unresolved_reference_modality_fails_with_compilation_error(tmp_path):
    from astrid.packs.h3_av.shared.compile import _compile_native_v2

    raw = _request(source=True)
    raw["media"].append({"id": "unknown", "asset": "unknown", "role": "reference"})
    request = normalize_request(raw)
    with pytest.raises(CompilationError, match="unresolved modality"):
        _compile_native_v2(request, {}, tmp_path / "out")
