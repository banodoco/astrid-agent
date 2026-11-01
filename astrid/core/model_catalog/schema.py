"""Model registry schema — frozen dataclasses and validation.

schema_version: 2 — model → mode → backend taxonomy.

Each model entry declares a ``modes`` map whose keys are canonical mode
names (``t2i``, ``i2i``, ``edit``, ``inpaint``, ``outpaint``, ``upscale``
for the image modality).  Each :class:`ModeSpec` carries per-mode
``supports`` / ``requires`` and a ``backends`` dict of
:class:`BackendSpec` keyed by ``"local"`` and/or ``"cloud"``.
"""

from __future__ import annotations

import math
from collections.abc import Collection
from dataclasses import dataclass, field
from typing import Any

from astrid.core.model_catalog.taxonomy import (
    AUDIO_MODALITY,
    CLOUD_BACKEND_ID,
    GENERATION_TAXONOMY,
    IMAGE_MODALITY,
    LOCAL_BACKEND_ID,
    VIDEO_MODALITY,
    Feature,
    GenerationTaxonomyRegistry,
)
from astrid.core.model_catalog.taxonomy import (
    CANONICAL_IMAGE_MODES as CANONICAL_IMAGE_MODES,
)
from astrid.core.model_catalog.taxonomy import (
    CANONICAL_VIDEO_MODES as CANONICAL_VIDEO_MODES,
)

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Price:
    """Validated per-output price metadata for a backend endpoint."""

    usd: float
    unit: str


@dataclass(frozen=True)
class BackendSpec:
    """Specification for a single execution backend (local or cloud).

    Kept unchanged from v1 — same shape, now nested under (mode, backend)
    rather than hanging directly off the model entry.
    """

    #: For local backends: vibecomfy ready-template identifier
    #: (e.g. ``"image/z_image"``).
    template: str = ""

    #: SHA-256 hash of the ready-template source file (local only).
    template_hash: str = ""

    #: Falcon endpoint slug for cloud backends
    #: (e.g. ``"fal-ai/flux/dev"``).
    endpoint: str = ""

    #: Falcon endpoint slug for LoRA routing (cloud only).
    #: When set and LoRAs are requested, this overrides ``endpoint``
    #: (e.g. ``"fal-ai/flux-lora"``).
    lora_endpoint: str | None = None

    #: Mapping from canonical feature names to backend-specific parameter
    #: names.  Every key in this map MUST correspond to a feature declared
    #: in the parent ``ModeSpec.supports`` list.
    #:
    #: Example (cloud): ``{"prompt": "prompt", "seed": "seed", "size":
    #: "image_size"}``.
    param_map: dict[str, str] = field(default_factory=dict)

    #: Optional validated unit price for this backend endpoint.
    price: Price | None = None

    #: Backend-specific hints that the adapter should apply to every
    #: request payload (e.g. ``{"guidance_scale_override": 1.0}``).
    #: Hints are model×mode×backend configuration, not per-request
    #: caller parameters.
    hints: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ModeSpec:
    """Per-mode capabilities and backend wiring for one model.

    A mode represents a specific generation capability (e.g. ``t2i``,
    ``i2i``, ``edit``).  Features live on the mode, not on the
    model — the same model can expose different feature sets across its
    modes.
    """

    #: Features this mode supports on *at least one* backend.
    #: Every value must be a valid ``Feature`` literal.
    supports: tuple[Feature, ...] = ()

    #: Features the caller MUST provide for this mode.
    #: Missing a required feature causes a hard-fail at request-validation
    #: time (before the generation loop).
    #: Must be a subset of ``supports``.
    requires: tuple[Feature, ...] = ()

    #: Backend specifications keyed by execution target.
    #: Keys are ``"local"`` and/or ``"cloud"``.
    #: A mode can be local-only, cloud-only, or both.
    #: Absence of a key means that backend is not available for this mode.
    backends: dict[str, BackendSpec] = field(default_factory=dict)


