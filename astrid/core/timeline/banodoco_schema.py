#!/usr/bin/env python3
"""Timeline schema mirroring the shared Banodoco TimelineConfig.

TimelineConfig / TimelineClip / ThemeOverrides / TimelineOutput / AssetEntry /
Theme are re-exported from `banodoco_timeline_schema` (the Python package in
the external banodoco-workspace repo's `packages/timeline-schema/python/` tree).
The role-aware Runtime JSON Schema vendored beside this module is the canonical
shape check. Everything else in this file (pool/arrangement/metadata/registry
types, transition validation, element registry checks) is Banodoco-only.
"""

# Validator exports intentionally remain below the optional schema import.
# ruff: noqa: E402, I001

from __future__ import annotations

import copy
import functools
import hashlib
import json
import os
import sys
from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from typing import Any, List, Literal, TypedDict, Union, cast

import jsonschema

from astrid.core.env_vars import ASTRID_TIMELINE_SCHEMA_PYTHONPATH

_schema_pythonpath = os.environ.get(ASTRID_TIMELINE_SCHEMA_PYTHONPATH, "").strip()
if _schema_pythonpath:
    _schema_root = Path(_schema_pythonpath).expanduser()
    if _schema_root.is_absolute():
        _schema_root = _schema_root.resolve()
        if str(_schema_root) not in sys.path:
            sys.path.insert(0, str(_schema_root))

try:
    from banodoco_timeline_schema import (
        AssetEntry as UpstreamSharedAssetEntry,
    )
    from banodoco_timeline_schema import (
        Theme as SharedTheme,
    )
    from banodoco_timeline_schema import (
        ThemeOverrides as SharedThemeOverrides,
    )
    from banodoco_timeline_schema import (
        TimelineClip as SharedTimelineClip,
    )
    from banodoco_timeline_schema import (
        TimelineConfig as SharedTimelineConfig,
    )
    from banodoco_timeline_schema import (
        TimelineOutput as SharedTimelineOutput,
    )
    from banodoco_timeline_schema import (
        materialize_output as _materialize_output,
    )
except ImportError:  # pragma: no cover
    # Neutralized fallback (plan-v5 B2): there is deliberately NO hand-written
    # schema mirror here. The old TypedDict stubs drifted and silently accepted
    # invalid timelines — the exact class of bug behind the save incident.
    # Without the contract package, typing degrades to plain dicts and
    # validation fails loudly with install instructions. Install with:
    #   python -m pip install -e /path/to/banodoco-workspace/packages/timeline-schema/python
    SharedTimelineOutput = dict  # type: ignore[assignment,misc]
    SharedTimelineClip = dict  # type: ignore[assignment,misc]
    SharedThemeOverrides = dict  # type: ignore[assignment,misc]
    SharedTheme = dict  # type: ignore[assignment,misc]
    SharedTimelineConfig = dict  # type: ignore[assignment,misc]
    UpstreamSharedAssetEntry = dict  # type: ignore[assignment,misc]

    def _materialize_output(config: Any, theme: Any) -> dict[str, Any]:
        canvas = theme.get("visual", {}).get("canvas", {}) if isinstance(theme, dict) else {}
        width = int(canvas.get("width", 1920)) if isinstance(canvas, dict) else 1920
        height = int(canvas.get("height", 1080)) if isinstance(canvas, dict) else 1080
        fps = float(canvas.get("fps", 30)) if isinstance(canvas, dict) else 30.0
        return {"resolution": f"{width}x{height}", "fps": fps, "file": "output.mp4"}

TimelineClip = SharedTimelineClip
TimelineConfig = SharedTimelineConfig
ThemeOverrides = SharedThemeOverrides
TimelineOutput = SharedTimelineOutput
Theme = SharedTheme

materialize_output = _materialize_output

