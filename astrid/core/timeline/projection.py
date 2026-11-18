"""Canonical event-to-assembly projector (backend-agnostic).

This module defines the **sole** applicator table and two public surfaces
for projecting a sequence of ``TimelineEvent`` objects into an in-memory
timeline configuration:

* ``apply_event_to_assembly(state: dict, event: TimelineEvent) -> dict``
  – single-event fold (returns a *new* dict).

* ``project_to_assembly(events: Sequence[TimelineEvent], *, initial_assembly=None) -> dict``
  – full replay over an ordered event sequence.

The projector is **pure**: no filesystem reads, no current time, no random
IDs, no network, no mutation of input events.

Lifecycle events
----------------

* ``timeline.created``, ``timeline.renamed``, ``timeline.default_set``,
  ``timeline.tombstoned``, ``timeline.deleted`` are **intentional no-ops**
  for the assembly projection.
Dispatch conventions
--------------------

The dispatch table maps event kinds to ``(ensure_keys, dispatch_fn, applicator)``:

* ``ensure_keys`` – list of assembly keys that must exist before applying.
* ``dispatch_fn`` – one of the ``_dispatch_*`` helpers that extracts the
  correct scope (clips-only, clips+assembly, assembly-only) and calls the
  applicator.
* ``applicator`` – mutates the scope in-place.

Applicators are **private** to this module.  External code must only use
the two public surfaces.  When ``project_to_assembly`` is called, each
applicator runs against a fresh copy of the state, so input events are
never mutated.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Literal, Sequence

from astrid.core.contracts.errors import AstridError
from astrid.core.timeline.banodoco_schema import (
    canonical_empty_timeline,
    canonical_timeline_config,
    validate_timeline_config_for_container,
)
from astrid.core.timeline.kinds import normalize_track_kind

from .events.schema import (
    AudioBoundPayload,
    AudioUnboundPayload,
    ClipAddedPayload,
    ClipMovedPayload,
    ClipPosition,
    ClipRemovedPayload,
    ClipReplacedPayload,
    ClipRetimedPayload,
    ClipRetrackedPayload,
    ClipSwappedPayload,
    ClipTextSetPayload,
    EffectAddedPayload,
    EffectRemovedPayload,
    EffectTunedPayload,
    ErasedPayload,
    ThemeOverriddenPayload,
    ThemeSetPayload,
    TimelineConfigReplacedPayload,
    TimelineEvent,
    TimelineRecoveredPayload,
    TrackAddedPayload,
    TrackRemovedPayload,
    TransitionRemovedPayload,
    TransitionSetPayload,
)

# ============================================================================
# ProjectionError
# ============================================================================


@dataclass
class ProjectionError(RuntimeError):
    """Raised when a single event cannot be projected onto the assembly.

    Carries enough context for an m7 audit tool to pinpoint the failure
    without opening the event log.
    """

    event_id: str
    """ULID of the event that caused the error."""

    kind: str
    """Event kind (e.g. ``"clip.added"``)."""

    reason: str
    """Human-readable diagnostic."""

    def __str__(self) -> str:
        return f"projection error at event {self.event_id!r} ({self.kind!r}): {self.reason}"


@dataclass
class ErasedPayloadProjectionError(ProjectionError):
    """Raised when an erased event cannot be safely skipped in projection.

    Read paths must catch this typed error rather than silently hiding it.
    Safe-to-skip erased kinds (e.g. clip.*, transition.*, effect.*, theme.*,
    track.*, audio.*, pool.*) are skipped; all other erased events raise
    this error.
    """

    def __str__(self) -> str:
        return (
            f"erased payload cannot be projected at event {self.event_id!r} "
            f"({self.kind!r}): {self.reason}"
        )


class TimelineProjectionBoundaryError(AstridError, ValueError):
    """Raised when a projected TimelineConfig fails the runtime boundary check."""


_PROJECTED_TOP_ALLOWED = frozenset({
    "tracks",
    "clips",
    "theme",
    "theme_overrides",
    "generation_defaults",
    "pinnedShotGroups",
    "output",
    "app",
})
_PROJECTED_FORBIDDEN_CLIP_KEYS = frozenset({"kind", "asset_id", "start", "duration"})


def _validate_projected_timeline_boundary(state: dict[str, Any]) -> dict[str, Any]:
    """Validate the raw TimelineConfig projection boundary without registry lookups."""
    if not isinstance(state, dict):
        raise TimelineProjectionBoundaryError("projected TimelineConfig must be an object")
    unknown = sorted(key for key in state if key not in _PROJECTED_TOP_ALLOWED)
    if unknown:
        raise TimelineProjectionBoundaryError(f"projected TimelineConfig has unknown key(s): {unknown}")
    for key in ("clips", "tracks"):
        if key not in state:
            raise TimelineProjectionBoundaryError(f"projected TimelineConfig missing required key: {key}")
        if not isinstance(state[key], list):
            raise TimelineProjectionBoundaryError(f"projected TimelineConfig.{key} must be a list")
    for index, track in enumerate(state["tracks"]):
        if not isinstance(track, dict):
            raise TimelineProjectionBoundaryError(f"projected TimelineConfig.tracks[{index}] must be an object")
        if not isinstance(track.get("id"), str) or not track.get("id"):
            raise TimelineProjectionBoundaryError(
                f"projected TimelineConfig.tracks[{index}].id must be a non-empty string"
            )
        try:
            normalize_track_kind(track.get("kind"))
        except ValueError as exc:
            raise TimelineProjectionBoundaryError(
                f"projected TimelineConfig.tracks[{index}].kind {exc}"
            ) from exc
        if not isinstance(track.get("label"), str) or not track.get("label"):
            raise TimelineProjectionBoundaryError(
                f"projected TimelineConfig.tracks[{index}].label must be a non-empty string"
            )
    for index, clip in enumerate(state["clips"]):
        if not isinstance(clip, dict):
            raise TimelineProjectionBoundaryError(
                f"projected TimelineConfig.clips[{index}] must be an object"
            )
        legacy = sorted(key for key in clip if key in _PROJECTED_FORBIDDEN_CLIP_KEYS)
        if legacy:
            raise TimelineProjectionBoundaryError(
                f"projected TimelineConfig.clips[{index}] has legacy key(s): {legacy}"
            )
        for key in ("id", "track", "clipType"):
            if not isinstance(clip.get(key), str) or not clip.get(key):
                raise TimelineProjectionBoundaryError(
                    f"projected TimelineConfig.clips[{index}].{key} must be a non-empty string"
                )
        if not isinstance(clip.get("at"), (int, float)) or isinstance(clip.get("at"), bool):
            raise TimelineProjectionBoundaryError(
                f"projected TimelineConfig.clips[{index}].at must be a number"
            )
    return json.loads(json.dumps(state, sort_keys=True, separators=(",", ":"), allow_nan=False))


# ============================================================================
# Projection classification and assembly key defaults
# ============================================================================

ProjectionKindClassification = Literal[
    "timeline_config_mutation",
    "validated_full_config_replacement",
    "metadata_noop",
    "non_container_read_model",
    "migration_only_legacy",
]

PROJECTOR_EVENT_CLASSIFICATION: dict[str, ProjectionKindClassification] = {
    "timeline.created": "metadata_noop",
    "timeline.renamed": "metadata_noop",
    "timeline.default_set": "metadata_noop",
    "timeline.tombstoned": "metadata_noop",
    "timeline.deleted": "metadata_noop",
    "timeline.reverted": "metadata_noop",
    "timeline.branched_from": "metadata_noop",
    "timeline.erased": "metadata_noop",
    "timeline.recovered": "validated_full_config_replacement",
    "clip.added": "timeline_config_mutation",
    "clip.removed": "timeline_config_mutation",
    "clip.moved": "timeline_config_mutation",
    "clip.retracked": "timeline_config_mutation",
    "clip.retimed": "timeline_config_mutation",
    "clip.swapped": "timeline_config_mutation",
    "clip.replaced": "timeline_config_mutation",
    "clip.text_set": "timeline_config_mutation",
    "clip.annotated": "non_container_read_model",
    "transition.set": "timeline_config_mutation",
    "transition.removed": "timeline_config_mutation",
    "effect.added": "timeline_config_mutation",
    "effect.removed": "timeline_config_mutation",
    "effect.tuned": "timeline_config_mutation",
    "theme.set": "timeline_config_mutation",
    "theme.overridden": "timeline_config_mutation",
    "track.added": "timeline_config_mutation",
    "track.removed": "timeline_config_mutation",
    "audio.bound": "timeline_config_mutation",
    "audio.unbound": "timeline_config_mutation",
    "pool.asset_added": "non_container_read_model",
    "pool.asset_removed": "non_container_read_model",
    "pool.asset_scored": "non_container_read_model",
    "arrangement.replaced": "migration_only_legacy",
    "timeline.config_replaced": "validated_full_config_replacement",
    "timeline.asset_registry_replaced": "non_container_read_model",
}

MATERIALIZER_ALLOWED_CLASSIFICATIONS: frozenset[ProjectionKindClassification] = frozenset({
    "timeline_config_mutation",
    "validated_full_config_replacement",
    "metadata_noop",
})


def classify_projector_event_kind(kind: str) -> ProjectionKindClassification:
    """Return the planned container routing class for a timeline event kind."""
    return PROJECTOR_EVENT_CLASSIFICATION.get(kind, "metadata_noop")


_EMPTY_INIT_DEFAULTS: dict[str, Any] = {
    "clips": [],
    "tracks": [],
    "theme": "",
    "theme_overrides": {},
}

_THEME_OVERRIDE_KEYS = frozenset({"visual", "generation", "voice", "audio", "pacing"})


def _copy_empty_default(value: Any) -> Any:
    return deepcopy(value)


# ============================================================================
# Clip helpers
# ============================================================================


def _clip_index(clips: list[dict[str, Any]], clip_id: str) -> int | None:
    """Return the list index of *clip_id* in *clips*, or ``None``."""
    for i, clip in enumerate(clips):
        if clip.get("id") == clip_id:
            return i
    return None


def _insert_at_position(
    clips: list[dict[str, Any]], new_clip: dict[str, Any], position: ClipPosition | None
) -> None:
    """Insert *new_clip* into *clips* at *position* (append if None)."""
    if position is None:
        clips.append(new_clip)
        return
    if position.mode == "index":
        idx = max(0, min(position.index or 0, len(clips)))
        clips.insert(idx, new_clip)
    elif position.mode == "after":
        ref_idx = _clip_index(clips, position.ref_clip_id or "")
        if ref_idx is not None:
            clips.insert(ref_idx + 1, new_clip)
        else:
            clips.append(new_clip)
    elif position.mode == "before":
        ref_idx = _clip_index(clips, position.ref_clip_id or "")
        if ref_idx is not None:
            clips.insert(ref_idx, new_clip)
        else:
            clips.append(new_clip)


def _make_clip_entry(payload: ClipAddedPayload) -> dict[str, Any]:
    """Build a minimal clip dict from a ``clip.added`` payload."""
    clip_type = "text" if payload.kind == "text" else "media"
    entry: dict[str, Any] = {
        "id": payload.clip_id,
        "at": payload.start,
        "track": payload.track_id,
        "clipType": clip_type,
    }
    if payload.asset_id:
        entry["asset"] = payload.asset_id
    if payload.duration is not None:
        entry["hold"] = payload.duration
    if clip_type == "text":
        entry["text"] = {"content": ""}
    return entry


# ============================================================================
# Per-kind clip applicators (m2)
# ============================================================================


def _apply_clip_added(clips: list[dict[str, Any]], payload: ClipAddedPayload) -> None:
    _insert_at_position(clips, _make_clip_entry(payload), payload.position)


def _apply_clip_removed(clips: list[dict[str, Any]], payload: ClipRemovedPayload) -> None:
    idx = _clip_index(clips, payload.clip_id)
    if idx is not None:
        clips.pop(idx)


def _apply_clip_moved(clips: list[dict[str, Any]], payload: ClipMovedPayload) -> None:
    idx = _clip_index(clips, payload.clip_id)
    if idx is None:
        return
    clip = clips.pop(idx)
    _insert_at_position(clips, clip, payload.position)


def _apply_clip_retracked(clips: list[dict[str, Any]], payload: ClipRetrackedPayload) -> None:
    for clip in clips:
        if clip.get("id") == payload.clip_id:
            clip["track"] = payload.track_id
            return


def _apply_clip_retimed(clips: list[dict[str, Any]], payload: ClipRetimedPayload) -> None:
    for clip in clips:
        if clip.get("id") == payload.clip_id:
            clip["at"] = payload.start
            clip["hold"] = payload.duration
            return


def _apply_clip_swapped(clips: list[dict[str, Any]], payload: ClipSwappedPayload) -> None:
    idx_a = _clip_index(clips, payload.clip_a_id)
    idx_b = _clip_index(clips, payload.clip_b_id)
    if idx_a is not None and idx_b is not None:
        clips[idx_a], clips[idx_b] = clips[idx_b], clips[idx_a]


def _apply_clip_replaced(clips: list[dict[str, Any]], payload: ClipReplacedPayload) -> None:
    for clip in clips:
        if clip.get("id") == payload.clip_id:
            clip["asset"] = payload.with_asset_id
            return


def _apply_clip_text_set(clips: list[dict[str, Any]], payload: ClipTextSetPayload) -> None:
    for clip in clips:
        if clip.get("id") == payload.clip_id:
            clip["text"] = {"content": payload.text}
            return


# ============================================================================
# transition.* applicators (m3)
# ============================================================================


def _apply_transition_set(clips: list[dict[str, Any]], payload: TransitionSetPayload) -> None:
    for clip in clips:
        if clip.get("id") == payload.left_clip_id:
            clip["transition"] = {
                "type": payload.kind,
                "duration": payload.duration_seconds,
                "params": {"right_clip_id": payload.right_clip_id},
            }
            return


def _apply_transition_removed(clips: list[dict[str, Any]], payload: TransitionRemovedPayload) -> None:
    for clip in clips:
        if clip.get("id") == payload.left_clip_id:
            clip.pop("transition", None)
            return


# ============================================================================
# effect.* applicators (m3)
# ============================================================================


def _apply_effect_added(clips: list[dict[str, Any]], payload: EffectAddedPayload, assembly: dict[str, Any]) -> None:
    for clip in clips:
        if clip.get("id") == payload.clip_id:
            params = clip.setdefault("params", {})
            effects = params.setdefault("effects", {})
            effects[payload.effect_id] = dict(payload.params) if payload.params else {}
            return


def _apply_effect_removed(clips: list[dict[str, Any]], payload: EffectRemovedPayload, assembly: dict[str, Any]) -> None:
    for clip in clips:
        if clip.get("id") == payload.clip_id:
            params = clip.get("params")
            effects = params.get("effects") if isinstance(params, dict) else None
            if isinstance(effects, dict):
                effects.pop(payload.effect_id, None)
            return


def _apply_effect_tuned(clips: list[dict[str, Any]], payload: EffectTunedPayload, assembly: dict[str, Any]) -> None:
    for clip in clips:
        if clip.get("id") == payload.clip_id:
            params = clip.setdefault("params", {})
            effects = params.setdefault("effects", {})
            effect_params = effects.setdefault(payload.effect_id, {})
            effect_params[payload.param] = payload.value
            return


# ============================================================================
# theme.* applicators (m3)
# ============================================================================


def _apply_theme_set(assembly: dict[str, Any], payload: ThemeSetPayload) -> None:
    assembly["theme"] = payload.theme_id


def _apply_theme_overridden(assembly: dict[str, Any], payload: ThemeOverriddenPayload) -> None:
    overrides: dict[str, Any] = assembly["theme_overrides"]
    if payload.override_id in _THEME_OVERRIDE_KEYS and isinstance(payload.value, dict):
        overrides[payload.override_id] = payload.value


# ============================================================================
# track.* applicators (m3)
# ============================================================================


def _apply_track_added(assembly: dict[str, Any], payload: TrackAddedPayload) -> None:
    from astrid.core.timeline.track_appearance import stamp_new_track_appearance

    tracks: list[dict[str, Any]] = assembly["tracks"]
    track_entry = stamp_new_track_appearance({
        "id": payload.track_id,
        "kind": payload.kind,
        "label": payload.label,
    })
    tracks.append(track_entry)


def _apply_track_removed(assembly: dict[str, Any], payload: TrackRemovedPayload) -> None:
    tracks: list[dict[str, Any]] = assembly["tracks"]
    assembly["tracks"] = [t for t in tracks if t.get("id") != payload.track_id]


# ============================================================================
# audio.* applicators (m3)
# ============================================================================


def _apply_audio_bound(clips: list[dict[str, Any]], payload: AudioBoundPayload) -> None:
    for clip in clips:
        if clip.get("id") == payload.clip_id:
            clip["asset"] = payload.asset_id
            return


def _apply_audio_unbound(clips: list[dict[str, Any]], payload: AudioUnboundPayload) -> None:
    for clip in clips:
        if clip.get("id") == payload.clip_id:
            clip.pop("asset", None)
            return


# ============================================================================
# Dispatch infrastructure
# ============================================================================


# Lifecycle event kinds that are intentional assembly no-ops.
_LIFECYCLE_NOOP_KINDS: frozenset[str] = frozenset({
    "timeline.created",
    "timeline.renamed",
    "timeline.default_set",
    "timeline.tombstoned",
    "timeline.deleted",
    # Ops/lifecycle events that don't change assembly state
    "timeline.reverted",
    "timeline.branched_from",
    "timeline.erased",
})

# Event kinds whose erased payloads can be safely skipped in projection.
# These are runtime domain events that mutate specific raw TimelineConfig keys
# or explicit non-container read models. Migration-only legacy events are not
# included in this canonical projection skip list.
_ERASED_SAFE_KINDS: frozenset[str] = frozenset({
    "clip.added", "clip.removed", "clip.moved", "clip.retracked", "clip.retimed",
    "clip.swapped", "clip.replaced", "clip.text_set", "clip.annotated",
    "transition.set", "transition.removed",
    "effect.added", "effect.removed", "effect.tuned",
    "theme.set", "theme.overridden",
    "track.added", "track.removed",
    "audio.bound", "audio.unbound",
    "pool.asset_added", "pool.asset_removed", "pool.asset_scored",
    "timeline.asset_registry_replaced",
})


def _dispatch_clip_applicator(
    assembly: dict[str, Any],
    payload: Any,
    fn: Any,
) -> None:
    """Dispatch a clip-scoped applicator that only needs the clips list."""
    clips: list[dict[str, Any]] = assembly["clips"]
    fn(clips, payload)


def _dispatch_clip_assembly_applicator(
    assembly: dict[str, Any],
    payload: Any,
    fn: Any,
) -> None:
    """Dispatch a clip-scoped applicator that may also touch assembly context."""
    clips: list[dict[str, Any]] = assembly["clips"]
    fn(clips, payload, assembly)


def _dispatch_assembly_applicator(
    assembly: dict[str, Any],
    payload: Any,
    fn: Any,
) -> None:
    """Dispatch an assembly-scoped applicator."""
    fn(assembly, payload)


# (event_kind, ensure_keys, dispatch_fn, applicator)
_DISPATCH: list[tuple[str, list[str], Any, Any]] = [
    # -- clip.* (m2, unchanged) --
    ("clip.added", ["clips"], _dispatch_clip_applicator, _apply_clip_added),
    ("clip.removed", ["clips"], _dispatch_clip_applicator, _apply_clip_removed),
    ("clip.moved", ["clips"], _dispatch_clip_applicator, _apply_clip_moved),
    ("clip.retracked", ["clips"], _dispatch_clip_applicator, _apply_clip_retracked),
    ("clip.retimed", ["clips"], _dispatch_clip_applicator, _apply_clip_retimed),
    ("clip.swapped", ["clips"], _dispatch_clip_applicator, _apply_clip_swapped),
    ("clip.replaced", ["clips"], _dispatch_clip_applicator, _apply_clip_replaced),
    ("clip.text_set", ["clips"], _dispatch_clip_applicator, _apply_clip_text_set),
    # -- transition.* (m3) --
    ("transition.set", ["clips"], _dispatch_clip_applicator, _apply_transition_set),
    ("transition.removed", ["clips"], _dispatch_clip_applicator, _apply_transition_removed),
    # -- effect.* (m3) --
    ("effect.added", ["clips"], _dispatch_clip_assembly_applicator, _apply_effect_added),
    ("effect.removed", ["clips"], _dispatch_clip_assembly_applicator, _apply_effect_removed),
    ("effect.tuned", ["clips"], _dispatch_clip_assembly_applicator, _apply_effect_tuned),
    # -- theme.* (m3) --
    ("theme.set", ["theme", "theme_overrides"], _dispatch_assembly_applicator, _apply_theme_set),
    ("theme.overridden", ["theme", "theme_overrides"], _dispatch_assembly_applicator, _apply_theme_overridden),
    # -- track.* (m3) --
    ("track.added", ["tracks"], _dispatch_assembly_applicator, _apply_track_added),
    ("track.removed", ["tracks"], _dispatch_assembly_applicator, _apply_track_removed),
    # -- audio.* (m3) --
    ("audio.bound", ["clips"], _dispatch_clip_applicator, _apply_audio_bound),
    ("audio.unbound", ["clips"], _dispatch_clip_applicator, _apply_audio_unbound),
]

# Build fast lookup
_DISPATCH_MAP: dict[str, tuple[list[str], Any, Any]] = {}
for kind, keys, dispatcher, applicator in _DISPATCH:
    _DISPATCH_MAP[kind] = (keys, dispatcher, applicator)


# ============================================================================
# Public API
# ============================================================================


def apply_event_to_assembly(
    state: dict[str, Any],
    event: TimelineEvent,
    *,
    _copy_state: bool = True,
) -> dict[str, Any]:
    """Fold a single *event* onto an assembly *state*, returning a **new** dict.

    The input *state* is never mutated.  This is the unit operation behind
    ``project_to_assembly`` and is also useful for incremental or preview
    consumers that want to see the effect of one event without replaying
    the full stream.

    Raises ``ProjectionError`` when *event* is unrecognised and cannot be
    applied.
    """
    # Lifecycle no-ops
    if event.kind in _LIFECYCLE_NOOP_KINDS:
        return state

    if event.kind not in PROJECTOR_EVENT_CLASSIFICATION:
        return state

    # Erased payload envelope: checked BEFORE any kind-specific logic,
    # since erased events keep their original kind but have ErasedPayload.
    if isinstance(event.payload, ErasedPayload):
        if event.kind in _ERASED_SAFE_KINDS:
            # Safe to skip — the event existed but its payload is erased
            return state
        raise ErasedPayloadProjectionError(
            event_id=event.event_id,
            kind=event.kind,
            reason=(
                f"erased event of kind {event.kind!r} cannot be safely skipped "
                f"during projection; only domain events that modify specific "
                f"assembly keys can be skipped"
            ),
        )

    classification = classify_projector_event_kind(event.kind)
    if classification in {"metadata_noop", "non_container_read_model"}:
        return state

    # timeline.config_replaced — replace projected assembly with a validated
    # raw TimelineConfig container. This event is the lossless runtime full
    # replacement surface; legacy wrappers are rejected by payload validation.
    if event.kind == "timeline.config_replaced":
        payload = event.payload
        if isinstance(payload, TimelineConfigReplacedPayload):
            return validate_timeline_config_for_container(payload.config)
        return state

    # timeline.recovered — replace projected assembly with anchor projection
    if event.kind == "timeline.recovered":
        payload = event.payload
        if isinstance(payload, TimelineRecoveredPayload):
            if payload.projected_state_summary is not None:
                return validate_timeline_config_for_container(
                    payload.projected_state_summary
                )
            # Fall through: if no projected state in payload, treat as no-op
            # (requires upstream caller to have materialised the anchor)
            return state
        return state

    if classification == "migration_only_legacy":
        raise ProjectionError(
            event_id=event.event_id,
            kind=event.kind,
            reason=(
                f"{event.kind} is not a TimelineConfig container mutation; "
                "use a standalone migration tool before importing legacy data"
            ),
        )

    # Domain mutation events — look up in dispatch table
    dispatch_entry = _DISPATCH_MAP.get(event.kind)
    if dispatch_entry is None:
        raise ProjectionError(
            event_id=event.event_id,
            kind=event.kind,
            reason=f"unsupported event kind for assembly projection: {event.kind!r}",
        )

    ensure_keys, dispatcher, applicator = dispatch_entry

    # Work on a deep copy of the state so the input is never mutated.
    # A shallow dict() copy would still share nested lists/dicts (clips,
    # tracks, pool entries, etc.) with the input, causing silent corruption
    # when applicators mutate those structures in-place.
    assembly = deepcopy(state) if _copy_state else state

    # Runtime assembly state is always a raw TimelineConfig container.
    # Granular legacy events may arrive before any track/clip event, so keep
    # the required container keys present even when a test or checkpoint seed
    # omits them.
    if "clips" not in assembly:
        assembly["clips"] = []
    if "tracks" not in assembly:
        assembly["tracks"] = []

    # Ensure every required domain key exists, initialising from defaults
    # when the key is missing from the assembly (not just when empty).
    for key in ensure_keys:
        if key not in assembly and key in _EMPTY_INIT_DEFAULTS:
            assembly[key] = _copy_empty_default(_EMPTY_INIT_DEFAULTS[key])

    # Apply the event
    try:
        dispatcher(assembly, event.payload, applicator)
    except Exception as exc:
        raise ProjectionError(
            event_id=event.event_id,
            kind=event.kind,
            reason=f"failed to apply event: {exc}",
        ) from exc

    return assembly


def project_to_assembly(
    events: Sequence[TimelineEvent],
    *,
    initial_assembly: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Replay an ordered sequence of *events* into a projected assembly dict.

    The projector is storage-agnostic: it accepts ``TimelineEvent`` objects
    materialized by the runtime or constructed by a pure test fixture.

    Parameters
    ----------
    events:
        Ordered sequence of ``TimelineEvent`` objects to replay.
    initial_assembly:
        Optional seed state for the projection.  When supplied, events are
        folded on top of a deep copy of this dict.  Useful for
        checkpoint-assisted replay where only suffix events need to be
        applied.

    Returns
    -------
    dict
        The projected in-memory timeline configuration.

    Raises
    ------
    ProjectionError
        When any event cannot be projected.
    """
    state: dict[str, Any] = (
        canonical_timeline_config(initial_assembly)
        if initial_assembly is not None
        else canonical_empty_timeline()
    )
    for event in events:
        state = apply_event_to_assembly(state, event, _copy_state=False)
    return _validate_projected_timeline_boundary(state)


