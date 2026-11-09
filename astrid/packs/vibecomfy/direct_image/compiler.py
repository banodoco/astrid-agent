"""Pure compiler for Astrid's direct VibeComfy image capabilities.

The compiler has no engine, filesystem, network, or Runtime dependency.  It
turns one typed capability request into a canonical workflow binding plus a
portable execution digest.  Machine-local launch paths, ports, and GPU facts
are intentionally absent from the digest; they belong to host readiness and
runner reuse identity.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping


SCHEMA_VERSION = "direct-image.v1"
_PROFILE_NAMES = ("local_server", "checkout_server")
_SAFE_REF_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")


class ImageCompileError(ValueError):
    """A direct image request is not valid for its declared capability."""


@dataclass(frozen=True, slots=True)
class ProfileConfig:
    """Portable profile semantics; no resolved machine paths are accepted."""

    name: str
    dependency_lock: str
    session_kind: str
    warm_policy: str
    cancellation: str

    def __post_init__(self) -> None:
        if self.name not in _PROFILE_NAMES:
            raise ImageCompileError(f"unsupported Vibe profile: {self.name!r}")
        for field_name in ("dependency_lock", "session_kind", "warm_policy", "cancellation"):
            value = getattr(self, field_name)
            if not isinstance(value, str) or not value.strip():
                raise ImageCompileError(f"profile {field_name} is required")

    def portable_dict(self) -> dict[str, str]:
        return {
            "name": self.name,
            "dependency_lock": self.dependency_lock,
            "session_kind": self.session_kind,
            "warm_policy": self.warm_policy,
            "cancellation": self.cancellation,
        }

    @property
    def digest(self) -> str:
        return _digest(self.portable_dict())


# The two profiles deliberately have different portable dependencies and
# session policies.  They share request/output meaning but never silently
# select one another.
PROFILE_CONFIGS: dict[str, ProfileConfig] = {
    "local_server": ProfileConfig(
        name="local_server",
        dependency_lock="vibecomfy-pip-comfyui-0.26.0",
        session_kind="external_server",
        warm_policy="never",
        cancellation="delete-owned-prompt-and-interrupt-if-running",
    ),
    "checkout_server": ProfileConfig(
        name="checkout_server",
        dependency_lock="vibecomfy-checkout-comfyui-0.26.0",
        session_kind="server_session",
        warm_policy="auto",
        cancellation="interrupt-clear-free-then-owned_process_group",
    ),
}


@dataclass(frozen=True, slots=True)
class CapabilitySpec:
    capability_id: str
    family: str
    model_id: str
    mode: str
    template_id: str
    template_module: str
    required: tuple[str, ...] = ()
    optional: tuple[str, ...] = ()
    output_type: str = "image/png"

    @property
    def allowed_fields(self) -> frozenset[str]:
        return frozenset({*self.required, *self.optional, "profile", "task_identity"})


# Route IDs remain distinct even when several routes use one graph.  In
# particular qwen_image and qwen_image_2512 are separate model identities.
CANONICAL_CAPABILITIES: dict[str, CapabilitySpec] = {
    "z_image_turbo": CapabilitySpec(
        "z_image_turbo", "z_image_t2i", "z-image-turbo", "t2i", "image/z_image",
        "astrid.packs.vibecomfy.direct_image.templates.z_image", required=("prompt",), optional=("negative_prompt", "seed", "width", "height", "steps", "guidance_scale"),
    ),
    "z_image_turbo_i2i": CapabilitySpec(
        "z_image_turbo_i2i", "z_image_i2i", "z-image-turbo", "i2i", "image/z_image_img2img",
        "astrid.packs.vibecomfy.direct_image.templates.z_image", required=("prompt", "image_ref"), optional=("seed", "width", "height", "steps", "strength", "guidance_scale"),
    ),
    "qwen_image": CapabilitySpec(
        "qwen_image", "qwen_t2i_variants", "qwen-image", "t2i", "image/qwen_image_2512",
        "astrid.packs.vibecomfy.direct_image.templates.qwen_image", required=("prompt",), optional=("negative_prompt", "seed", "width", "height", "steps", "guidance_scale"),
    ),
    "qwen_image_2512": CapabilitySpec(
        "qwen_image_2512", "qwen_t2i_variants", "qwen-image-2512", "t2i", "image/qwen_image_2512",
        "astrid.packs.vibecomfy.direct_image.templates.qwen_image", required=("prompt",), optional=("negative_prompt", "seed", "width", "height", "steps", "guidance_scale"),
    ),
    "qwen_image_edit": CapabilitySpec(
        "qwen_image_edit", "qwen_edit_mask", "qwen-image-edit", "edit", "edit/qwen_image_edit",
        "astrid.packs.vibecomfy.direct_image.templates.qwen_edit", required=("prompt", "image_ref"), optional=("seed", "width", "height", "steps"),
    ),
    "qwen_image_style": CapabilitySpec(
        "qwen_image_style", "qwen_edit_mask", "qwen-image-style", "style", "edit/qwen_image_edit",
        "astrid.packs.vibecomfy.direct_image.templates.qwen_edit", required=("prompt", "image_ref"), optional=("seed", "width", "height", "steps", "style_strength"),
    ),
    "image_inpaint": CapabilitySpec(
        "image_inpaint", "qwen_edit_mask", "qwen-image-inpaint", "inpaint", "edit/qwen_image_edit",
        "astrid.packs.vibecomfy.direct_image.templates.qwen_edit", required=("prompt", "image_ref", "mask_ref"), optional=("seed", "width", "height", "steps"),
    ),
    "annotated_image_edit": CapabilitySpec(
        "annotated_image_edit", "qwen_edit_mask", "qwen-image-annotated-edit", "mask_edit", "edit/qwen_image_edit",
        "astrid.packs.vibecomfy.direct_image.templates.qwen_edit", required=("prompt", "image_ref", "mask_ref"), optional=("seed", "width", "height", "steps"),
    ),
    "flux_klein_edit": CapabilitySpec(
        "flux_klein_edit", "flux_klein_edit", "flux-klein-4b-edit", "edit", "edit/flux2_klein_4b_image_edit_distilled",
        "astrid.packs.vibecomfy.direct_image.templates.flux_klein", required=("prompt", "image_ref"), optional=("seed", "width", "height", "steps", "guidance_scale"),
    ),
    "image_upscale": CapabilitySpec(
        "image_upscale", "image_upscale", "seedvr2-upscaler", "upscale", "image/basic_image_upscale",
        "astrid.packs.vibecomfy.direct_image.templates.image_upscale", required=("image_ref",), optional=("upscale_factor", "width", "height", "noise_scale", "seed"),
    ),
}

_ALIASES = {
    "z_image": "z_image_turbo",
    "image-upscale": "image_upscale",
}


# These are the public inputs exposed by the current VibeComfy ready-template
# checkout. Fields accepted by the portable leaf compiler but absent here are
# rejected by the adapter rather than silently discarded.
_CANONICAL_BINDINGS: dict[str, tuple[str, dict[str, str]]] = {
    "z_image_turbo": (
        "image/z_image",
        {"prompt": "prompt", "seed": "seed", "steps": "steps"},
    ),
    "z_image_turbo_i2i": (
        "image/z_image_img2img",
        {
            "prompt": "prompt",
            "seed": "seed",
            "steps": "steps",
            "width": "width",
            "height": "height",
            "image_ref": "image",
        },
    ),
    "qwen_image": (
        "image/qwen_image_2512",
        {"prompt": "prompt", "seed": "seed", "width": "width", "height": "height"},
    ),
    "qwen_image_2512": (
        "image/qwen_image_2512",
        {"prompt": "prompt", "seed": "seed", "width": "width", "height": "height"},
    ),
    "qwen_image_edit": (
        "edit/qwen_image_edit",
        {"prompt": "prompt", "seed": "seed", "image_ref": "image"},
    ),
    "qwen_image_style": (
        "edit/qwen_image_edit",
        {"prompt": "prompt", "seed": "seed", "image_ref": "image"},
    ),
    "image_inpaint": (
        "edit/qwen_image_edit",
        {"prompt": "prompt", "seed": "seed", "image_ref": "image"},
    ),
    "annotated_image_edit": (
        "edit/qwen_image_edit",
        {"prompt": "prompt", "seed": "seed", "image_ref": "image"},
    ),
    "flux_klein_edit": (
        "edit/flux2_klein_4b_image_edit_distilled",
        {"prompt": "prompt", "seed": "seed", "image_ref": "image"},
    ),
    "image_upscale": (
        "image/basic_image_upscale",
        {"image_ref": "image"},
    ),
}


@dataclass(frozen=True, slots=True)
class ImageRequest:
    capability_id: str
    profile: str
    params: dict[str, Any]
    task_identity: str | None = None


@dataclass(frozen=True, slots=True)
class CompiledImageRequest:
    capability_id: str
    family: str
    model_id: str
    mode: str
    template_id: str
    template_module: str
    profile: ProfileConfig
    params: dict[str, Any]
    task_identity: str | None
    execution_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "capability_id": self.capability_id,
            "family": self.family,
            "model_id": self.model_id,
            "mode": self.mode,
            "template_id": self.template_id,
            "template_module": self.template_module,
            "profile": self.profile.name,
            "profile_digest": self.profile.digest,
            "params": self.params,
            "task_identity": self.task_identity,
            "execution_digest": self.execution_digest,
        }


@dataclass(frozen=True, slots=True)
class CanonicalWorkflowBinding:
    """Current production-engine ready-template binding envelope."""

    template_id: str
    capability_id: str
    model_id: str
    execution_digest: str
    bindings: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "astrid.vibecomfy.canonical-workflow.v1",
            "template_id": self.template_id,
            "capability_id": self.capability_id,
            "model_id": self.model_id,
            "execution_digest": self.execution_digest,
            "bindings": self.bindings,
        }


def _digest(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def normalize_capability_id(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ImageCompileError("capability_id is required")
    normalized = _ALIASES.get(value.strip(), value.strip())
    if normalized not in CANONICAL_CAPABILITIES:
        raise ImageCompileError(f"unsupported direct image capability: {value!r}")
    return normalized


def _safe_ref(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or not _SAFE_REF_RE.fullmatch(value.strip()):
        raise ImageCompileError(f"{field} must be a safe CAS/input reference")
    ref = value.strip()
    if ref.startswith("/") or ".." in ref.split("/"):
        raise ImageCompileError(f"{field} must not escape the attempt input root")
    return ref


def _normalize_params(spec: CapabilitySpec, raw: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise ImageCompileError("params must be an object")
    unknown = sorted(set(raw) - spec.allowed_fields)
    if unknown:
        raise ImageCompileError(f"unknown {spec.capability_id} fields: {', '.join(unknown)}")
    params: dict[str, Any] = {}
    for field in spec.required:
        if field not in raw or raw[field] is None or (isinstance(raw[field], str) and not raw[field].strip()):
            raise ImageCompileError(f"{spec.capability_id} requires {field}")
    for key, value in raw.items():
        if key in {"profile", "task_identity"} or value is None:
            continue
        if key in {"image_ref", "mask_ref"}:
            params[key] = _safe_ref(value, key)
        elif key == "prompt" or key == "negative_prompt":
            if not isinstance(value, str) or not value.strip():
                raise ImageCompileError(f"{key} must be a non-empty string")
            params[key] = value.strip()
        elif key in {"seed", "width", "height", "steps"}:
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ImageCompileError(f"{key} must be a non-negative integer")
            params[key] = value
        elif key in {"strength", "guidance_scale", "style_strength", "upscale_factor", "noise_scale"}:
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ImageCompileError(f"{key} must be numeric")
            params[key] = float(value)
        else:
            params[key] = value
    if "width" in params and params["width"] == 0 or "height" in params and params["height"] == 0:
        raise ImageCompileError("width and height must be positive")
    if "strength" in params and not 0 <= params["strength"] <= 1:
        raise ImageCompileError("strength must be between 0 and 1")
    if "style_strength" in params and not 0 <= params["style_strength"] <= 1:
        raise ImageCompileError("style_strength must be between 0 and 1")
    if "upscale_factor" in params and not 1 <= params["upscale_factor"] <= 10:
        raise ImageCompileError("upscale_factor must be between 1 and 10")
    if "noise_scale" in params and not 0 <= params["noise_scale"] <= 1:
        raise ImageCompileError("noise_scale must be between 0 and 1")
    return dict(sorted(params.items()))


def portable_execution_digest(compiled: CompiledImageRequest | Mapping[str, Any]) -> str:
    """Hash task meaning plus profile semantics, excluding host-local facts."""
    if isinstance(compiled, CompiledImageRequest):
        value = {
            "schema_version": SCHEMA_VERSION,
            "capability_id": compiled.capability_id,
            "family": compiled.family,
            "model_id": compiled.model_id,
            "mode": compiled.mode,
            "template_id": compiled.template_id,
            "template_module": compiled.template_module,
            "profile": compiled.profile.portable_dict(),
            "params": compiled.params,
        }
    else:
        raw = dict(compiled)
        profile_name = raw.get("profile")
        profile = PROFILE_CONFIGS.get(str(profile_name))
        if profile is None:
            raise ImageCompileError("portable digest requires a known profile")
        value = {
            "schema_version": raw.get("schema_version", SCHEMA_VERSION),
            "capability_id": raw.get("capability_id"),
            "family": raw.get("family"),
            "model_id": raw.get("model_id"),
            "mode": raw.get("mode"),
            "template_id": raw.get("template_id"),
            "template_module": raw.get("template_module"),
            "profile": profile.portable_dict(),
            "params": raw.get("params", {}),
        }
    return _digest(value)


def canonical_workflow_binding(
    compiled: CompiledImageRequest,
) -> CanonicalWorkflowBinding:
    """Convert one typed request to the reviewed ready-template envelope.

    Input references remain explicit object references until the production
    engine resolves its host-owned reference map. No local paths, ports, GPU
    facts, or fallback template selection enter this envelope.
    """

    if compiled.capability_id not in CANONICAL_CAPABILITIES:
        raise ImageCompileError("compiled capability is not in the canonical image catalog")
    template_id, field_map = _CANONICAL_BINDINGS[compiled.capability_id]
    unsupported = sorted(set(compiled.params) - set(field_map))
    if unsupported:
        raise ImageCompileError(
            f"{compiled.capability_id} cannot represent fields in current ready template: "
            + ", ".join(unsupported)
        )
    bindings: dict[str, Any] = {}
    for source_name, target_name in field_map.items():
        if source_name not in compiled.params:
            continue
        value = compiled.params[source_name]
        if source_name.endswith("_ref"):
            value = {"kind": "media", "object_id": value}
        bindings[target_name] = value
    return CanonicalWorkflowBinding(
        template_id=template_id,
        capability_id=compiled.capability_id,
        model_id=compiled.model_id,
        execution_digest=compiled.execution_digest,
        bindings=bindings,
    )


def compile_image_request(
    capability_id: str,
    params: Mapping[str, Any],
    *,
    profile: str,
    task_identity: str | None = None,
) -> CompiledImageRequest:
    """Compile one direct image request without selecting a fallback profile."""
    normalized_id = normalize_capability_id(capability_id)
    if profile not in PROFILE_CONFIGS:
        raise ImageCompileError(f"unsupported Vibe profile: {profile!r}")
    spec = CANONICAL_CAPABILITIES[normalized_id]
    normalized_params = _normalize_params(spec, params)
    if task_identity is not None and (not isinstance(task_identity, str) or not task_identity.strip()):
        raise ImageCompileError("task_identity must be a non-empty string when supplied")
    compiled = CompiledImageRequest(
        capability_id=normalized_id,
        family=spec.family,
        model_id=spec.model_id,
        mode=spec.mode,
        template_id=spec.template_id,
        template_module=spec.template_module,
        profile=PROFILE_CONFIGS[profile],
        params=normalized_params,
        task_identity=task_identity,
        execution_digest="",
    )
    return CompiledImageRequest(
        capability_id=compiled.capability_id,
        family=compiled.family,
        model_id=compiled.model_id,
        mode=compiled.mode,
        template_id=compiled.template_id,
        template_module=compiled.template_module,
        profile=compiled.profile,
        params=compiled.params,
        task_identity=compiled.task_identity,
        execution_digest=portable_execution_digest(compiled),
    )


__all__ = [
    "CANONICAL_CAPABILITIES",
    "CanonicalWorkflowBinding",
    "CompiledImageRequest",
    "ImageCompileError",
    "ImageRequest",
    "PROFILE_CONFIGS",
    "ProfileConfig",
    "SCHEMA_VERSION",
    "canonical_workflow_binding",
    "compile_image_request",
    "normalize_capability_id",
    "portable_execution_digest",
]
