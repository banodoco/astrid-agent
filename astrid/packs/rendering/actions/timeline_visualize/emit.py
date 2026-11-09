"""R9 — semantic core and action graph emitters.

Each emitter returns the *content* of one evidence-pack artifact as a plain
JSON-ready dict (or, for the two markdown companions, a deterministic string).
No emitter writes files, reads the wall clock, or mutates its inputs; R13 owns
materialization, hashing, and ``manifest.json``.

Artifacts produced:

* ``emit_ground_truth`` — frozen semantic model (identity, event head, extents,
  tracks, clips, assets, snapshot SNS, scope).  With a ``scope`` the ``scope``
  block narrows to the selected kind/ref/bounds and ``objects``/``clips``/
  ``assets`` filter to the scoped set (identity ordinals never renumber).
* ``emit_action_index`` — executable navigation graph per display id; with a
  ``scope`` only scoped objects are indexed and relations stay resolved.
* ``emit_asset_index`` — per-asset provenance, integrity, and contained path
  (always the full frozen project; scoped views never hide library state).
* ``emit_transcript_index`` — empty-valid shape (TS/SP declared, empty).
* ``emit_diagnostics`` — every model/snapshot warning with severity/code; with
  a ``scope`` narrowed to the scoped objects plus the scope's own warnings.
* ``emit_reading_guide`` — generic prose teaching the qualified-id rule.
* ``emit_structure_md`` — breadcrumb + deterministic suggested next actions.

Frozen-artifact notes (R13 must know):

* ``timestamps.frozen_at`` is a deterministic sentinel
  (:data:`FROZEN_AT_SENTINEL`), never wall-clock time and never part of the
  SNS preimage; R13 may replace it with the real freeze instant when the pack
  is materialized.
* The compositor version and transition-default fingerprint have no home in
  the frozen ``ground-truth.json`` schema (``additionalProperties: false``),
  so they are emitted as factual lines in ``structure.md``; the fingerprint
  also belongs in ``manifest.json.compositor`` (R13).
* The action ``argv`` arrays use the canonical ``python3 -m astrid timelines
  visualize`` prefix; ``--from-view`` is always the exact manifest path string
  passed in, so R13 can rewrite it to the final pack-relative absolute path.

Identity maps are duck-typed: any object exposing ``lookup_semantic(kind,
authored_id)``/``lookup_display(display_id)`` (R8's ``IdentityMap``) or a
Mapping-shaped twin with ``semantic_to_display``/``display_to_semantic`` fields
is accepted.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

from astrid.core.timeline.duration import resolve_transition_duration_frames
from astrid.core.timeline.snapshot import TimelineSnapshot
from astrid.packs.rendering.actions.timeline_visualize.ids import (
    parse_qualified_ref,
)
from astrid.packs.rendering.actions.timeline_visualize.model import (
    _PINNED_TRANSITION_DEFAULTS,
    COMPOSITOR_VERSION,
    TRANSITION_FALLBACK_FRAMES,
    TimelineInspectionModel,
)
from astrid.packs.rendering.actions.timeline_visualize.navigation import (
    IdentityMap,
    assign_range_ids,
)
from astrid.packs.rendering.actions.timeline_visualize.scope import Scope
from astrid.core.timeline.snapshot_digest import (
    canonical_json_bytes,
    sha256_bytes,
)
from astrid.packs.rendering.actions.timeline_visualize.transcript_attach import (
    TranscriptAttachment,
)
from astrid.packs.rendering.actions.timeline_visualize.transcripts import (
    SpeechOccurrence,
    TranscriptSegment,
    speech_occurrence_authored_id,
    transcript_segment_authored_id,
)

SCHEMA_VERSION = 1

#: Deterministic operational sentinel for ``ground-truth.json.timestamps``.
#: Excluded from every SNS preimage; never derived from the wall clock.
FROZEN_AT_SENTINEL = "2026-08-11T00:00:00Z"

#: Context window (seconds) attached to ``focus_context`` visualize actions.
FOCUS_CONTEXT_SECONDS = "2"

#: Context window (seconds) for the root timeline's ``focus_timestamp`` action.
TIMESTAMP_CONTEXT_SECONDS = "3"

_TIMELINE_REF = "TL01"

_ASSET_ROLES = frozenset(
    {
        "timeline_media",
        "generation_reference",
        "generation_output",
        "thumbnail_only",
        "rendered_sample",
    }
)

_ASSET_STATE_CODES: Mapping[str, str] = {
    "missing": "MISSING_MEDIA",
    "hash_mismatch": "HASH_MISMATCH",
    "hash_unrecorded": "HASH_UNRECORDED",
    "remote": "REMOTE_MEDIA",
    "thumbnail_only": "THUMBNAIL_ONLY",
    "unsupported": "UNSUPPORTED_MEDIA",
}

_OBJECT_KIND_ORDER: Mapping[str, int] = {
    "timeline": 0,
    "shot": 1,
    "clip": 2,
    "asset": 3,
    "range": 4,
    "transcript_source_segment": 5,
    "speech_occurrence": 6,
}

_DIAGNOSTIC_CODE_RE = re.compile(r"^([A-Z][A-Z0-9_]*): (.*)$", flags=re.DOTALL)

#: Multi-segment absolute path token (POSIX ``/a/b`` or Windows ``C:\\a\\b``).
#: Used only to scrub *defensive* path leaks out of diagnostics; a lookbehind
#: excludes scheme separators (``https://…``) so URLs survive.
_ABS_PATH_TOKEN_RE = re.compile(
    r"(?<!\w)(?:/(?:[\w.\- ]+/)+[\w.\- ]+|[A-Za-z]:[\\/](?:[\w.\- ]+[\\/])*[\w.\- ]+)"
)


def _sanitize_diagnostic_message(message: str) -> str:
    """Strip absolute path substrings from one diagnostics message (R13).

    Reasons and warnings must never carry absolute worktree/project paths;
    this is the emit-side backstop on top of resolution.py's sanitized reason
    strings.  Deterministic: every match becomes ``[redacted-path]``.
    """

    if not isinstance(message, str) or not message:
        return message
    return _ABS_PATH_TOKEN_RE.sub("[redacted-path]", message)


# ---------------------------------------------------------------------------
# Identity-map access (duck typed: IdentityMap or Mapping-shaped twin).
# ---------------------------------------------------------------------------


def _identity_attr(identity_map: Any, name: str) -> Any:
    value = getattr(identity_map, name, None)
    if value is None and isinstance(identity_map, Mapping):
        value = identity_map.get(name)
    return value


def _lookup_display(identity_map: Any, display_id: str) -> tuple[str, str, str] | None:
    method = getattr(identity_map, "lookup_display", None)
    if callable(method):
        return method(display_id)
    mapping = _identity_attr(identity_map, "display_to_semantic")
    if isinstance(mapping, Mapping):
        return mapping.get(display_id)
    raise TypeError("identity_map must expose lookup_display or display_to_semantic")


def _lookup_semantic(identity_map: Any, kind: str, authored_id: str) -> str | None:
    method = getattr(identity_map, "lookup_semantic", None)
    if callable(method):
        return method(kind, authored_id)
    mapping = _identity_attr(identity_map, "semantic_to_display")
    timeline_uuid = _identity_attr(identity_map, "timeline_uuid")
    if isinstance(mapping, Mapping) and isinstance(timeline_uuid, str):
        return mapping.get((timeline_uuid, kind, authored_id))
    raise TypeError("identity_map must expose lookup_semantic or semantic_to_display")


def _canonical_ref(identity_map: Any, display_id: str) -> dict[str, str]:
    identity = _lookup_display(identity_map, display_id)
    if identity is None:
        raise ValueError(f"display id {display_id!r} has no semantic identity")
    return {
        "timeline_uuid": identity[0],
        "kind": identity[1],
        "authored_id": identity[2],
    }


def _ordered_object_refs(model: TimelineInspectionModel, identity_map: Any) -> list[str]:
    """All display ids in deterministic order: TL, SH, CL, AS, RG, TS, SP."""
    mapping = _identity_attr(identity_map, "display_to_semantic")
    if not isinstance(mapping, Mapping):
        raise TypeError("identity_map must expose display_to_semantic")
    refs = list(mapping.keys())

    def sort_key(ref: str) -> tuple[int, int]:
        identity = _lookup_display(identity_map, ref)
        kind_order = _OBJECT_KIND_ORDER.get(identity[1] if identity else "", 99)
        ordinal = parse_qualified_ref(ref).object_ordinal or 0
        return (kind_order, ordinal)

    return sorted(refs, key=sort_key)


# ---------------------------------------------------------------------------
# Shared snapshot/scope blocks (byte-identical across every artifact).
# ---------------------------------------------------------------------------


def _snapshot_block(
    model: TimelineInspectionModel, snapshot: TimelineSnapshot
) -> list[dict[str, Any]]:
    slug = model.slug if isinstance(model.slug, str) and model.slug else model.timeline_ulid.lower()
    return [
        {
            "timeline": {
                "stable_id": _TIMELINE_REF,
                "qualified_ref": _TIMELINE_REF,
                "uuid": model.timeline_uuid,
                "ulid": model.timeline_ulid,
                "slug": slug,
            },
            "digest": model.snapshot_sns,
            "event_head": {
                "version": snapshot.head_version,
                "last_event_id": snapshot.last_event_id,
                "last_hash": snapshot.last_hash,
            },
            "fps": model.fps,
        }
    ]


_SCOPE_WARNING_CODES: tuple[tuple[str, str], ...] = (
    # Emit-side message -> code map keyed on distinctive substrings.  Scope
    # warnings are plain strings whose configured phrase occurs mid-message
    # (e.g. "clip 'x' is not present in the snapshot"), so codes are derived
    # from message content rather than a prefix match.  Substrings are pairwise
    # non-overlapping and ordered for determinism.
    ("range was clipped to the composition bounds", "CLIP_RANGE_CLIPPED"),
    ("timestamp lies outside the composition bounds", "TIMESTAMP_CONTEXT_CLIPPED"),
    ("unavailable; timeline.pinnedShotGroups has no match", "SHOT_GROUPS_ABSENT"),
    ("is not present in the snapshot", "CLIP_ABSENT_FROM_SNAPSHOT"),
    ("has no clip uses in the snapshot", "ASSET_NO_CLIP_USES"),
    ("has neither valid authored bounds nor present member clips", "SHOT_BOUNDS_ABSENT"),
)


def _scope_effective(
    model: TimelineInspectionModel,
    identity_map: Any,
    scope: Scope | None,
) -> tuple[Any, Scope | None]:
    """Return ``(identity_map, scope)`` with range RG ids minted when needed.

    Range scopes carry an authored range id in ``Scope.ref``; the ground-truth
    scope block and action index require a qualified ``TL01.RGxx`` ref, so the
    display ordinal is minted via :func:`navigation.assign_range_ids` when the
    authored id has none yet.  ``assign_range_ids`` never mutates its input and
    never renumbers an already-allocated range, so re-emission is stable and
    the caller's map stays untouched.  All other scope kinds (and ``None``)
    pass through unchanged.
    """

    if scope is None or scope.kind != "range" or not scope.ref:
        return identity_map, scope
    if scope.start_frame is None or scope.end_frame is None:
        return identity_map, scope
    identity = _lookup_display(identity_map, scope.ref)
    if identity is not None and identity[1] == "range":
        return identity_map, scope
    if _lookup_semantic(identity_map, "range", scope.ref) is not None:
        return identity_map, scope
    if not isinstance(identity_map, IdentityMap):
        raise ValueError(
            "range scope emission requires a navigation.IdentityMap so the RG "
            "display id can be minted via assign_range_ids"
        )
    ranges = [
        (
            scope.ref,
            scope.start_frame / model.fps,
            scope.end_frame / model.fps,
        )
    ]
    return assign_range_ids(identity_map, ranges), scope


def _scope_ref(
    model: TimelineInspectionModel,
    identity_map: Any,
    scope: Scope | None,
) -> str | None:
    """Qualified ref for a scope block: display id or ``TL01@HH:MM:SS.fff``.

    Timeline/project scopes use the timeline ref or ``None``; timestamp scopes
    reuse the caller's locator when it parses (the CLI always supplies one),
    otherwise anchor on ``Scope.at_seconds`` when present, and only fall back
    to the context-window midpoint for legacy timestamp scopes without
    ``at_seconds``; clip/asset/shot/range scopes resolve through the identity
    map — a ``Scope.ref`` may already be the display id or the authored id.
    Unresolvable authored refs (degenerate empty scopes) pass through
    verbatim; the R3 schema only accepts qualified refs, so R13 must decide
    how to serialize those.
    """

    if scope is None:
        return _TIMELINE_REF
    kind = scope.kind
    if kind == "timeline":
        return _TIMELINE_REF
    if kind == "project":
        return None
    if kind == "timestamp":
        if scope.ref is not None:
            try:
                parsed = parse_qualified_ref(scope.ref)
            except ValueError:
                parsed = None
            if parsed is not None and parsed.is_timestamp:
                return scope.ref
        if scope.at_seconds is not None:
            return _timestamp_locator(scope.at_seconds, _TIMELINE_REF)
        if scope.start_frame is None or scope.end_frame is None:
            return None
        midpoint_seconds = (scope.start_frame + scope.end_frame) / 2.0 / model.fps
        return _timestamp_locator(midpoint_seconds, _TIMELINE_REF)
    semantic_kind = {"shot": "shot", "range": "range", "clip": "clip", "asset": "asset"}.get(kind)
    if semantic_kind is None:
        return scope.ref
    if scope.ref is not None:
        identity = _lookup_display(identity_map, scope.ref)
        if identity is not None and identity[1] == semantic_kind:
            return scope.ref
    if scope.ref is not None:
        display = _lookup_semantic(identity_map, semantic_kind, scope.ref)
        if display is not None:
            return display
    return scope.ref


def _in_scope_refs(
    model: TimelineInspectionModel,
    identity_map: Any,
    scope: Scope | None,
) -> set[str]:
    """Display refs of the objects a scoped emission still shows.

    ``None`` (full timeline) keeps every object.  A real scope keeps the
    timeline, every clip in ``Scope.clip_ids``, the assets those clips
    reference, and the scope's own object when it has a display id (the minted
    ``RG`` for range scopes, the focused ``CL``/``AS``/``SH`` otherwise).
    Ordinals never renumber: children simply show fewer entries.
    """

    if scope is None:
        return set(_ordered_object_refs(model, identity_map))
    refs: set[str] = {_TIMELINE_REF}
    clip_ids = set(scope.clip_ids)
    for clip in model.clips:
        if clip.clip_id not in clip_ids:
            continue
        clip_ref = _lookup_semantic(identity_map, "clip", clip.clip_id)
        if clip_ref is not None:
            refs.add(clip_ref)
        for asset_key in clip.asset_refs:
            asset_ref = _lookup_semantic(identity_map, "asset", asset_key)
            if asset_ref is not None:
                refs.add(asset_ref)
    # Transcript identity is a frozen evidence layer. Keeping its complete
    # TS/SP graph in descendants makes every CL/AS/TS/SP action resolvable
    # without reopening the transcript or consulting current project state.
    for ref in _ordered_object_refs(model, identity_map):
        identity = _lookup_display(identity_map, ref)
        if identity is not None and identity[1] in {
            "transcript_source_segment",
            "speech_occurrence",
        }:
            refs.add(ref)
    scope_ref = _scope_ref(model, identity_map, scope)
    if scope_ref is not None:
        try:
            parsed = parse_qualified_ref(scope_ref)
        except ValueError:
            parsed = None
        if parsed is not None and not parsed.is_timestamp:
            refs.add(scope_ref)
    return refs


def _scope_warning_code(warning: str) -> str:
    """Deterministic diagnostics code for a ``Scope.warnings`` string.

    A warning that already carries a ``CODE: message`` prefix keeps its code;
    otherwise the message is matched against the distinctive-substring map
    (:data:`_SCOPE_WARNING_CODES`) so mid-message phrases resolve to the
    structured codes (``CLIP_ABSENT_FROM_SNAPSHOT``, ``ASSET_NO_CLIP_USES``,
    ``SHOT_GROUPS_ABSENT``, ``SHOT_BOUNDS_ABSENT``, ...).  Unknown messages
    fall back to ``SCOPE_WARNING``.
    """

    match = _DIAGNOSTIC_CODE_RE.match(warning)
    if match is not None:
        return match.group(1)
    for fragment, code in _SCOPE_WARNING_CODES:
        if fragment in warning:
            return code
    return "SCOPE_WARNING"


def _scope_warning_ref(
    model: TimelineInspectionModel,
    identity_map: Any,
    scope: Scope | None,
) -> str | None:
    """Diagnostics ``object_ref`` for scope warnings.

    Timestamp locators are not valid diagnostics refs (the schema accepts only
    qualified refs or null), so they report against the timeline; unresolvable
    refs also fall back to the timeline.
    """

    if scope is None:
        return None
    ref = _scope_ref(model, identity_map, scope)
    if ref is None:
        return _TIMELINE_REF
    try:
        parsed = parse_qualified_ref(ref)
    except ValueError:
        return _TIMELINE_REF
    if parsed.is_timestamp:
        return _TIMELINE_REF
    return ref


def _scope_block(
    model: TimelineInspectionModel,
    identity_map: Any,
    scope: Scope | None,
) -> dict[str, Any]:
    """Serialize the scope block: kind, qualified ref, closed-open bounds.

    ``None`` emits the full timeline scope exactly as before.  A real scope
    emits its kind, its qualified ref, and its exact frame bounds converted to
    seconds at the model fps (nullable when the scope is empty).  The timeline
    entry always stays ``[0, composition)``; only this block narrows.
    """

    if scope is None:
        return {
            "kind": "timeline",
            "ref": _TIMELINE_REF,
            "start_frame": 0,
            "end_frame": model.extents.composition_frames,
            "start_seconds": 0.0,
            "end_seconds": model.extents.composition_seconds,
        }
    start = scope.start_frame
    end = scope.end_frame
    return {
        "kind": scope.kind,
        "ref": _scope_ref(model, identity_map, scope),
        "start_frame": start,
        "end_frame": end,
        "start_seconds": (start / model.fps) if start is not None else None,
        "end_seconds": (end / model.fps) if end is not None else None,
    }


# ---------------------------------------------------------------------------
# Ground truth.
# ---------------------------------------------------------------------------


def _track_kind(model: TimelineInspectionModel, track_id: str) -> str:
    for track in model.tracks:
        if track.track_id == track_id:
            return track.kind
    return "other"


def _durations(model: TimelineInspectionModel) -> dict[str, Any]:
    visual_end = max(
        (
            clip.authored.end
            for clip in model.clips
            if _track_kind(model, clip.track_id) == "visual"
        ),
        default=0.0,
    )
    return {
        "authored_visual_only_end_seconds": float(visual_end),
        "frame_quantized_visual_end": {
            "frames": model.extents.visual_frames,
            "seconds": model.extents.visual_seconds,
        },
        "all_track_composition": {
            "frames": model.extents.composition_frames,
            "seconds": model.extents.composition_seconds,
        },
        "authored_all_track_composition": {
            "frames": (
                model.extents.authored_composition_frames
                if model.extents.authored_composition_frames is not None
                else model.extents.composition_frames
            ),
            "seconds": (
                model.extents.authored_composition_seconds
                if model.extents.authored_composition_seconds is not None
                else model.extents.composition_seconds
            ),
        },
    }


def _tracks(model: TimelineInspectionModel, snapshot: TimelineSnapshot) -> list[dict[str, Any]]:
    raw_by_id: dict[str, Mapping[str, Any]] = {}
    raw_tracks = snapshot.assembly.get("tracks", [])
    if isinstance(raw_tracks, list):
        for raw in raw_tracks:
            if isinstance(raw, Mapping):
                track_id = raw.get("id")
                if isinstance(track_id, str):
                    raw_by_id[track_id] = raw
    result: list[dict[str, Any]] = []
    for track in model.tracks:
        if track.kind not in {"visual", "audio"}:
            raise ValueError(
                f"track {track.track_id!r} has unsupported kind {track.kind!r}; "
                "ground truth only emits visual/audio tracks"
            )
        raw = raw_by_id.get(track.track_id, {})
        muted = raw.get("muted") if isinstance(raw.get("muted"), bool) else False
        result.append(
            {
                "authored_id": track.track_id,
                "kind": track.kind,
                "label": track.label if isinstance(track.label, str) else "",
                "muted": muted,
                "config_order": track.config_order,
                "paint_order": track.paint_index if track.kind == "visual" else None,
            }
        )
    return result


def _number_or_none(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _source_bounds(clip: Any, raw: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "from_seconds": _number_or_none(raw.get("from")),
        "to_seconds": _number_or_none(raw.get("to")),
        "hold_seconds": _number_or_none(raw.get("hold")),
        "duration_seconds": clip.authored.duration,
    }


def _frame_round(seconds: float, fps: int) -> int:
    return int(round(seconds * fps))


def _resolved_transition_frames(model: TimelineInspectionModel, clip: Any) -> int | None:
    same_track = [c for c in model.clips if c.track_id == clip.track_id]
    index = same_track.index(clip)
    successor = same_track[index + 1] if index + 1 < len(same_track) else None
    if successor is None:
        return None
    transition_id = clip.transition.get("id", clip.transition.get("type"))
    registered_default = _PINNED_TRANSITION_DEFAULTS.get(transition_id)
    return resolve_transition_duration_frames(
        clip.transition,
        clip.frames.duration_frames,
        successor.frames.duration_frames,
        registered_default,
        fps=model.fps,
    )


def _ignored_transition_reason(model: TimelineInspectionModel, clip: Any) -> str:
    if _track_kind(model, clip.track_id) != "visual":
        return "transition applies only to visual tracks"
    same_track = [c for c in model.clips if c.track_id == clip.track_id]
    index = same_track.index(clip)
    successor = same_track[index + 1] if index + 1 < len(same_track) else None
    if successor is None:
        return "no same-track successor clip; transition cannot be scheduled"
    if (
        successor.frames.start_frame < clip.frames.start_frame
        or successor.frames.start_frame > clip.frames.end_frame
    ):
        return "same-track successor does not overlap the clip window"
    if clip.kind == "effect-layer" or successor.kind == "effect-layer":
        return "effect-layer clips exclude transitions"
    return "resolved transition duration is non-positive or exceeds a clip duration"


def _transition(model: TimelineInspectionModel, clip: Any) -> dict[str, Any] | None:
    raw = clip.transition
    if raw is None:
        return None
    transition_id = raw.get("id", raw.get("type"))
    if not isinstance(transition_id, str) or not transition_id:
        raise ValueError(f"clip {clip.clip_id!r} transition id must be a non-empty string")
    accepted = clip.effective != clip.frames.as_seconds()
    if not accepted:
        return {
            "id": transition_id,
            "state": "ignored",
            "ignored_reason": _ignored_transition_reason(model, clip),
            "requested_duration_frames": _int_or_none(raw.get("durationFrames")),
            "requested_duration_seconds": _number_or_none(raw.get("duration")),
            "resolution_source": None,
            "resolved_duration_frames": None,
            "effective_interval": None,
        }
    resolved = _resolved_transition_frames(model, clip)
    if resolved is None:
        return {
            "id": transition_id,
            "state": "ignored",
            "ignored_reason": _ignored_transition_reason(model, clip),
            "requested_duration_frames": _int_or_none(raw.get("durationFrames")),
            "requested_duration_seconds": _number_or_none(raw.get("duration")),
            "resolution_source": None,
            "resolved_duration_frames": None,
            "effective_interval": None,
        }
    if raw.get("durationFrames") is not None:
        resolution_source = "explicit_frames"
    elif raw.get("duration") is not None:
        resolution_source = "explicit_seconds"
    else:
        resolution_source = "registry_default"
    effective = clip.effective
    return {
        "id": transition_id,
        "state": "accepted",
        "ignored_reason": None,
        "requested_duration_frames": _int_or_none(raw.get("durationFrames")),
        "requested_duration_seconds": _number_or_none(raw.get("duration")),
        "resolution_source": resolution_source,
        "resolved_duration_frames": resolved,
        "effective_interval": {
            "start_frame": _frame_round(effective.start, model.fps),
            "end_frame": _frame_round(effective.end, model.fps),
            "start_seconds": effective.start,
            "end_seconds": effective.end,
        },
    }


def _clips(
    model: TimelineInspectionModel,
    identity_map: Any,
    snapshot: TimelineSnapshot,
    scope: Scope | None = None,
    occurrences: list[SpeechOccurrence] | None = None,
    attachment: TranscriptAttachment | None = None,
) -> list[dict[str, Any]]:
    raw_by_id: dict[str, Mapping[str, Any]] = {}
    raw_clips = snapshot.assembly.get("clips", [])
    if isinstance(raw_clips, list):
        for raw in raw_clips:
            if isinstance(raw, Mapping):
                clip_id = raw.get("id")
                if isinstance(clip_id, str):
                    raw_by_id[clip_id] = raw
    clip_ids = set(scope.clip_ids) if scope is not None else None
    # Emphasis is serialized exactly when the scope declares a non-empty
    # active-stack set (timestamp/clip/asset/shot scopes).  Full timeline and
    # empty-emphasis scopes omit the field so unscoped emission stays stable.
    emphasized = (
        set(scope.emphasized_clip_ids) if scope is not None and scope.emphasized_clip_ids else None
    )
    result: list[dict[str, Any]] = []
    for clip in model.clips:
        if clip_ids is not None and clip.clip_id not in clip_ids:
            continue
        ref = _lookup_semantic(identity_map, "clip", clip.clip_id)
        if ref is None:
            raise ValueError(f"clip {clip.clip_id!r} has no display id in the identity map")
        parsed = parse_qualified_ref(ref)
        raw = raw_by_id.get(clip.clip_id, {})
        asset_refs = [
            asset_ref
            for asset_key in clip.asset_refs
            if (asset_ref := _lookup_semantic(identity_map, "asset", asset_key)) is not None
        ]
        entry: dict[str, Any] = {
            "stable_id": parsed.stable_id,
            "qualified_ref": ref,
            "canonical_ref": _canonical_ref(identity_map, ref),
            "track_authored_id": clip.track_id,
            "clip_type": clip.kind,
            "at_seconds": clip.authored.start,
            "start_frame": clip.frames.start_frame,
            "end_frame": clip.frames.end_frame,
            "mounted_interval": {
                "start_frame": clip.mounted.start_frame,
                "end_frame": clip.mounted.end_frame,
                "start_seconds": clip.mounted.start_frame / model.fps,
                "end_seconds": clip.mounted.end_frame / model.fps,
            },
            "source_bounds": _source_bounds(clip, raw),
            "speed": clip.speed,
            "transition": _transition(model, clip),
            "asset_refs": asset_refs,
            "authored_text": clip.authored_text,
            "pixel_text": (
                "not_inspected"
                if _track_kind(model, clip.track_id) == "visual" and clip.kind != "text"
                else None
            ),
            "mapped_speech": [
                (
                    _lookup_semantic(
                        identity_map,
                        "speech_occurrence",
                        speech_occurrence_authored_id(
                            attachment.transcript_sha256,
                            occurrence.segment_id,
                            occurrence.clip_id,
                        ),
                    )
                    if attachment is not None
                    else occurrence.occurrence_id
                )
                for occurrence in (occurrences or [])
                if occurrence.clip_id == clip.clip_id
            ],
        }
        if emphasized is not None:
            entry["emphasized"] = clip.clip_id in emphasized
        result.append(entry)
    return result


def _frozen_shots(
    model: TimelineInspectionModel,
    identity_map: Any,
) -> list[dict[str, Any]]:
    """Lossless pinned-shot facts needed by snapshot-only descendants."""

    result: list[dict[str, Any]] = []
    for shot in model.shots:
        ref = _lookup_semantic(identity_map, "shot", shot.shot_id)
        if ref is None:
            raise ValueError(f"shot {shot.shot_id!r} has no display id in the identity map")
        result.append(
            {
                "stable_id": parse_qualified_ref(ref).stable_id,
                "qualified_ref": ref,
                "canonical_ref": _canonical_ref(identity_map, ref),
                "member_clip_ids": list(shot.member_clip_ids),
                "authored_interval": (
                    {
                        "start_seconds": shot.authored.start,
                        "end_seconds": shot.authored.end,
                    }
                    if shot.authored is not None
                    else None
                ),
                "frame_interval": (
                    {
                        "start_frame": shot.frames.start_frame,
                        "end_frame": shot.frames.end_frame,
                    }
                    if shot.frames is not None
                    else None
                ),
                "warnings": list(shot.warnings),
            }
        )
    return result


def _frozen_ranges(
    model: TimelineInspectionModel,
    identity_map: Any,
    scope: Scope | None,
) -> list[dict[str, Any]]:
    """Serialize navigation ranges whose bounds are known in this lineage.

    An RG identity is allocated only for an explicit range scope.  Descendants
    preserve this list byte-for-byte, so the one allocation remains usable
    after navigating from that range into a clip or asset.
    """

    if (
        scope is None
        or scope.kind != "range"
        or not scope.ref
        or scope.start_frame is None
        or scope.end_frame is None
    ):
        return []
    ref = _lookup_semantic(identity_map, "range", scope.ref)
    if ref is None:
        identity = _lookup_display(identity_map, scope.ref)
        ref = scope.ref if identity is not None and identity[1] == "range" else None
    if ref is None:
        raise ValueError(f"range {scope.ref!r} has no display id in the identity map")
    identity = _lookup_display(identity_map, ref)
    if identity is None:
        raise ValueError(f"range ref {ref!r} has no semantic identity")
    return [
        {
            "stable_id": parse_qualified_ref(ref).stable_id,
            "qualified_ref": ref,
            "canonical_ref": _canonical_ref(identity_map, ref),
            "start_frame": scope.start_frame,
            "end_frame": scope.end_frame,
            "start_seconds": scope.start_frame / model.fps,
            "end_seconds": scope.end_frame / model.fps,
        }
    ]


def _schema_role(role: str) -> str:
    return role if role in _ASSET_ROLES else "timeline_media"


def _assets(
    model: TimelineInspectionModel,
    identity_map: Any,
    scope: Scope | None = None,
) -> list[dict[str, Any]]:
    wanted: set[str] | None = None
    if scope is not None:
        wanted = set()
        clip_ids = set(scope.clip_ids)
        for clip in model.clips:
            if clip.clip_id in clip_ids:
                wanted.update(clip.asset_refs)
    result: list[dict[str, Any]] = []
    for key in sorted(model.registry_keys):
        if wanted is not None and key not in wanted:
            continue
        ref = _lookup_semantic(identity_map, "asset", key)
        if ref is None:
            raise ValueError(f"asset {key!r} has no display id in the identity map")
        integrity = model.media_integrity[key]
        parsed = parse_qualified_ref(ref)
        result.append(
            {
                "stable_id": parsed.stable_id,
                "qualified_ref": ref,
                "canonical_ref": _canonical_ref(identity_map, ref),
                "role": _schema_role(integrity.role),
                "integrity_state": integrity.state,
            }
        )
    return result


def _objects(
    model: TimelineInspectionModel,
    identity_map: Any,
    scope: Scope | None = None,
) -> list[dict[str, Any]]:
    in_scope = _in_scope_refs(model, identity_map, scope)
    return [
        {
            "stable_id": parse_qualified_ref(ref).stable_id,
            "qualified_ref": ref,
            "canonical_ref": _canonical_ref(identity_map, ref),
        }
        for ref in _ordered_object_refs(model, identity_map)
        if ref in in_scope
    ]


def _timeline_entry(
    model: TimelineInspectionModel,
    identity_map: Any,
    snapshot: TimelineSnapshot,
    scope: Scope | None = None,
    attachment: TranscriptAttachment | None = None,
    occurrences: list[SpeechOccurrence] | None = None,
) -> dict[str, Any]:
    result = {
        "timeline_ref": _TIMELINE_REF,
        "durations": _durations(model),
        "tracks": _tracks(model, snapshot),
        "clips": _clips(model, identity_map, snapshot, scope, occurrences, attachment),
        "assets": _assets(model, identity_map, scope),
    }
    if attachment is not None:
        result["transcript_attachment"] = {
            "schema_version": attachment.schema_version,
            "source_id": attachment.source_id,
            "source_version": attachment.source_version,
            "transcript_sha256": attachment.transcript_sha256,
            "media_identity": attachment.media_identity,
            "media_sha256": attachment.media_sha256,
            "producer": attachment.producer,
            "producer_version": attachment.producer_version,
            "model": attachment.model,
            "integrity": attachment.integrity,
        }
    return result


def emit_ground_truth(
    model: TimelineInspectionModel,
    identity_map: Any,
    snapshot: TimelineSnapshot,
    scope: Scope | None = None,
    attachment: TranscriptAttachment | None = None,
    occurrences: list[SpeechOccurrence] | None = None,
) -> dict[str, Any]:
    """Return the ``ground-truth.json`` content for one root visualization.

    ``scope=None`` emits the full timeline exactly as before.  With a scope,
    the ``scope`` block narrows to the selected kind/ref/bounds, and ``objects``
    plus the timeline entry's ``clips``/``assets`` are filtered to the scoped
    set (clips from ``Scope.clip_ids`` and the assets they reference); the
    timeline entry itself always stays ``[0, composition)``.

    Emphasis: when the scope declares a non-empty ``emphasized_clip_ids`` set
    (timestamp/clip/asset/shot scopes), every emitted clip carries an
    ``emphasized`` boolean — true for the active stack / focused object, false
    for context clips.  Full timeline and empty-emphasis scopes omit the field.
    """
    if not isinstance(model, TimelineInspectionModel):
        raise TypeError("model must be a TimelineInspectionModel")
    if not isinstance(snapshot, TimelineSnapshot):
        raise TypeError("snapshot must be a TimelineSnapshot")
    effective, scope = _scope_effective(model, identity_map, scope)
    return {
        "schema_version": SCHEMA_VERSION,
        "snapshots": _snapshot_block(model, snapshot),
        "project_slug": snapshot.project_slug,
        "scope": _scope_block(model, effective, scope),
        "objects": _objects(model, effective, scope),
        "timelines": [_timeline_entry(model, effective, snapshot, scope, attachment, occurrences)],
        # These three fields are the frozen-lineage substrate.  The ordinary
        # objects/timelines fields remain scope-filtered for consumers, while
        # descendants reconstruct the complete root map and normalized model
        # exclusively from these hashed facts.  A child copies them verbatim.
        "frozen_objects": _objects(model, effective, None),
        "frozen_timeline": _timeline_entry(
            model, effective, snapshot, None, attachment, occurrences
        ),
        "frozen_shots": _frozen_shots(model, effective),
        "frozen_ranges": _frozen_ranges(model, effective, scope),
        "timestamps": {"frozen_at": FROZEN_AT_SENTINEL},
    }


# ---------------------------------------------------------------------------
# Metric definitions (R13): versioned machine artifact defining every metric.
# ---------------------------------------------------------------------------


#: Metric-definition entries.  Fixed and ordered so identical inputs produce
#: identical bytes; ``derivation`` names the duration.py / model.py function
#: the formula mirrors; ``scope`` is ``per-timeline`` (timeline-level extents
#: and fps) or ``per-scope`` (clip-level intervals emitted inside a scope's
#: clip set).
_METRIC_DEFINITIONS: tuple[dict[str, str], ...] = (
    {
        "id": "authored_visual_only_end_seconds",
        "name": "Authored visual-only end (seconds)",
        "definition": (
            "Latest authored end, in seconds, across every clip on a visual "
            "track: authored placement plus source duration.  No speed, no "
            "frame quantization."
        ),
        "formula": "max over visual-track clips of (authored.start + clip_source_duration(clip))",
        "derivation": "duration.py: clip_source_duration",
        "unit": "seconds",
        "scope": "per-timeline",
    },
    {
        "id": "frame_quantized_visual_end_frames",
        "name": "Frame-quantized visual end (frames)",
        "definition": (
            "Compositor frame-quantized end of the visual tracks: the maximum "
            "independently rounded clip end frame over visual-track clips."
        ),
        "formula": "max over visual-track clips of clip_end_frame(clip, fps)",
        "derivation": "duration.py: clip_end_frame (Math.round, one-frame floor)",
        "unit": "frames",
        "scope": "per-timeline",
    },
    {
        "id": "frame_quantized_visual_end_seconds",
        "name": "Frame-quantized visual end (seconds)",
        "definition": ("The frame-quantized visual end expressed in seconds: visual_frames / fps."),
        "formula": "frame_quantized_visual_end_frames / fps",
        "derivation": "duration.py: clip_end_frame / fps",
        "unit": "seconds",
        "scope": "per-timeline",
    },
    {
        "id": "all_track_composition_frames",
        "name": "All-track composition extent (frames)",
        "definition": (
            "All-track composition duration in frames with a one-frame floor; "
            "every clip counts, including audio and muted tracks."
        ),
        "formula": "timeline_duration_frames(assembly, fps)",
        "derivation": "duration.py: timeline_duration_frames",
        "unit": "frames",
        "scope": "per-timeline",
    },
    {
        "id": "all_track_composition_seconds",
        "name": "All-track composition extent (seconds)",
        "definition": ("The all-track composition extent in seconds: composition_frames / fps."),
        "formula": "timeline_duration_frames(assembly, fps) / fps",
        "derivation": "duration.py: timeline_duration_seconds",
        "unit": "seconds",
        "scope": "per-timeline",
    },
    {
        "id": "authored_all_track_composition_frames",
        "name": "Authored all-track composition extent (frames)",
        "definition": (
            "All-track authored composition duration in frames, before any "
            "declared render-only tail; equals the all-track composition "
            "extent when the timeline declares no tail."
        ),
        "formula": "timeline_duration_frames(assembly, fps)",
        "derivation": "duration.py: timeline_duration_frames",
        "unit": "frames",
        "scope": "per-timeline",
    },
    {
        "id": "authored_all_track_composition_seconds",
        "name": "Authored all-track composition extent (seconds)",
        "definition": (
            "The authored all-track composition extent in seconds: "
            "authored_all_track_composition_frames / fps."
        ),
        "formula": "timeline_duration_frames(assembly, fps) / fps",
        "derivation": "duration.py: timeline_duration_seconds",
        "unit": "seconds",
        "scope": "per-timeline",
    },
    {
        "id": "composition_extent",
        "name": "Composition extent (seconds)",
        "definition": (
            "The frame-quantized all-track composition extent in seconds; "
            "identical to all_track_composition_seconds.  This is the model "
            "extent every page ruler window subdivides."
        ),
        "formula": "timeline_duration_frames(assembly, fps) / fps",
        "derivation": "duration.py: timeline_duration_seconds",
        "unit": "seconds",
        "scope": "per-timeline",
    },
    {
        "id": "audible_extent",
        "name": "Audible extent (seconds)",
        "definition": (
            "Latest frame-quantized end across audio-track clips, in seconds "
            "(audible_frames / fps); the audible half of the composition "
            "extent."
        ),
        "formula": "max over audio-track clips of clip_end_frame(clip, fps) / fps",
        "derivation": "duration.py: clip_end_frame / fps",
        "unit": "seconds",
        "scope": "per-timeline",
    },
    {
        "id": "fps",
        "name": "Timeline frame rate",
        "definition": (
            "Timeline canvas frame rate; the compositor's frame-quantization "
            "divisor for every frame/second conversion in this pack."
        ),
        "formula": "assembly.theme_overrides.visual.canvas.fps (default 30)",
        "derivation": "model.py: _timeline_fps",
        "unit": "frames_per_second",
        "scope": "per-timeline",
    },
    {
        "id": "clip_authored_interval",
        "name": "Clip authored interval (seconds)",
        "definition": (
            "Authored placement interval [at, at + source_duration) in "
            "seconds; no speed applied and no frame quantization."
        ),
        "formula": "[clip.at, clip.at + clip_source_duration(clip))",
        "derivation": "duration.py: clip_source_duration",
        "unit": "seconds",
        "scope": "per-scope",
    },
    {
        "id": "clip_frame_interval",
        "name": "Clip frame interval (frames)",
        "definition": (
            "Independently rounded compositor Sequence interval: "
            "Math.round(at * fps) to rounded start plus a minimum-one-frame "
            "rounded duration."
        ),
        "formula": "[clip_start_frame(clip, fps), clip_end_frame(clip, fps))",
        "derivation": "duration.py: clip_start_frame, clip_end_frame",
        "unit": "frames",
        "scope": "per-scope",
    },
    {
        "id": "clip_mounted_interval",
        "name": "Clip mounted interval (frames)",
        "definition": (
            "Sequence interval actually mounted by the compositor after "
            "transition-group scheduling (TimelineComposition.tsx:208-237); "
            "equals the frame interval when no transition is scheduled."
        ),
        "formula": "transition_mounted_intervals(model)[clip_id]",
        "derivation": "model.py: transition_mounted_intervals",
        "unit": "frames",
        "scope": "per-scope",
    },
    {
        "id": "clip_effective_interval",
        "name": "Clip effective interval (seconds)",
        "definition": (
            "Non-transition presentation interval after v0.0.6 transition "
            "grouping, retiming, and composition clipping; the transition-"
            "clipped interval a viewer actually sees."
        ),
        "formula": "transition_effective_intervals(model)[clip_id]",
        "derivation": "model.py: transition_effective_intervals",
        "unit": "seconds",
        "scope": "per-scope",
    },
    {
        "id": "transition_resolved_duration_frames",
        "name": "Resolved transition duration (frames)",
        "definition": (
            "Resolved duration in frames for an accepted transition: explicit "
            "durationFrames, else explicit seconds rounded at fps, else the "
            "registered default, else the 12-frame hard fallback; bounded by "
            "both clip durations (else ignored)."
        ),
        "formula": "resolve_transition_duration_frames(transition, from_frames, to_frames, default, fps=fps)",
        "derivation": "duration.py: resolve_transition_duration_frames",
        "unit": "frames",
        "scope": "per-scope",
    },
    {
        "id": "clip_source_duration_seconds",
        "name": "Clip source duration (seconds)",
        "definition": (
            "Source duration in seconds: a numeric hold wins unconditionally; "
            "otherwise (to ?? 0) - (from ?? 0)."
        ),
        "formula": "clip_source_duration(clip)",
        "derivation": "duration.py: clip_source_duration",
        "unit": "seconds",
        "scope": "per-scope",
    },
)

#: Version of the metric-definitions artifact (independent of SCHEMA_VERSION).
METRIC_DEFINITIONS_VERSION = 1

#: Pack-relative artifact name (sibling of pack-hashes.json coverage).
METRIC_DEFINITIONS_NAME = "metric-definitions.json"


def emit_metric_definitions(
    model: TimelineInspectionModel,
    identity_map: Any,
    snapshot: TimelineSnapshot,
) -> dict[str, Any]:
    """Return the versioned ``metric-definitions.json`` machine artifact.

    Every metric name ground truth stores a value for is defined here: id,
    name, precise prose definition, formula/derivation (the ``duration.py`` /
    ``model.py`` function it mirrors), unit (frames / seconds /
    frames_per_second), and scope (per-timeline / per-scope).  The block is
    versioned (``schema_version`` 1) and notes the compositor version it
    mirrors (0.0.6).  Deterministic: a fixed, ordered tuple — no wall clock,
    no inputs beyond the model identity.
    """

    return {
        "schema_version": METRIC_DEFINITIONS_VERSION,
        "kind": "timeline_visualize_metric_definitions",
        "compositor_version": COMPOSITOR_VERSION,
        "metrics": [dict(entry) for entry in _METRIC_DEFINITIONS],
    }


# ---------------------------------------------------------------------------
# Action index.
# ---------------------------------------------------------------------------


def _clip_with_ref(model: TimelineInspectionModel, identity_map: Any, ref: str) -> Any:
    for clip in model.clips:
        if _lookup_semantic(identity_map, "clip", clip.clip_id) == ref:
            return clip
    raise ValueError(f"clip ref {ref!r} is not present in the model")


def _relations(
    model: TimelineInspectionModel,
    identity_map: Any,
    ref: str,
    in_scope: set[str] | None = None,
    attachment: TranscriptAttachment | None = None,
    occurrences: list[SpeechOccurrence] | None = None,
) -> dict[str, Any]:
    parsed = parse_qualified_ref(ref)
    all_refs = _ordered_object_refs(model, identity_map)
    if parsed.kind == "TL":
        # Transcript source segments and mapped speech occurrences form their
        # own evidence namespace.  They are not timeline children: TS is the
        # root of each TS -> SP evidence subtree, while CL <-> SP semantic
        # linkage lives in ground-truth/transcript-index relations.
        children = [
            child
            for child in all_refs[1:]
            if parse_qualified_ref(child).kind in {"SH", "RG", "CL", "AS"}
        ]
        if in_scope is not None:
            children = [child for child in children if child in in_scope]
        return {
            "parent": None,
            "previous": None,
            "next": None,
            "children": children,
        }
    previous: str | None = None
    next_ref: str | None = None
    if parsed.kind == "CL":
        clip = _clip_with_ref(model, identity_map, ref)
        same_track = [c for c in model.clips if c.track_id == clip.track_id]
        index = same_track.index(clip)
        if index > 0:
            previous = _lookup_semantic(identity_map, "clip", same_track[index - 1].clip_id)
        if index + 1 < len(same_track):
            next_ref = _lookup_semantic(identity_map, "clip", same_track[index + 1].clip_id)
    children: list[str] = []
    parent = _TIMELINE_REF
    if attachment is not None:
        transcript_hash = attachment.transcript_sha256
        if parsed.kind == "TS":
            identity = _lookup_display(identity_map, ref)
            authored_id = identity[2] if identity is not None else ""
            segment_id = next(
                (
                    occurrence.segment_id
                    for occurrence in (occurrences or [])
                    if transcript_segment_authored_id(transcript_hash, occurrence.segment_id)
                    == authored_id
                ),
                None,
            )
            children = [
                sp_ref
                for occurrence in (occurrences or [])
                if segment_id is not None
                and occurrence.segment_id == segment_id
                and (
                    sp_ref := _lookup_semantic(
                        identity_map,
                        "speech_occurrence",
                        speech_occurrence_authored_id(
                            transcript_hash, occurrence.segment_id, occurrence.clip_id
                        ),
                    )
                )
                is not None
            ]
            # TS is a top-level node in the separate text-evidence namespace,
            # not a child of TL or the media asset it describes.
            parent = None
        elif parsed.kind == "SP":
            identity = _lookup_display(identity_map, ref)
            authored_id = identity[2] if identity is not None else ""
            matching = next(
                (
                    occurrence
                    for occurrence in (occurrences or [])
                    if speech_occurrence_authored_id(
                        transcript_hash, occurrence.segment_id, occurrence.clip_id
                    )
                    == authored_id
                ),
                None,
            )
            if matching is not None:
                parent = (
                    _lookup_semantic(
                        identity_map,
                        "transcript_source_segment",
                        transcript_segment_authored_id(transcript_hash, matching.segment_id),
                    )
                    or _TIMELINE_REF
                )
                # The reverse SP -> CL semantic relation is emitted as
                # transcript-index.clip_ref, not as a hierarchy edge.
                children = []
    if in_scope is not None:
        if parent is not None and parent not in in_scope:
            parent = None
        if previous is not None and previous not in in_scope:
            previous = None
        if next_ref is not None and next_ref not in in_scope:
            next_ref = None
        children = [child for child in children if child in in_scope]
    return {
        "parent": parent,
        "previous": previous,
        "next": next_ref,
        "children": children,
    }


def _focus_context_action(
    project_slug: str,
    manifest_path: str,
    ref: str,
    scope_kind: str,
) -> dict[str, Any]:
    return {
        "kind": "visualize",
        "argv": [
            "python3",
            "-m",
            "astrid",
            "timelines",
            "visualize",
            "--view",
            "structure",
            "--project",
            project_slug,
            "--from-view",
            manifest_path,
            "--focus",
            ref,
            "--context",
            FOCUS_CONTEXT_SECONDS,
        ],
        "focus": ref,
        "result_scope": scope_kind,
        "available": True,
        "unavailable_reason": None,
        "reads": "snapshot",
    }


def _timestamp_locator(seconds: float, timeline_ref: str) -> str:
    total_ms = int(round(seconds * 1000))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    secs, millis = divmod(remainder, 1_000)
    return f"{timeline_ref}@{hours:02d}:{minutes:02d}:{secs:02d}.{millis:03d}"


def _focus_timestamp_action(
    model: TimelineInspectionModel,
    project_slug: str,
    manifest_path: str,
    ref: str,
) -> dict[str, Any]:
    locator = _timestamp_locator(model.extents.composition_seconds / 2.0, ref)
    return {
        "kind": "visualize",
        "argv": [
            "python3",
            "-m",
            "astrid",
            "timelines",
            "visualize",
            "--view",
            "structure",
            "--project",
            project_slug,
            "--from-view",
            manifest_path,
            "--focus",
            locator,
            "--context",
            TIMESTAMP_CONTEXT_SECONDS,
        ],
        "focus": locator,
        "result_scope": "timestamp",
        "available": True,
        "unavailable_reason": None,
        "reads": "snapshot",
    }


def _refresh_root_action(project_slug: str, manifest_path: str) -> dict[str, Any]:
    return {
        "kind": "visualize",
        "argv": [
            "python3",
            "-m",
            "astrid",
            "timelines",
            "visualize",
            "--view",
            "structure",
            "--project",
            project_slug,
            "--from-view",
            manifest_path,
            "--focus",
            _TIMELINE_REF,
            "--refresh-root",
        ],
        "focus": _TIMELINE_REF,
        "result_scope": "timeline",
        "available": True,
        "unavailable_reason": None,
        "reads": "current",
    }


def _inspect_unavailable_reason(state: str) -> str:
    reasons = {
        "missing": (
            "missing — asset file is missing from the frozen project sources; "
            "restore or re-verify the source, then refresh the root"
        ),
        "hash_unrecorded": (
            "hash_unrecorded — no expected sha256 is recorded for this asset; "
            "original inspection is refused until a hash is recorded and the "
            "root is refreshed"
        ),
        "hash_mismatch": (
            "hash_mismatch — observed content hash differs from the recorded "
            "hash; original inspection is refused"
        ),
        "remote": "remote — asset is a remote reference; offline inspection never fetches",
        "thumbnail_only": (
            "thumbnail_only — asset is thumbnail-only; there is no original to inspect"
        ),
        "unsupported": (
            "unsupported — asset is not a runtime-managed object admitted for "
            "this project"
        ),
    }
    return reasons.get(
        state, f"unavailable — asset integrity state {state!r} blocks original inspection"
    )


def _inspect_original_action(
    model: TimelineInspectionModel,
    identity_map: Any,
    project_slug: str,
    manifest_path: str,
    ref: str,
) -> dict[str, Any]:
    identity = _lookup_display(identity_map, ref)
    if identity is None:
        raise ValueError(f"asset ref {ref!r} has no semantic identity")
    integrity = model.media_integrity[identity[2]]
    available = integrity.state == "verified_original"
    return {
        "kind": "inspect_media",
        "argv": [
            "python3",
            "-m",
            "astrid",
            "timelines",
            "visualize",
            "--view",
            "structure",
            "--project",
            project_slug,
            "--from-view",
            manifest_path,
            "--focus",
            ref,
        ],
        "focus": ref,
        "result_scope": "asset",
        "available": available,
        "unavailable_reason": None if available else _inspect_unavailable_reason(integrity.state),
        "reads": "snapshot",
    }


def _actions(
    model: TimelineInspectionModel,
    identity_map: Any,
    snapshot: TimelineSnapshot,
    manifest_path: str,
    ref: str,
    attachment: TranscriptAttachment | None = None,
    occurrences: list[SpeechOccurrence] | None = None,
) -> dict[str, dict[str, Any]]:
    parsed = parse_qualified_ref(ref)
    actions: dict[str, dict[str, Any]] = {}
    if parsed.kind == "TL":
        actions["focus_timestamp"] = _focus_timestamp_action(
            model, snapshot.project_slug, manifest_path, ref
        )
        actions["refresh_root"] = _refresh_root_action(snapshot.project_slug, manifest_path)
    elif parsed.kind == "CL":
        actions["focus_context"] = _focus_context_action(
            snapshot.project_slug, manifest_path, ref, "clip"
        )
    elif parsed.kind == "SH":
        actions["focus_context"] = _focus_context_action(
            snapshot.project_slug, manifest_path, ref, "shot"
        )
    elif parsed.kind == "RG":
        actions["focus_context"] = _focus_context_action(
            snapshot.project_slug, manifest_path, ref, "range"
        )
    elif parsed.kind == "AS":
        actions["focus_context"] = _focus_context_action(
            snapshot.project_slug, manifest_path, ref, "asset"
        )
        actions["inspect_original"] = _inspect_original_action(
            model, identity_map, snapshot.project_slug, manifest_path, ref
        )
    elif parsed.kind == "TS":
        actions["focus_occurrences"] = _focus_context_action(
            snapshot.project_slug, manifest_path, ref, "text"
        )
    elif parsed.kind == "SP":
        actions["focus_clip_context"] = _focus_context_action(
            snapshot.project_slug, manifest_path, ref, "speech"
        )
    return actions


def emit_action_index(
    model: TimelineInspectionModel,
    identity_map: Any,
    snapshot: TimelineSnapshot,
    manifest_path: str | Path,
    scope: Scope | None = None,
    attachment: TranscriptAttachment | None = None,
    occurrences: list[SpeechOccurrence] | None = None,
) -> dict[str, Any]:
    """Return the ``action-index.json`` content for one root visualization.

    ``scope=None`` indexes every object.  With a scope only the scoped objects
    are indexed (the timeline, in-scope clips, their assets, and the scope's
    own object), while parent/previous/next relations still resolve — targets
    outside the scope are reported as ``None`` so no relation dangles.
    ``refresh_root`` on ``TL01`` always remains.
    """
    manifest = str(Path(manifest_path))
    effective, scope = _scope_effective(model, identity_map, scope)
    in_scope = _in_scope_refs(model, effective, scope) if scope is not None else None
    entries: dict[str, dict[str, Any]] = {}
    for ref in _ordered_object_refs(model, effective):
        if in_scope is not None and ref not in in_scope:
            continue
        entries[ref] = {
            "canonical_ref": _canonical_ref(effective, ref),
            "relations": _relations(model, effective, ref, in_scope, attachment, occurrences),
            "actions": _actions(model, effective, snapshot, manifest, ref, attachment, occurrences),
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "snapshots": _snapshot_block(model, snapshot),
        "entries": entries,
    }


# ---------------------------------------------------------------------------
# Asset index.
# ---------------------------------------------------------------------------


def emit_asset_index(
    model: TimelineInspectionModel,
    identity_map: Any,
    snapshot: TimelineSnapshot,
) -> dict[str, Any]:
    """Return the ``asset-index.json`` content for one root visualization."""
    registry_assets = snapshot.registry.get("assets", snapshot.registry)
    assets: list[dict[str, Any]] = []
    for key in sorted(model.registry_keys):
        ref = _lookup_semantic(identity_map, "asset", key)
        if ref is None:
            raise ValueError(f"asset {key!r} has no display id in the identity map")
        integrity = model.media_integrity[key]
        parsed = parse_qualified_ref(ref)
        media_type: str | None = None
        raw_entry = registry_assets.get(key) if isinstance(registry_assets, Mapping) else None
        raw_type = raw_entry.get("type") if isinstance(raw_entry, Mapping) else None
        if isinstance(raw_type, str):
            candidate = raw_type.strip().lower().split("/", 1)[0]
            if candidate in {"image", "video", "audio"}:
                media_type = candidate
        assets.append(
            {
                "stable_id": parsed.stable_id,
                "qualified_ref": ref,
                "canonical_ref": _canonical_ref(identity_map, ref),
                "source_id": integrity.source_id,
                "source_version": integrity.source_version,
                "role": _schema_role(integrity.role),
                "media_type": media_type,
                "integrity_state": integrity.state,
                "expected_sha256": integrity.expected_sha256,
                "observed_sha256": integrity.observed_sha256,
                # Runtime-managed media is not represented by a local source
                # locator. Keep the schema-required provenance slot explicit
                # and null rather than leaking an ambient filesystem path.
                "contained_path": None,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "snapshots": _snapshot_block(model, snapshot),
        "assets": assets,
    }


# ---------------------------------------------------------------------------
# Transcript index (empty-valid).
# ---------------------------------------------------------------------------


def emit_transcript_index(
    model: TimelineInspectionModel,
    identity_map: Any,
    snapshot: TimelineSnapshot,
    attachment: TranscriptAttachment | None = None,
    segments: list[TranscriptSegment] | None = None,
    occurrences: list[SpeechOccurrence] | None = None,
    asset_key: str | None = None,
) -> dict[str, Any]:
    """Return hash-scoped TS records and occurrence-specific SP records.

    Missing/non-verifiable attachment state remains the empty-valid shape;
    transcript content is never fabricated or substituted.
    """
    normalized_segments = list(segments or [])
    normalized_occurrences = list(occurrences or [])
    if attachment is None or attachment.integrity != "ok":
        normalized_segments = []
        normalized_occurrences = []
    segment_by_id = {item.segment_id: item for item in normalized_segments}
    resolved_asset_key = asset_key
    if resolved_asset_key is None and normalized_occurrences:
        resolved_asset_key = normalized_occurrences[0].asset_key
    if resolved_asset_key is None and attachment is not None:
        if attachment.media_identity in model.registry_keys:
            resolved_asset_key = attachment.media_identity
        else:
            matches = sorted(
                key
                for key, integrity in model.media_integrity.items()
                if integrity.source_id == attachment.media_identity
            )
            if len(matches) == 1:
                resolved_asset_key = matches[0]
    asset_ref = (
        _lookup_semantic(identity_map, "asset", resolved_asset_key)
        if resolved_asset_key is not None
        else None
    )
    if (normalized_segments or normalized_occurrences) and asset_ref is None:
        raise ValueError("transcript attachment media has no frozen asset ref")

    source_rows: list[dict[str, Any]] = []
    for segment in normalized_segments:
        assert attachment is not None and asset_ref is not None
        authored_id = transcript_segment_authored_id(
            attachment.transcript_sha256, segment.segment_id
        )
        ref = _lookup_semantic(identity_map, "transcript_source_segment", authored_id)
        if ref is None:
            raise ValueError(f"transcript segment {segment.segment_id!r} has no TS id")
        source_rows.append(
            {
                "stable_id": parse_qualified_ref(ref).stable_id,
                "qualified_ref": ref,
                "canonical_ref": _canonical_ref(identity_map, ref),
                "asset_ref": asset_ref,
                "transcript_sha256": attachment.transcript_sha256,
                "source_segment_id": segment.segment_id,
                "source_interval": {
                    "start_seconds": segment.source_start,
                    "end_seconds": segment.source_end,
                },
                "speaker_state": segment.speaker_state,
                "speaker": segment.speaker,
                "text": segment.text,
                "word_timing": ("available" if segment.word_timing is not None else "unavailable"),
                "words": (
                    [
                        {
                            "start_seconds": start,
                            "end_seconds": end,
                            "text": text,
                        }
                        for start, end, text in segment.word_timing
                    ]
                    if segment.word_timing is not None
                    else None
                ),
            }
        )

    occurrence_rows: list[dict[str, Any]] = []
    for occurrence in normalized_occurrences:
        assert attachment is not None and asset_ref is not None
        segment = segment_by_id.get(occurrence.segment_id)
        if segment is None:
            raise ValueError(
                f"speech occurrence references unknown segment {occurrence.segment_id!r}"
            )
        source_authored_id = transcript_segment_authored_id(
            attachment.transcript_sha256, occurrence.segment_id
        )
        occurrence_authored_id = speech_occurrence_authored_id(
            attachment.transcript_sha256, occurrence.segment_id, occurrence.clip_id
        )
        source_ref = _lookup_semantic(identity_map, "transcript_source_segment", source_authored_id)
        occurrence_ref = _lookup_semantic(identity_map, "speech_occurrence", occurrence_authored_id)
        clip_ref = _lookup_semantic(identity_map, "clip", occurrence.clip_id)
        if None in (source_ref, occurrence_ref, clip_ref):
            raise ValueError("speech occurrence TS/SP/CL identity is incomplete")

        def mapping(state: str, start: float | None, end: float | None) -> dict[str, Any]:
            if start is None or end is None:
                return {"state": "unavailable", "interval": None}
            return {
                "state": state,
                "interval": {
                    "start_frame": _frame_round(start, model.fps),
                    "end_frame": _frame_round(end, model.fps),
                    "start_seconds": start,
                    "end_seconds": end,
                },
            }

        occurrence_rows.append(
            {
                "stable_id": parse_qualified_ref(occurrence_ref).stable_id,
                "qualified_ref": occurrence_ref,
                "canonical_ref": _canonical_ref(identity_map, occurrence_ref),
                "source_ref": source_ref,
                "clip_ref": clip_ref,
                "asset_ref": asset_ref,
                "authored_mapping": mapping(
                    occurrence.mapping_state,
                    occurrence.timeline_start,
                    occurrence.timeline_end,
                ),
                "effective_mapping": mapping(
                    occurrence.effective_state,
                    occurrence.effective_start,
                    occurrence.effective_end,
                ),
                "speaker_state": segment.speaker_state,
                "speaker": segment.speaker,
                "text": segment.text,
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "snapshots": _snapshot_block(model, snapshot),
        "sources": source_rows,
        "speech_occurrences": occurrence_rows,
    }


# ---------------------------------------------------------------------------
# Diagnostics.
# ---------------------------------------------------------------------------


def _split_diagnostic(raw: str) -> tuple[str, str]:
    match = _DIAGNOSTIC_CODE_RE.match(raw)
    if match is not None:
        return match.group(1), match.group(2)
    return "SNAPSHOT_DIAGNOSTIC", raw


def emit_diagnostics(
    model: TimelineInspectionModel,
    identity_map: Any,
    snapshot: TimelineSnapshot,
    scope: Scope | None = None,
) -> dict[str, Any]:
    """Return ``diagnostics.json``: every model/snapshot warning.

    ``scope=None`` reports every warning.  With a scope, clip/shot/asset
    diagnostics are narrowed to the scoped objects (so every ``object_ref``
    still resolves through the scoped ground truth) and the scope's own
    warnings are appended with deterministic codes (e.g. ``CLIP_RANGE_CLIPPED``
    for a range clipped to the composition bounds).
    """
    effective, scope = _scope_effective(model, identity_map, scope)
    in_scope = _in_scope_refs(model, effective, scope) if scope is not None else None
    diagnostics: list[dict[str, Any]] = []

    for raw in snapshot.diagnostics:
        code, message = _split_diagnostic(raw)
        if not message:
            message = raw
        diagnostics.append(
            {
                "severity": "warning",
                "code": code,
                "message": _sanitize_diagnostic_message(message),
                "object_ref": _TIMELINE_REF,
            }
        )

    for key in sorted(model.registry_keys):
        integrity = model.media_integrity[key]
        code = _ASSET_STATE_CODES.get(integrity.state)
        if code is None:
            continue
        asset_ref = _lookup_semantic(effective, "asset", key)
        if in_scope is not None and asset_ref not in in_scope:
            continue
        diagnostics.append(
            {
                "severity": "warning",
                "code": code,
                "message": _sanitize_diagnostic_message(integrity.reason),
                "object_ref": asset_ref,
            }
        )

    for shot in model.shots:
        shot_ref = _lookup_semantic(effective, "shot", shot.shot_id)
        if in_scope is not None and shot_ref not in in_scope:
            continue
        for warning in shot.warnings:
            diagnostics.append(
                {
                    "severity": "warning",
                    "code": "SHOT_MISSING_CLIP",
                    "message": _sanitize_diagnostic_message(warning),
                    "object_ref": shot_ref,
                }
            )

    if not isinstance(snapshot.assembly.get("pinnedShotGroups"), list):
        diagnostics.append(
            {
                "severity": "warning",
                "code": "SHOT_GROUPS_ABSENT",
                "message": "timeline has no pinnedShotGroups; shot scope is unavailable",
                "object_ref": _TIMELINE_REF,
            }
        )

    for clip in model.clips:
        clip_ref = _lookup_semantic(effective, "clip", clip.clip_id)
        if in_scope is not None and clip_ref not in in_scope:
            continue
        for asset_key in clip.asset_refs:
            if asset_key not in model.registry_keys:
                diagnostics.append(
                    {
                        "severity": "warning",
                        "code": "CLIP_ASSET_UNRESOLVED",
                        "message": _sanitize_diagnostic_message(
                            f"clip references asset {asset_key!r} that is absent "
                            "from the frozen registry"
                        ),
                        "object_ref": clip_ref,
                    }
                )
        if clip.transition is not None and clip.effective == clip.frames.as_seconds():
            transition_id = clip.transition.get("id", clip.transition.get("type"))
            diagnostics.append(
                {
                    "severity": "warning",
                    "code": "TRANSITION_IGNORED",
                    "message": _sanitize_diagnostic_message(
                        f"transition {transition_id!r} on clip {clip.clip_id!r} was not scheduled"
                    ),
                    "object_ref": clip_ref,
                }
            )

    if scope is not None:
        for warning in scope.warnings:
            diagnostics.append(
                {
                    "severity": "warning",
                    "code": _scope_warning_code(warning),
                    "message": _sanitize_diagnostic_message(warning),
                    "object_ref": _scope_warning_ref(model, effective, scope),
                }
            )

    return {
        "schema_version": SCHEMA_VERSION,
        "snapshots": _snapshot_block(model, snapshot),
        "diagnostics": diagnostics,
    }


# ---------------------------------------------------------------------------
# Markdown companions.
# ---------------------------------------------------------------------------

_READING_GUIDE = """# Reading this evidence pack