ParameterType = Literal["number", "select", "boolean", "color", "audio-binding"]
# Model-level TrackKind; mirrors ``astrid.core.timeline.events.schema.types.TrackKind``
# and the built-in track catalog (catalog="track") in ``astrid.core.pack``.
# This definition is intentionally duplicated rather than imported from the
# event-schema module: keeping event-payload schemas decoupled from the
# Banodoco-schema implementation avoids import-time coupling between the two
# layers. Do not consolidate into a shared kinds module.
TrackKind = Literal["visual", "audio"]
TrackFit = Literal["cover", "contain", "manual"]
TrackBlendMode = Literal[
    "normal", "multiply", "screen", "overlay",
    "darken", "lighten", "soft-light", "hard-light",
]
BUILTIN_CLIP_TYPES = ("media", "hold", "text", "effect-layer")
ClipType = Literal["media", "hold", "text", "effect-layer"]
TextAlignment = Literal["left", "center", "right"]
AudioBindingSource = Literal["bass", "mid", "treble", "amplitude"]
AssetOrigin = Literal["immutable-public", "refreshable-from-generation", "opaque-foreign"]

class DerivedFrom(TypedDict, total=False):
    assetId: str
    content_sha256: str
    role: Literal["thumbnail", "proxy", "render-output"]

class SharedAssetEntry(TypedDict, total=False):
    file: str
    media_id: str
    url: str
    etag: str
    content_sha256: str
    url_expires_at: str
    type: str
    duration: float
    resolution: str
    fps: float
    origin: AssetOrigin
    derivedFrom: DerivedFrom
    generationId: str
    variantId: str
    thumbnailUrl: str

AssetEntry = SharedAssetEntry

class TimelineEffect(TypedDict, total=False):
    fade_in: float
    fade_out: float

class AnimationReferenceObject(TypedDict, total=False):
    id: str
    durationFrames: float
    easing: str
    params: dict[str, Any]

AnimationReference = Union[str, AnimationReferenceObject]
AnimationReferenceList = Union[AnimationReference, List[AnimationReference]]

class AudioBindingValue(TypedDict):
    source: AudioBindingSource
    min: float
    max: float

class ParameterOption(TypedDict):
    label: str
    value: str

class _ParameterDefinitionRequired(TypedDict):
    name: str
    label: str
    description: str
    type: ParameterType

class ParameterDefinition(_ParameterDefinitionRequired, total=False):
    default: Any
    min: float
    max: float
    step: float
    options: list[ParameterOption]

class _TrackDefinitionRequired(TypedDict):
    id: str
    kind: TrackKind
    label: str

class TrackDefinition(_TrackDefinitionRequired, total=False):
    scale: float
    fit: TrackFit
    opacity: float
    volume: float
    muted: bool
    blendMode: TrackBlendMode
    # Editor extension metadata is intentionally opaque to Astrid but must
    # round-trip through the shared timeline contract.
    app: dict[str, Any]

class ClipEntrance(TypedDict, total=False):
    type: str
    duration: float
    intensity: float
    params: dict[str, Any]

class ClipExit(TypedDict, total=False):
    type: str
    duration: float
    intensity: float
    params: dict[str, Any]

class ClipContinuous(TypedDict, total=False):
    type: str
    intensity: float
    params: dict[str, Any]

class ClipTransition(TypedDict):
    type: str
    duration: float

class ClipTransitionReference(TypedDict, total=False):
    id: str
    type: str
    duration: float
    durationFrames: float
    params: dict[str, Any]

class TextClipData(TypedDict, total=False):
    content: str
    fontFamily: str
    fontSize: float
    color: str
    align: TextAlignment
    bold: bool
    italic: bool

# TimelineClip / TimelineConfig / ThemeOverrides / TimelineOutput / AssetEntry
# come from banodoco_timeline_schema (re-exported above). PinnedShotGroup and
# AssetRegistry are Banodoco-only wrappers retained here.

AssetRegistryEntry = AssetEntry

