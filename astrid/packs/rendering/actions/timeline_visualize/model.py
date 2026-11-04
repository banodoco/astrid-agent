"""Renderer-independent inspection semantics for one frozen timeline.

The model deliberately keeps four time domains separate:

* :class:`IntervalSeconds` on ``ClipModel.authored`` is authored placement plus
  source duration.  It is not frame quantized and does not apply speed.
* :class:`IntervalFrames` on ``ClipModel.frames`` is the compositor's
  independently rounded authored Sequence interval, including its one-frame
  duration floor.
* ``ClipModel.mounted`` is the Sequence interval actually mounted by the
  compositor after transition-group scheduling.
* ``ClipModel.effective`` is the non-transition presentation interval after
  v0.0.6 transition grouping, retiming, and composition clipping.

All frame arithmetic delegates to :mod:`astrid.core.timeline.duration`.  The
only pinned compositor facts kept here are provenance and the generated
transition registry defaults that accompany the v0.0.6 source snapshot.

Asset classification is scoped to the snapshot project and the runtime's
project-scoped object admission. The model never guesses a local root or
fetches a source locator.
"""

from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import dataclass, field, replace
from numbers import Real
from typing import Any, Mapping

from astrid.core.timeline.duration import (
    clip_end_frame,
    clip_source_duration,
    clip_start_frame,
    resolve_transition_duration_frames,
    timeline_duration_frames,
    timeline_render_duration_frames,
    visual_tracks_paint_order,
)
from astrid.core.timeline.resolution import AssetIntegrity, classify_registry
from astrid.core.timeline.snapshot import TimelineSnapshot
from astrid.packs.rendering.actions.timeline_visualize.validate import (
    validate_structural,
)

COMPOSITOR_VERSION = "0.0.6"
TRANSITION_FALLBACK_FRAMES = 12
DEFAULT_FPS = 30

# Pinned generated registry: docs/reference/timeline-composition-v0.0.6/
# transitions.generated.ts:7-16.  The compositor's separate hard fallback is
# 12 frames (lib/transitions.tsx:43-47).
_PINNED_TRANSITION_DEFAULTS: Mapping[str, int | None] = {
    "cross-fade": 8,
    "fade": 8,
}


def _is_number(value: Any) -> bool:
    return isinstance(value, Real) and not isinstance(value, bool)


@dataclass(frozen=True, slots=True)
class IntervalSeconds:
    """Closed-open interval in seconds."""

    start: float
    end: float

    def __post_init__(self) -> None:
        if not _is_number(self.start) or not _is_number(self.end):
            raise ValueError("second interval bounds must be numbers")
        if not math.isfinite(float(self.start)) or not math.isfinite(float(self.end)):
            raise ValueError("second interval bounds must be finite")
        if float(self.end) < float(self.start):
            raise ValueError("second interval end must not precede start")

    @property
    def start_seconds(self) -> float:
        return float(self.start)

    @property
    def end_seconds(self) -> float:
        return float(self.end)

    @property
    def duration(self) -> float:
        return float(self.end) - float(self.start)


@dataclass(frozen=True, slots=True)
class IntervalFrames:
    """Closed-open compositor interval with its FPS provenance."""

    start_frame: int
    end_frame: int
    fps: int

    def __post_init__(self) -> None:
        for label, value in (
            ("start_frame", self.start_frame),
            ("end_frame", self.end_frame),
            ("fps", self.fps),
        ):
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(f"{label} must be an integer")
        if self.start_frame < 0:
            raise ValueError("start_frame must not be negative")
        if self.end_frame < self.start_frame:
            raise ValueError("end_frame must not precede start_frame")
        if self.fps <= 0:
            raise ValueError("fps must be positive")

    @property
    def start(self) -> int:
        return self.start_frame

    @property
    def end(self) -> int:
        return self.end_frame

    @property
    def duration_frames(self) -> int:
        return self.end_frame - self.start_frame

    def as_seconds(self) -> IntervalSeconds:
        return IntervalSeconds(self.start_frame / self.fps, self.end_frame / self.fps)


