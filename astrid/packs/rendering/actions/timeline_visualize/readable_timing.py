"""Bounded readable facts over the existing managed/compositor timing producer.

No custom keyframe clocks are inferred. AnimatedMediaTransform's canonical
motion.ts uses clip-local frame/fps; media trim, speed and sourceSegments are
separate. Managed expansion restarts this local clock after a left trim.
"""
from __future__ import annotations

from collections.abc import Mapping
from fractions import Fraction
from functools import lru_cache
import math

from astrid.core.timeline.duration import clip_start_frame, clip_end_frame, clip_source_duration
from .model import (ClipModel, IntervalFrames, IntervalSeconds, ModelExtents,
                    TimelineInspectionModel, TrackModel, _inspection_transition, _transition_interval_maps)

_FIELDS = ("x", "y", "width", "height", "opacity")


def _number(value):
    try:
        return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    except OverflowError:
        return False


def _wire(value):
    fraction = Fraction(str(value)).limit_denominator(1_000_000)
    return [fraction.numerator, fraction.denominator]


def _raw(clip):
    return clip.get("authored_fields") if isinstance(clip.get("authored_fields"), Mapping) else {}


def _params(clip):
    raw = _raw(clip)
    if isinstance(raw.get("params"), Mapping):
        return raw["params"]
    if clip.get("parameters_source") == "params" and isinstance(clip.get("parameters"), Mapping):
        return clip["parameters"]
    return {}


def _declares_keyframes(clip):
    raw = _raw(clip)
    containers = [clip, raw, clip.get("parameters"), clip.get("presentation_fields")]
    containers.extend(raw.get(alias) for alias in ("params", "parameters", "props"))
    return any(isinstance(value, Mapping) and "keyframes" in value for value in containers)


@lru_cache(maxsize=1)
def _transform_descriptor():
    from astrid.core.element.catalog import list_element_descriptors
    return next((row for row in list_element_descriptors()
                 if row.get("id") == "animated-media-transform" and row.get("kind") == "effect"), None)


def _canonical_transform(clip):
    raw = _raw(clip)
    ref = clip.get("element_ref", raw.get("elementRef"))
    kind = raw.get("clipType", clip.get("clip_type"))
    descriptor = _transform_descriptor()
    if not descriptor:
        return False, "canonical animated-media-transform element unavailable"
    if ref is not None:
        if not isinstance(ref, Mapping) or ref.get("id") != descriptor["id"] or ref.get("kind") != "effect":
            return False, "custom element keyframe clock is unsupported"
        if ref.get("revision") != descriptor.get("revision"):
            return False, "animated-media-transform element revision is unverified or stale"
        if kind not in {"effect-layer", "animated-media-transform"}:
            return False, "element reference is not dispatched as animated-media-transform"
        return True, None
    if kind == "animated-media-transform":
        return True, None
    return False, "custom keyframe clock is unsupported"


def _schedule(context):
    """Feed the complete bounded closure to the existing transition scheduler."""
    fps = context["fps"]
    if not _number(fps) or fps <= 0 or not float(fps).is_integer():
        raise ValueError("existing mounted transition model requires whole-number FPS")
    fps = int(fps)
    tracks = tuple(TrackModel(row["id"], row.get("kind", "other"), i, i, None)
                   for i, row in enumerate(context["tracks"]) if isinstance(row.get("id"), str))
    clips = []
    from astrid.packs.rendering.actions.render.managed_timeline import _normalize_pinned_element_references
    raw_clips = _normalize_pinned_element_references({"clips": context["clips"]})["clips"]
    for row in raw_clips:
        frames = IntervalFrames(clip_start_frame(row, fps), clip_end_frame(row, fps), fps)
        transition, legacy_incoming = _inspection_transition(row.get("transition"))
        clips.append(ClipModel(row["id"], row.get("track"),
                               IntervalSeconds(row["at"], row["at"] + clip_source_duration(row)),
                               frames, frames.as_seconds(), row.get("speed", 1),
                               transition, None, row.get("clipType", "media"),
                               legacy_incoming_transition=legacy_incoming))
    clips.sort(key=lambda c: (str(c.track_id), c.authored.start))
    end = max((c.frames.end_frame for c in clips), default=1)
    model = TimelineInspectionModel("inspection", "inspection", None, fps, tracks, tuple(clips),
                                    ModelExtents(end, end/fps, end, end/fps, end, fps),
                                    "0.0.6", 12, frozenset(), {}, "pinned-native-closure")
    declarations = []
    mounted, effective = _transition_interval_maps(model, declarations=declarations)
    return fps, {c.clip_id: c for c in clips}, mounted, effective, declarations


def _keys(value):
    if not isinstance(value, list) or not value:
        raise ValueError("canonical transform keyframes are absent or omitted")
    previous = -1
    for key in value:
        if not isinstance(key, Mapping) or not all(_number(key.get(field)) for field in ("at", *_FIELDS)):
            raise ValueError("canonical transform keyframes are malformed")
        if key["at"] < 0 or key["at"] <= previous or key["width"] <= 0 or key["height"] <= 0 or not 0 <= key["opacity"] <= 1:
            raise ValueError("canonical transform keyframes are invalid or unordered")
        previous = key["at"]
    return value