@dataclass(frozen=True)
class ModelEntry:
    """A registered generation model with per-mode backend wiring.

    Each entry maps to exactly one real-world model checkpoint or vendor
    offering.  The ``modes`` dict keys are canonical mode names;
    each value is a :class:`ModeSpec` declaring what that mode supports,
    requires, and which backends are available.
    """

    #: Unique model identifier (e.g. ``"z-image"``).
    id: str

    #: Modality this model belongs to (``"image"``, ``"video"``,
    #: ``"audio"``).
    modality: str

    #: Per-mode capability declarations.  Keys are canonical mode names
    #: (``"t2i"``, ``"i2i"``, ``"edit"``, etc.).  Each value is a
    #: :class:`ModeSpec`.
    modes: dict[str, ModeSpec] = field(default_factory=dict)

    #: When ``True``, this is a closed-weight model (e.g. Recraft,
    #: Ideogram).  Closed entries are retained in the registry for backend
    #: selection and policy checks.
    #: ``None`` is treated as falsy / open-weight.
    closed: bool | None = None


# ---------------------------------------------------------------------------
# Registry validation
# ---------------------------------------------------------------------------


def validate_registry(raw: dict[str, Any]) -> list[ModelEntry]:
    """Validate a raw registry dict and return a list of :class:`ModelEntry`.

    Delegates to :func:`validate_registry_with_backends` with default
    backend-id resolution.
    """
    return validate_registry_with_backends(raw)


def validate_registry_with_backends(
    raw: dict[str, Any],
    *,
    allowed_backend_ids: Collection[str] | None = None,
    taxonomy_registry: GenerationTaxonomyRegistry | None = None,
) -> list[ModelEntry]:
    """Validate a raw registry dict against the currently known backend ids."""
    if not isinstance(raw, dict):
        raise ValueError(f"registry must be a dict, got {type(raw).__name__}")

    registry = taxonomy_registry or GenerationTaxonomyRegistry()
    backend_ids = frozenset(allowed_backend_ids or registry.backend_ids())
    if not backend_ids:
        raise ValueError("allowed backend ids must not be empty")

    schema_version = raw.get("schema_version")

    # ── schema_version: reject v1, require v2 ──────────────────────────
    if schema_version != 2:
        if schema_version == 1:
            raise ValueError(
                "Schema version 1 is no longer supported.  "
                "The flat model-entry schema was replaced with a "
                "model → mode → backend taxonomy (schema_version: 2).  "
                "Please rewrite your model entries in the v2 shape: "
                "each model declares a 'modes' dict whose values carry "
                "per-mode 'supports', 'requires', and 'backends' with "
                "'local'/'cloud' sub-entries.  See "
                "docs/generation/10-registry-schema.md for the "
                "full v2 specification."
            )
        raise ValueError(
            f"unsupported registry schema_version: {schema_version!r} "
            f"(expected 2)"
        )

    models_raw = raw.get("models")
    if not isinstance(models_raw, list):
        raise ValueError(
            f"'models' must be a list, got {type(models_raw).__name__}"
        )

    entries: list[ModelEntry] = []
    seen_ids: set[str] = set()

    for idx, m in enumerate(models_raw):
        prefix = f"models[{idx}]"
        if not isinstance(m, dict):
            raise ValueError(f"{prefix}: must be a dict, got {type(m).__name__}")

        model_id = _require_str(m, "id", prefix)

        # -- duplicate check -------------------------------------------------
        if model_id in seen_ids:
            raise ValueError(f"{prefix}: duplicate model id {model_id!r}")
        seen_ids.add(model_id)

        modality = _require_str(m, "modality", prefix)

        # -- closed flag (optional) ------------------------------------------
        closed = m.get("closed")
        if closed is not None and not isinstance(closed, bool):
            raise ValueError(
                f"{prefix}.closed: must be a boolean or null, "
                f"got {type(closed).__name__}"
            )

        # -- modes -----------------------------------------------------------
        modes_raw = m.get("modes")
        if not isinstance(modes_raw, dict):
            raise ValueError(
                f"{prefix}.modes: must be a dict, got {type(modes_raw).__name__}"
            )
        if not modes_raw:
            raise ValueError(f"{prefix}.modes: at least one mode is required")

        modes: dict[str, ModeSpec] = {}
        for mode_name, mode_raw in modes_raw.items():
            mode_prefix = f"{prefix}.modes[{mode_name!r}]"
            modes[mode_name] = _validate_mode_spec(
                mode_raw,
                mode_name,
                mode_prefix,
                modality,
                allowed_backend_ids=backend_ids,
                taxonomy_registry=registry,
            )

        entries.append(
            ModelEntry(
                id=model_id,
                modality=modality,
                modes=modes,
                closed=closed,
            )
        )

    return entries