class AssetRegistry(TypedDict):
    assets: dict[str, AssetRegistryEntry]

class ClipClassifiedKind(str, Enum):
    VIDEO = "video"
    IMAGE = "image"
    AUDIO = "audio"
    TEXT = "text"
    EFFECT = "effect"
    OPAQUE = "opaque"

PoolKind = Literal["source", "generative"]
PoolCategory = Literal["dialogue", "visual", "reaction", "applause", "music"]
PipelinePoolKind = Literal["dialogue", "visual", "reaction", "applause", "music", "text"]

class SourceIds(TypedDict, total=False):
    segment_ids: list[int]
    scene_id: str

class PoolScores(TypedDict, total=False):
    triage: float
    deep: float
    quotability: float

class _PoolEntryRequired(TypedDict):
    id: str
    kind: PoolKind
    category: PoolCategory
    duration: float
    scores: PoolScores
    excluded: bool

class PoolEntry(_PoolEntryRequired, total=False):
    asset: str
    src_start: float
    src_end: float
    source_ids: SourceIds
    effect_id: str
    param_schema: dict[str, Any]
    defaults: dict[str, Any]
    meta: dict[str, Any]
    excluded_reason: str | None
    text: str
    speaker: str | None
    quote_kind: str
    motion_tags: list[str]
    mood_tags: list[str]
    subject: str
    camera: str
    intensity: float
    event_label: str
    bed_kind: str
    energy: float

class Pool(TypedDict, total=False):
    version: int
    generated_at: str
    source_slug: str
    entries: list[PoolEntry]

class ArrangementTextOverlay(TypedDict, total=False):
    content: str
    style_preset: str

ArrangementVisualRole = Literal["primary", "overlay", "stinger"]

class ArrangementAudioSource(TypedDict):
    pool_id: str
    trim_sub_range: list[float]

class _ArrangementVisualSourceRequired(TypedDict):
    pool_id: str
    role: ArrangementVisualRole

class ArrangementVisualSource(_ArrangementVisualSourceRequired, total=False):
    params: dict[str, Any]

class _ArrangementClipRequired(TypedDict):
    uuid: str
    order: int
    audio_source: ArrangementAudioSource | None
    visual_source: ArrangementVisualSource
    rationale: str

class ArrangementClip(_ArrangementClipRequired, total=False):
    text_overlay: ArrangementTextOverlay | None

class Arrangement(TypedDict, total=False):
    version: int
    generated_at: str
    brief_text: str
    target_duration_sec: float
    source_slug: str
    brief_slug: str
    pool_sha256: str
    brief_sha256: str
    clips: list[ArrangementClip]

class PipelineMetadataClipEntry(TypedDict, total=False):
    source_uuid: str
    caption_kind: Literal["dialogue", "visual"]
    picked_by: str
    pick_rationale: str
    pool_id: str | None
    pool_kind: PipelinePoolKind
    source_ids: SourceIds
    source_scene_id: str
    source_transcript_text: str | None
    arrangement_notes: str | None
    text_overlay_content: str
    score: float

class PipelineMetadata(TypedDict):
    version: int
    generated_at: str
    pipeline: dict[str, Any]
    clips: dict[str, PipelineMetadataClipEntry]
    sources: dict[str, dict[str, Any]]