@dataclass(frozen=True, slots=True)
class TrackModel:
    track_id: str
    kind: str
    config_order: int
    paint_index: int
    label: str | None


@dataclass(frozen=True, slots=True)
class ClipModel:
    clip_id: str
    track_id: str
    authored: IntervalSeconds
    frames: IntervalFrames
    effective: IntervalSeconds
    speed: float
    transition: dict[str, Any] | None
    source: dict[str, Any] | None
    kind: str
    # R7 cold asset scope must remain derivable from this frozen model.  The
    # public core fields above match the task contract; this normalized index
    # preserves direct ``asset`` / legacy ``source`` references without keeping
    # the whole assembly clip alive.
    asset_keys: tuple[str, ...] = ()
    # Authored text is preserved only from an explicit timeline text payload.
    # Pixel-baked text is never inferred; visual media remains not_inspected.
    authored_text: str | None = None
    pixel_text_state: str = "not_inspected"
    # ``None`` is an init-only compatibility sentinel.  Existing ClipModel
    # constructions automatically retain their raw frame interval.
    mounted: IntervalFrames = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.mounted is None:
            object.__setattr__(self, "mounted", self.frames)
        elif self.mounted.fps != self.frames.fps:
            raise ValueError("mounted interval FPS must match clip frame interval FPS")

    @property
    def asset_refs(self) -> tuple[str, ...]:
        """Alias used by later ground-truth emitters."""

        return self.asset_keys


@dataclass(frozen=True, slots=True)
class ShotModel:
    """Frozen pinned-shot membership and bounds needed by cold shot scope."""

    shot_id: str
    member_clip_ids: tuple[str, ...]
    authored: IntervalSeconds | None
    frames: IntervalFrames | None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ModelExtents:
    composition_frames: int
    composition_seconds: float
    visual_frames: int
    visual_seconds: float
    audible_frames: int
    fps: int
    # Authored composition extent stays available when the renderer declares
    # an explicit render-only tail. Frozen views produced before this field
    # existed leave it unset and retain their original composition extent.
    authored_composition_frames: int | None = None
    authored_composition_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class TimelineInspectionModel:
    timeline_uuid: str
    timeline_ulid: str
    slug: str | None
    fps: int
    tracks: tuple[TrackModel, ...]
    clips: tuple[ClipModel, ...]
    extents: ModelExtents
    compositor_version: str
    transition_default_frames: int
    registry_keys: frozenset[str]
    media_integrity: dict[str, AssetIntegrity]
    snapshot_sns: str
    # ``select_scope`` receives only the model.  Keeping normalized shot data is
    # therefore necessary for a pure, cold shot selector.
    shots: tuple[ShotModel, ...] = ()
    #: Width/height aspect per asset key (from registry ``resolution``), for
    #: aspect-aware card geometry.  Defaulted so callers that construct the
    #: model directly (frozen packs, tests) are unaffected.
    asset_aspects: dict[str, float] = field(default_factory=dict)

    @property
    def pinned_shot_groups(self) -> tuple[ShotModel, ...]:
        return self.shots


def _timeline_fps(assembly: Mapping[str, Any]) -> int:
    """Return the compositor input FPS, matching the render runner's default."""

    raw: Any = None
    overrides = assembly.get("theme_overrides")
    if isinstance(overrides, Mapping):
        visual = overrides.get("visual")
        if isinstance(visual, Mapping):
            canvas = visual.get("canvas")
            if isinstance(canvas, Mapping):
                raw = canvas.get("fps")
    if raw is None:
        return DEFAULT_FPS
    if not _is_number(raw) or not math.isfinite(float(raw)) or float(raw) <= 0:
        raise ValueError("timeline canvas fps must be a positive finite number")
    if not float(raw).is_integer():
        raise ValueError("TimelineInspectionModel requires a whole-number fps")
    return int(raw)


def _track_kind(raw: Any) -> str:
    return raw if raw in {"visual", "audio"} else "other"


