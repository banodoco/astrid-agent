"""Canonical Python mirror of timeline-composition v0.0.6 timing rules.

The reference implementation is the pinned TypeScript snapshot in
``docs/reference/timeline-composition-v0.0.6``.  This module intentionally
uses only the Python standard library so renderers, validators, storyboard
tools, and timeline-inspection code can share the same arithmetic.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from numbers import Real
from typing import Any

_MISSING = object()
_TRANSITION_FALLBACK_FRAMES = 12
_RENDER_CLOCK_APP_KEY = "astrid_render_clock"


def _field(value: Any, name: str, default: Any = _MISSING) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _is_number(value: Any) -> bool:
    """Match JavaScript's ``typeof value === 'number'`` for JSON values."""

    return isinstance(value, Real) and not isinstance(value, bool)


def _finite_number(value: Any, label: str) -> float:
    if not _is_number(value):
        raise ValueError(f"{label} must be a number")
    try:
        result = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{label} must be finite") from exc
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _positive_fps(fps: Any) -> float:
    result = _finite_number(fps, "fps")
    if result <= 0:
        raise ValueError("fps must be positive")
    return result


def _js_round(value: float) -> int:
    """Implement ``Math.round`` (half toward +infinity), not Python round."""

    if not math.isfinite(value):
        raise ValueError("frame value must be finite")
    return math.floor(value + 0.5)


def validate_clip_timing(clip: Any) -> None:
    """Validate timing before applying compositor arithmetic.

    The compositor divides by raw ``speed ?? 1`` in
    ``typescript/src/lib/duration.ts:15-18``; its playback-only sanitizer at
    lines 20-22 cannot protect duration calculation.  Consequently this
    contract rejects non-numeric, non-finite, or non-positive speeds before
    division.  It also rejects a negative/non-finite numeric ``hold``, a
    missing or non-numeric/non-finite ``at``, invalid present ``from``/``to``
    values, and an effective ``from`` greater than ``to`` when no numeric
    ``hold`` is present.  A numeric ``hold`` wins before the compositor reads
    either trim endpoint, so trim validation is bypassed in that branch.
    Missing trim endpoints retain the compositor's zero defaults from lines
    7-13.

    A non-numeric ``hold`` is ignored, matching the TypeScript
    ``typeof clip.hold === 'number'`` check.  Structural schema validation may
    reject such a value earlier.

    Raises:
        ValueError: If timing cannot be evaluated safely and deterministically.
    """

    _finite_number(_field(clip, "at"), "clip.at")

    hold = _field(clip, "hold", None)
    if _is_number(hold):
        hold_value = _finite_number(hold, "clip.hold")
        if hold_value < 0:
            raise ValueError("clip.hold must not be negative")
    else:
        source_from_raw = _field(clip, "from", None)
        source_to_raw = _field(clip, "to", None)
        source_from = (
            0.0
            if source_from_raw is None
            else _finite_number(source_from_raw, "clip.from")
        )
        source_to = (
            0.0
            if source_to_raw is None
            else _finite_number(source_to_raw, "clip.to")
        )
        if source_from > source_to:
            raise ValueError("clip.from must not be greater than clip.to")

    speed = _field(clip, "speed", None)
    if speed is not None:
        speed_value = _finite_number(speed, "clip.speed")
        if speed_value <= 0:
            raise ValueError("clip.speed must be positive")


def clip_source_duration(clip: Any) -> float:
    """Return source seconds, with numeric ``hold`` overriding trim bounds.

    This mirrors ``typescript/src/lib/duration.ts:7-13``: a numeric ``hold``
    wins unconditionally; otherwise the result is
    ``(to ?? 0) - (from ?? 0)``.  Timing is validated before subtraction.
    """

    validate_clip_timing(clip)
    hold = _field(clip, "hold", None)
    if _is_number(hold):
        return float(hold)
    source_from = _field(clip, "from", None)
    source_to = _field(clip, "to", None)
    return float(0 if source_to is None else source_to) - float(
        0 if source_from is None else source_from
    )


def clip_timeline_duration(clip: Any) -> float:
    """Return source duration divided by the raw positive playback speed.

    ``typescript/src/lib/duration.ts:15-18`` divides source duration by
    ``speed ?? 1``.  Validation happens first because the sanitizer at lines
    20-22 is used for playback, not for this duration arithmetic.
    """

    validate_clip_timing(clip)
    speed = _field(clip, "speed", None)
    return clip_source_duration(clip) / (1.0 if speed is None else float(speed))