def _value(keys, field, at):
    if at <= keys[0]["at"]:
        return keys[0][field]
    for a, b in zip(keys, keys[1:]):
        if at <= b["at"]:
            return a[field] + (b[field] - a[field]) * (at - a["at"]) / (b["at"] - a["at"])
    return keys[-1][field]


def _transform_changes(keys, start, end):
    duration = end - start
    changes = []
    for field in _FIELDS:
        # Constant properties create no redundant rows. Holds in a changing
        # property remain explicit even when a neighbouring property moves.
        if len({key[field] for key in keys}) < 2:
            continue
        points = sorted({0., duration, *(key["at"] for key in keys if 0 < key["at"] < duration)})
        intervals = []
        for a, b in zip(points, points[1:]):
            before, after = _value(keys, field, a), _value(keys, field, b)
            direction = (after > before) - (after < before)
            if intervals and intervals[-1][4] == direction:
                intervals[-1] = (intervals[-1][0], b, intervals[-1][2], after, direction)
            else:
                intervals.append((a, b, before, after, direction))
        for a, b, before, after, direction in intervals:
            changes.append({"kind": "transform", "property": field,
                            "start": _wire(start+a), "end": _wire(start+b),
                            "before": before, "after": after, "hold": direction == 0})
    return changes


def _fade_values(effects):
    rows = effects if isinstance(effects, list) else [effects]
    values = {}
    for key in ("fade_in", "fade_out"):
        for row in rows:
            if isinstance(row, Mapping) and _number(row.get(key)):
                values[key] = row[key]
                break
    return values


def _compositor_dispatch(clip, context):
    """Resolve expanded IDs in the compositor's parent tracks, never local tracks."""
    timing = clip.get("render_timing")
    if not isinstance(timing, Mapping):
        return {"status": "unknown", "reason": clip.get("render_timing_unknown", "managed render dispatch unavailable")}
    matches = [row for row in context.get("tracks", []) if isinstance(row, Mapping)
               and row.get("id") == timing.get("track") and row.get("kind") in {"visual", "audio"}]
    if len(matches) != 1:
        return {"status": "unknown", "reason": "compositor dispatch track unresolved or ambiguous; fade/control contract unavailable"}
    track = dict(matches[0])
    kind = timing.get("clipType", _raw(clip).get("clipType", clip.get("clip_type", "media")))
    audio = track["kind"] == "audio"
    media = kind in {"media", "video", "image", "overlay", "audio"}
    result = {"status": "resolved", "authority": "managed_shot_expansion+compositor_0.0.6",
              "track": track, "fade_contract": "audio_params" if audio else "visual_effects" if media else "component_owned",
              "control_contract": "audio_track" if audio else "visual_media" if media else "visual_component"}
    controls = {}
    if audio or media:
        # AudioTrack and VisualClip sanitize volume independently. clip.mute,
        # occurrence gain/mute and the authored child track are not inherited
        # by managed expansion and must not be folded into this value.
        volume = lambda value: max(0, value) if _number(value) else 1
        controls["base_gain"] = 0 if track.get("muted") else volume(track.get("volume")) * volume(timing.get("volume"))
    if not audio:
        controls["track_opacity_multiplier"] = track.get("opacity") if track.get("opacity") is not None else 1
        if media:
            controls["clip_opacity_multiplier"] = timing.get("opacity") if timing.get("opacity") is not None else 1
    result["controls"] = controls
    return result


def _fades(clip, dispatch, start_frame, end_frame, raw_duration_frames, fps):
    raw = _raw(clip)
    params = _params(clip)
    audio = dispatch["fade_contract"] == "audio_params"
    # Effect components bypass VisualClip.tsx/useFadeOpacity entirely.
    visual = dispatch["fade_contract"] == "visual_effects"
    presentation = clip.get("presentation_fields") if isinstance(clip.get("presentation_fields"), Mapping) else {}
    authored_effects = raw.get("effects", presentation.get("effects"))
    fades = {"fade_in": params.get("fadeIn"), "fade_out": params.get("fadeOut")} if audio else _fade_values(authored_effects) if visual else {}
    records = []
    for key, seconds in fades.items():
        if not _number(seconds) or seconds <= 0:
            continue
        if not _number(seconds * fps):
            raise ValueError("fade duration cannot be represented by pinned frame arithmetic")
        frames = math.floor(seconds * fps + .5)
        if frames < 1:
            continue
        local_a, local_b = (0, frames) if key == "fade_in" else (raw_duration_frames - frames, raw_duration_frames)
        a, b = max(0, local_a), min(end_frame-start_frame, local_b)
        if b <= a:
            continue
        value = lambda t: max(0., min(1., (t-local_a)/frames if key == "fade_in" else (local_b-t)/frames))
        records.append({"kind": key, "property": "gain multiplier" if audio else "opacity multiplier",
                        "start": _wire((start_frame+a)/fps), "end": _wire((start_frame+b)/fps),
                        "before": value(a), "after": value(b),
                        "overlapping_fades": len(fades) > 1 and sum(math.floor(v*fps+.5) for v in fades.values() if _number(v) and v>0 and _number(v*fps)) > raw_duration_frames})
    return records