# ---------------------------------------------------------------------------
# Internal: mode-level validation
# ---------------------------------------------------------------------------


def _validate_mode_spec(
    raw: Any,
    mode_name: str,
    prefix: str,
    modality: str,
    *,
    allowed_backend_ids: Collection[str],
    taxonomy_registry: GenerationTaxonomyRegistry,
) -> ModeSpec:
    """Validate a single mode entry and return a :class:`ModeSpec`."""

    if not isinstance(raw, dict):
        raise ValueError(f"{prefix}: must be a dict, got {type(raw).__name__}")

    # -- validate mode name is canonical (modality-dispatch) --------------
    if modality not in {IMAGE_MODALITY, VIDEO_MODALITY, AUDIO_MODALITY}:
        raise ValueError(
            f"{prefix}: unknown modality {modality!r}; "
            f"expected 'image', 'video', or 'audio'"
        )
    try:
        taxonomy_registry.require_mode(modality, mode_name, path=prefix)
    except ValueError as exc:
        message = str(exc)
        if message.startswith(f"{prefix}:"):
            raise
        raise ValueError(f"{prefix}: {message}") from exc

    # -- supports --------------------------------------------------------
    supports_raw = raw.get("supports", [])
    if not isinstance(supports_raw, list):
        raise ValueError(f"{prefix}.supports: must be a list")
    supports: tuple[Feature, ...] = tuple(
        _require_feature(s, f"{prefix}.supports[{i}]", taxonomy_registry=taxonomy_registry)
        for i, s in enumerate(supports_raw)
    )

    # -- requires --------------------------------------------------------
    requires_raw = raw.get("requires", [])
    if not isinstance(requires_raw, list):
        raise ValueError(f"{prefix}.requires: must be a list")
    requires: tuple[Feature, ...] = tuple(
        _require_feature(r, f"{prefix}.requires[{i}]", taxonomy_registry=taxonomy_registry)
        for i, r in enumerate(requires_raw)
    )

    # requires ⊆ supports
    for req in requires:
        if req not in supports:
            raise ValueError(
                f"{prefix}: 'requires' feature {req!r} not in 'supports'"
            )

    # -- backends --------------------------------------------------------
    backends_raw = raw.get("backends")
    if not isinstance(backends_raw, dict):
        raise ValueError(
            f"{prefix}.backends: must be a dict, got {type(backends_raw).__name__}"
        )
    if not backends_raw:
        raise ValueError(
            f"{prefix}.backends: at least one backend (local or cloud) is required"
        )

    backends: dict[str, BackendSpec] = {}
    for backend_key, backend_raw in backends_raw.items():
        if backend_key not in allowed_backend_ids:
            available = ", ".join(sorted(allowed_backend_ids))
            raise ValueError(
                f"{prefix}.backends[{backend_key!r}]: "
                f"unknown backend key; available backend ids: {available}"
            )
        backends[backend_key] = _validate_backend_spec(
            backend_raw,
            f"{prefix}.backends[{backend_key!r}]",
            backend_id=backend_key,
            supports=supports,
            taxonomy_registry=taxonomy_registry,
        )

    return ModeSpec(
        supports=supports,
        requires=requires,
        backends=backends,
    )