# `from` is a Python keyword, so TimelineClip stores it as `from_` in memory and
# swaps to/from `"from"` at the JSON boundary. Every other field is 1:1 with TS.
_FROM_ALIAS = ("from_", "from")
_TIMELINE_TOP_ALLOWED = frozenset(
    {"theme", "theme_overrides", "generation_defaults", "clips", "tracks", "pinnedShotGroups", "app", "output"}
)
_TIMELINE_CONTAINER_REQUIRED = frozenset({"clips", "tracks"})
_LEGACY_CONTAINER_KEYS = frozenset({"schema_version", "assembly", "pool", "arrangement"})
_THEME_OVERRIDES_ALLOWED = frozenset({"visual", "generation", "voice", "audio", "pacing"})
_CLIP_ALLOWED = frozenset(
    {
        "id", "at", "track", "clipType", "label", "asset", "from", "to", "speed", "hold",
        "volume", "x", "y", "width", "height", "cropTop", "cropBottom",
        "cropLeft", "cropRight", "opacity", "params", "text", "entrance", "exit",
        "continuous", "transition", "effects", "source_uuid", "generation",
        "pool_id", "clip_order", "app", "label", "keyframes",
        # Revision-pinned Astrid element identity. This is editor-owned
        # metadata projected to the Remotion fields during managed render.
        "elementRef",
        # Immutable render-admission provenance retained when shot composites
        # are flattened into media/image/text clips.  These fields are not
        # authoring hints: admission stamps them from the registered shot.
        "shot_id", "shot_occurrence_id", "shot_name",
    }
)
_TRACK_ALLOWED = frozenset(
    {"id", "kind", "label", "scale", "fit", "opacity", "volume", "muted", "blendMode", "app", "role"}
)
_ASSET_ENTRY_ALLOWED = frozenset(
    {
        "file",
        "media_id",
        "url",
        "etag",
        "content_sha256",
        "url_expires_at",
        "type",
        "duration",
        "resolution",
        "fps",
        "origin",
        "derivedFrom",
        "generationId",
        "variantId",
        "thumbnailUrl",
    }
)
METADATA_VERSION = 1
POOL_VERSION = 1
ARRANGEMENT_VERSION = 1
# Fields here survive ffprobe cache hits. Run-specific fields (*_ref) must NOT be listed.
CARRY_FORWARD_SOURCE_FIELDS: frozenset[str] = frozenset({"codec"})
_POOL_ENTRY_ALLOWED = frozenset(
    {
        "id",
        "kind",
        "category",
        "asset",
        "src_start",
        "src_end",
        "duration",
        "source_ids",
        "scores",
        "excluded",
        "excluded_reason",
        "effect_id",
        "param_schema",
        "defaults",
        "meta",
        "text",
        "speaker",
        "quote_kind",
        "motion_tags",
        "mood_tags",
        "subject",
        "camera",
        "intensity",
        "event_label",
        "bed_kind",
        "energy",
    }
)
# Astrid's editor keeps these on registry entries (swap-asset provenance, the
# DAMAGED-ART pixel measurement). They are editor metadata, not wire contract:
# render paths drop them before the strict validator, as _known_timeline_payload
# does for clip and track metadata. Retired locators (file/url/path) are not here.
EDITOR_ASSET_METADATA_KEYS = frozenset({"handle", "filename", "pixel_provenance", "source", "size", "size_bytes"})
_POOL_ALLOWED = frozenset({"version", "generated_at", "source_slug", "entries"})
_SOURCE_IDS_ALLOWED = frozenset({"segment_ids", "scene_id"})
_POOL_SCORES_ALLOWED = frozenset({"triage", "deep", "quotability"})
_ARRANGEMENT_ALLOWED = frozenset(
    {
        "version",
        "generated_at",
        "brief_text",
        "target_duration_sec",
        "source_slug",
        "brief_slug",
        "pool_sha256",
        "brief_sha256",
        "clips",
    }
)
_ARRANGEMENT_CLIP_ALLOWED = frozenset({"uuid", "order", "audio_source", "visual_source", "text_overlay", "rationale"})
_ARRANGEMENT_AUDIO_SOURCE_ALLOWED = frozenset({"pool_id", "trim_sub_range"})
_ARRANGEMENT_VISUAL_SOURCE_ALLOWED = frozenset({"pool_id", "role", "params"})
_ARRANGEMENT_TEXT_OVERLAY_ALLOWED = frozenset({"content", "style_preset"})
_FORBIDDEN_ARRANGEMENT_TIME_KEYS = frozenset({"src_start", "src_end", "duration", "from", "to", "at", "start", "end", "time"})