def _source_bounds(clip: Mapping[str, Any]) -> dict[str, Any] | None:
    source: dict[str, Any] = {}
    for key in ("from", "to", "trim"):
        if key in clip:
            source[key] = deepcopy(clip[key])
    # Some standalone inputs use Python's collision-safe spelling.
    if "from" not in source and "from_" in clip:
        source["from"] = deepcopy(clip["from_"])
    return source or None


def _asset_keys(clip: Mapping[str, Any]) -> tuple[str, ...]:
    """Normalize direct rendered-media references without guessing provenance."""

    values: list[str] = []

    def add(value: Any) -> None:
        if isinstance(value, str) and value and value not in values:
            values.append(value)

    add(clip.get("asset"))
    raw_source = clip.get("source")
    if isinstance(raw_source, str):
        add(raw_source)
    elif isinstance(raw_source, Mapping):
        for key in ("asset", "assetKey", "key", "id"):
            add(raw_source.get(key))
    raw_assets = clip.get("assets")
    if isinstance(raw_assets, (list, tuple)):
        for value in raw_assets:
            add(value)
    return tuple(values)


def _authored_text(clip: Mapping[str, Any]) -> str | None:
    raw = clip.get("text")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, Mapping):
        content = raw.get("content")
        if isinstance(content, str):
            return content
    return None