def _validate_backend_spec(
    raw: Any,
    path: str,
    *,
    backend_id: str,
    supports: tuple[Feature, ...],
    taxonomy_registry: GenerationTaxonomyRegistry,
) -> BackendSpec:
    """Validate a single backend specification, cross-checking param_map
    against the mode's *supports*."""

    if not isinstance(raw, dict):
        raise ValueError(f"{path}: must be a dict, got {type(raw).__name__}")

    template = raw.get("template", "")
    template_hash = raw.get("template_hash", "")
    endpoint = raw.get("endpoint", "")

    if backend_id == LOCAL_BACKEND_ID and not template:
        raise ValueError(f"{path}.template: must be a non-empty string for local backend")
    if backend_id == CLOUD_BACKEND_ID and not endpoint:
        raise ValueError(f"{path}.endpoint: must be a non-empty string for cloud backend")

    price = _validate_backend_price(raw.get("price"), f"{path}.price")

    # -- param_map -------------------------------------------------------
    param_map_raw = raw.get("param_map", {})
    if not isinstance(param_map_raw, dict):
        raise ValueError(f"{path}.param_map: must be a dict")

    param_map: dict[str, str] = {}
    for k, v in param_map_raw.items():
        if not isinstance(v, str) or not v.strip():
            raise ValueError(
                f"{path}.param_map[{k!r}]: value must be a non-empty string"
            )
        key = k.strip()
        val = v.strip()

        # Every param_map key must be a valid Feature
        _require_feature(
            key,
            f"{path}.param_map[{k!r}]",
            taxonomy_registry=taxonomy_registry,
        )

        # Every param_map key must be in the mode's supports
        if key not in supports:
            raise ValueError(
                f"{path}.param_map[{k!r}]: feature {key!r} is not in "
                f"the mode's 'supports' list"
            )

        param_map[key] = val

    # Cross-check: every feature in supports that has an entry in
    # param_map on this backend must have a non-empty value (already
    # enforced above).  Features in supports that are NOT in param_map
    # are allowed — they may be mapped on a different backend.
    lora_endpoint = raw.get("lora_endpoint")
    if lora_endpoint is not None:
        if not isinstance(lora_endpoint, str) or not lora_endpoint.strip():
            raise ValueError(
                f"{path}.lora_endpoint: must be a non-empty string or null, "
                f"got {type(lora_endpoint).__name__}"
            )
        lora_endpoint = lora_endpoint.strip()

    # -- hints -----------------------------------------------------------
    hints_raw = raw.get("hints", {})
    if not isinstance(hints_raw, dict):
        raise ValueError(f"{path}.hints: must be a dict")
    hints: dict[str, Any] = {}
    for k, v in hints_raw.items():
        if not isinstance(k, str) or not k.strip():
            raise ValueError(f"{path}.hints: keys must be non-empty strings")
        hints[k.strip()] = v

    return BackendSpec(
        template=str(template),
        template_hash=str(template_hash),
        endpoint=str(endpoint),
        lora_endpoint=lora_endpoint,
        param_map=param_map,
        price=price,
        hints=hints,
    )


_ALLOWED_PRICE_UNITS = frozenset({"image", "output", "video", "audio", "second"})


def _validate_backend_price(raw: Any, path: str) -> Price | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: must be a dict, got {type(raw).__name__}")
    expected_keys = {"unit", "usd"}
    actual_keys = set(raw)
    unknown = sorted(actual_keys - expected_keys)
    missing = sorted(expected_keys - actual_keys)
    if unknown or missing:
        problems: list[str] = []
        if missing:
            problems.append(f"missing keys: {', '.join(missing)}")
        if unknown:
            problems.append(f"unknown keys: {', '.join(unknown)}")
        raise ValueError(f"{path}: price objects must contain exactly 'unit' and 'usd' ({'; '.join(problems)})")
    unit = _require_str(raw, "unit", path)
    if unit not in _ALLOWED_PRICE_UNITS:
        allowed = ", ".join(sorted(_ALLOWED_PRICE_UNITS))
        raise ValueError(f"{path}.unit: unsupported price unit {unit!r}; expected one of: {allowed}")
    usd = raw.get("usd")
    if isinstance(usd, bool) or not isinstance(usd, (int, float)):
        raise ValueError(f"{path}.usd: must be a non-negative number, got {type(usd).__name__}")
    usd_value = float(usd)
    if not math.isfinite(usd_value) or usd_value < 0:
        raise ValueError(f"{path}.usd: must be a non-negative finite number")
    return Price(usd=usd_value, unit=unit)


# ---------------------------------------------------------------------------
# Internal helpers (shared with v1, preserved)
# ---------------------------------------------------------------------------


def _require_str(data: dict[str, Any], key: str, path: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}.{key}: must be a non-empty string")
    return value.strip()


def _require_feature(
    value: Any,
    path: str,
    *,
    taxonomy_registry: GenerationTaxonomyRegistry = GENERATION_TAXONOMY,
) -> Feature:
    """Validate that *value* is a recognised ``Feature`` literal."""
    return taxonomy_registry.require_feature(value, path=path)