# ============================================================================
# Pure replay (never writes files)
# ============================================================================


def replay_projection(
    backend: Any,
    *,
    stop_at_event_id: str | None = None,
) -> dict[str, Any]:
    """Replay the full event stream into an assembly dict **without any disk I/O**.

    It reads events through an explicitly supplied read-only source and folds
    them via ``apply_event_to_assembly``. Live product code receives timeline
    documents through the generated runtime client instead.

    Parameters
    ----------
    backend:
        Any object with a ``read_events(*, after=None, limit=None) -> list[TimelineEvent]``
        method.
    stop_at_event_id:
        When set, replay stops after applying the event whose ``event_id``
        matches this value.  The returned assembly reflects state **after**
        that event was applied.

    Returns
    -------
    dict
        The **inner** projected assembly dict (not the wrapper).

    Raises
    ------
    ProjectionError
        When ``stop_at_event_id`` is provided but not found in the stream,
        or when any event cannot be projected.
    """
    all_events = backend.read_events()

    if stop_at_event_id is None:
        return project_to_assembly(all_events)

    # Prefix replay: fold events one by one, check each event_id.
    state: dict[str, Any] = {}
    for event in all_events:
        state = apply_event_to_assembly(state, event)
        if event.event_id == stop_at_event_id:
            return state

    raise ProjectionError(
        event_id=stop_at_event_id,
        kind="(target)",
        reason=f"stop_at_event_id {stop_at_event_id!r} not found in event stream",
    )