This pack explains one frozen timeline snapshot. Every object in it — the
timeline itself, shots, clips, and assets — carries a *qualified id* such as
`TL01` or `TL01.CL03`: a timeline ordinal, then a kind code and a stable
ordinal.

Card labels print the **stable ordinal only** (`CL03`) so every page reads
consistently; the qualified id (`TL01.CL03`) is always available in the cue
line, `ground-truth.json`, and `action-index.json`.

## The one rule

Read a qualified id, then open `action-index.json` and use its entry. That
entry lists the object's canonical identity, its neighbors, and executable
`argv` arrays that reproduce the next visualization. Run the `argv` array
exactly as written; it is the execution contract, not a display string.

## Cues in images

Pages print compact cues only, never shell commands. Every page carries one
chrome line:

```text
FOCUS {id|none} · PARENT {id} · SOURCE {id|none} · {role|none} · {state|none} · TEXT {id|none} · SPEAKER {name|none} [· SP @ {start}s–{end}s]
```

- `FOCUS` is the qualified id to look up next (the next action's target:
  `focus_context` for a clip, `inspect_original` for an asset, the parent
  scope when returning). A value of `none` means the page directs no further
  drill-down. Example: `FOCUS TL01.AS03 · PARENT TL01 · SOURCE TL01.AS03 ·
  timeline_media · verified_original · TEXT none · SPEAKER none` is the cue
  of a focused clip whose next step is inspecting its exact original.
