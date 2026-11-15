"""Typed direct-media request compilers for VibeComfy.

This module owns the media-family contract only.  It does not discover ready
workflows, choose a backend, or own task lifecycle.  A compiler loads one
pinned template fixture, applies the operation's typed inputs, and emits a
portable request carrying the exact capability, model, profile, and template
identities that an executor must preserve.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import asdict, dataclass
from importlib.resources import files
from typing import Any, Mapping

from astrid.core.generation.vibecomfy_dependency import VIBECOMFY_ENGINE_REVISION

ENGINE_REVISION = VIBECOMFY_ENGINE_REVISION


class MediaCompileError(ValueError):
    """Raised when a typed direct-media request cannot be admitted."""


UNSUPPORTED_MEDIA_CAPABILITIES: dict[str, str] = {}

_UNSUPPORTED_MEDIA_CONTROLS: dict[str, dict[str, str]] = {
    "vibecomfy.video_enhance": {
        "enable_interpolation": "the canonical app-active graph intentionally omits the gated interpolation model",
        "color_fix": "the canonical app-active graph does not contain a color-correction node",
        "output_quality": "the canonical app-active graph exposes one fixed maximum-quality encoder profile",
    },
    "vibecomfy.character_animation": {
        "mode": "the canonical Wan Animate graph represents animate mode only; replacement mode is not wired",
    },
}


def ensure_media_capability_supported(capability_id: str) -> None:
    """Reject media families whose current pinned graph is not semantically proven."""

    reason = UNSUPPORTED_MEDIA_CAPABILITIES.get(capability_id)
    if reason is not None:
        raise MediaCompileError(
            f"{capability_id} is unsupported until its pinned ready graph is repaired: {reason}"
        )


@dataclass(frozen=True, slots=True)
class VibeProfileSemantics:
    """The lifecycle facts a profile-specific executor must preserve."""

    profile_id: str
    engine_identity: str
    lifecycle: str
    output_transport: str
    required_runtime_fields: tuple[str, ...]


_LOCAL_SERVER = VibeProfileSemantics(
    profile_id="local_server",
    engine_identity=f"vibecomfy.local_server@{ENGINE_REVISION}",
    lifecycle="external_server",
    output_transport="published_outputs",
    required_runtime_fields=("task_identity", "profile_digest", "model_bytes_digest"),
)


def profile_semantics(profile_id: str) -> VibeProfileSemantics:
    """Resolve one of the two explicitly supported Vibe execution profiles."""

    if profile_id == _LOCAL_SERVER.profile_id:
        return _LOCAL_SERVER
    raise MediaCompileError(
        f"unsupported Vibe profile {profile_id!r}; expected local_server"
    )


@dataclass(frozen=True, slots=True)
class WanT2IRequest:
    prompt: str
    negative_prompt: str = ""
    seed: int = 0
    width: int = 832
    height: int = 480
    steps: int = 6
    guidance_scale: float = 1.0


@dataclass(frozen=True, slots=True)
class WanI2VRequest:
    image_ref: str
    prompt: str
    negative_prompt: str = ""
    seed: int = 0
    width: int = 832
    height: int = 480
    frames: int = 81
    fps: int = 16
    steps: int = 6
    guidance_scale: float = 1.0


@dataclass(frozen=True, slots=True)
class VideoEnhanceRequest:
    video_ref: str
    scale: float = 2.0
    upscale_method: str = "lanczos"
    # Retained for compatibility so older callers receive a clear rejection.
    enable_interpolation: bool = False
    enable_upscale: bool = True
    interpolation_frames: int = 1
    color_fix: bool = False
    output_quality: str = "maximum"

@dataclass(frozen=True, slots=True)
class CharacterAnimationRequest:
    reference_image_ref: str
    driving_video_ref: str
    mode: str = "animate"
    resolution: str = "480p"
    prompt: str = ""
    negative_prompt: str = ""
    seed: int = 42
    width: int = 832
    height: int = 480
    frames: int = 81
    fps: int = 16
    steps: int = 6


@dataclass(frozen=True, slots=True)
class CompiledVibeMedia:
    """Portable, identity-bearing request handed to a direct media executor."""

    capability_id: str
    model_identity: str
    profile: VibeProfileSemantics
    template_id: str
    template_digest: str
    inputs: Mapping[str, Any]
    bindings: Mapping[str, Any]
    workflow: Mapping[str, Any]
    request_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "capability_id": self.capability_id,
            "model_identity": self.model_identity,
            "profile": asdict(self.profile),
            "template_id": self.template_id,
            "template_digest": self.template_digest,
            "inputs": copy.deepcopy(dict(self.inputs)),
            "bindings": copy.deepcopy(dict(self.bindings)),
            "workflow": copy.deepcopy(dict(self.workflow)),
            "request_digest": self.request_digest,
        }

    def verify_integrity(self) -> None:
        """Reject mutation of a compiled request before profile execution."""

        payload = self.to_dict()
        observed = payload.pop("request_digest")
        if observed != _digest(payload):
            raise MediaCompileError("compiled Vibe media request digest mismatch")


_TEMPLATE_DIR = files("astrid.packs.vibecomfy.media") / "templates"


def _canonical(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise MediaCompileError("media request contains non-canonical JSON values") from exc


def _digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def _required_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise MediaCompileError(f"{field} must be a non-empty string")
    return value.strip()


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise MediaCompileError(f"{field} must be a positive integer")
    return value


def _nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise MediaCompileError(f"{field} must be a non-negative integer")
    return value


def _positive_number(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise MediaCompileError(f"{field} must be a positive number")
    return float(value)


def _template(name: str) -> tuple[str, str, dict[str, Any]]:
    path = _TEMPLATE_DIR / f"{name}.json"
    try:
        raw = path.read_bytes()
        document = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MediaCompileError(f"media template {name!r} is unavailable") from exc
    if not isinstance(document, dict) or not isinstance(document.get("workflow"), dict):
        raise MediaCompileError(f"media template {name!r} is malformed")
    return str(document.get("template_id", "")), "sha256:" + hashlib.sha256(raw).hexdigest(), document


def _set_workflow_inputs(
    workflow: dict[str, Any], values: Mapping[str, Any], *, template: Mapping[str, Any]
) -> dict[str, Any]:
    result = copy.deepcopy(workflow)
    bindings = template.get("bindings")
    if not isinstance(bindings, Mapping):
        raise MediaCompileError("media template has no typed bindings")
    nodes = result.get("nodes")
    if not isinstance(nodes, Mapping):
        raise MediaCompileError("media template workflow has no node map")
    for name, value in values.items():
        target = bindings.get(name)
        if target is None:
            continue
        if not isinstance(target, Mapping) or not isinstance(target.get("node"), str) or not isinstance(target.get("field"), str):
            raise MediaCompileError(f"media template binding {name!r} is malformed")
        node = nodes.get(target["node"])
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            raise MediaCompileError(f"media template binding {name!r} targets a missing node")
        node["inputs"][target["field"]] = copy.deepcopy(value)
    return result


def _compile(
    *,
    capability_id: str,
    model_identity: str,
    template_name: str,
    profile_id: str,
    typed_inputs: Mapping[str, Any],
    bindings: Mapping[str, Any] | None = None,
) -> CompiledVibeMedia:
    profile = profile_semantics(profile_id)
    template_id, template_digest, document = _template(template_name)
    if document.get("capability_id") != capability_id:
        raise MediaCompileError(f"template {template_name!r} is not owned by {capability_id}")
    if document.get("model_identity") != model_identity:
        raise MediaCompileError(f"template {template_name!r} has the wrong model identity")
    inputs = copy.deepcopy(dict(typed_inputs))
    execution_bindings = copy.deepcopy(dict(bindings if bindings is not None else inputs))
    workflow = _set_workflow_inputs(document["workflow"], inputs, template=document)
    request = {
        "capability_id": capability_id,
        "model_identity": model_identity,
        "profile": asdict(profile),
        "template_id": template_id,
        "template_digest": template_digest,
        "inputs": inputs,
        "bindings": execution_bindings,
        "workflow": workflow,
    }
    return CompiledVibeMedia(
        capability_id=capability_id,
        model_identity=model_identity,
        profile=profile,
        template_id=template_id,
        template_digest=template_digest,
        inputs=inputs,
        bindings=execution_bindings,
        workflow=workflow,
        request_digest=_digest(request),
    )


def compile_wan_2_2_t2i(request: WanT2IRequest, *, profile: str = "local_server") -> CompiledVibeMedia:
    """Compile direct Vibe Wan 2.2 text-to-image semantics.

    This is intentionally a VibeComfy capability, not an invocation of the
    native ``wan2gp.generate_video`` capability.  The one-frame contract is
    carried by the direct template and never inferred by the native compiler.
    """

    prompt = _required_text(request.prompt, "prompt")
    width = _positive_int(request.width, "width")
    height = _positive_int(request.height, "height")
    steps = _positive_int(request.steps, "steps")
    seed = _nonnegative_int(request.seed, "seed")
    guidance = _positive_number(request.guidance_scale, "guidance_scale")
    return _compile(
        capability_id="vibecomfy.wan_2_2_t2i",
        model_identity="wan-2.2-a14b-t2v",
        template_name="wan_2_2_t2i",
        profile_id=profile,
        typed_inputs={
            "prompt": prompt,
            "negative_prompt": request.negative_prompt,
            "seed": seed,
            "width": width,
            "height": height,
            "frames": 1,
            "steps": steps,
            "guidance_scale": guidance,
        },
    )


def compile_wan_2_2_i2v(request: WanI2VRequest, *, profile: str = "local_server") -> CompiledVibeMedia:
    """Compile direct Vibe Wan 2.2 image-to-video semantics."""

    image_ref = _required_text(request.image_ref, "image_ref")
    prompt = _required_text(request.prompt, "prompt")
    return _compile(
        capability_id="vibecomfy.wan_2_2_i2v",
        model_identity="wan-2.2-a14b-i2v",
        template_name="wan_2_2_i2v",
        profile_id=profile,
        typed_inputs={
            "image_ref": image_ref,
            "prompt": prompt,
            "negative_prompt": request.negative_prompt,
            "seed": _nonnegative_int(request.seed, "seed"),
            "width": _positive_int(request.width, "width"),
            "height": _positive_int(request.height, "height"),
            "frames": _positive_int(request.frames, "frames"),
            "fps": _positive_int(request.fps, "fps"),
            "steps": _positive_int(request.steps, "steps"),
            "guidance_scale": _positive_number(request.guidance_scale, "guidance_scale"),
        },
    )


def compile_video_enhance(request: VideoEnhanceRequest, *, profile: str = "local_server") -> CompiledVibeMedia:
    """Compile the bounded canonical video-upscale profile.

    The ready graph is deliberately narrower than the historical UI request:
    it performs source-FPS/audio-preserving upscale only. Controls that are
    not represented by the graph are rejected before readiness or scratch
    resolution rather than being silently ignored.
    """

    ensure_media_capability_supported("vibecomfy.video_enhance")

    if request.enable_interpolation:
        raise MediaCompileError(
            "vibecomfy.video_enhance enable_interpolation is unsupported: "
            f"{_UNSUPPORTED_MEDIA_CONTROLS['vibecomfy.video_enhance']['enable_interpolation']}"
        )
    if request.enable_upscale is not True:
        raise MediaCompileError("vibecomfy.video_enhance requires enable_upscale=true")
    interpolation_frames = _positive_int(request.interpolation_frames, "interpolation_frames")
    if interpolation_frames != 1:
        raise MediaCompileError(
            "vibecomfy.video_enhance interpolation_frames is unsupported when interpolation is disabled"
        )
    if request.color_fix:
        raise MediaCompileError(
            "vibecomfy.video_enhance color_fix is unsupported: "
            f"{_UNSUPPORTED_MEDIA_CONTROLS['vibecomfy.video_enhance']['color_fix']}"
        )
    output_quality = _required_text(request.output_quality, "output_quality")
    if output_quality != "maximum":
        raise MediaCompileError(
            "vibecomfy.video_enhance output_quality is unsupported: "
            f"{_UNSUPPORTED_MEDIA_CONTROLS['vibecomfy.video_enhance']['output_quality']}"
        )
    video_ref = _required_text(request.video_ref, "video_ref")
    scale = _positive_number(request.scale, "scale")
    upscale_method = _required_text(request.upscale_method, "upscale_method")
    return _compile(
        capability_id="vibecomfy.video_enhance",
        model_identity="video-enhance.upscale-2x",
        template_name="video_enhance",
        profile_id=profile,
        typed_inputs={
            "video_ref": video_ref,
            "scale": scale,
            "upscale_method": upscale_method,
            "enable_upscale": request.enable_upscale,
            "color_fix": request.color_fix,
            "output_quality": output_quality,
            "preserve_audio": True,
            "preserve_source_fps": True,
        },
        bindings={
            "video_ref": video_ref,
            "scale": scale,
            "upscale_method": upscale_method,
        },
    )


def compile_character_animation(request: CharacterAnimationRequest, *, profile: str = "local_server") -> CompiledVibeMedia:
    """Compile Wan Animate reference-image plus driving-video semantics."""

    ensure_media_capability_supported("vibecomfy.character_animation")

    mode = _required_text(request.mode, "mode")
    if mode != "animate":
        raise MediaCompileError(
            "vibecomfy.character_animation mode is unsupported: "
            f"{_UNSUPPORTED_MEDIA_CONTROLS['vibecomfy.character_animation']['mode']}"
        )
    resolution = _required_text(request.resolution, "resolution")
    dimensions = {"480p": (832, 480), "720p": (1280, 720)}.get(resolution)
    if dimensions is None:
        raise MediaCompileError("resolution must be 480p or 720p")
    width, height = dimensions
    prompt = _required_text(request.prompt, "prompt")
    reference_image_ref = _required_text(request.reference_image_ref, "reference_image_ref")
    driving_video_ref = _required_text(request.driving_video_ref, "driving_video_ref")
    seed = _nonnegative_int(request.seed, "seed")
    frames = _positive_int(request.frames, "frames")
    fps = _positive_int(request.fps, "fps")
    steps = _positive_int(request.steps, "steps")

    return _compile(
        capability_id="vibecomfy.character_animation",
        model_identity="wan-2.2-animate-14b",
        template_name="character_animation",
        profile_id=profile,
        typed_inputs={
            "reference_image_ref": reference_image_ref,
            "driving_video_ref": driving_video_ref,
            "mode": mode,
            "resolution": resolution,
            "prompt": prompt,
            "negative_prompt": request.negative_prompt,
            "seed": seed,
            "width": width,
            "height": height,
            "frames": frames,
            "fps": fps,
            "steps": steps,
        },
        bindings={
            "input_image": reference_image_ref,
            "driving_video": driving_video_ref,
            "prompt": prompt,
            "negative_prompt": request.negative_prompt,
            "seed": seed,
            "width": width,
            "height": height,
            "frames": frames,
            "fps": fps,
            "steps": steps,
        },
    )


__all__ = [
    "CharacterAnimationRequest",
    "CompiledVibeMedia",
    "MediaCompileError",
    "UNSUPPORTED_MEDIA_CAPABILITIES",
    "VideoEnhanceRequest",
    "VibeProfileSemantics",
    "WanI2VRequest",
    "WanT2IRequest",
    "compile_character_animation",
    "compile_video_enhance",
    "compile_wan_2_2_i2v",
    "compile_wan_2_2_t2i",
    "ensure_media_capability_supported",
    "profile_semantics",
]
