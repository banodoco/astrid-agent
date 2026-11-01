from __future__ import annotations

from typing import Any

from astrid.core.timeline.banodoco_schema import (
    _CLIP_ALLOWED,
    _THEME_OVERRIDES_ALLOWED,
    _TRACK_ALLOWED,
    _known_timeline_payload,
    _normalize_clip_for_validation,
    _raise_unknown_keys,
    _validate_shared_timeline,
)
from astrid.core.timeline.kinds import normalize_track_kind

# Registry lookups are late-imported through banodoco_schema so that
# mock.patch.object(banodoco_schema, ...) still affects internal callers.
# DO NOT import _animation_ids/_animation_meta/_transition_ids directly from
# validators.registry here.


def _validate_animation_reference(ref: Any, phase: str, path: str, known_ids: set[str]) -> None:
    if isinstance(ref, str):
        animation_id = ref
    elif isinstance(ref, dict):
        _raise_unknown_keys(path, ref, frozenset({"id", "durationFrames", "easing", "params"}))
        animation_id = ref.get("id")
        if "durationFrames" in ref and (
            not isinstance(ref.get("durationFrames"), (int, float)) or float(ref["durationFrames"]) <= 0
        ):
            raise ValueError(f"{path}.durationFrames must be a positive number")
        if "easing" in ref and not isinstance(ref.get("easing"), str):
            raise ValueError(f"{path}.easing must be a string")
        if "params" in ref and not isinstance(ref.get("params"), dict):
            raise ValueError(f"{path}.params must be an object")
    else:
        raise ValueError(f"{path} must be an animation id string or object")
    if not isinstance(animation_id, str) or not animation_id:
        raise ValueError(f"{path}.id must be a non-empty string")
    if known_ids and animation_id not in known_ids:
        raise ValueError(f"{path} animation id {animation_id!r} is not present in the animations catalog")
    from astrid.core.timeline.banodoco_schema import _animation_meta
    meta = _animation_meta(animation_id)
    meta_phase = meta.get("phase")
    phase_matches = (
        meta_phase in (None, "any", phase)
        or (isinstance(meta_phase, list) and phase in meta_phase)
    )
    if not phase_matches:
        raise ValueError(f"{path} animation {animation_id!r} has phase {meta_phase!r}, expected {phase!r}")


def _validate_animation_reference_list(value: Any, phase: str, path: str, known_ids: set[str]) -> None:
    if value is None:
        return
    refs = value if isinstance(value, list) else [value]
    if not refs:
        raise ValueError(f"{path} must not be an empty animation list")
    for index, ref in enumerate(refs):
        _validate_animation_reference(ref, phase, f"{path}[{index}]", known_ids)


def _schema_params_for_animation_refs(schema: dict[str, Any], params: dict[str, Any]) -> dict[str, Any]:
    """Keep legacy strict effect schemas usable while standardized animation refs roll out."""
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return params
    next_params = dict(params)
    for phase in ("entrance", "sustain", "exit"):
        if phase not in next_params:
            continue
        prop = properties.get(phase)
        if not isinstance(prop, dict):
            next_params.pop(phase)
            continue
        enum = prop.get("enum")
        if isinstance(enum, list) and "none" in enum:
            next_params[phase] = "none"
        elif prop.get("type") == "string":
            next_params[phase] = "none"
    return next_params


def _validate_effect_params(effect_id: str, params: Any, path: str, theme: str | None = None) -> None:
    if params is None:
        return
    if not isinstance(params, dict):
        raise ValueError(f"{path} must be an object")
    from astrid.core.timeline.banodoco_schema import _animation_ids
    known_animation_ids = _animation_ids()
    for phase in ("entrance", "sustain", "exit"):
        if phase in params:
            _validate_animation_reference_list(params[phase], phase, f"{path}.{phase}", known_animation_ids)
    from astrid.core.element import catalog as effects_catalog
    try:
        import jsonschema
    except ImportError:
        return
    schema = effects_catalog.read_effect_schema(effect_id)
    jsonschema.validate(_schema_params_for_animation_refs(schema, params), schema)