# ---------------------------------------------------------------------------
# LoRA registry schema
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LoraSource:
    """Source information for a LoRA adapter."""

    #: HuggingFace repository (e.g. ``"XLabs-AI/flux-RealismLora"``).
    repo: str = ""

    #: Filename within the repository (e.g. ``"lora.safetensors"``).
    file: str = ""

    #: Direct download URL for the LoRA weights.
    url: str = ""


@dataclass(frozen=True)
class LoraEntry:
    """A registered LoRA adapter entry."""

    #: Unique LoRA identifier (e.g. ``"flux-realism"``).
    id: str

    #: Human-readable name (e.g. ``"FLUX Realism"``).
    name: str = ""

    #: Model ID this LoRA is designed for (e.g. ``"flux-dev"``).
    base_model: str = ""

    #: Intent category (e.g. ``"realism"``, ``"style"``).
    intent: str = ""

    #: Source information for the LoRA weights.
    source: LoraSource = field(default_factory=LoraSource)

    #: Default scale/strength to apply (0.0–1.0).
    default_scale: float = 1.0

    #: Whether this LoRA has been visually verified.
    verified: bool = False

    #: Free-form notes about this LoRA.
    notes: str = ""


def validate_lora_registry(
    raw: dict[str, Any],
    *,
    model_ids: frozenset[str] | None = None,
) -> list[LoraEntry]:
    """Validate a raw LoRA registry dict and return a list of :class:`LoraEntry`.

    The *raw* dict must have the top-level shape::

        {
            "schema_version": 1,
            "loras": [ { ... }, ... ]
        }

    Raises:
        ValueError: If any validation rule is violated.
    """
    if not isinstance(raw, dict):
        raise ValueError(f"lora registry must be a dict, got {type(raw).__name__}")

    schema_version = raw.get("schema_version")
    if schema_version != 1:
        raise ValueError(
            f"unsupported lora registry schema_version: {schema_version!r} (expected 1)"
        )

    loras_raw = raw.get("loras")
    if not isinstance(loras_raw, list):
        raise ValueError(
            f"'loras' must be a list, got {type(loras_raw).__name__}"
        )
    if not loras_raw:
        raise ValueError("'loras' must not be empty")

    entries: list[LoraEntry] = []
    seen_ids: set[str] = set()
    model_id_set: frozenset[str] = model_ids or frozenset()

    for idx, item in enumerate(loras_raw):
        prefix = f"loras[{idx}]"
        if not isinstance(item, dict):
            raise ValueError(f"{prefix}: must be a dict, got {type(item).__name__}")

        lora_id = _require_str(item, "id", prefix)

        # -- duplicate check -------------------------------------------------
        if lora_id in seen_ids:
            raise ValueError(f"{prefix}: duplicate lora id {lora_id!r}")
        seen_ids.add(lora_id)

        name = item.get("name", "")
        base_model = _require_str(item, "base_model", prefix)
        intent = item.get("intent", "")

        # -- base_model cross-check ------------------------------------------
        if model_id_set and base_model not in model_id_set:
            raise ValueError(
                f"{prefix}: base_model {base_model!r} not found in model registry"
            )

        # -- source -----------------------------------------------------------
        source_raw = item.get("source")
        if not isinstance(source_raw, dict):
            raise ValueError(
                f"{prefix}.source: must be a dict, got {type(source_raw).__name__}"
            )
        source = LoraSource(
            repo=source_raw.get("repo", ""),
            file=source_raw.get("file", ""),
            url=source_raw.get("url", ""),
        )

        default_scale = item.get("default_scale", 1.0)
        if not isinstance(default_scale, (int, float)):
            raise ValueError(
                f"{prefix}.default_scale: must be a number, "
                f"got {type(default_scale).__name__}"
            )

        verified = item.get("verified", False)
        if not isinstance(verified, bool):
            raise ValueError(
                f"{prefix}.verified: must be a boolean, "
                f"got {type(verified).__name__}"
            )

        notes = item.get("notes", "")
        if not isinstance(notes, str):
            raise ValueError(
                f"{prefix}.notes: must be a string, got {type(notes).__name__}"
            )

        entries.append(
            LoraEntry(
                id=lora_id,
                name=str(name),
                base_model=base_model,
                intent=str(intent),
                source=source,
                default_scale=float(default_scale),
                verified=verified,
                notes=str(notes),
            )
        )

    return entries