- `PARENT` is the breadcrumb parent of the focused object — the timeline
  (`TL01`) for timeline/range/shot/timestamp/clip scopes, the parent clip
  for asset scopes, the source segment for `SP` scopes. It is printed
  explicitly so it is never confused with `FOCUS`.
- `SOURCE` is the source card: the asset id, its role (`timeline_media`,
  `rendered_sample`, `thumbnail_only`, …), and its integrity state
  (`verified_original` means an exact original; every other state is derived,
  missing, or unavailable). `SOURCE none` means the page has no single source
  card.
- `TEXT` is the text-evidence id (`TS…` transcript source segment or `SP…`
  speech occurrence) bound to the focused object, or `none`.
- `SPEAKER` is the mapped segment's speaker name, its speaker state
  (`legacy_unavailable`), or `none`.
- `SP @ {start}s–{end}s` (present only when a speech occurrence is in scope)
  is the occurrence's exact timeline window in seconds, three decimals, e.g.
  `SP @ 9.083s–10.083s`. Timing answers must be read from this token, never
  estimated from ruler spacing.
- `FOCUS CLIP {start}–{end}fr · {start}s→{end}s` (present on clip/timestamp/
  range/shot scopes) is the focused clip's exact frame window and seconds.
  On a full-timeline page the clip rectangle may be too narrow to carry its
  own label; this token is the authoritative window.