def _transition_reference(value: Any, path: str) -> tuple[str, float | None]:
    if isinstance(value, str):
        return value, None
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be a transition id string or object")
    _raise_unknown_keys(path, value, frozenset({"id", "type", "duration", "durationFrames", "params"}))
    transition_id = value.get("id", value.get("type"))
    if not isinstance(transition_id, str) or not transition_id:
        raise ValueError(f"{path}.id must be a non-empty string")
    if "params" in value and not isinstance(value.get("params"), dict):
        raise ValueError(f"{path}.params must be an object")
    duration_frames = value.get("durationFrames")
    duration_seconds = value.get("duration")
    if duration_frames is not None:
        if not isinstance(duration_frames, (int, float)) or float(duration_frames) <= 0:
            raise ValueError(f"{path}.durationFrames must be a positive number")
        return transition_id, float(duration_frames)
    if duration_seconds is not None:
        if not isinstance(duration_seconds, (int, float)) or float(duration_seconds) <= 0:
            raise ValueError(f"{path}.duration must be a positive number")
        return transition_id, None
    return transition_id, None


def _clip_duration_seconds(clip: dict[str, Any]) -> float | None:
    hold = clip.get("hold")
    if isinstance(hold, (int, float)) and float(hold) >= 0:
        return float(hold)
    start = clip.get("from", 0)
    end = clip.get("to")
    if isinstance(start, (int, float)) and isinstance(end, (int, float)) and float(end) >= float(start):
        return float(end) - float(start)
    return None


def _validate_clip_transitions(clips: list[dict[str, Any]], fps: float) -> None:
    from astrid.core.timeline.banodoco_schema import _transition_ids
    known_ids = _transition_ids()
    by_track: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    for index, clip in enumerate(clips):
        track = clip.get("track")
        if isinstance(track, str):
            by_track.setdefault(track, []).append((index, clip))
    for track_clips in by_track.values():
        track_clips.sort(key=lambda item: float(item[1].get("at", 0)))
        for position, (index, clip) in enumerate(track_clips):
            if "transition" not in clip:
                continue
            transition_id, duration_frames = _transition_reference(clip["transition"], f"clips[{index}].transition")
            if known_ids and transition_id not in known_ids:
                raise ValueError(
                    f"clips[{index}].transition id {transition_id!r} is not present in the transitions catalog"
                )
            next_clip = track_clips[position + 1][1] if position + 1 < len(track_clips) else None
            current_duration = _clip_duration_seconds(clip)
            next_duration = _clip_duration_seconds(next_clip) if next_clip is not None else None
            duration_seconds = duration_frames / fps if duration_frames is not None else None
            if duration_seconds is None and isinstance(clip.get("transition"), dict):
                raw_duration = clip["transition"].get("duration")
                duration_seconds = float(raw_duration) if isinstance(raw_duration, (int, float)) else None
            if duration_seconds is None or current_duration is None or next_duration is None:
                continue
            if duration_seconds > current_duration or duration_seconds > next_duration:
                raise ValueError(
                    f"clips[{index}].transition duration {duration_seconds:.3f}s must fit both adjacent same-track clips"
                )


def _timeline_fps(config: dict[str, Any]) -> float:
    """Best-effort fps for timeline-internal validation (transitions, etc.).

    The authoritative fps comes from theme.visual.canvas at render time. This helper
    looks for a theme_overrides override; otherwise it returns a sentinel default
    (30) used only for clip-transition duration checks.
    """
    overrides = config.get("theme_overrides")
    if isinstance(overrides, dict):
        visual = overrides.get("visual")
        if isinstance(visual, dict):
            canvas = visual.get("canvas")
            if isinstance(canvas, dict):
                fps_value = canvas.get("fps")
                if isinstance(fps_value, (int, float)) and float(fps_value) > 0:
                    return float(fps_value)
    return 30.0


