"""General single-pass H3 reference-to-AV adapter; no project or fixture inputs.

Node contract: ComfyUI ee71d5c4993f29086b27fde1629a945ae48425bf,
comfy_extras/nodes_minimax_h3.py, MiniMaxH3ReferenceToVideo.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any, Mapping

from .timing import frame_index, smallest_h3_run_at_least

IMAGE_REFERENCE_CAPACITY = 9
VIDEO_REFERENCE_CAPACITY = 3
CHARACTER_SWAP_ID = "character_swap_v1"
CHARACTER_SWAP_REPOSITORY = "akatz-ai/MiniMax-H3-Character-Swap-LoRA"
CHARACTER_SWAP_REVISION = "62407e0cc8089c363abd9ce4b0b27662abb237af"
CHARACTER_SWAP_FILENAME = "h3_character_swap_pro4500_1000.safetensors"
CHARACTER_SWAP_SHA256 = "4b2a3f420ae804c0aa3422761ff84dbd1bf52eef6900ffab6d2e66df63cb4e79"
TURBO_FILENAME = "minimax_h3_fl2v_turbo_8step_v1.0_comfyui_bf16.safetensors"
_T9_MODEL_SUBSTITUTE_MODE = "deterministic_cpu_model_boundary_v1"
_T9_MODEL_SUBSTITUTE_PURPOSE = "h3_character_swap_public_runtime_cpu_acceptance_v1"
_CANDIDATE_MANIFEST_SCHEMA = "astrid-h3-candidate-source-manifest/v1"
_T9_FIXTURE_RELATIVE_PATH = "tests/fixtures/h3_cpu/approved_character_swap_cpu_boundary.py"


def _validated_synthetic_fixture_path(substitute: Mapping[str, Any], source_files: Mapping[str, Any]) -> Path:
    """Resolve the explicitly pinned fixture independently of installed code."""
    raw_path = substitute.get("source_path")
    if not isinstance(raw_path, str) or not raw_path:
        raise ValueError("approved T9 fixture source is unavailable or aliased")
    path = Path(raw_path)
    if (not path.is_absolute() or path.is_symlink() or not path.is_file()
            or any(parent.is_symlink() for parent in path.parents)
            or path.resolve(strict=True) != path):
        raise ValueError("approved T9 fixture source is unavailable or aliased")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    if source_files.get(_T9_FIXTURE_RELATIVE_PATH) != digest:
        raise ValueError("T9 fixture source does not match the selected candidate pin")
    if substitute.get("source_sha256") != "sha256:" + digest:
        raise ValueError("T9 profile source digest differs from the approved fixture")
    return path


def character_swap_asset_sha256() -> str:
    """Return the production LoRA pin, or a source-bound T9 test asset digest.

    Production callers do not need an environment setting and always receive
    ``CHARACTER_SWAP_SHA256``. The override exists only for the explicit,
    synthetic CPU boundary. It verifies the candidate manifest, the exact
    fixture source, and the synthetic file against the readiness profile's
    digest-bound model-root inventory before returning a different digest.
    """
    raw_profile = os.environ.get("ASTRID_H3_T9_MODEL_SUBSTITUTE_PROFILE")
    if raw_profile is None:
        return CHARACTER_SWAP_SHA256
    if not raw_profile:
        raise ValueError("T9 model substitute profile path is empty")

    profile_path = Path(raw_profile)
    if not profile_path.is_absolute() or profile_path.is_symlink() or not profile_path.is_file():
        raise ValueError("T9 model substitute profile must be an absolute regular non-symlink file")
    try:
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("T9 model substitute profile is unreadable") from exc
    if not isinstance(profile, Mapping):
        raise ValueError("T9 model substitute profile must be an object")
    if profile.get("schema_version") != "hc03-worker-readiness.v1" or profile.get("status") != "ready":
        raise ValueError("T9 model substitute profile is not a ready profile")
    substitute = profile.get("t9_model_substitute")
    if not isinstance(substitute, Mapping) or (
        substitute.get("approved") is not True
        or substitute.get("mode") != _T9_MODEL_SUBSTITUTE_MODE
        or substitute.get("purpose") != _T9_MODEL_SUBSTITUTE_PURPOSE
    ):
        raise ValueError("T9 model substitute profile does not identify the approved synthetic boundary")

    candidate_raw = os.environ.get("ASTRID_H3_CANDIDATE_MANIFEST")
    if not isinstance(candidate_raw, str) or not candidate_raw:
        raise ValueError("T9 digest override requires ASTRID_H3_CANDIDATE_MANIFEST")
    candidate_path = Path(candidate_raw)
    if not candidate_path.is_absolute() or candidate_path.is_symlink() or not candidate_path.is_file():
        raise ValueError("T9 candidate manifest must be an absolute regular non-symlink file")
    if substitute.get("candidate_manifest_path") != str(candidate_path.resolve(strict=True)):
        raise ValueError("T9 profile candidate manifest path differs from the selected candidate")
    candidate_bytes = candidate_path.read_bytes()
    candidate_digest = hashlib.sha256(candidate_bytes).hexdigest()
    if substitute.get("candidate_manifest_sha256") != candidate_digest:
        raise ValueError("T9 profile candidate manifest digest differs from the selected candidate")
    try:
        candidate = json.loads(candidate_bytes)
    except json.JSONDecodeError as exc:
        raise ValueError("T9 candidate manifest is invalid JSON") from exc
    if not isinstance(candidate, Mapping) or (
        candidate.get("schema") != _CANDIDATE_MANIFEST_SCHEMA
        or candidate.get("synthetic_inference") is not True
        or candidate.get("synthetic_inference_classification") != "synthetic-inference-only"
    ):
        raise ValueError("T9 digest override requires a synthetic-only H3 candidate manifest")
    source_files = candidate.get("source_files")
    if not isinstance(source_files, Mapping):
        raise ValueError("T9 candidate manifest has no source file pins")

    astrid_root = Path(__file__).resolve().parents[4]
    _validated_synthetic_fixture_path(substitute, source_files)

    for relative in (
        "astrid/packs/h3_av/shared/generation.py",
        "astrid/packs/h3_av/actions/compile/compile.py",
    ):
        path = astrid_root / relative
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"T9 source pin target is unavailable or aliased: {relative}")
        if source_files.get(relative) != hashlib.sha256(path.read_bytes()).hexdigest():
            raise ValueError(f"T9 source file does not match the selected candidate pin: {relative}")

    model_asset = substitute.get("model_asset")
    if not isinstance(model_asset, Mapping) or model_asset.get("name") != CHARACTER_SWAP_FILENAME:
        raise ValueError("T9 model asset must name the pinned Character Swap LoRA")
    digest_value = model_asset.get("sha256")
    if not isinstance(digest_value, str) or not re.fullmatch(r"sha256:[0-9a-f]{64}", digest_value):
        raise ValueError("T9 model asset digest is malformed")
    size = model_asset.get("size")
    if isinstance(size, bool) or not isinstance(size, int):
        raise ValueError("T9 model asset size is malformed")
    if size <= 0:
        raise ValueError("T9 model asset size must be positive")
    if model_asset.get("subdir") != "loras":
        raise ValueError("T9 model asset must be bound under the loras directory")

    launch = profile.get("launch")
    model_root = launch.get("model_root") if isinstance(launch, Mapping) else None
    if not isinstance(model_root, Mapping):
        raise ValueError("T9 profile has no digest-bound model root")
    from astrid.core.generation.model_root import ModelRootBindingError, validate_model_root_binding

    try:
        bound_model_root = validate_model_root_binding(model_root, verify_files=True)
    except ModelRootBindingError as exc:
        raise ValueError("T9 model-root inventory is not an exact file binding") from exc
    if bound_model_root.qualification_identity is not None:
        raise ValueError("T9 synthetic model root must not claim a deployment qualification identity")
    matches = [
        row for row in bound_model_root.inventory
        if row.get("name") == CHARACTER_SWAP_FILENAME
        and row.get("subdir") == "loras"
    ]
    if len(matches) != 1:
        raise ValueError("T9 model-root inventory must contain exactly one Character Swap LoRA")
    row = matches[0]
    model_path = bound_model_root.path / "loras" / CHARACTER_SWAP_FILENAME
    if model_path.is_symlink() or not model_path.is_file():
        raise ValueError("T9 synthetic Character Swap LoRA is unavailable or aliased")
    actual_digest = "sha256:" + hashlib.sha256(model_path.read_bytes()).hexdigest()
    actual_size = model_path.stat().st_size
    if (
        digest_value != actual_digest
        or size != actual_size
        or row.get("sha256") != actual_digest
        or row.get("size") != actual_size
    ):
        raise ValueError("T9 synthetic LoRA does not match its profile and model-root inventory")
    return actual_digest.removeprefix("sha256:")


def generation_timing(duration: float) -> dict[str, Any]:
    frames = frame_index(duration, "output.duration")
    raw = smallest_h3_run_at_least(frames)
    # Bound this adapter to the upstream documented training range.
    if frames < 5 or raw > 362:
        raise ValueError("source-free duration must request 5..362 frames at 24 fps")
    return {"fps": 24, "requested_frames": frames, "raw_frames": raw,
            "trim_tail_frames": raw - frames, "duration": frames / 24}


def build_generation_workflow(*, references: list[str], prompt: str, frames: int,
                              model: str, steps: int, seed: int,
                              sampler: str = "res_multistep", guidance: float = 0.95,
                              scheduler: str = "simple", video_references: list[dict[str, Any]] | None = None,
                              adapter_strength: float | None = None, turbo_enabled: bool = True,
                              character_swap_sha256: str = CHARACTER_SWAP_SHA256):
    """Build ordinary H3 nodes and one muxed sink, ready for canonical emission.

    Graph shape depends only on reference list length. Filenames remain managed
    run bindings, not graph defaults; ordered slots bind Picture 1..N without
    implicit keyframe constraints.
    """
    from vibecomfy.templates import ModelAsset, OutputSpec, ReadyMetadata, new_workflow, node
    from vibecomfy.nodes.core import (
        BasicGuider, BasicScheduler, CLIPLoader, KSamplerSelect, LoadImage,
        LoraLoaderModelOnly, RandomNoise, SamplerCustomAdvanced, UNETLoader,
        VAEDecode, VAEDecodeAudio, VAELoader,
    )
    from vibecomfy.nodes.videohelpersuite import VHS_LoadVideoFFmpeg, VHS_VideoCombine

    if not 1 <= len(references) <= IMAGE_REFERENCE_CAPACITY:
        raise ValueError("this adapter requires one to nine image references")
    video_references = list(video_references or [])
    if len(video_references) > VIDEO_REFERENCE_CAPACITY:
        raise ValueError("this adapter requires at most three video references")
    base = "https://huggingface.co/Comfy-Org/MiniMax-H3/resolve/main"
    names = {
        "model": ("diffusion_models", "minimax_h3_ref2va_pruned_int8_convrot.safetensors"),
        "clip": ("text_encoders", "qwen3vl_32b_minimax_h3_int8_convrot.safetensors"),
        "video_vae": ("vae", "minimax_h3_video_vae_int8_convrot.safetensors"),
        "audio_vae": ("vae", "minimax_h3_audio_vae_fp32.safetensors"),
    }
    model_assets = {
        key: ModelAsset(url=f"{base}/{subdir}/{name}", subdir=subdir)
        for key, (subdir, name) in names.items()
    }
    if turbo_enabled:
        model_assets["turbo_lora"] = ModelAsset(
            filename=TURBO_FILENAME,
            url=f"{base}/loras/{TURBO_FILENAME}",
            subdir="loras",
        )
    if adapter_strength is not None:
        model_assets["character_swap_lora"] = ModelAsset(
            filename=CHARACTER_SWAP_FILENAME,
            url=(f"https://huggingface.co/{CHARACTER_SWAP_REPOSITORY}/resolve/"
                 f"{CHARACTER_SWAP_REVISION}/{CHARACTER_SWAP_FILENAME}"),
            subdir="loras",
            sha256=character_swap_sha256,
            hf_revision=CHARACTER_SWAP_REVISION,
        )
    metadata = ReadyMetadata.build(
        capability="video", template_id="h3_av_reference_generation",
        models=model_assets,
        requirements={
            "runtime": {
                "comfy_commit": "ee71d5c4993f29086b27fde1629a945ae48425bf",
                "comfy_version": "==0.36.0",
                "packages": {"torch": "==2.10.0+cu130", "comfy-kitchen": "==0.2.34", "comfy-aimdo": "==0.5.3"},
                "launch_flags": ["--use-ck-attention", "--disable-comfy-compiler"],
            },
            "custom_node_refs": [
                {"slug": "ComfyUI-H3-Motion-Context-MultiRef", "source": "git",
                 "url": "https://github.com/seitanism/ComfyUI-H3-Motion-Context-MultiRef.git",
                 "commit": "361624fb406b63eb6694442eac6c895fc1533a70",
                 "classes": ["MiniMaxH3AudioVAECompatibility"]},
                {"slug": "ComfyUI-VideoHelperSuite", "source": "git",
                 "url": "https://github.com/Kosinkadink/ComfyUI-VideoHelperSuite.git",
                 "commit": "4ee72c065db22c9d96c2427954dc69e7b908444b",
                 "classes": ["VHS_LoadVideoFFmpeg", "VHS_VideoCombine"]},
            ],
        },
    )
    with new_workflow(metadata, source_path=__file__) as wf:
        unet = UNETLoader(unet_name=model, weight_dtype="default", _id="model")
        clip = CLIPLoader(clip_name=names["clip"][1], type="minimax", device="default")
        video_vae = VAELoader(vae_name=names["video_vae"][1])
        audio_vae = VAELoader(vae_name=names["audio_vae"][1])
        audio = node("MiniMaxH3AudioVAECompatibility", _outputs=("audio_vae",), audio_vae=audio_vae.out("VAE"))
        attention = node("ModelAttentionBackend", _outputs=("MODEL",), model=unet.out("MODEL"), attention="comfy kitchen attention")
        upstream = attention.out("MODEL")
        if adapter_strength is not None:
            character_swap = LoraLoaderModelOnly(
                model=upstream, lora_name=CHARACTER_SWAP_FILENAME,
                strength_model=adapter_strength, _id="character_swap",
            )
            upstream = character_swap.out("MODEL")
        if turbo_enabled:
            turbo = LoraLoaderModelOnly(
                model=upstream, lora_name=TURBO_FILENAME,
                strength_model=guidance, _id="turbo_lora",
            )
            upstream = turbo.out("MODEL")
        shifted = node("MiniMaxH3SigmaShift", _outputs=("MODEL",), model=upstream, shift_audio=3.0, shift_video=12.0)
        refs = {}
        for index in range(len(references)):
            ident = f"reference_{index}"
            # Canonical emission deliberately clears media defaults. Keep the
            # template unbound and supply filenames only via managed run inputs
            # so emitting/reloading cannot change the graph's semantic digest.
            loaded = LoadImage(image="", _id=ident)
            wf.register_input(ident, ident, "image", "", type="CHOICE", default="", required=True, media_semantics="image")
            refs[f"ref_images.ref_image_{index}"] = loaded.out("IMAGE")
        video_audio_inputs: set[str] = set()
        for index, reference in enumerate(video_references):
            ident = str(reference.get("binding", f"video_reference_{index}"))
            loader = VHS_LoadVideoFFmpeg(
                custom_height=0,
                custom_width=0,
                force_rate=24,
                format="AnimateDiff",
                frame_load_cap=int(reference.get("frame_load_cap", 0)),
                start_time=float(reference.get("start_time", 0.0)),
                video="",
                videopreview={"hidden": False, "paused": False, "params": {}, "muted": False},
                _id=ident,
            )
            wf.register_input(ident, ident, "video", "", type="CHOICE", default="", required=True, media_semantics="video")
            refs[f"ref_videos.ref_video_{index}"] = loader.out("IMAGE")
            if reference.get("audio", True):
                audio_port = f"ref_video_audios.ref_video_audio_{index}"
                refs[audio_port] = loader.out(2)
                video_audio_inputs.add(audio_port)
        # Retain the pinned define_schema contract: the offline schema predates
        # this node. Declare only the exact dynamic reference ports used here.
        target_inputs = ["clip", "vae", "audio_vae", "prompt", "width", "height", "length", "ref_image_size", *refs]
        target = node("MiniMaxH3ReferenceToVideo", _id="target", _outputs=("positive", "LATENT"),
                      _native_ports={
                          "native_input_names": target_inputs,
                          "native_input_types": ["CLIP", "VAE", "VAE", "STRING", "INT", "INT", "INT", "COMBO", *["AUDIO" if name in video_audio_inputs else "IMAGE" for name in refs]],
                          "native_output_names": ["positive", "LATENT"],
                          "native_output_types": ["CONDITIONING", "LATENT"],
                      },
                      clip=clip.out("CLIP"), vae=video_vae.out("VAE"), audio_vae=audio.out("audio_vae"),
                      prompt=prompt, width=1024, height=576, length=frames, ref_image_size="match", **refs)
        schedule = BasicScheduler(model=shifted.out("MODEL"), scheduler=scheduler, steps=steps, denoise=1.0, _id="schedule")
        selected = KSamplerSelect(sampler_name=sampler, _id="sampler")
        noise = RandomNoise(noise_seed=seed, _id="noise")
        guider = BasicGuider(model=shifted.out("MODEL"), conditioning=target.out("positive"))
        sampled = SamplerCustomAdvanced(guider=guider.out("GUIDER"), latent_image=target.out("LATENT"),
                                       noise=noise.out("NOISE"), sampler=selected.out("SAMPLER"), sigmas=schedule.out("SIGMAS"))
        video = VAEDecode(samples=sampled.out("OUTPUT"), vae=video_vae.out("VAE"))
        sound = VAEDecodeAudio(samples=sampled.out("OUTPUT"), vae=audio.out("audio_vae"))
        movie = VHS_VideoCombine(images=video.out("IMAGE"), audio=sound.out("AUDIO"),
                                frame_rate=24, loop_count=0, filename_prefix="h3_av/generated",
                                format="video/h264-mp4", pingpong=False, save_output=True,
                                crf=19, pix_fmt="yuv420p", save_metadata=False, trim_to_audio=False)
        input_rows = [
            ("prompt", "target", "prompt", prompt, "STRING"),
            ("model", "model", "unet_name", model, "CHOICE"),
            ("steps", "schedule", "steps", steps, "INT"),
            ("seed", "noise", "noise_seed", seed, "INT"),
            ("sampler", "sampler", "sampler_name", sampler, "COMBO"),
            ("scheduler", "schedule", "scheduler", scheduler, "COMBO"),
        ]
        if turbo_enabled:
            input_rows.append(("guidance", "turbo_lora", "strength_model", guidance, "FLOAT"))
        for name, ident, field, value, kind in input_rows:
            wf.register_input(name, ident, field, value, type=kind, default=value)
        return wf.finalize({}, outputs=[OutputSpec(
            node=movie, output_type="VHS_VideoCombine", name="video", artifact_kind="video",
            mime_type="video/mp4", filename_prefix="h3_av/generated", expected_cardinality="one",
        )])