- `RANGE ◀ {n} clips · {t}s ▶ {m} clips · {t}s` (present on clip/timestamp/
  range/shot scopes, absent on full-timeline pages) is the directional
  context: how many clips and how many seconds exist BEFORE (◀) and AFTER
  (▶) the focused clip.  Orients spatial jumps without scanning.
- Gap/overlap markers print both boundary clip ids, e.g.
  `1fr gap TL01.CL02→TL01.CL03`, so a join is never ambiguous.

The same ids appear in `ground-truth.json` so every visual claim can be
checked exactly. Lane bands print the active tracks (`lane {n} · label ·
kind`), clip rectangles print their frame intervals, and the three text
lanes distinguish `SPEECH`, `CAPTION`, and `OTHER TEXT · not_inspected`
(pixel-baked text with no recorded OCR evidence).

## Actions and scopes

- `focus_context` visualizes an object with a small time context window.
- `inspect_original` opens the verified original media file for an asset.
- `focus_timestamp` jumps to one instant inside the frozen snapshot.
- `refresh_root` repeats the original root query against *current* state and
  creates a new lineage; the old lineage never changes.

Each action declares `available` and, when unavailable, one deterministic
`unavailable_reason`. Never fall back to a similarly named timeline, a newer
clip, or a different asset: re-run the root query instead.