def clip_start_frame(clip: Any, fps: float) -> int:
    """Return ``Math.round(clip.at * fps)``.

    The conversion is defined by ``typescript/src/lib/duration.ts:3-5`` and
    used for visual and audio Sequence starts in
    ``typescript/src/TimelineComposition.tsx:174-178,300-304``.  JavaScript
    half ties round toward positive infinity.
    """

    validate_clip_timing(clip)
    frame_rate = _positive_fps(fps)
    return _js_round(float(_field(clip, "at")) * frame_rate)


def clip_end_frame(clip: Any, fps: float) -> int:
    """Return rounded start plus a rounded, minimum-one-frame duration.

    ``typescript/src/lib/duration.ts:30-32`` defines clip duration frames as
    ``Math.max(1, Math.round(timelineDuration * fps))``.  The returned end is
    that duration added to the independently rounded start used at
    ``TimelineComposition.tsx:176,302``.
    """

    frame_rate = _positive_fps(fps)
    start_frame = clip_start_frame(clip, frame_rate)
    duration_frames = max(1, _js_round(clip_timeline_duration(clip) * frame_rate))
    return start_frame + duration_frames


def _authored_timeline_duration_frames(timeline: Any, fps: float) -> int:
    """Return the all-clip authored duration, with a one-frame floor.

    ``typescript/src/lib/duration.ts:34-41`` takes
    ``max(1, startFrame + clipDurationFrames)`` over every clip.  It does not
    filter audio, muted tracks, missing tracks, or metadata duration fields.
    """

    frame_rate = _positive_fps(fps)
    clips = _field(timeline, "clips", ())
    if clips is None or isinstance(clips, (str, bytes, Mapping)):
        raise ValueError("timeline.clips must be an iterable of clips")
    try:
        ends = (clip_end_frame(clip, frame_rate) for clip in clips)
        return max(1, max(ends, default=1))
    except TypeError as exc:
        raise ValueError("timeline.clips must be an iterable of clips") from exc


def _render_clock(timeline: Any) -> Mapping[str, Any] | None:
    """Return the optional explicit render-only clock extension.

    The shared timeline schema reserves ``app`` for application-owned data.
    Astrid's render clock lives under ``app.astrid_render_clock`` so authored
    clip intervals remain unchanged while a producer can declare a longer,
    independently verified decoded output.
    """

    app = _field(timeline, "app", None)
    if not isinstance(app, Mapping):
        return None
    if _RENDER_CLOCK_APP_KEY not in app:
        return None
    value = app[_RENDER_CLOCK_APP_KEY]
    if not isinstance(value, Mapping):
        raise ValueError("render clock must be an object")
    return value


def render_clock(timeline: Any, fps: float) -> Mapping[str, Any] | None:
    """Validate and return the explicit render-only clock, when present."""

    frame_rate = _positive_fps(fps)
    value = _render_clock(timeline)
    if value is None:
        return None
    authored = _authored_timeline_duration_frames(timeline, frame_rate)
    for key in ("authored_duration_frames", "render_duration_frames"):
        raw = value.get(key)
        if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
            raise ValueError(f"render clock {key} must be a positive integer")
    if value["authored_duration_frames"] != authored:
        raise ValueError(
            "render clock authored_duration_frames must match the authored clip extent"
        )
    rendered = value["render_duration_frames"]
    if rendered <= authored:
        raise ValueError(
            "render clock render_duration_frames must be longer than authored duration"
        )
    tail = value.get("tail")
    if not isinstance(tail, Mapping):
        raise ValueError("render clock tail must be an object")
    if tail.get("start_frame") != authored or tail.get("end_frame") != rendered:
        raise ValueError(
            "render clock tail must cover exactly the rendered extension after authored duration"
        )
    if not isinstance(tail.get("source_asset"), str) or not tail["source_asset"].strip():
        raise ValueError("render clock tail source_asset must be a non-empty string")
    if tail.get("source_start_frame") != authored or tail.get("source_end_frame") != rendered:
        raise ValueError(
            "render clock tail source interval must match the rendered extension"
        )
    if tail.get("policy") != "unmapped_excess_rendered_region":
        raise ValueError(
            "render clock tail policy must be unmapped_excess_rendered_region"
        )
    return value


def timeline_duration_frames(timeline: Any, fps: float) -> int:
    """Return the authored all-clip composition duration.

    This is intentionally unchanged for authored interval consumers. Use
    :func:`timeline_render_duration_frames` when a producer has declared an
    explicit rendered tail.
    """

    return _authored_timeline_duration_frames(timeline, fps)


def timeline_render_duration_frames(timeline: Any, fps: float) -> int:
    """Return the decoded render duration, including a declared render tail."""

    authored = _authored_timeline_duration_frames(timeline, fps)
    clock = render_clock(timeline, fps)
    return authored if clock is None else int(clock["render_duration_frames"])