def validate_timeline(config: Any, *, strict: bool = True) -> None:
    """Validate a Banodoco timeline.

    `clipType` is an open string for editor compatibility. Built-in media/text
    clips and registered effects get semantic checks where Astrid knows how;
    unknown clip types stay valid and classify as opaque at runtime.

    Callers that need to accept legacy/under-construction timelines (e.g.
    in-flight pipeline outputs that reference theme content not yet on
    disk) can opt into `strict=False`.
    """
    if not isinstance(config, dict):
        raise ValueError("Timeline must be a JSON object")
    theme = config.get("theme")
    if theme is not None and (not isinstance(theme, str) or not theme):
        raise ValueError("Timeline.theme must be a non-empty slug")
    # Shape-check against the shared JSON Schema first (via the compiled,
    # process-cached validator — no per-call check_schema / $ref re-walk);
    # then run the Banodoco-only semantic checks (effect-id registry,
    # transition durations).
    normalized_for_shared = _known_timeline_payload(config)
    # Top-level ``app`` is the editor-extension metadata namespace owned by
    # Astrid's container contract. The currently pinned shared render schema
    # does not declare that namespace, so validate the renderable subset while
    # retaining ``app`` unchanged for the caller and event-log round trip.
    normalized_for_shared.pop("app", None)
    if isinstance(normalized_for_shared.get("clips"), list):
        normalized_for_shared["clips"] = [
            (
                _normalize_clip_for_validation(
                    {key: value for key, value in c.items() if key != "label"}
                )
                if isinstance(c, dict)
                else c
            )
            for c in normalized_for_shared["clips"]
        ]
    _validate_shared_timeline(normalized_for_shared)
    fps = _timeline_fps(config)
    tracks = config.get("tracks")
    if tracks is not None:
        if not isinstance(tracks, list):
            raise ValueError("Timeline.tracks must be a list")
        for index, track in enumerate(tracks):
            if not isinstance(track, dict):
                raise ValueError(f"tracks[{index}] must be an object")
            _raise_unknown_keys(f"tracks[{index}]", track, _TRACK_ALLOWED)
            for field in ("id", "kind", "label"):
                if field not in track:
                    raise ValueError(f"tracks[{index}].{field} is required")
            normalize_track_kind(track.get("kind"))
    overrides = config.get("theme_overrides")
    if overrides is not None:
        if not isinstance(overrides, dict):
            raise ValueError("Timeline.theme_overrides must be an object")
        _raise_unknown_keys("Timeline.theme_overrides", overrides, _THEME_OVERRIDES_ALLOWED)
    clips = config.get("clips")
    if not isinstance(clips, list):
        raise ValueError("Timeline.clips must be a list")
    clip_ids: set[str] = set()
    normalized_clips: list[dict[str, Any]] = []
    for index, clip_raw in enumerate(clips):
        if not isinstance(clip_raw, dict):
            raise ValueError(f"clips[{index}] must be an object")
        clip = _normalize_clip_for_validation(clip_raw)
        normalized_clips.append(clip)
        _raise_unknown_keys(f"clips[{index}]", clip, _CLIP_ALLOWED)
        for field in ("id", "at", "track"):
            if field not in clip:
                raise ValueError(f"clips[{index}].{field} is required")
        clip_type = clip.get("clipType", "media")
        if not isinstance(clip_type, str):
            raise ValueError(f"clips[{index}].clipType must be a string")
        # A structured text payload without an explicit text clip type is
        # accepted by the shared compatibility schema as a media clip. That
        # silently renders as an empty/black layer in Remotion, so fail close
        # while the maker is still editing the timeline.
        if isinstance(clip.get("text"), dict) and clip_type != "text":
            raise ValueError(
                f"clips[{index}] contains structured text; set clipType to 'text'"
            )
        if clip_type == "text":
            text_payload = clip.get("text")
            if not isinstance(text_payload, dict):
                raise ValueError(f"clips[{index}].text must be an object for clipType 'text'")
            if not isinstance(text_payload.get("content"), str):
                raise ValueError(
                    f"clips[{index}].text.content must be a string for clipType 'text'"
                )
        # The authored theme label is not an element-registry selector.
        # Open-string clipTypes that are not registered elements remain opaque.
        from astrid.core.timeline.validators._type_resolve import is_visual_clip_element
        if is_visual_clip_element(clip_type, None):
            _validate_effect_params(clip_type, clip.get("params"), f"clips[{index}].params", theme=None)
        if "pool_id" in clip and not isinstance(clip["pool_id"], str):
            raise ValueError(f"clips[{index}].pool_id must be a string")
        if "clip_order" in clip:
            order = clip["clip_order"]
            if not isinstance(order, int) or isinstance(order, bool) or order <= 0:
                raise ValueError(f"clips[{index}].clip_order must be a positive integer")
        if clip["id"] in clip_ids:
            raise ValueError(f"clips[{index}].id {clip['id']!r} is not unique")
        clip_ids.add(clip["id"])
    _validate_clip_transitions(normalized_clips, float(fps))