## Snapshot safety

Every artifact is bound to one source-normalized-snapshot digest (`SNS:…`)
and one event-head version. Drill-down never silently reads a newer timeline;
if current state differs, only `refresh_root` crosses that boundary.

## Metrics

Every metric name in `ground-truth.json` is defined in the sibling machine
artifact `metric-definitions.json` (schema_version 1, mirroring compositor
0.0.6): each entry carries an id, name, precise definition, formula/derivation
(the `duration.py`/`model.py` function it mirrors), unit (frames, seconds, or
frames per second), and scope (`per-timeline` vs `per-scope`). Read a value,
then look up its definition before comparing it across packs; the definitions
block is versioned, so a schema bump is explicit, never silent.
"""


def emit_reading_guide(
    model: TimelineInspectionModel,
    identity_map: Any,
    snapshot: TimelineSnapshot,
) -> str:
    """Return the generic ``reading-guide.md`` content (deterministic prose)."""
    return _READING_GUIDE


def _transition_default_fingerprint() -> str:
    payload = canonical_json_bytes(
        {
            "compositor_version": COMPOSITOR_VERSION,
            "transition_default_frames": TRANSITION_FALLBACK_FRAMES,
            "transition_registry_defaults": dict(sorted(_PINNED_TRANSITION_DEFAULTS.items())),
        }
    )
    return sha256_bytes(payload)


def emit_structure_md(
    model: TimelineInspectionModel,
    identity_map: Any,
    snapshot: TimelineSnapshot,
    attachment: TranscriptAttachment | None = None,
    segments: list[TranscriptSegment] | None = None,
    occurrences: list[SpeechOccurrence] | None = None,
) -> str:
    """Return the factual ``structure.md`` content (breadcrumb + next actions)."""
    authored_frames = (
        model.extents.authored_composition_frames
        if model.extents.authored_composition_frames is not None
        else model.extents.composition_frames
    )
    authored_seconds = (
        model.extents.authored_composition_seconds
        if model.extents.authored_composition_seconds is not None
        else model.extents.composition_seconds
    )
    lines: list[str] = [
        "# Structure",
        "",
        f"SNAPSHOT · TL01 v{snapshot.head_version} · {model.snapshot_sns}",
        "PROJECT > TL01",
        "",
        "## Frozen facts",
        "",
        f"- timeline uuid: {model.timeline_uuid}",
        f"- timeline ulid: {model.timeline_ulid}",
        f"- timeline slug: {model.slug if model.slug else model.timeline_ulid.lower()}",
        f"- event head: version {snapshot.head_version}, "
        f"last_event_id {snapshot.last_event_id}, last_hash {snapshot.last_hash}",
        f"- fps: {model.fps}",
        f"- composition extent: {model.extents.composition_frames} frames / "
        f"{model.extents.composition_seconds:g} seconds",
        f"- authored composition extent: {authored_frames} frames / "
        f"{authored_seconds:g} seconds",
        f"- visual extent: {model.extents.visual_frames} frames / "
        f"{model.extents.visual_seconds:g} seconds",
        f"- audible extent: {model.extents.audible_frames} frames / "
        f"{model.extents.audible_frames / model.fps:g} seconds",
        f"- tracks: {len(model.tracks)} ({len([t for t in model.tracks if t.kind == 'visual'])} visual, "
        f"{len([t for t in model.tracks if t.kind == 'audio'])} audio)",
        f"- clips: {len(model.clips)}",
        f"- assets: {len(model.registry_keys)}",
        "",
        "## Compositor fingerprint",
        "",
        f"- compositor_version: {model.compositor_version}",
        f"- transition_default_frames: {model.transition_default_frames}",
        "- transition_registry_defaults: "
        + json.dumps(dict(sorted(_PINNED_TRANSITION_DEFAULTS.items())), sort_keys=True),
        f"- transition_default_fingerprint: {_transition_default_fingerprint()}",
    ]
    if attachment is not None:
        lines.extend(
            [
                "",
                "## Transcript attachment",
                "",
                f"- source: {attachment.source_id}@{attachment.source_version}",
                f"- transcript_sha256: {attachment.transcript_sha256}",
                f"- media_identity: {attachment.media_identity}",
                f"- media_sha256: {attachment.media_sha256 or 'unavailable'}",
                f"- producer: {attachment.producer}",
                f"- model: {attachment.model or 'unavailable'}",
                f"- integrity: {attachment.integrity}",
            ]
        )
    lines.extend(["", "## Text evidence", ""])
    segment_by_id = {item.segment_id: item for item in (segments or [])}
    if not segments:
        lines.append("- SPEECH: unavailable (no verified transcript attachment)")
    else:
        for segment in segments:
            assert attachment is not None
            ref = _lookup_semantic(
                identity_map,
                "transcript_source_segment",
                transcript_segment_authored_id(attachment.transcript_sha256, segment.segment_id),
            )
            lines.append(
                f"- SPEECH SOURCE {ref}: [{segment.source_start:g},{segment.source_end:g}) "
                f"speaker={segment.speaker if segment.speaker is not None else segment.speaker_state}; "
                f"word_timing={'available' if segment.word_timing is not None else 'unavailable'}; "
                f"text={json.dumps(segment.text, ensure_ascii=False)}"
            )
        for occurrence in occurrences or []:
            segment = segment_by_id[occurrence.segment_id]
            ref = _lookup_semantic(
                identity_map,
                "speech_occurrence",
                speech_occurrence_authored_id(
                    attachment.transcript_sha256,
                    occurrence.segment_id,
                    occurrence.clip_id,
                ),
            )
            lines.append(
                f"- SPEECH {ref}: clip={occurrence.clip_id}; "
                f"authored=[{occurrence.timeline_start:g},{occurrence.timeline_end:g}); "
                f"effective={('[%g,%g)' % (occurrence.effective_start, occurrence.effective_end)) if occurrence.effective_start is not None and occurrence.effective_end is not None else 'unavailable'}; "
                f"text={json.dumps(segment.text, ensure_ascii=False)}"
            )
    for clip in model.clips:
        clip_ref = _lookup_semantic(identity_map, "clip", clip.clip_id)
        if clip.authored_text is not None:
            lines.append(
                f"- CAPTION {clip_ref}: {json.dumps(clip.authored_text, ensure_ascii=False)}"
            )
        elif _track_kind(model, clip.track_id) == "visual":
            lines.append(f"- OTHER TEXT {clip_ref}: not_inspected")
    lines.extend(["", "## Suggested next actions", ""])
    suggested = ["Refresh against current state (action-index.json: TL01 → refresh_root)"]
    for ref in _ordered_object_refs(model, identity_map):
        parsed = parse_qualified_ref(ref)
        if parsed.kind == "CL":
            suggested.append(f"Focus clip {ref} (action-index.json: {ref} → focus_context)")
        elif parsed.kind == "AS":
            integrity = model.media_integrity[_lookup_display(identity_map, ref)[2]]
            if integrity.state == "verified_original":
                suggested.append(
                    f"Inspect original media {ref} (action-index.json: {ref} → inspect_original)"
                )
            else:
                suggested.append(
                    f"Recover asset {ref} (action-index.json: {ref} → inspect_original, "
                    f"unavailable: {integrity.state})"
                )
        elif parsed.kind == "TS":
            suggested.append(
                f"Focus transcript segment {ref} (action-index.json: {ref} → focus_occurrences)"
            )
        elif parsed.kind == "SP":
            suggested.append(
                f"Focus speech occurrence {ref} (action-index.json: {ref} → focus_clip_context)"
            )
    lines.extend(f"- {item}" for item in suggested)
    return "\n".join(lines) + "\n"


__all__ = [
    "FOCUS_CONTEXT_SECONDS",
    "FROZEN_AT_SENTINEL",
    "METRIC_DEFINITIONS_NAME",
    "METRIC_DEFINITIONS_VERSION",
    "SCHEMA_VERSION",
    "TIMESTAMP_CONTEXT_SECONDS",
    "emit_action_index",
    "emit_asset_index",
    "emit_diagnostics",
    "emit_ground_truth",
    "emit_metric_definitions",
    "emit_reading_guide",
    "emit_structure_md",
    "emit_transcript_index",
]
