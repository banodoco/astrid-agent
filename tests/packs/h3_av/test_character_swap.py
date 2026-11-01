from __future__ import annotations

import copy
import hashlib
import json
import subprocess
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator, ValidationError

from astrid.packs.h3_av.actions.compile.compile import CompilationError, compile_preparation
from astrid.packs.h3_av.actions.compile.run import main as compile_executor_main
from astrid.packs.h3_av.shared.generation import (
    CHARACTER_SWAP_FILENAME,
    CHARACTER_SWAP_REVISION,
    CHARACTER_SWAP_SHA256,
    TURBO_FILENAME,
)
from astrid.packs.h3_av.actions.prepare.run import main as prepare_executor_main
from astrid.packs.h3_av.shared.input_bundle import build_input_bundle
from astrid.packs.h3_av.shared.request import H3RequestError, load_request_document, load_request, normalize_request
from astrid.packs.h3_av.shared.request_v2 import REF2VA_MODEL, effective_settings

PACK = Path(__file__).resolve().parents[3] / "astrid/packs/h3_av"
SCHEMA = Draft202012Validator(json.loads((PACK / "schemas/request.v2.json").read_text()))


def _request(*, adapter: bool = True, turbo: bool = True, source: bool = False) -> dict:
    media = [
        {"id": "performance", "asset": "performance.mp4", "role": "reference", "modality": "video", "range": [1, 2], "audio": False},
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
    files["performance.mp4"].write_bytes(b"reference video fixture")
    files["replacement.png"].write_bytes(b"reference image fixture")
    if source:
        files["baseline.mp4"] = tmp_path / "baseline.mp4"
        files["baseline.mp4"].write_bytes(b"timeline baseline fixture")
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
    assert normalized.value["model_tags"] == {"performance": "<Video 1>", "replacement": "<Picture 1>"}
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
    tampered_payload = json.dumps(tampered_replay, sort_keys=True, separators=(",", ":")).encode()
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
    assert "model_tags" in normalized.value
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
    assert video_loader["inputs"]["frame_load_cap"] == 24
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
def test_managed_v2_prepare_to_compile_preserves_normalized_request(tmp_path: Path) -> None:
    """The executor boundary must reuse the sealed v2 request from prepare."""
    import vibecomfy  # noqa: F401  (a sibling checkout; compile loads it lazily)
    raw = _request()
    request_path = tmp_path / "request.json"
    request_path.write_text(json.dumps(raw, sort_keys=True), encoding="utf-8")
    files = _files(tmp_path)
    bundle = build_input_bundle(
        normalize_request(raw),
        {name: str(path) for name, path in files.items()},
        tmp_path / "input-bundle.zip",
    )

    preparation_path = tmp_path / "prepare" / "preparation.json"
    assert prepare_executor_main([
        "--request", str(request_path),
        "--input-bundle", str(bundle),
        "--out", str(preparation_path),
    ]) == 0
    preparation = json.loads(preparation_path.read_text(encoding="utf-8"))
    assert {"profile", "output_count", "model_tags"}.issubset(preparation["request"])
    assert all("path" not in record for record in preparation["assets"])

    compiled_path = tmp_path / "compile"
    assert compile_executor_main([
        "--preparation", str(preparation_path),
        "--input-bundle", str(bundle),
        "--out", str(compiled_path),
    ]) == 0
    compilation = json.loads((compiled_path / "compilation.json").read_text(encoding="utf-8"))
    assert compilation["profile"] == "h3_av.native.v2"
    assert compilation["request_digest"] == preparation["request_digest"]
    assert compilation["workflow_inputs"]["reference_0"]
    assert compilation["managed_assets"]["sha256"]


@pytest.mark.integration
def test_character_swap_rejects_spatial_and_channel_specific_composition_before_execution(tmp_path: Path) -> None:
    import vibecomfy  # noqa: F401  (a sibling checkout; compile loads it lazily)
    raw = _request(source=True)
    raw["media"][0]["edit"][0]["mask"] = {"rectangle": [0, 0, 0.5, 0.5]}
    with pytest.raises(CompilationError, match="not rasterized"):
        compile_preparation(_preparation(raw, _files(tmp_path, source=True)), out_dir=tmp_path / "regional")

    raw = _request(source=True)
    raw["media"][0]["edit"].append({"stream": "audio", "during": [0, 1], "channels": "voice", "action": "generate"})
    with pytest.raises(CompilationError, match="channel-specific audio"):
        compile_preparation(_preparation(raw, _files(tmp_path, source=True)), out_dir=tmp_path / "voice-only")


@pytest.mark.ffmpeg
def test_character_swap_preserve_only_publishes_authoritative_source_bytes(tmp_path: Path) -> None:

    from astrid.packs.h3_av.shared.compose import compose_candidate
    from astrid.packs.h3_av.actions.prepare.prepare import prepare_request
    from astrid.packs.h3_av.actions.verify.verify import verify_candidate

    files = _files(tmp_path, source=True)
    source = tmp_path / "source.mkv"
    files["baseline.mp4"] = source
    generated = tmp_path / "model-output.mkv"
    for path, color, tone in ((source, "blue", 440), (generated, "red", 880)):
        subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                "-f", "lavfi", "-i", f"color=c={color}:s=64x48:r=24:d=1",
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
    from astrid.packs.h3_av.actions.prepare import prepare as prepare_module

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


def test_finalizer_contract_is_declared_and_copies_candidate_without_claiming_acceptance(tmp_path: Path) -> None:
    from astrid.packs.h3_av.actions.publication_finalizer.run import main

    source = tmp_path / "candidate.mp4"
    payload = b"\x00\x00\x00\x18ftypisom" + b"candidate bytes"
    source.write_bytes(payload)
    verification = tmp_path / "verification.json"
    verification.write_text(json.dumps({
        "schema_version": 1,
        "kind": "h3_av_verification",
        "status": "verified",
        "request_digest": "a" * 64,
        "candidate_sha256": hashlib.sha256(payload).hexdigest(),
        "lifecycle": {"prepared": True, "composed": True, "verified": True},
    }))
    output = tmp_path / "finalizer"
    assert main([
        "--candidate", str(source), "--verification", str(verification),
        "--expected-request-digest", "a" * 64, "--out", str(output),
    ]) == 0
    copied = output / "verified-candidate.media"
    assert copied.read_bytes() == payload
    result = json.loads((output / "manifest.json").read_text())
    row = result["outputs"][0]
    assert row["name"] == "verified_candidate"
    assert row["output_port"] == "verified_candidate"
    assert row["media_type"] == "video/mp4"
    assert row["content_hash"] == "sha256:" + hashlib.sha256(payload).hexdigest()
    assert result["inputs"]["candidate_sha256"] == row["content_hash"]
    assert result["inputs"]["request_digest"] == "a" * 64
    assert result["inputs"]["verification_sha256"] == hashlib.sha256(verification.read_bytes()).hexdigest()

    executor = yaml.safe_load((PACK / "pack.yaml").read_text())["actions"]["publication_finalizer"]
    orchestrator = yaml.safe_load((PACK / "pack.yaml").read_text())["actions"]["transform"]
    pack = yaml.safe_load((PACK / "pack.yaml").read_text())
    assert "v1 or v2" in orchestrator["inputs"][0]["description"]
    assert "Character Swap" in orchestrator["inputs"][0]["description"]
    assert [(item["name"], item["type"], item["required"]) for item in executor["inputs"]] == [
        ("candidate", "file", True),
        ("verification", "file", True),
        ("expected_request_digest", "string", True),
        ("generation_intent", "json", True),
    ]
    assert executor["inputs"][1]["artifact_type"] == "h3_av/verification"
    assert executor["outputs"][0]["name"] == "verified_candidate"
    assert executor["metadata"]["generation_publication"]["output_port"] == "verified_candidate"
    assert "{expected_request_digest}" in executor["invocation"]["command"]["argv"]
    assert "h3_av.publication_finalizer" in orchestrator["graph"]["child_actions"]
    assert "finalize_publication" in pack["capabilities"]

    from astrid.core.execution.executor.registry import load_default_registry

    registered = load_default_registry().get("h3_av.publication_finalizer")
    assert registered.metadata["runtime_module"] == (
        "astrid.packs.h3_av.actions.publication_finalizer.run"
    )
    # h3_av.transform is a metadata.local_orchestrator action: it is registered in the
    # orchestrator registry, not the executor registry (71d43e00 folded it into pack v3).
    from astrid.core.execution.orchestrator.registry import load_default_registry as load_orchestrators

    registered_transform = load_orchestrators().get("h3_av.transform")
    assert registered_transform.metadata["runtime_module"] == (
        "astrid.packs.h3_av.actions.transform.run"
    )


@pytest.mark.parametrize(("header", "expected_media_type"), [
    (b"\x00\x00\x00\x18ftypisom", "video/mp4"),
    (b"\x00\x00\x00\x18ftypqt  ", "video/quicktime"),
    (b"\x1aE\xdf\xa3", "video/x-matroska"),
])
def test_compose_executor_records_container_mime_for_stable_finalizer_republication(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, header: bytes, expected_media_type: str
) -> None:
    from astrid.packs.h3_av.actions.compose import run as compose_executor

    preparation = tmp_path / "preparation.json"
    preparation.write_text("{}", encoding="utf-8")
    candidate_source = tmp_path / "composed-source.bin"
    candidate_source.write_bytes(header + b"synthetic composed bytes")

    def fake_compose_candidate(*, preparation, generated, source, out_dir, preservation_evidence):
        out_dir.mkdir(parents=True, exist_ok=True)
        return {"candidate": {"path": str(candidate_source)}}

    monkeypatch.setattr(compose_executor, "compose_candidate", fake_compose_candidate)
    output = tmp_path / "compose-output"
    assert compose_executor.main([
        "--preparation", str(preparation), "--generated", str(tmp_path / "generated.mp4"),
        "--out", str(output),
    ]) == 0

    manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
    candidate = manifest["outputs"][0]
    assert candidate["name"] == "candidate"
    assert candidate["media_type"] == expected_media_type
    assert candidate["content_hash"] == "sha256:" + hashlib.sha256(
        (output / "candidate.media").read_bytes()
    ).hexdigest()

    from astrid.packs.h3_av.actions.publication_finalizer.run import main as finalizer_main

    candidate_bytes = (output / "candidate.media").read_bytes()
    verification = tmp_path / "verification.json"
    verification.write_text(json.dumps({
        "schema_version": 1,
        "kind": "h3_av_verification",
        "status": "verified",
        "request_digest": "a" * 64,
        "candidate_sha256": hashlib.sha256(candidate_bytes).hexdigest(),
        "lifecycle": {"prepared": True, "composed": True, "verified": True},
    }), encoding="utf-8")
    finalizer_output = tmp_path / "finalizer-output"
    assert finalizer_main([
        "--candidate", str(output / "candidate.media"),
        "--verification", str(verification),
        "--expected-request-digest", "a" * 64,
        "--out", str(finalizer_output),
    ]) == 0
    finalizer_manifest = json.loads((finalizer_output / "manifest.json").read_text(encoding="utf-8"))
    assert finalizer_manifest["outputs"][0]["media_type"] == candidate["media_type"]


def test_compose_executor_rejects_unknown_container_before_publishing_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from astrid.packs.h3_av.actions.compose import run as compose_executor

    preparation = tmp_path / "preparation.json"
    preparation.write_text("{}", encoding="utf-8")
    candidate_source = tmp_path / "composed-source.bin"
    candidate_source.write_bytes(b"not a recognized video container")

    def fake_compose_candidate(*, preparation, generated, source, out_dir, preservation_evidence):
        out_dir.mkdir(parents=True, exist_ok=True)
        return {"candidate": {"path": str(candidate_source)}}

    monkeypatch.setattr(compose_executor, "compose_candidate", fake_compose_candidate)
    output = tmp_path / "compose-output"
    with pytest.raises(ValueError, match="must be an MP4/QuickTime or Matroska"):
        compose_executor.main([
            "--preparation", str(preparation), "--generated", str(tmp_path / "generated.mp4"),
            "--out", str(output),
        ])
    assert not (output / "manifest.json").exists()


def test_transform_declares_the_selected_local_runtime_support_root() -> None:
    orchestrator = yaml.safe_load(
        (PACK / "pack.yaml").read_text(encoding="utf-8")
    )["actions"]["transform"]
    from astrid.core.subprocess_env import build_child_subprocess_env

    # The orchestrator calls AstridClient.open_from_launcher in its own
    # isolated subprocess. Its public SDK bootstrap must discover the same
    # disposable support root used by the host and public workspace fixture.
    declared = orchestrator["isolation"]["env_passthrough"]
    support_root = "/tmp/h3-selected-runtime-support"
    child_environment = build_child_subprocess_env(
        base={
            "ASTRID_RUNTIME_DATA_ROOT": support_root,
            "ASTRID_RUNTIME_HOME": "/tmp/stale-home",
            "ASTRID_RUNTIME_SOURCE_MANIFEST": "/tmp/stale-source-profile.json",
        },
        parent={},
        passthrough=declared,
        declared_passthrough=declared,
    )

    assert child_environment["ASTRID_RUNTIME_DATA_ROOT"] == support_root
    assert "ASTRID_RUNTIME_HOME" not in child_environment
    assert "ASTRID_RUNTIME_SOURCE_MANIFEST" not in child_environment


def test_finalizer_rejects_verification_for_different_candidate(tmp_path: Path) -> None:
    from astrid.packs.h3_av.actions.publication_finalizer.run import main

    source = tmp_path / "candidate.mp4"
    payload = b"\x00\x00\x00\x18ftypisom" + b"candidate bytes"
    source.write_bytes(payload)
    verification = tmp_path / "verification.json"
    verification.write_text(json.dumps({
        "schema_version": 1,
        "kind": "h3_av_verification",
        "status": "verified",
        "request_digest": "a" * 64,
        "candidate_sha256": "b" * 64,
        "lifecycle": {"prepared": True, "composed": True, "verified": True},
    }))
    output = tmp_path / "finalizer"
    with pytest.raises(ValueError, match="candidate digest does not match"):
        main([
            "--candidate", str(source), "--verification", str(verification),
            "--expected-request-digest", "a" * 64, "--out", str(output),
        ])
    assert not output.exists()


def test_finalizer_rejects_verification_for_different_request(tmp_path: Path) -> None:
    from astrid.packs.h3_av.actions.publication_finalizer.run import main

    source = tmp_path / "candidate.mp4"
    source.write_bytes(b"\x00\x00\x00\x18ftypisomcandidate")
    verification = tmp_path / "verification.json"
    verification.write_text(json.dumps({
        "schema_version": 1,
        "kind": "h3_av_verification",
        "status": "verified",
        "request_digest": "a" * 64,
        "candidate_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
        "lifecycle": {"prepared": True, "composed": True, "verified": True},
    }))
    output = tmp_path / "finalizer"
    with pytest.raises(ValueError, match="verification request digest does not match"):
        main([
            "--candidate", str(source), "--verification", str(verification),
            "--expected-request-digest", "c" * 64, "--out", str(output),
        ])
    assert not output.exists()


def test_merged_manifest_retains_finalizer_semantics_and_bundle_ports() -> None:
    from astrid.core.pack.canonical import validate_canonical_pack
    manifest = yaml.safe_load((PACK / "pack.yaml").read_text())
    finalizer = manifest["actions"]["publication_finalizer"]
    assert finalizer["metadata"]["generation_publication"] == {
        "version": 1, "modality": "video", "output_port": "verified_candidate",
    }
    assert finalizer["metadata"]["output_result_manifest"] is True
    assert finalizer["outputs"][0]["artifact_type"] == "h3_av/candidate"
    assert [(row["name"], row["type"], row["required"]) for row in finalizer["inputs"]] == [
        ("candidate", "file", True), ("verification", "file", True),
        ("expected_request_digest", "string", True), ("generation_intent", "json", True),
    ]
    for stage in ("prepare", "compile", "compose", "verify"):
        action = manifest["actions"][stage]
        assert action["metadata"]["output_result_manifest"] is True
        assert any(row["name"] == "input_bundle" and row["type"] == "file" for row in action["inputs"])
    for stage in ("compose", "verify"):
        assert {"input": "input_bundle", "flag": "--input-bundle", "optional": True} in manifest["actions"][stage]["invocation"]["command"]["input_args"]
    transform = manifest["actions"]["transform"]
    assert "h3_av.publication_finalizer" in transform["graph"]["child_actions"]
    assert {"input": "prepared_worker", "flag": "--prepared-worker", "optional": True} in transform["invocation"]["command"]["input_args"]
    validate_canonical_pack(PACK, expected_pack_id="h3_av")


@pytest.mark.parametrize("streams", [set(), {"audio"}])
def test_character_swap_timeline_without_video_fails_before_permissions(monkeypatch, tmp_path: Path, streams: set[str]) -> None:
    from astrid.packs.h3_av.actions.prepare import prepare as prepare_module
    from astrid.packs.h3_av.shared.errors import PreparationError
    monkeypatch.setattr(prepare_module, "_video_stream_types", lambda _path: streams)
    with pytest.raises(PreparationError, match="contains no video stream"):
        prepare_module.prepare_request(normalize_request(_request(source=True)), asset_map={name: str(path) for name, path in _files(tmp_path, source=True).items()})


def test_character_swap_inspection_requires_available_probe(monkeypatch, tmp_path: Path) -> None:
    from astrid.packs.h3_av.actions.prepare import prepare as prepare_module
    from astrid.packs.h3_av.shared.errors import PreparationError
    monkeypatch.setattr(prepare_module.shutil, "which", lambda _name: None)
    with pytest.raises(PreparationError, match="requires ffprobe"):
        prepare_module._video_stream_types(tmp_path / "source.mp4")