def timeline_duration_seconds(timeline: Any, fps: float) -> float:
    """Return canonical composition frames divided by FPS.

    The frame count comes from ``typescript/src/lib/duration.ts:34-41``;
    division by the same positive FPS exposes that frame-quantized extent in
    seconds without consulting authored metadata.
    """

    frame_rate = _positive_fps(fps)
    return timeline_duration_frames(timeline, frame_rate) / frame_rate


def timeline_render_duration_seconds(timeline: Any, fps: float) -> float:
    """Return decoded render seconds, including an explicit render tail."""

    frame_rate = _positive_fps(fps)
    return timeline_render_duration_frames(timeline, frame_rate) / frame_rate


def visual_tracks_paint_order(tracks: Iterable[Any]) -> list[Any]:
    """Return visual track IDs in bottom-to-top compositor paint order.

    ``typescript/src/lib/tracks.ts:9-14`` preserves configuration order while
    filtering visual tracks.  ``typescript/src/TimelineComposition.tsx:314``
    reverses that list before painting, so the first visual config track is
    painted last and is topmost.  Audio tracks are excluded and not reversed.
    """

    visual_ids: list[Any] = []
    for track in tracks:
        if _field(track, "kind", None) == "visual":
            track_id = _field(track, "id")
            if track_id is _MISSING:
                raise ValueError("visual track.id is required")
            visual_ids.append(track_id)
    visual_ids.reverse()
    return visual_ids


def resolve_transition_duration_frames(
    clip_transition: Any,
    from_dur_frames: int,
    to_dur_frames: int,
    registered_default_frames: int | None = 12,
    *,
    fps: float | None = None,
) -> int | None:
    """Resolve and bound one already-eligible transition duration.

    Precedence mirrors ``typescript/src/lib/transitions.tsx:27-51``:
    explicit ``durationFrames``; then ``Math.round(duration * fps)``; then a
    registered default; then the 12-frame hard fallback.  The source helper
    receives FPS from Remotion.  Because the originally requested Python
    positional API omitted it, this mirror adds keyword-only ``fps`` and
    requires it only when the seconds branch is selected.

    ``typescript/src/TimelineComposition.tsx:196-200`` ignores resolved
    durations that are non-positive or exceed either clip.  Those compositor
    ignore cases, and an absent transition, return ``None``.  Malformed input
    or a seconds duration without FPS raises ``ValueError`` so contract misuse
    is not silently presented as an ignored transition.  Registry lookup,
    same-track adjacency, overlap, effect-layer exclusion, and last-clip
    exclusion occur before this helper in ``TimelineComposition.tsx:241-275``
    and remain the caller's responsibility.
    """

    if clip_transition is None:
        return None

    if isinstance(clip_transition, str):
        if not clip_transition:
            raise ValueError("transition id must not be empty")
        transition: Mapping[str, Any] | None = None
    elif isinstance(clip_transition, Mapping):
        transition = clip_transition
        transition_id = transition.get("id", transition.get("type"))
        if not isinstance(transition_id, str) or not transition_id:
            raise ValueError("transition id or type must be a non-empty string")
    else:
        raise ValueError("transition must be an id string or mapping")

    from_frames = _finite_number(from_dur_frames, "from_dur_frames")
    to_frames = _finite_number(to_dur_frames, "to_dur_frames")
    if not from_frames.is_integer() or not to_frames.is_integer():
        raise ValueError("clip durations must be whole frames")

    duration_frames: int | None = None
    if transition is not None and transition.get("durationFrames") is not None:
        explicit = _finite_number(transition["durationFrames"], "transition.durationFrames")
        if not explicit.is_integer():
            raise ValueError("transition.durationFrames must be a whole frame count")
        duration_frames = int(explicit)
    elif transition is not None and transition.get("duration") is not None:
        duration_seconds = _finite_number(transition["duration"], "transition.duration")
        if fps is None:
            raise ValueError("fps is required to resolve transition.duration seconds")
        duration_frames = _js_round(duration_seconds * _positive_fps(fps))
    elif registered_default_frames is not None:
        registered = _finite_number(registered_default_frames, "registered_default_frames")
        if not registered.is_integer():
            raise ValueError("registered_default_frames must be a whole frame count")
        duration_frames = int(registered)
    else:
        duration_frames = _TRANSITION_FALLBACK_FRAMES

    if duration_frames <= 0 or duration_frames > from_frames or duration_frames > to_frames:
        return None
    return duration_frames


__all__ = [
    "clip_end_frame",
    "clip_source_duration",
    "clip_start_frame",
    "clip_timeline_duration",
    "resolve_transition_duration_frames",
    "timeline_duration_frames",
    "timeline_duration_seconds",
    "validate_clip_timing",
    "visual_tracks_paint_order",
]