def project_readable_timing(clips, context=None):
    """Add derived facts without replacing authored fields or exact targets."""
    context = context if isinstance(context, Mapping) else {}
    scheduling_error = context.get("reason", "complete compositor scheduling context unavailable")
    scheduling = None
    if context.get("status") == "complete" or context.get("transition_free") is True:
        try:
            scheduling_context = context if context.get("status") == "complete" else {
                "fps": context.get("fps", 30), "tracks": context.get("tracks", []),
                "clips": [clip["render_timing"] for clip in clips if isinstance(clip.get("render_timing"), Mapping)]}
            scheduling = _schedule(scheduling_context)
        except (ValueError, TypeError, KeyError) as exc:
            scheduling_error = str(exc)
    identities = {row["id"]: row.get("target", {"render_clip_id": row["id"]})
                  for row in context.get("clips", []) if isinstance(row, Mapping)}
    identities.update({(row.get("render_timing") or {}).get("id"): {
        key: row.get(key) for key in ("occurrence_id", "shot_id", "clip_id", "target_kind") if row.get(key) is not None}
        for row in clips})
    result = []
    for original in clips:
        clip = dict(original)
        raw, params = _raw(clip), _params(clip)
        changes, unknowns = [], []
        has_keys = _declares_keyframes(clip)
        supported, clock_reason = _canonical_transform(clip) if has_keys else (False, None)
        if has_keys and not supported:
            unknowns.append(clock_reason)
        timing = clip.get("render_timing")
        dispatch = _compositor_dispatch(clip, context)
        clip["compositor_dispatch"] = dispatch
        mount_track_proven = dispatch["status"] == "resolved"
        if isinstance(timing, Mapping) and scheduling is not None and not mount_track_proven:
            scheduling_error = "compositor track unresolved; mounted clock unavailable"
        if isinstance(timing, Mapping) and scheduling is not None and mount_track_proven and timing.get("id") in scheduling[1]:
            fps, models, mounted, effective, declarations = scheduling
            model = models[timing["id"]]
            interval = mounted[timing["id"]]
            clip["timing_projection"] = {"authority": "managed_shot_expansion+compositor_0.0.6",
                "fps": fps, "mounted": {"start_frame": interval.start, "end_frame": interval.end,
                                           "start": _wire(interval.start/fps), "end": _wire(interval.end/fps)},
                "effective": {"start": _wire(effective[timing["id"]].start), "end": _wire(effective[timing["id"]].end)}}
            try:
                changes.extend(_fades(clip, dispatch, interval.start, interval.end, model.frames.duration_frames, fps))
            except ValueError as exc:
                unknowns.append(str(exc))
            if has_keys:
                if supported:
                    if dispatch["track"]["kind"] == "audio":
                        unknowns.append("canonical transform is dispatched on an audio track; transform keyframes are not rendered")
                    else:
                        try:
                            changes.extend(_transform_changes(_keys(params.get("keyframes")), interval.start/fps, interval.end/fps))
                        except ValueError as exc:
                            unknowns.append(str(exc))
            for declaration in declarations:
                if declaration.get("owner_clip_id", declaration["from_clip_id"]) != timing["id"]:
                    continue
                record = dict(declaration)
                record.pop("owner_clip_id", None)
                record["from_target"] = identities.get(record["from_clip_id"], {"render_clip_id": record["from_clip_id"]})
                record["to_target"] = identities.get(record["to_clip_id"], {"render_clip_id": record["to_clip_id"]})
                if record["status"] == "resolved":
                    record.update(start=_wire(record["start_frame"]/fps), end=_wire(record["end_frame"]/fps))
                clip["transition_projection"] = record
            if raw.get("transition") and "transition_projection" not in clip:
                clip["transition_projection"] = {"status": "unresolved", "transition": raw["transition"],
                                                  "reason": "compositor track is unavailable or not visual"}
        elif has_keys or raw.get("effects") or params.get("fadeIn") or params.get("fadeOut"):
            unknowns.append(clip.get("render_timing_unknown", scheduling_error))
        declared_kind = raw.get("clipType", clip.get("clip_type", "media"))
        if (declared_kind not in {"media", "video", "image", "overlay", "audio", "text", "animated-media-transform"}
                and not unknowns and params.get("keyframes") is None):
            unknowns.append("custom element animation clock is unsupported")
        if clip.get("timing_unknown"):
            unknowns.append("authored clock explicitly unknown")
        if raw.get("transition") and "transition_projection" not in clip:
            clip["transition_projection"] = {"status": "unresolved", "transition": raw["transition"], "reason": scheduling_error}
        if params.get("sourceSegments"):
            clip["source_clock"] = "source playback segments use a separate media clock"
        clip["timed_changes"] = changes
        clip["timing_unknowns"] = list(dict.fromkeys(unknowns))
        result.append(clip)
    return result