# ============================================================================
# Helpers for projecting to a specific event ID and checkpoint verification
# ============================================================================


def project_to_event_id(
    backend: Any,
    target_event_id: str,
) -> dict[str, Any]:
    """Replay the event stream up to (and including) *target_event_id*.

    Verifies the anchor hash against the backend (via ``head()`` or
    ``verify_chain()``) rather than trusting checkpoint files alone.

    Parameters
    ----------
    backend:
        Any object with ``read_events(*, after=None, limit=None)`` and
        ``verify_chain()`` methods.
    target_event_id:
        The ULID of the target event to project to.

    Returns
    -------
    dict
        The **inner** projected assembly dict after applying all events
        up to and including *target_event_id*.

    Raises
    ------
    ProjectionError
        When *target_event_id* is not found in the stream or any event
        cannot be projected.
    """
    return replay_projection(backend, stop_at_event_id=target_event_id)


def project_to_checkpoint(
    backend: Any,
    *,
    verify: bool = True,
) -> dict[str, Any]:
    """Replay the full event stream into an assembly dict with optional verification.

    This helper does **not** write files. It is suitable for migrated fixture
    comparison and pure validation only.

    Parameters
    ----------
    backend:
        Any object with ``read_events()`` and ``verify_chain()`` methods.
    verify:
        When True (default), verify the event hash chain after projection.
        Raises ``ProjectionError`` if chain verification fails.

    Returns
    -------
    dict
        The **inner** projected assembly dict.

    Raises
    ------
    ProjectionError
        When any event cannot be projected or (if *verify*) when the
        hash chain verification fails.
    """
    state = replay_projection(backend)
    if verify:
        verification = backend.verify_chain()
        if not verification.ok:
            raise ProjectionError(
                event_id=verification.last_event_id or "(unknown)",
                kind="(chain-verification)",
                reason=verification.error or "hash chain verification failed",
            )
        # Additional safety: the projected state is from events only — caller
        # must never treat stale compatibility blobs as authority.
    return state