def _raise_unknown_keys(path: str, payload: dict[str, Any], allowed: frozenset[str]) -> None:
    for key in payload:
        if key not in allowed:
            raise ValueError(f"{path} has unknown key {key!r}")

def _normalize_clip_for_validation(clip: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(clip)
    if "from_" in normalized and "from" not in normalized:
        normalized["from"] = normalized.pop("from_")
    return normalized

def _known_timeline_payload(config: Mapping[str, Any]) -> dict[str, Any]:
    """Build the shared-schema view without editor-owned opaque metadata.

    The canonical Banodoco schema intentionally has no ``app`` extension
    fields, while editors persist opaque application metadata at
    the timeline, clip, and track levels.  Astrid must retain those fields in
    the returned timeline, but must not ask the upstream validator to accept
    them as part of its stricter wire contract.  Keep this projection local
    to validation; never mutate the caller's config or strip the metadata from
    the persisted representation.
    """
    known = {
        key: value for key, value in config.items() if key in _TIMELINE_TOP_ALLOWED
    }
    known.pop("app", None)
    render_provenance_fields = {"shot_id", "shot_occurrence_id", "shot_name", "elementRef"}
    for collection in ("clips", "tracks"):
        entries = known.get(collection)
        if not isinstance(entries, list):
            continue
        known[collection] = [
            (
                {
                    key: value for key, value in entry.items()
                    if key != "app" and not (
                        collection == "clips" and key in render_provenance_fields
                    )
                }
                if isinstance(entry, Mapping)
                else entry
            )
            for entry in entries
        ]
    return known

def _json_safe_copy(value: Any) -> Any:
    """Return a deep JSON-compatible copy using the persisted serialization contract."""
    return json.loads(json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False))


_VENDORED_SCHEMA = Path(__file__).with_name("schema") / "timeline-config.schema.json"


@functools.lru_cache(maxsize=1)
def load_vendored_timeline_schema() -> dict[str, Any]:
    """Load the TimelineConfig JSON Schema vendored from the Runtime contract.

    The pinned ``banodoco_timeline_schema`` package does not define
    ``tracks[].role``. The Runtime's role-aware ``contract/schemas`` artifact
    is copied here by ``scripts/vendor_lock.py update`` (see
    ``runtime.lock.json``) and is the validation source.
    """
    return cast(dict[str, Any], json.loads(_VENDORED_SCHEMA.read_text(encoding="utf-8")))


@functools.lru_cache(maxsize=1)
def _shared_timeline_validator() -> jsonschema.protocols.Validator:
    """Compile the shared TimelineConfig JSON-Schema validator once per process.

    ``jsonschema.validate`` re-runs ``check_schema`` (a full walk of the
    schema's ``$ref`` tree) and rebuilds the validator on every call — the
    dominant per-save cost in the HTTP save path (6–8 full-config validations
    per save).  Compiling once keeps the exact draft-07 semantics
    (``validator_for`` selection + ``best_match`` error raising) with no
    per-call schema re-walk.
    """
    schema = load_vendored_timeline_schema()
    validator_cls = jsonschema.validators.validator_for(schema)
    validator_cls.check_schema(schema)
    return validator_cls(schema)


def _validate_shared_timeline(config: Any) -> None:
    """Validate against the shared JSON Schema with the compiled validator.

    Mirrors ``jsonschema.validate(config, schema)`` exactly (``validator_for``
    selection, ``check_schema`` at compile time, ``best_match`` error raising)
    but skips the per-call ``check_schema`` + validator construction that
    ``jsonschema.validate`` performs internally.  The shared validator is
    strict-independent in the shared TimelineConfig contract, so ``strict`` is
    intentionally not threaded through here.
    """
    validator = _shared_timeline_validator()
    error = jsonschema.exceptions.best_match(validator.iter_errors(config))
    if error is not None:
        raise error