def _raw_transition(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return {"id": value}
    if isinstance(value, Mapping):
        return deepcopy(dict(value))
    raise ValueError("clip transition must be a string or object")


def _registry_entries(registry: Mapping[str, Any]) -> dict[str, Any]:
    raw = registry.get("assets", registry)
    if isinstance(raw, Mapping):
        return {str(key): value for key, value in raw.items()}
    if isinstance(raw, list):
        entries: dict[str, Any] = {}
        for index, item in enumerate(raw):
            value = item
            key: Any = None
            if isinstance(item, Mapping):
                key = item.get("asset_key") or item.get("key") or item.get("id")
                if key is None and len(item) == 1:
                    candidate_key, candidate_value = next(iter(item.items()))
                    if isinstance(candidate_value, Mapping):
                        key = candidate_key
                        value = candidate_value
            entries[str(key) if key is not None else f"asset-{index}"] = value
        return entries
    raise ValueError("snapshot.registry.assets must be an object or array")


def _media_integrity(
    registry: Mapping[str, Any], *, project_ref: str,
    runtime_client: Any | None = None,
    media_snapshot: Any | None = None,
) -> dict[str, AssetIntegrity]:
    classified = classify_registry(
        dict(registry),
        project_ref=project_ref,
        runtime_client=runtime_client,
        media_snapshot=media_snapshot,
    )
    return {key: classified[key] for key in sorted(classified)}


def _asset_aspects(registry: Mapping[str, Any]) -> dict[str, float]:
    """Width/height aspect per asset key from the registry ``resolution``
    field ("WxH").  Unknown/unparseable entries are omitted — callers fall
    back to the default 16:9 filmstrip aspect.  This drives aspect-aware
    card geometry: a portrait frame (e.g. a poster) gets a portrait card
    instead of letterboxing inside a landscape one.
    """
    raw_assets = registry.get("assets") if isinstance(registry, Mapping) else None
    if not isinstance(raw_assets, Mapping):
        return {}
    aspects: dict[str, float] = {}
    for key, entry in raw_assets.items():
        if not isinstance(key, str) or not isinstance(entry, Mapping):
            continue
        resolution = entry.get("resolution")
        if not isinstance(resolution, str) or "x" not in resolution:
            continue
        width_raw, _, height_raw = resolution.partition("x")
        try:
            width, height = float(width_raw), float(height_raw)
        except (TypeError, ValueError):
            continue
        if width > 0 and height > 0:
            aspects[key] = width / height
    return aspects


def _seconds_to_frame(seconds: float, fps: int) -> int:
    """Use duration.py's JS Math.round mirror for an arbitrary boundary."""

    return clip_start_frame({"at": seconds, "hold": 0.0}, fps)


def _shot_models(
    assembly: Mapping[str, Any],
    clips: tuple[ClipModel, ...],
    fps: int,
) -> tuple[ShotModel, ...]:
    app = assembly.get("app") if isinstance(assembly, Mapping) else None
    composition = app.get("astrid_shot_composition") if isinstance(app, Mapping) else None
    canonical_occurrences = composition.get("occurrences") if isinstance(composition, Mapping) else None
    if isinstance(canonical_occurrences, list):
        by_occurrence: dict[str, list[ClipModel]] = {}
        for clip in clips:
            raw_app = None
            # ClipModel intentionally retains only normalized fields; the
            # occurrence id is available from the projected clip id prefix and
            # from the canonical occurrence table. Match by stable prefix so
            # repeated linked revisions never collapse into one shot row.
            if ":" in clip.clip_id:
                prefix = clip.clip_id.split(":", 1)[0]
                by_occurrence.setdefault(prefix, []).append(clip)
        result: list[ShotModel] = []
        for raw in canonical_occurrences:
            if not isinstance(raw, Mapping):
                continue
            occurrence_id = raw.get("occurrence_id")
            if not isinstance(occurrence_id, str) or not occurrence_id:
                continue
            members = by_occurrence.get(occurrence_id, [])
            at = raw.get("at")
            if at is None and isinstance(raw.get("at_ms"), (int, float)):
                at = float(raw["at_ms"]) / 1000.0
            duration = raw.get("duration_seconds")
            if duration is None and isinstance(raw.get("duration_ms"), (int, float)):
                duration = float(raw["duration_ms"]) / 1000.0
            if isinstance(at, (int, float)) and isinstance(duration, (int, float)):
                authored = IntervalSeconds(float(at), float(at) + max(0.0, float(duration)))
                frames = IntervalFrames(
                    _seconds_to_frame(authored.start, fps),
                    _seconds_to_frame(authored.end, fps),
                    fps,
                )
            elif members:
                authored = IntervalSeconds(min(c.authored.start for c in members), max(c.authored.end for c in members))
                frames = IntervalFrames(min(c.frames.start_frame for c in members), max(c.frames.end_frame for c in members), fps)
            else:
                authored = None
                frames = None
            result.append(ShotModel(occurrence_id, tuple(c.clip_id for c in members), authored, frames, ()))
        return tuple(result)

    raw_groups = assembly.get("pinnedShotGroups", ())
    if raw_groups is None:
        return ()
    if not isinstance(raw_groups, (list, tuple)):
        raise ValueError("timeline.pinnedShotGroups must be an array")

    by_id = {clip.clip_id: clip for clip in clips}
    shots: list[ShotModel] = []
    seen: set[str] = set()
    for index, raw_group in enumerate(raw_groups):
        if not isinstance(raw_group, Mapping):
            continue
        raw_id = raw_group.get("shotId")
        shot_id = raw_id if isinstance(raw_id, str) and raw_id else f"shot-{index + 1}"
        if shot_id in seen:
            raise ValueError(f"duplicate pinned shot id {shot_id!r}")
        seen.add(shot_id)
        raw_members = raw_group.get("clipIds", ())
        member_ids = (
            tuple(dict.fromkeys(item for item in raw_members if isinstance(item, str) and item))
            if isinstance(raw_members, (list, tuple))
            else ()
        )
        members = [by_id[clip_id] for clip_id in member_ids if clip_id in by_id]
        dangling = [clip_id for clip_id in member_ids if clip_id not in by_id]
        warnings = tuple(
            f"pinned shot {shot_id!r} references missing clip {clip_id!r}" for clip_id in dangling
        )

        raw_start = raw_group.get("start")
        raw_end = raw_group.get("end")
        has_authored_bounds = (
            _is_number(raw_start)
            and _is_number(raw_end)
            and math.isfinite(float(raw_start))
            and math.isfinite(float(raw_end))
            and float(raw_start) >= 0
            and float(raw_end) > float(raw_start)
        )
        if has_authored_bounds:
            authored = IntervalSeconds(float(raw_start), float(raw_end))
            frames = IntervalFrames(
                _seconds_to_frame(authored.start, fps),
                _seconds_to_frame(authored.end, fps),
                fps,
            )
        elif members:
            authored = IntervalSeconds(
                min(clip.authored.start for clip in members),
                max(clip.authored.end for clip in members),
            )
            # Member-derived selection follows the clips' actual compositor
            # windows.  ``authored`` remains separate so R9 can expose both.
            frames = IntervalFrames(
                min(clip.frames.start_frame for clip in members),
                max(clip.frames.end_frame for clip in members),
                fps,
            )
        else:
            authored = None
            frames = None
        shots.append(ShotModel(shot_id, member_ids, authored, frames, warnings))
    return tuple(shots)


def build_model(
    snapshot: TimelineSnapshot,
    *,
    runtime_client: Any | None = None,
    media_snapshot: Any | None = None,
) -> TimelineInspectionModel:
    """Normalize one frozen :class:`TimelineSnapshot` without writes.

    Media classification is always scoped to the snapshot's project slug and
    the supplied runtime admission; no filesystem root is accepted.
    """

    if not isinstance(snapshot, TimelineSnapshot):
        raise TypeError("snapshot must be a TimelineSnapshot")
    assembly = snapshot.assembly
    structural_errors = validate_structural(assembly)
    if structural_errors:
        raise ValueError("snapshot assembly is invalid: " + "; ".join(structural_errors))

    fps = _timeline_fps(assembly)
    raw_tracks = assembly.get("tracks", [])
    raw_clips = assembly.get("clips", [])
    if not isinstance(raw_tracks, list) or not isinstance(raw_clips, list):
        raise ValueError("snapshot assembly tracks and clips must be arrays")

    paint_order = visual_tracks_paint_order(raw_tracks)
    visual_paint_indices = {track_id: index for index, track_id in enumerate(paint_order)}
    tracks: list[TrackModel] = []
    track_order: dict[str, int] = {}
    track_kinds: dict[str, str] = {}
    for config_order, raw_track in enumerate(raw_tracks):
        if not isinstance(raw_track, Mapping):
            raise ValueError(f"tracks[{config_order}] must be an object")
        track_id = raw_track.get("id")
        if not isinstance(track_id, str) or not track_id:
            raise ValueError(f"tracks[{config_order}].id must be a non-empty string")
        kind = _track_kind(raw_track.get("kind"))
        paint_index = visual_paint_indices[track_id] if kind == "visual" else config_order
        raw_label = raw_track.get("label")
        label = raw_label if isinstance(raw_label, str) else None
        tracks.append(TrackModel(track_id, kind, config_order, paint_index, label))
        track_order[track_id] = config_order
        track_kinds[track_id] = kind

    ordered_raw_clips = sorted(
        enumerate(raw_clips),
        key=lambda item: (
            track_order.get(item[1].get("track"), len(raw_tracks))
            if isinstance(item[1], Mapping)
            else len(raw_tracks),
            float(item[1].get("at", 0)) if isinstance(item[1], Mapping) else 0.0,
            item[0],
        ),
    )
    clips: list[ClipModel] = []
    for _source_index, raw_clip in ordered_raw_clips:
        if not isinstance(raw_clip, Mapping):
            raise ValueError("timeline clips must be objects")
        clip_id = raw_clip.get("id")
        track_id = raw_clip.get("track")
        if not isinstance(clip_id, str) or not clip_id:
            raise ValueError("clip.id must be a non-empty string")
        if not isinstance(track_id, str) or not track_id:
            raise ValueError(f"clip {clip_id!r}.track must be a non-empty string")
        authored_start = float(raw_clip["at"])
        authored = IntervalSeconds(
            authored_start,
            authored_start + clip_source_duration(raw_clip),
        )
        frames = IntervalFrames(
            clip_start_frame(raw_clip, fps),
            clip_end_frame(raw_clip, fps),
            fps,
        )
        speed_raw = raw_clip.get("speed")
        speed = 1.0 if speed_raw is None else float(speed_raw)
        # The compositor's effect-layer exclusion is keyed by ``clipType``.
        kind_raw = raw_clip.get("clipType", raw_clip.get("kind", raw_clip.get("type")))
        kind = kind_raw if isinstance(kind_raw, str) and kind_raw else "other"
        clips.append(
            ClipModel(
                clip_id=clip_id,
                track_id=track_id,
                authored=authored,
                frames=frames,
                effective=frames.as_seconds(),
                speed=speed,
                transition=_raw_transition(raw_clip.get("transition")),
                source=_source_bounds(raw_clip),
                kind=kind,
                asset_keys=_asset_keys(raw_clip),
                authored_text=_authored_text(raw_clip) if kind == "text" else None,
                pixel_text_state="not_inspected",
            )
        )

    composition_frames = timeline_render_duration_frames(assembly, fps)
    visual_frames = max(
        (clip.frames.end_frame for clip in clips if track_kinds.get(clip.track_id) == "visual"),
        default=0,
    )
    audible_frames = max(
        (clip.frames.end_frame for clip in clips if track_kinds.get(clip.track_id) == "audio"),
        default=0,
    )
    extents = ModelExtents(
        composition_frames=composition_frames,
        composition_seconds=composition_frames / fps,
        visual_frames=visual_frames,
        visual_seconds=visual_frames / fps,
        audible_frames=audible_frames,
        fps=fps,
        authored_composition_frames=timeline_duration_frames(assembly, fps),
        authored_composition_seconds=timeline_duration_frames(assembly, fps) / fps,
    )
    registry_entries = _registry_entries(snapshot.registry)
    preliminary = TimelineInspectionModel(
        timeline_uuid=snapshot.timeline_id,
        timeline_ulid=snapshot.timeline_ulid,
        slug=snapshot.slug,
        fps=fps,
        tracks=tuple(tracks),
        clips=tuple(clips),
        extents=extents,
        compositor_version=COMPOSITOR_VERSION,
        transition_default_frames=TRANSITION_FALLBACK_FRAMES,
        registry_keys=frozenset(registry_entries),
        media_integrity=_media_integrity(
            snapshot.registry,
            project_ref=snapshot.project_slug,
            runtime_client=runtime_client,
            media_snapshot=media_snapshot,
        ),
        asset_aspects=_asset_aspects(snapshot.registry),
        snapshot_sns=snapshot.sns(),
        shots=(),
    )
    mounted, effective = _transition_interval_maps(preliminary)
    final_clips = tuple(
        replace(
            clip,
            mounted=mounted[clip.clip_id],
            effective=effective[clip.clip_id],
        )
        for clip in preliminary.clips
    )
    return replace(
        preliminary,
        clips=final_clips,
        shots=_shot_models(assembly, final_clips, fps),
    )


def _transition_id(transition: Mapping[str, Any]) -> str:
    value = transition.get("id", transition.get("type"))
    if not isinstance(value, str) or not value:
        raise ValueError("transition id or type must be a non-empty string")
    if value not in _PINNED_TRANSITION_DEFAULTS:
        raise ValueError(f"unknown transition id {value!r} in compositor {COMPOSITOR_VERSION}")
    return value


def _transition_interval_maps(
    model: TimelineInspectionModel,
    *, declarations: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, IntervalFrames], dict[str, IntervalSeconds]]:
    """Return v0.0.6 mounted and non-transition intervals per clip.

    ``TimelineComposition.tsx:208-237`` mounts an accepted transition group at
    ``F`` with ``toOffset = Df - T`` and ``groupDuration = toOffset + Dt``.
    Its child Sequences therefore occupy:

    * mounted source: ``[F, F + Df)``;
    * mounted destination: ``[F + Df - T, F + Df - T + Dt)``;
    * the overlap is ``[F + Df - T, F + Df)``;
    * effective source is ``[F, F + Df - T)``;
    * effective destination is ``[F + Df, F + Df - T + Dt)``.

    The final endpoint is clipped to the raw all-track composition duration,
    exactly as Remotion clips a Sequence that extends beyond the composition.
    Ignored/absent transitions retain the raw frame interval divided by FPS.
    Successful pairs consume both chronological entries, matching the
    compositor loop at ``TimelineComposition.tsx:263-275``.
    """

    mounted = {clip.clip_id: clip.frames for clip in model.clips}
    effective = {clip.clip_id: clip.frames.as_seconds() for clip in model.clips}
    composition_end = model.extents.composition_frames

    for track in model.tracks:
        if track.kind != "visual":
            continue
        clips = [clip for clip in model.clips if clip.track_id == track.track_id]
        index = 0
        while index < len(clips):
            from_clip = clips[index]
            to_clip = clips[index + 1] if index + 1 < len(clips) else None
            transition = from_clip.transition
            declaration = {"from_clip_id": from_clip.clip_id, "to_clip_id": to_clip.clip_id if to_clip else None,
                           "transition": transition, "status": "unresolved"}
            if transition is not None and declarations is not None:
                declarations.append(declaration)
            if transition is None or to_clip is None:
                declaration["reason"] = "no following clip on compositor track"
                index += 1
                continue
            if from_clip.kind == "effect-layer" or to_clip.kind == "effect-layer":
                declaration["reason"] = "effect-layer excluded from compositor transition grouping"
                index += 1
                continue
            from_start = from_clip.frames.start_frame
            from_end = from_clip.frames.end_frame
            to_start = to_clip.frames.start_frame
            if to_start < from_start or to_start > from_end:
                declaration["reason"] = "following clip does not overlap or abut source"
                index += 1
                continue

            try:
                transition_id = _transition_id(transition)
            except ValueError as exc:
                if declarations is None:
                    raise
                declaration["reason"] = str(exc)
                index += 1
                continue
            from_duration = from_clip.frames.duration_frames
            to_duration = to_clip.frames.duration_frames
            registered_default = _PINNED_TRANSITION_DEFAULTS[transition_id]
            try:
                duration = resolve_transition_duration_frames(
                    transition, from_duration, to_duration, registered_default, fps=model.fps,
                )
            except ValueError as exc:
                if declarations is None:
                    raise
                declaration["reason"] = str(exc)
                index += 1
                continue
            if duration is None:
                declaration["reason"] = "duration non-positive or exceeds either clip"
                index += 1
                continue

            to_offset = max(0, from_duration - duration)
            from_effective_end = from_start + to_offset
            to_mounted_start = from_start + to_offset
            to_effective_start = from_start + to_offset + duration
            scheduled_to_end = min(
                composition_end,
                from_start + to_offset + to_duration,
            )
            mounted[from_clip.clip_id] = IntervalFrames(
                from_start,
                min(composition_end, from_start + from_duration),
                model.fps,
            )
            mounted[to_clip.clip_id] = IntervalFrames(
                to_mounted_start,
                scheduled_to_end,
                model.fps,
            )
            effective[from_clip.clip_id] = IntervalSeconds(
                from_start / model.fps,
                from_effective_end / model.fps,
            )
            effective[to_clip.clip_id] = IntervalSeconds(
                to_effective_start / model.fps,
                scheduled_to_end / model.fps,
            )
            declaration.update(status="resolved", start_frame=to_mounted_start,
                               end_frame=min(composition_end, from_start + from_duration), fps=model.fps)
            if to_clip.transition is not None and declarations is not None:
                declarations.append({"from_clip_id": to_clip.clip_id, "to_clip_id": None,
                                     "transition": to_clip.transition, "status": "unresolved",
                                     "reason": "clip consumed as destination of previous transition group"})
            index += 2

    return mounted, effective


def transition_mounted_intervals(
    model: TimelineInspectionModel,
) -> dict[str, IntervalFrames]:
    """Return actual v0.0.6 compositor-mounted Sequence intervals per clip."""

    mounted, _effective = _transition_interval_maps(model)
    return mounted


def transition_effective_intervals(
    model: TimelineInspectionModel,
) -> dict[str, IntervalSeconds]:
    """Return v0.0.6 non-transition presentation intervals per clip."""

    _mounted, effective = _transition_interval_maps(model)
    return effective


__all__ = [
    "COMPOSITOR_VERSION",
    "ClipModel",
    "IntervalFrames",
    "IntervalSeconds",
    "ModelExtents",
    "ShotModel",
    "TimelineInspectionModel",
    "TrackModel",
    "build_model",
    "transition_effective_intervals",
    "transition_mounted_intervals",
]
