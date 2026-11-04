"""Small, code-first helpers for editing a detached timeline candidate.

The helpers intentionally operate on ordinary JSON-shaped authoring objects.
They do not publish, talk to Runtime, or introduce a second timeline format;
callers pass the resulting candidate to the existing validate/commit path.
All mutations are explicit and deterministic so an agent can use the helpers
without rediscovering identity, timing, and layout edge cases.
"""

from __future__ import annotations

import copy
import hashlib
import math
import re
import uuid
from collections.abc import Mapping, MutableMapping, Sequence
from typing import Any

from astrid.sdk.timeline._common import TimelineEditError


def _is_media_digest(value: Any) -> bool:
    return isinstance(value, str) and bool(re.fullmatch(r"(?:sha256:)?[0-9a-f]{64}", value))


def _mapping(value: Any, label: str) -> MutableMapping[str, Any]:
    if not isinstance(value, MutableMapping):
        raise TimelineEditError(f"{label} must be a mutable object")
    return value


def _list(value: Any, label: str) -> list[Any]:
    if not isinstance(value, list):
        raise TimelineEditError(f"{label} must be a list")
    return value


def _id(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TimelineEditError(f"{label} must be a non-empty string")
    return value


def _stable_id(prefix: str, *parts: Any) -> str:
    seed = "\x1f".join(str(part) for part in parts)
    return f"{prefix}-{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:20]}"


def _next_id(prefix: str, source: Mapping[str, Any] | None = None) -> str:
    if source and isinstance(source.get("id"), str) and source["id"]:
        return _stable_id(prefix, source["id"], uuid.uuid4().hex)
    return f"{prefix}-{uuid.uuid4().hex}"


def _remap_authoring_item_refs(value: Any, item_ids: Mapping[str, str], *, key: str | None = None) -> Any:
    """Remap schema-owned item references while leaving opaque strings alone."""

    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for child_key, child in value.items():
            if isinstance(child_key, str) and isinstance(child, str) and (
                child_key in {"item_id", "source_item_id", "selected_item_id", "parent_item_id"}
                or child_key.endswith("_item_id")
            ):
                result[child_key] = item_ids.get(child, child)
            elif isinstance(child_key, str) and isinstance(child, list) and child_key.endswith("_item_ids"):
                result[child_key] = [item_ids.get(item, item) if isinstance(item, str) else copy.deepcopy(item) for item in child]
            else:
                result[child_key] = _remap_authoring_item_refs(child, item_ids, key=child_key)
        return result
    if isinstance(value, list):
        return [_remap_authoring_item_refs(item, item_ids, key=key) for item in value]
    return copy.deepcopy(value)


def clone_candidate(candidate: Mapping[str, Any]) -> dict[str, Any]:
    """Return a detached candidate while preserving opaque fields."""

    if not isinstance(candidate, Mapping):
        raise TimelineEditError("candidate must be an object")
    return copy.deepcopy(dict(candidate))


def _timeline(container: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    """Resolve a main/shot container to its timeline mapping."""

    if isinstance(container.get("internal_timeline"), MutableMapping):
        return container["internal_timeline"]
    if isinstance(container.get("timeline"), MutableMapping):
        return container["timeline"]
    if isinstance(container.get("config"), MutableMapping):
        return container["config"]
    return container


def _tracks(timeline: MutableMapping[str, Any]) -> list[MutableMapping[str, Any]]:
    tracks = _list(timeline.setdefault("tracks", []), "timeline.tracks")
    if any(not isinstance(track, MutableMapping) for track in tracks):
        raise TimelineEditError("timeline.tracks entries must be objects")
    return tracks


def _track_for(timeline: MutableMapping[str, Any], track: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    """Resolve a track belonging to ``timeline`` before attaching clips."""

    _mapping(track, "track")
    track_id = _id(track.get("id"), "track.id")
    for existing in _tracks(timeline):
        if existing.get("id") == track_id:
            return existing
    raise TimelineEditError(f"track {track_id} does not belong to this timeline")


def _clips(timeline: MutableMapping[str, Any]) -> list[MutableMapping[str, Any]]:
    """Return canonical top-level clips (with a legacy track-local fallback)."""

    clips = _list(timeline.setdefault("clips", []), "timeline.clips")
    if any(not isinstance(clip, MutableMapping) for clip in clips):
        raise TimelineEditError("timeline.clips entries must be objects")
    return clips


def _clip_speed(clip: Mapping[str, Any]) -> Any:
    speed = clip.get("speed", 1)
    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or speed <= 0:
        raise TimelineEditError("clip speed must be positive")
    return speed


def _clip_duration(clip: Mapping[str, Any]) -> Any:
    speed = _clip_speed(clip)
    hold = clip.get("hold")
    if hold is not None:
        if isinstance(hold, bool) or not isinstance(hold, (int, float)) or hold < 0:
            raise TimelineEditError("clip hold must be non-negative")
        source_duration = hold
    else:
        source_from, source_to = clip.get("from", 0), clip.get("to", 0)
        source_duration = source_to - source_from
    duration = source_duration / speed
    if duration <= 0:
        raise TimelineEditError("clip interval must be non-empty")
    return duration


def _set_clip_interval(clip: MutableMapping[str, Any], start: Any, end: Any) -> None:
    if end <= start:
        raise TimelineEditError("clip interval must be non-empty")
    speed = _clip_speed(clip)
    source_duration = (end - start) * speed
    if "hold" in clip:
        clip["hold"] = source_duration
    else:
        source_from = clip.get("from", 0)
        clip["from"] = source_from
        clip["to"] = source_from + source_duration
    clip["at"] = start


def add_track(container: MutableMapping[str, Any], *, kind: str = "video", name: str | None = None, track_id: str | None = None) -> MutableMapping[str, Any]:
    """Add and return a valid empty track in a main timeline or shot."""

    from astrid.sdk.timeline.native_checkout import AttachedCheckout

    attached = container if isinstance(container, AttachedCheckout) else None
    if attached is not None:
        timeline = _timeline(attached.bundle["parent"])
    else:
        timeline = _timeline(container)
    tracks = _tracks(timeline)
    new_id = track_id or _next_id("track")
    if any(track.get("id") == new_id for track in tracks):
        raise TimelineEditError(f"track id already exists: {new_id}")
    # Runtime's canonical timeline schema requires a human-readable label on
    # every track.  Keep ``name`` as a compatibility alias when supplied, but
    # always emit the field the renderer/validator actually consumes.
    track: MutableMapping[str, Any] = {
        "id": new_id,
        "kind": kind,
        "label": name or str(kind).replace("_", " ").title(),
    }
    if name is not None:
        track["name"] = name
    if attached is not None:
        from astrid.sdk.timeline._command_state import State

        attached._unchanged_preview()
        with attached.session.outbox.locked():
            state = attached.session._state()
            if state['pending'] or state['flight'] is not None:
                raise TimelineEditError('attached track creation cannot be mixed with another draft')
        attached._queue({'schema_version': 1, 'op': 'track.create', 'version': '1',
                         'select': 'parent', 'args': {'row': copy.deepcopy(track)}})
        return State(attached.bundle).entity("parent/track:" + new_id)
    tracks.append(track)
    return track


def add_shot(candidate: MutableMapping[str, Any], *, name: str | None = None, shot_id: str | None = None, timeline: Mapping[str, Any] | None = None) -> MutableMapping[str, Any]:
    """Create an independent shot with a fresh identity and detached timeline."""

    shots = _list(candidate.setdefault("shots", []), "candidate.shots")
    new_id = shot_id or _next_id("shot")
    if any(isinstance(shot, Mapping) and shot.get("id") == new_id for shot in shots):
        raise TimelineEditError(f"shot id already exists: {new_id}")
    shot: MutableMapping[str, Any] = {"id": new_id, "timeline": copy.deepcopy(dict(timeline or {"tracks": [], "clips": []}))}
    if name is not None:
        shot["name"] = name
    shots.append(shot)
    return shot


def place_media(container: MutableMapping[str, Any], media: Mapping[str, Any] | str, *, track: MutableMapping[str, Any], start: int | float, end: int | float, clip_id: str | None = None, fit: str | None = None, rect: Mapping[str, Any] | None = None, source_start: int | float | None = None, source_end: int | float | None = None, **fields: Any) -> MutableMapping[str, Any]:
    """Place one exact admitted media reference on an existing track."""

    if end <= start:
        raise TimelineEditError("placement interval must be non-empty")
    reserved = {"at", "from", "to", "source_start", "source_end", "track", "id"}
    overridden = sorted(reserved.intersection(fields))
    if overridden:
        raise TimelineEditError(f"place_media cannot override reserved fields: {', '.join(overridden)}")
    speed = fields.get("speed", 1)
    if isinstance(speed, bool) or not isinstance(speed, (int, float)) or speed <= 0:
        raise TimelineEditError("clip speed must be positive")
    source_from = 0 if source_start is None else source_start
    source_to = source_from + (end - start) * speed if source_end is None else source_end
    if source_to <= source_from:
        raise TimelineEditError("source trim interval must be non-empty")
    if not math.isclose((source_to - source_from) / speed, end - start, rel_tol=1e-9, abs_tol=1e-9):
        raise TimelineEditError("source trim duration and requested timeline duration are inconsistent")
    hold = fields.get("hold")
    if hold is not None:
        if isinstance(hold, bool) or not isinstance(hold, (int, float)) or hold < 0:
            raise TimelineEditError("clip hold must be non-negative")
        if not math.isclose(hold / speed, end - start, rel_tol=1e-9, abs_tol=1e-9):
            raise TimelineEditError("clip hold and requested timeline duration are inconsistent")
    timeline = _timeline(container)
    owned_track = _track_for(timeline, track)
    clips = _clips(timeline)
    media_id = media if isinstance(media, str) else media.get("id", media.get("media_id", media.get("object_id")))
    _id(media_id, "media id")
    selector = "media_id" if _is_media_digest(media_id) else "asset"
    clip: MutableMapping[str, Any] = {"id": clip_id or _next_id("clip"), selector: media_id, "track": owned_track.get("id"), "at": start, "from": source_from, "to": source_to}
    if fit is not None:
        clip["fit"] = fit
    if rect is not None:
        clip["rect"] = copy.deepcopy(dict(rect))
    clip.update(copy.deepcopy(fields))
    if any(existing.get("id") == clip["id"] for existing in clips):
        raise TimelineEditError(f"clip id already exists: {clip['id']}")
    clips.append(clip)
    return clip


def _find_clip(container: MutableMapping[str, Any], clip_id: str) -> tuple[MutableMapping[str, Any], list[MutableMapping[str, Any]], MutableMapping[str, Any]]:
    timeline = _timeline(container)
    clips = _clips(timeline)
    tracks_by_id = {track.get("id"): track for track in _tracks(timeline)}
    for clip in clips:
        if clip.get("id") == clip_id:
            track = tracks_by_id.get(clip.get("track"))
            if track is None:
                raise TimelineEditError(f"clip {clip_id} references unknown track")
            return track, clips, clip
    raise TimelineEditError(f"clip not found: {clip_id}")


def replace_media(container: MutableMapping[str, Any], clip_id: str, media: Mapping[str, Any] | str, *, preserve_interval: bool = True, **fields: Any) -> MutableMapping[str, Any]:
    """Replace the authoritative asset on one clip, preserving timing by default."""

    _, _, clip = _find_clip(container, clip_id)
    media_id = media if isinstance(media, str) else media.get("id", media.get("media_id", media.get("object_id")))
    _id(media_id, "media id")
    old_start = clip.get("at")
    selector = "media_id" if _is_media_digest(media_id) else "asset"
    for key in ("asset", "asset_id", "media_id", "object_id"):
        if key != selector:
            clip.pop(key, None)
    clip[selector] = media_id
    if preserve_interval:
        clip["at"] = old_start
    clip.update(copy.deepcopy(fields))
    return clip


def duplicate(value: MutableMapping[str, Any], *, new_id: str | None = None, id_field: str = "id", remap_prefix: str | None = None) -> MutableMapping[str, Any]:
    """Deep-copy an editable object and remap its owned timeline identities.

    Provenance and arbitrary opaque strings are deliberately untouched. Only
    schema-owned ``id`` fields and clip-to-track references are remapped.
    """

    source = _mapping(value, "value")
    result = copy.deepcopy(dict(source))
    old_id = result.get(id_field)
    result[id_field] = new_id or _stable_id(remap_prefix or str(old_id or "item"), old_id, uuid.uuid4().hex)
    timeline = result.get("timeline")
    if isinstance(timeline, MutableMapping):
        track_map: dict[str, str] = {}
        for track in timeline.get("tracks", []):
            if isinstance(track, MutableMapping) and isinstance(track.get("id"), str):
                old_track_id = track["id"]
                track_map[old_track_id] = _stable_id("track", old_track_id, result[id_field])
                track["id"] = track_map[old_track_id]
        for clip in timeline.get("clips", []):
            if isinstance(clip, MutableMapping):
                if isinstance(clip.get("id"), str):
                    clip["id"] = _stable_id("clip", clip["id"], result[id_field])
                if clip.get("track") in track_map:
                    clip["track"] = track_map[clip["track"]]
    return result


def remove(container: MutableMapping[str, Any], item_id: str, *, kind: str = "clip") -> MutableMapping[str, Any]:
    """Remove one shot/track/clip from its owner without touching media."""

    if kind == "shot":
        items = _list(container.setdefault("shots", []), "candidate.shots")
    else:
        timeline = _timeline(container)
        if kind == "track":
            items = _tracks(timeline)
            for index, item in enumerate(items):
                if item.get("id") == item_id:
                    # Removing a track also removes its owned clips.  Leaving
                    # them behind would create dangling references that fail
                    # the same candidate validation used for publication.
                    clips = _clips(timeline)
                    timeline["clips"] = [
                        clip for clip in clips if clip.get("track") != item_id
                    ]
                    return items.pop(index)
            raise TimelineEditError(f"track not found: {item_id}")
        elif kind == "clip":
            clips = _clips(timeline)
            for index, clip in enumerate(clips):
                if clip.get("id") == item_id:
                    return clips.pop(index)
            raise TimelineEditError(f"clip not found: {item_id}")
        else:
            raise TimelineEditError(f"unsupported removal kind: {kind}")
    for index, item in enumerate(items):
        if item.get("id") == item_id:
            return items.pop(index)
    raise TimelineEditError(f"{kind} not found: {item_id}")


def move(container: MutableMapping[str, Any], clip_id: str, *, track: MutableMapping[str, Any], start: int | float | None = None, end: int | float | None = None) -> MutableMapping[str, Any]:
    """Move one clip to a declared track; timing changes are explicit."""

    timeline = _timeline(container)
    target_track = _track_for(timeline, track)
    old_track, clips, clip = _find_clip(container, clip_id)
    new_start = clip.get("at", 0) if start is None else start
    old_end = new_start + _clip_duration(clip)
    new_end = old_end if end is None else end
    if new_end is None or new_end <= new_start:
        raise TimelineEditError("moved interval must be non-empty")
    clips.remove(clip)
    _set_clip_interval(clip, new_start, new_end)
    clip["track"] = target_track.get("id")
    _clips(timeline).append(clip)
    return clip


def sequence(clips: Sequence[MutableMapping[str, Any]], *, start: int | float = 0, durations: Sequence[int | float] | None = None, gap: int | float = 0) -> Sequence[MutableMapping[str, Any]]:
    """Place supplied clips consecutively; never shifts unrelated tracks."""

    if durations is not None and len(durations) != len(clips):
        raise TimelineEditError("durations must match clips")
    cursor = start
    for index, clip in enumerate(clips):
        duration = durations[index] if durations is not None else _clip_duration(clip)
        if duration <= 0:
            raise TimelineEditError("sequence durations must be positive")
        _set_clip_interval(clip, cursor, cursor + duration)
        cursor += duration + gap
    return clips


def align(clips: Sequence[MutableMapping[str, Any]], anchors: Sequence[int | float], *, edge: str = "start") -> Sequence[MutableMapping[str, Any]]:
    if len(clips) != len(anchors) or edge not in {"start", "end", "center"}:
        raise TimelineEditError("align requires equal clips/anchors and a supported edge")
    for clip, anchor in zip(clips, anchors):
        duration = _clip_duration(clip)
        if duration <= 0:
            raise TimelineEditError("cannot align an empty clip")
        if edge == "start":
            _set_clip_interval(clip, anchor, anchor + duration)
        elif edge == "end":
            _set_clip_interval(clip, anchor - duration, anchor)
        else:
            _set_clip_interval(clip, anchor - duration / 2, anchor + duration / 2)
    return clips


def fit_duration(container: MutableMapping[str, Any], *, mode: str = "extend") -> int | float:
    """Explicitly fit a container's duration to its latest clip end."""

    if mode not in {"extend", "trim"}:
        raise TimelineEditError("mode must be 'extend' or 'trim'")
    timeline = _timeline(container)
    latest = 0
    for clip in _clips(timeline):
        latest = max(latest, clip.get("at", 0) + _clip_duration(clip))
    if mode == "extend":
        timeline["duration"] = max(timeline.get("duration", 0), latest)
    else:
        timeline["duration"] = latest
    return timeline["duration"]


def reorder_layers(container: MutableMapping[str, Any], track_ids: Sequence[str]) -> list[MutableMapping[str, Any]]:
    """Set explicit stacking order; this does not change clip chronology."""

    timeline = _timeline(container)
    tracks = _tracks(timeline)
    by_id = {track.get("id"): track for track in tracks}
    if set(track_ids) != set(by_id) or len(track_ids) != len(by_id):
        raise TimelineEditError("track_ids must contain each existing track exactly once")
    timeline["tracks"] = [by_id[track_id] for track_id in track_ids]
    return timeline["tracks"]


def _authoring_shot(bundle: MutableMapping[str, Any], shot_id: str) -> MutableMapping[str, Any]:
    shots = bundle.get("shots")
    if not isinstance(shots, MutableMapping):
        raise TimelineEditError("authoring bundle shots must be an object")
    shot = shots.get(shot_id)
    if not isinstance(shot, MutableMapping):
        raise TimelineEditError(f"authoring shot not found: {shot_id}")
    return shot


def add_authoring_shot(
    bundle: MutableMapping[str, Any],
    *,
    template_shot_id: str | None = None,
    name: str | None = None,
    shot_id: str | None = None,
    occurrence_id: str | None = None,
    start_ms: int | float | None = None,
) -> MutableMapping[str, Any]:
    """Add an independent authoring shot and optionally place it.

    The source mapping is copied explicitly so the existing bundle compiler
    can materialize a new immutable child revision on the next full commit.
    This helper never calls Runtime or persists a partial operation.
    """

    shots = bundle.get("shots")
    mapping = bundle.get("source_mapping")
    if not isinstance(shots, MutableMapping) or not isinstance(mapping, MutableMapping):
        raise TimelineEditError("bundle is not an authoring bundle")
    source_shots = mapping.get("shots")
    source_placements = mapping.get("placements")
    if not isinstance(source_shots, MutableMapping) or not isinstance(source_placements, MutableMapping):
        raise TimelineEditError("authoring bundle source mapping is incomplete")
    source: MutableMapping[str, Any] | None = None
    template: MutableMapping[str, Any] | None = None
    if template_shot_id is not None:
        template = _authoring_shot(bundle, template_shot_id)
        source_row = source_shots.get(template_shot_id)
        if not isinstance(source_row, MutableMapping):
            raise TimelineEditError(f"source mapping missing for authoring shot: {template_shot_id}")
        source = copy.deepcopy(dict(source_row))
    new_id = shot_id or _next_id("shot")
    if new_id in shots:
        raise TimelineEditError(f"authoring shot id already exists: {new_id}")
    if template is None:
        # Seed the complete canonical child envelopes.  Runtime publication
        # fills these defaults before hashing; emitting them here keeps the
        # detached compiler's digest identical to the server's digest.
        base_payload: MutableMapping[str, Any] = {
            "metadata": {}, "items": [], "pools": [],
            "selected_variants": {}, "provenance": {},
            "generation_inputs": {}, "audio_bindings": [], "text_bindings": [],
        }
        base_internal: MutableMapping[str, Any] = {
            "tracks": [], "clips": [], "effects": [], "audio": [],
            "layout": {}, "registry": {}, "assets": [],
        }
        source = {
            # A synthetic source identity forces the compiler to materialize
            # this new authored shot instead of treating it as an existing
            # immutable child with fabricated revision IDs.
            "shot_id": _stable_id("source-shot", new_id),
            "revision_id": _stable_id("source-shot", new_id),
            "content_digest": "sha256:" + "0" * 64,
            "internal_timeline_id": bundle.get("timeline_id", "main"),
            "internal_timeline_revision_id": _stable_id("source-timeline", new_id),
            "internal_timeline_content_digest": "sha256:" + "0" * 64,
            "item_id_map": {},
        }
    else:
        base_payload = copy.deepcopy(dict(template.get("base_payload", template.get("payload", {}))))
        base_internal = copy.deepcopy(
            dict(template.get("base_internal_timeline", template.get("internal_timeline", {})))
        )
        # A duplicate is an independently editable placement.  Remap only
        # schema-owned item references; arbitrary provenance/opaque literals
        # remain byte-for-byte unchanged.
        items = base_payload.get("items", [])
        if isinstance(items, list):
            item_ids: dict[str, str] = {}
            for index, raw_item in enumerate(items):
                if isinstance(raw_item, Mapping):
                    old_item_id = raw_item.get("item_id", raw_item.get("id"))
                    if isinstance(old_item_id, str) and old_item_id:
                        item_ids[old_item_id] = _stable_id("shot-item", old_item_id, new_id, index)
            if item_ids:
                remapped_items = _remap_authoring_item_refs(items, item_ids)
                base_payload["items"] = remapped_items
                base_internal = _remap_authoring_item_refs(base_internal, item_ids)
    if name is not None:
        base_payload["name"] = name
    shots[new_id] = {
        "shot_id": new_id,
        "base_payload": copy.deepcopy(base_payload),
        "payload": copy.deepcopy(base_payload),
        "base_internal_timeline": copy.deepcopy(base_internal),
        "internal_timeline": copy.deepcopy(base_internal),
    }
    source["shot_id"] = source.get("shot_id", template_shot_id or new_id)
    source_shots[new_id] = source
    if occurrence_id is not None:
        placements = bundle.get("placements")
        if not isinstance(placements, list):
            raise TimelineEditError("authoring bundle placements must be a list")
        if any(row.get("occurrence_id") == occurrence_id for row in placements if isinstance(row, Mapping)):
            raise TimelineEditError(f"occurrence id already exists: {occurrence_id}")
        template_placement = None
        if template_shot_id is not None:
            template_placement = next(
                (row for row in placements if isinstance(row, Mapping) and row.get("shot_id") == template_shot_id),
                None,
            )
        if template_placement is None:
            template_placement = {
                "placement": {"start_ms": 0},
                "source_offset": {"start": 0, "end": 0},
                "duration_ms": 1,
                "speed": {"numerator": 1, "denominator": 1},
                "track": "picture",
                "transform": {},
                "gain": 1,
                "muted": False,
                "provenance": {},
            }
        placement = copy.deepcopy(dict(template_placement))
        placement["occurrence_id"] = occurrence_id
        placement["shot_id"] = new_id
        if start_ms is not None:
            placement.setdefault("placement", {})["start_ms"] = start_ms
        placements.append(placement)
        # Pin the newly-created occurrence's starting point as its local
        # baseline. The compiler can then distinguish inherited legacy
        # metadata from a later unsupported edit on that occurrence.
        source_placements[occurrence_id] = copy.deepcopy(placement)
    return shots[new_id]


def duplicate_authoring_shot(
    bundle: MutableMapping[str, Any],
    shot_id: str,
    *,
    new_shot_id: str | None = None,
    occurrence_id: str | None = None,
    start_ms: int | float | None = None,
) -> MutableMapping[str, Any]:
    """Duplicate a bundle shot with independent payload/placement identities."""

    placements = bundle.get("placements", [])
    owner = next((row for row in placements if row.get("shot_id") == shot_id), None)
    if owner is None:
        raise TimelineEditError("duplicate requires a placed source shot")
    destination = new_shot_id or _next_id("shot")
    occurrence = occurrence_id or _next_id("occurrence")
    result = add_authoring_shot(
        bundle,
        template_shot_id=shot_id,
        shot_id=destination,
        occurrence_id=occurrence,
        start_ms=start_ms,
    )
    bundle.setdefault("_authoring_duplicates", []).append({
        "command": {"schema_version": 1, "op": "occurrence.duplicate", "version": "1",
                    "select": "occurrence:" + owner["occurrence_id"],
                    "args": {"occurrence_id": occurrence, "shot_id": destination},
                    "intent": {"ownership": "occurrence"}},
        "start_ms": start_ms,
    })
    return result


def remove_authoring_shot(bundle: MutableMapping[str, Any], shot_id: str) -> MutableMapping[str, Any]:
    """Remove a shot and its placements from a detached bundle."""

    shots = bundle.get("shots")
    mapping = bundle.get("source_mapping")
    if not isinstance(shots, MutableMapping) or not isinstance(mapping, MutableMapping):
        raise TimelineEditError("bundle is not an authoring bundle")
    shot = _authoring_shot(bundle, shot_id)
    del shots[shot_id]
    source_shots = mapping.get("shots")
    if isinstance(source_shots, MutableMapping):
        source_shots.pop(shot_id, None)
    placements = bundle.get("placements")
    if isinstance(placements, list):
        removed_ids = {
            row.get("occurrence_id") for row in placements
            if isinstance(row, Mapping) and row.get("shot_id") == shot_id
        }
        bundle["placements"] = [
            row for row in placements
            if not (isinstance(row, Mapping) and row.get("shot_id") == shot_id)
        ]
        source_placements = mapping.get("placements")
        if isinstance(source_placements, MutableMapping):
            for occurrence_id in removed_ids:
                source_placements.pop(occurrence_id, None)
    return shot


def move_occurrence_group(
    bundle: MutableMapping[str, Any],
    occurrence_id: str,
    *,
    before_occurrence_id: str,
) -> MutableMapping[str, Any]:
    """Move one complete shot occurrence immediately before another.

    The operation changes only placement order and derived ``start_ms`` values.
    Duration and every shot/item/clip binding remain attached to their stable
    occurrence identity. Existing gaps between non-overlapping placements are
    preserved, so frame-quantized timelines can be moved without flattening
    their authored timing.
    """

    placements = _list(bundle.get("placements"), "authoring bundle placements")
    target_id = _id(occurrence_id, "occurrence id")
    anchor_id = _id(before_occurrence_id, "before occurrence id")
    if target_id == anchor_id:
        raise TimelineEditError("an occurrence cannot be moved before itself")

    indexed: dict[str, MutableMapping[str, Any]] = {}
    for index, value in enumerate(placements):
        row = _mapping(value, f"placements[{index}]")
        row_id = _id(row.get("occurrence_id"), f"placements[{index}].occurrence_id")
        if row_id in indexed:
            raise TimelineEditError(f"duplicate occurrence id: {row_id}")
        indexed[row_id] = row
    if target_id not in indexed:
        raise TimelineEditError(f"occurrence not found: {target_id}")
    if anchor_id not in indexed:
        raise TimelineEditError(f"before occurrence not found: {anchor_id}")

    start_values: list[float] = []
    durations: dict[str, float] = {}
    for row in placements:
        occurrence = indexed[row["occurrence_id"]]
        placement = _mapping(occurrence.get("placement"), f"{occurrence['occurrence_id']}.placement")
        start = placement.get("start_ms")
        duration = occurrence.get("duration_ms")
        if (isinstance(start, bool) or not isinstance(start, (int, float)) or not math.isfinite(start)
                or isinstance(duration, bool) or not isinstance(duration, (int, float))
                or not math.isfinite(duration) or duration <= 0):
            raise TimelineEditError("group move requires finite starts and positive durations")
        start_values.append(float(start))
        durations[occurrence["occurrence_id"]] = float(duration)
    if any(start_values[i] < start_values[i - 1] + durations[placements[i - 1]["occurrence_id"]] - 1e-6
           for i in range(1, len(placements))):
        raise TimelineEditError("group move requires non-overlapping occurrence placements")

    reordered = list(placements)
    target_index = next(i for i, row in enumerate(reordered) if row["occurrence_id"] == target_id)
    anchor_index = next(i for i, row in enumerate(reordered) if row["occurrence_id"] == anchor_id)
    if target_index == anchor_index - 1:
        return bundle
    target = reordered.pop(target_index)
    anchor_index = next(i for i, row in enumerate(reordered) if row["occurrence_id"] == anchor_id)
    reordered.insert(anchor_index, target)

    # Keep the authored leading and inter-placement gaps attached to their
    # timeline slots while assigning the reordered durations. This preserves
    # the final end time even when frame quantization left small gaps.
    gaps = [
        start_values[index] - (
            start_values[index - 1] + durations[placements[index - 1]["occurrence_id"]]
        )
        for index in range(1, len(placements))
    ]
    cursor = start_values[0]
    for index, row in enumerate(reordered):
        _mapping(row.get("placement"), f"{row['occurrence_id']}.placement")["start_ms"] = cursor
        cursor += durations[row["occurrence_id"]]
        if index < len(reordered) - 1:
            cursor += gaps[index]
    bundle["placements"] = reordered
    return bundle


__all__ = [
    "TimelineEditError", "clone_candidate", "add_track", "add_shot", "place_media",
    "replace_media", "duplicate", "remove", "move",
    "sequence", "align", "fit_duration", "reorder_layers",
    "add_authoring_shot", "duplicate_authoring_shot", "remove_authoring_shot",
    "move_occurrence_group",
]