def canonical_empty_timeline() -> TimelineConfig:
    """Return the canonical empty raw TimelineConfig runtime container."""
    config = {"tracks": [], "clips": []}
    return validate_timeline_config_for_container(config)


def validate_timeline_config_for_container(config: Any) -> TimelineConfig:
    """Validate and return a JSON-safe copy of a raw TimelineConfig container.

    Runtime timeline containers are the raw renderable TimelineConfig shape.  This
    stricter surface rejects legacy wrapper/read-model keys that the looser render
    validator intentionally ignores for compatibility with old artifacts.
    """
    if not isinstance(config, Mapping):
        raise ValueError("TimelineConfig container must be a JSON object")
    data = copy.deepcopy(dict(config))
    legacy_keys = sorted(key for key in data if key in _LEGACY_CONTAINER_KEYS)
    if legacy_keys:
        raise ValueError(
            "TimelineConfig container must be raw; legacy wrapper/read-model keys "
            f"are not allowed: {legacy_keys}"
        )
    _raise_unknown_keys("TimelineConfig container", data, _TIMELINE_TOP_ALLOWED)
    missing = sorted(key for key in _TIMELINE_CONTAINER_REQUIRED if key not in data)
    if missing:
        raise ValueError(f"TimelineConfig container missing required key(s): {missing}")
    validate_timeline(data)
    return cast(TimelineConfig, _json_safe_copy(data))

def canonical_timeline_config(config: Any) -> TimelineConfig:
    """Return validated TimelineConfig data in a stable JSON-object order."""
    data = validate_timeline_config_for_container(config)
    return cast(TimelineConfig, _json_safe_copy(data))

def timeline_config_digest(config: Any) -> str:
    """Return a stable sha256 digest for a validated TimelineConfig container."""
    data = canonical_timeline_config(config)
    encoded = json.dumps(data, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()

def timeline_configs_equal(left: Any, right: Any) -> bool:
    """Compare two TimelineConfig containers after validation and canonicalization."""
    return canonical_timeline_config(left) == canonical_timeline_config(right)

# =========================================================================
# Re-exports — validators extracted to astrid.core.timeline.validators.*
# All public names remain importable from this module unchanged.
# =========================================================================
from astrid.core.timeline.validators.arrangement import (
    ArrangementDurationError as ArrangementDurationError,
    _reject_forbidden_arrangement_time_keys as _reject_forbidden_arrangement_time_keys,
    is_all_generative_arrangement as is_all_generative_arrangement,
    validate_arrangement as validate_arrangement,
    validate_arrangement_duration_window as validate_arrangement_duration_window,
)
from astrid.core.timeline.validators.metadata import (
    _validate_generated_at as _validate_generated_at,
    validate_metadata as validate_metadata,
)
from astrid.core.timeline.validators.pool import (
    _validate_pool_scores as _validate_pool_scores,
    _validate_source_ids as _validate_source_ids,
    validate_pool as validate_pool,
)
from astrid.core.timeline.validators.registry import (
    _animation_ids as _animation_ids,
    _animation_meta as _animation_meta,
    _transition_ids as _transition_ids,
    validate_registry as validate_registry,
)
from astrid.core.timeline.validators.timeline import (
    _clip_duration_seconds as _clip_duration_seconds,
    _schema_params_for_animation_refs as _schema_params_for_animation_refs,
    _timeline_fps as _timeline_fps,
    _transition_reference as _transition_reference,
    _validate_animation_reference as _validate_animation_reference,
    _validate_animation_reference_list as _validate_animation_reference_list,
    _validate_clip_transitions as _validate_clip_transitions,
    _validate_effect_params as _validate_effect_params,
    validate_timeline as validate_timeline,
)

for _exported_name in (
    "ArrangementDurationError",
    "validate_timeline",
):
    globals()[_exported_name].__module__ = __name__
del _exported_name
