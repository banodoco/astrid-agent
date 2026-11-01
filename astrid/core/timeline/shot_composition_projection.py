"""Project one prepared shot-composition graph into renderer input.

The editor/Travel lane stores a shot as an immutable revision and places it in
the parent with an occurrence.  Renderers and inspection tools still consume
the ordinary Astrid timeline shape, so this module is the one deliberately
boring bridge between those two representations:

* the graph is validated before it is read;
* internal media, effects, text, and audio clips are copied losslessly;
* occurrence placement is applied in memory, with the same trim/speed rules
  as the timeline duration helpers;
* occurrence identity is retained in ``app.astrid_shot_composition`` rather
  than being inferred from overlapping time ranges; and
* empty children remain visible as metadata and as a bounded blank window.

It never reads or writes legacy ``pinnedShotGroups`` or ``clipType: shot``
data.  Legacy interpretation belongs exclusively to the explicit migration
module.
"""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .duration import clip_timeline_duration
from .shot_composition import validate_shot_composition


class ShotCompositionProjectionError(ValueError):
    """The canonical graph cannot be represented by a flat timeline."""

    code = "unsupported_shot_composition"


@dataclass(frozen=True)
class ShotCompositionProjection:
    """Detached renderer-facing projection and its occurrence proof."""

    config: Mapping[str, Any]
    registry: Mapping[str, Any]
    occurrences: tuple[Mapping[str, Any], ...]
    outputs: tuple[Mapping[str, Any], ...]
    graph: Mapping[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return {
            "config": copy.deepcopy(dict(self.config)),
            "registry": copy.deepcopy(dict(self.registry)),
            "occurrences": copy.deepcopy(list(self.occurrences)),
            "outputs": copy.deepcopy(list(self.outputs)),
            "graph": copy.deepcopy(dict(self.graph)),
        }


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ShotCompositionProjectionError(f"{path} must be an object")
    return value


def _effective_parent_registry(payload: Mapping[str, Any], config: Mapping[str, Any]) -> Mapping[str, Any]:
    """Use the same single parent registry namespace as authoring validation."""
    from .parent_registry import ParentRegistryError, effective_parent_registry

    try:
        return effective_parent_registry(payload)
    except ParentRegistryError as exc:
        raise ShotCompositionProjectionError(f"parent_revision.payload.{exc}") from exc


def _number(value: Any, path: str, *, default: float | None = None) -> float:
    if value is None and default is not None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ShotCompositionProjectionError(f"{path} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise ShotCompositionProjectionError(f"{path} must be finite")
    return result


def _seconds(value: Any, path: str, *, default: float = 0.0) -> float:
    """Read canonical milliseconds or renderer seconds deterministically."""
    if value is None:
        return default
    raw = _number(value, path)
    return raw / 1000.0 if path.endswith("_ms") else raw


def _clip_type(raw: Mapping[str, Any]) -> str:
    value = raw.get("clipType", raw.get("clip_type", raw.get("type", "media")))
    if not isinstance(value, str) or not value:
        raise ShotCompositionProjectionError("internal timeline clip type must be a non-empty string")
    if value == "shot":
        raise ShotCompositionProjectionError("deeper shot nesting is unsupported")
    return value


def _timeline_clip(raw: Mapping[str, Any], *, path: str) -> dict[str, Any]:
    """Convert canonical snake-case fields while preserving ordinary fields."""
    result = copy.deepcopy(dict(raw))
    aliases = {
        "clip_type": "clipType",
        "asset_id": "asset",
        "from_ms": "from",
        "to_ms": "to",
    }
    for source, target in aliases.items():
        if source in result and target not in result:
            value = result.pop(source)
            result[target] = value / 1000.0 if source.endswith("_ms") else value
    if "at_ms" in result and "at" not in result:
        result["at"] = _seconds(result.pop("at_ms"), f"{path}.at_ms")
    if "duration_ms" in result and "hold" not in result and "to" not in result:
        result["hold"] = _seconds(result.pop("duration_ms"), f"{path}.duration_ms")
    result["clipType"] = _clip_type(result)
    if "id" not in result or not isinstance(result.get("id"), str) or not result["id"]:
        raise ShotCompositionProjectionError(f"{path}.id must be a non-empty string")
    if "at" not in result:
        result["at"] = 0.0
    if "track" not in result or not isinstance(result.get("track"), str) or not result["track"]:
        raise ShotCompositionProjectionError(f"{path}.track must be a non-empty string")
    # Canonical clip_type is a source spelling, not an extra renderer field.
    result.pop("clip_type", None)
    return result


def _duration(clip: Mapping[str, Any], *, path: str) -> float:
    try:
        return clip_timeline_duration(clip)
    except (TypeError, ValueError) as exc:
        raise ShotCompositionProjectionError(f"{path} has invalid timing: {exc}") from exc


def _placement_speed(value: Any, *, path: str) -> float:
    """Read a numeric or rational placement playback rate."""
    if isinstance(value, Mapping):
        numerator = _number(value.get("numerator"), f"{path}.numerator")
        denominator = _number(value.get("denominator"), f"{path}.denominator")
        if denominator == 0:
            raise ShotCompositionProjectionError(f"{path}.denominator must not be zero")
        value = numerator / denominator
    result = _number(value, path, default=1.0)
    if result <= 0:
        raise ShotCompositionProjectionError(f"{path} must be positive")
    return result


def _apply_placement_transform(clip: dict[str, Any], occurrence: Mapping[str, Any], *, path: str) -> None:
    """Apply the renderer's explicit geometry subset without guessing opaque keys."""
    transform = occurrence.get("transform")
    if not isinstance(transform, Mapping):
        return
    for key in ("x", "y", "width", "height", "cropTop", "cropBottom", "cropLeft", "cropRight", "opacity"):
        if key not in transform:
            continue
        value = _number(transform[key], f"{path}.transform.{key}")
        if key in {"width", "height"} and value < 0:
            raise ShotCompositionProjectionError(f"{path}.transform.{key} must not be negative")
        clip[key] = value
    if "scale" in transform:
        scale = _number(transform["scale"], f"{path}.transform.scale")
        if scale <= 0:
            raise ShotCompositionProjectionError(f"{path}.transform.scale must be positive")
        if "width" in clip:
            clip["width"] = float(clip["width"]) * scale
        if "height" in clip:
            clip["height"] = float(clip["height"]) * scale
        if "width" not in clip and "height" not in clip and scale != 1:
            raise ShotCompositionProjectionError(
                f"{path}.transform.scale requires explicit clip width/height"
            )


def _base_parts(
    graph: Mapping[str, Any],
    *,
    base_config: Mapping[str, Any] | None,
    base_registry: Mapping[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    parent = graph.get("parent_composition")
    if isinstance(parent, Mapping):
        config_value = parent.get("config", parent.get("timeline"))
        registry_value = parent.get("registry")
    else:
        config_value = None
        registry_value = None
    if config_value is None:
        config_value = base_config
    if config_value is None:
        config_value = graph.get("config", graph.get("timeline", {}))
    if registry_value is None:
        registry_value = base_registry
    if registry_value is None:
        registry_value = graph.get("registry", {"assets": {}})
    config = copy.deepcopy(dict(_mapping(config_value, "base_config")))
    registry = copy.deepcopy(dict(_mapping(registry_value, "base_registry")))
    if not isinstance(config.get("clips", []), list):
        raise ShotCompositionProjectionError("base_config.clips must be a list")
    if not isinstance(config.get("tracks", []), list):
        raise ShotCompositionProjectionError("base_config.tracks must be a list")
    return config, registry


def _add_assets(registry: dict[str, Any], revision: Mapping[str, Any]) -> None:
    assets = registry.setdefault("assets", {})
    if not isinstance(assets, dict):
        raise ShotCompositionProjectionError("registry.assets must be an object")
    for raw in revision.get("assets", []):
        asset = _mapping(raw, "shot_revision.assets[]")
        asset_id = asset.get("asset_id")
        if not isinstance(asset_id, str) or not asset_id:
            raise ShotCompositionProjectionError("shot_revision.assets[].asset_id must be a non-empty string")
        if asset_id in assets:
            continue
        digest = asset.get("digest")
        object_id = asset.get("object_id")
        entry = copy.deepcopy(dict(asset.get("source", {}))) if isinstance(asset.get("source"), Mapping) else {}
        if isinstance(object_id, str):
            entry.setdefault("media_id", object_id)
        if isinstance(digest, str):
            entry.setdefault("content_sha256", digest.removeprefix("sha256:"))
        media_type = asset.get("media_type")
        if media_type is not None and (not isinstance(media_type, str) or not media_type.strip()):
            raise ShotCompositionProjectionError(
                f"shot_revision.assets[{asset_id!r}].media_type must be a non-empty string"
            )
        normalized_media_type = media_type.strip().lower() if isinstance(media_type, str) else ""
        media_kind = next((kind for kind in ("image", "video", "audio")
                           if normalized_media_type == kind or normalized_media_type.startswith(f"{kind}/")), "unknown")
        # Runtime managed-media metadata is authoritative. Alias fields in the
        # payload or nested source are opaque and never default to video/image.
        entry["type"] = media_kind
        assets[asset_id] = entry


def _track_merge(config: dict[str, Any], timeline: Mapping[str, Any]) -> None:
    tracks = config.setdefault("tracks", [])
    by_id = {row.get("id"): row for row in tracks if isinstance(row, Mapping)}
    shot_tracks = timeline.get("tracks", [])
    for index, raw in enumerate(shot_tracks):
        track = _mapping(raw, "internal_timeline_revision.timeline.tracks[]")
        track_id = track.get("id")
        if not isinstance(track_id, str) or not track_id:
            raise ShotCompositionProjectionError("internal track id must be a non-empty string")
        if track_id not in by_id:
            # Keep shot-only layers adjacent to the nearest preceding layer
            # shared with the film, preserving the shot's local layer order.
            preceding = next((str(item.get("id")) for item in reversed(shot_tracks[:index])
                              if isinstance(item, Mapping) and item.get("id") in by_id), None)
            following = next((str(item.get("id")) for item in shot_tracks[index + 1:]
                              if isinstance(item, Mapping) and item.get("id") in by_id), None)
            if following is None:
                # A shot-only suffix belongs after the film's complete track list,
                # including unrelated film-only layers such as music.
                insert_at = len(tracks)
            elif preceding is not None:
                insert_at = next(i + 1 for i, item in enumerate(tracks) if str(item.get("id")) == preceding)
            else:
                # With no preceding shared layer, anchor before the nearest
                # following shared layer while retaining unrelated prefix rows.
                insert_at = next((i for i, item in enumerate(tracks) if str(item.get("id")) == following), len(tracks))
            merged = copy.deepcopy(dict(track))
            tracks.insert(insert_at, merged)
            by_id[track_id] = merged


def _occurrence_window(occurrence: Mapping[str, Any], *, path: str) -> tuple[float, float, float]:
    placement = occurrence.get("placement")
    if "at_ms" in occurrence:
        start_raw = occurrence["at_ms"]
        start_path = f"{path}.at_ms"
    elif isinstance(placement, Mapping) and "start_ms" in placement:
        start_raw = placement["start_ms"]
        start_path = f"{path}.placement.start_ms"
    else:
        start_raw = occurrence.get("at")
        start_path = f"{path}.at"
    duration_raw = occurrence.get("duration_ms", occurrence.get("duration"))
    start = _seconds(start_raw, start_path)
    duration = _seconds(duration_raw, f"{path}.duration_ms" if "duration_ms" in occurrence else f"{path}.duration", default=0.0)
    if duration < 0:
        raise ShotCompositionProjectionError(f"{path}.duration must not be negative")
    return start, start + duration, duration


def _project_clip(
    raw: Mapping[str, Any],
    *,
    occurrence: Mapping[str, Any],
    revision: Mapping[str, Any],
    occurrence_start: float,
    occurrence_end: float,
    occurrence_speed: float,
    path: str,
) -> dict[str, Any] | None:
    clip = _timeline_clip(raw, path=path)
    relative_at = _number(clip.get("at", 0), f"{path}.at", default=0.0)
    _placement_speed(occurrence_speed, path=f"{path}.occurrence_speed")
    source_duration = _duration(clip, path=path)
    # The occurrence speed is a placement property.  Keep the authored child
    # speed intact and expose the placement value in the immutable app
    # namespace; timing remains the authored child window, as in the editor.
    absolute_at = occurrence_start + relative_at
    absolute_end = absolute_at + source_duration
    if absolute_at >= occurrence_end or absolute_end <= occurrence_start:
        return None
    clipped_end = min(absolute_end, occurrence_end)
    visible_duration = max(0.0, clipped_end - absolute_at)
    if visible_duration <= 0:
        return None
    child_speed = _number(clip.get("speed", 1), f"{path}.speed", default=1.0)
    if child_speed <= 0:
        raise ShotCompositionProjectionError(f"{path}.speed must be positive")
    if "hold" in clip:
        # ``hold`` is stored in source seconds. Keep the authored speed on the
        # projected clip and convert the visible cap back to source units so
        # the renderer divides by speed exactly once.
        clip["hold"] = visible_duration * child_speed
    elif "to" in clip:
        source_from = _number(clip.get("from", 0), f"{path}.from", default=0.0)
        clip["to"] = source_from + visible_duration * child_speed
    clip["at"] = absolute_at
    _apply_placement_transform(clip, occurrence, path=path)
    original_id = str(clip["id"])
    occurrence_id = occurrence["occurrence_id"]
    clip["id"] = f"{occurrence_id}:{original_id}"
    clip["shot_id"] = revision["shot_id"]
    clip["shot_occurrence_id"] = occurrence_id
    name = occurrence.get("name")
    if isinstance(name, str) and name:
        clip["shot_name"] = name
    # ``app`` is an existing schema escape hatch for immutable provenance and
    # keeps output identity/source-offset/gain/mute available to every
    # downstream consumer without widening ordinary clip authoring fields.
    app = clip.setdefault("app", {})
    if not isinstance(app, dict):
        app = {}
        clip["app"] = app
    app["astrid_shot_composition"] = {
        "project_id": occurrence.get("project_id"),
        "timeline_id": occurrence.get("timeline_id"),
        "shot_id": revision["shot_id"],
        "shot_revision_id": revision["revision_id"],
        "internal_timeline_revision_id": _mapping(revision["internal_timeline_revision"], f"{path}.revision").get("revision_id"),
        "occurrence_id": occurrence_id,
        "output_identity": occurrence.get("output_identity"),
        "stable_deep_link": occurrence.get("stable_deep_link"),
        "source_offset": copy.deepcopy(occurrence.get("source_offset", 0)),
        "speed": occurrence_speed,
        "gain": occurrence.get("gain", 1),
        "muted": bool(occurrence.get("muted", occurrence.get("mute", False))),
        "source_clip_id": original_id,
    }
    gain = occurrence.get("gain", 1)
    # Only write volume when the clip already has one or the gain changes it;
    # an absent volume means unity, and inventing 1.0 would unmute footage.
    if isinstance(gain, (int, float)) and not isinstance(gain, bool) and ("volume" in clip or float(gain) != 1.0):
        clip["volume"] = float(clip.get("volume", 1)) * float(gain)
    if bool(occurrence.get("muted", occurrence.get("mute", False))):
        clip["volume"] = 0
    return clip


def project_shot_composition(
    graph: Mapping[str, Any],
    *,
    base_config: Mapping[str, Any] | None = None,
    base_registry: Mapping[str, Any] | None = None,
) -> ShotCompositionProjection:
    """Project a prepared canonical graph into ordinary timeline input."""
    prepared = validate_shot_composition(graph)
    config, registry = _base_parts(prepared, base_config=base_config, base_registry=base_registry)
    revisions = {
        (row["shot_id"], row["revision_id"]): row
        for row in prepared["shot_revisions"]
    }
    config_clips = [copy.deepcopy(dict(item)) for item in config.get("clips", [])]
    occurrences: list[dict[str, Any]] = []
    outputs: list[dict[str, Any]] = []
    for index, raw_occurrence in enumerate(prepared["occurrences"]):
        occurrence = dict(_mapping(raw_occurrence, f"occurrences[{index}]"))
        key = (occurrence["shot_id"], occurrence["revision_id"])
        revision = revisions.get(key)
        if revision is None:
            raise ShotCompositionProjectionError(
                f"occurrences[{index}] references missing dependency {key[0]!r}/{key[1]!r}"
            )
        internal = _mapping(revision["internal_timeline_revision"], f"shot_revisions[{index}].internal_timeline_revision")
        timeline = _mapping(internal.get("timeline"), f"shot_revisions[{index}].internal_timeline_revision.timeline")
        if timeline.get("compositing") not in (None, "source_over", "normal"):
            raise ShotCompositionProjectionError("unsupported compositing mode in internal timeline")
        _track_merge(config, timeline)
        start, end, duration = _occurrence_window(occurrence, path=f"occurrences[{index}]")
        occurrence["project_id"] = prepared["project"]["project_id"]
        occurrence["timeline_id"] = prepared["primary_timeline"]["document_id"]
        occurrence["duration_seconds"] = duration
        occurrence["blank"] = not bool(timeline.get("clips"))
        occurrences.append(copy.deepcopy(occurrence))
        outputs.append({
            "occurrence_id": occurrence["occurrence_id"],
            "output_identity": occurrence["output_identity"],
            "stable_deep_link": occurrence["stable_deep_link"],
            "shot_id": occurrence["shot_id"],
            "revision_id": occurrence["revision_id"],
            "internal_timeline_revision_id": internal.get("revision_id"),
            "at_ms": occurrence.get("at_ms", round(start * 1000)),
            "duration_ms": occurrence.get("duration_ms", round(duration * 1000)),
            "blank": occurrence["blank"],
        })
        speed = _placement_speed(
            occurrence.get("speed", 1), path=f"occurrences[{index}].speed"
        )
        for clip_index, raw_clip in enumerate(timeline.get("clips", [])):
            clip = _mapping(raw_clip, f"shot_revisions[{index}].timeline.clips[{clip_index}]")
            projected = _project_clip(
                clip,
                occurrence=occurrence,
                revision=revision,
                occurrence_start=start,
                occurrence_end=end,
                occurrence_speed=speed,
                path=f"shot_revisions[{index}].timeline.clips[{clip_index}]",
            )
            if projected is not None:
                config_clips.append(projected)
        _add_assets(registry, revision)

    config["clips"] = config_clips
    app = config.setdefault("app", {})
    if not isinstance(app, dict):
        app = {}
        config["app"] = app
    app["astrid_shot_composition"] = {
        "schema_version": 1,
        "project_id": prepared["project"]["project_id"],
        "timeline_id": prepared["primary_timeline"]["document_id"],
        "occurrences": copy.deepcopy(occurrences),
        "outputs": copy.deepcopy(outputs),
    }
    return ShotCompositionProjection(
        config=config,
        registry=registry,
        occurrences=tuple(occurrences),
        outputs=tuple(outputs),
        graph=copy.deepcopy(prepared),
    )


def project_runtime_parent_composition(
    parent_revision: Mapping[str, Any],
    *,
    shot_revisions: Sequence[Mapping[str, Any]],
    internal_timeline_revisions: Sequence[Mapping[str, Any]],
) -> ShotCompositionProjection:
    """Project one exact Runtime parent-revision closure for rendering.

    Runtime parent occurrences pin immutable shot revisions, and those shot
    revisions in turn pin immutable internal-timeline revisions.  This path
    deliberately consumes those records directly instead of consulting the
    mutable timeline documents used by the legacy ``clipType: shot`` adapter.

    Internal clip and asset identifiers are local to their child timeline.
    Namespace both by occurrence while flattening so independently-authored
    children may reuse the same local identifiers without colliding.
    """

    parent = _mapping(parent_revision, "parent_revision")
    project_id = parent.get("project_id")
    timeline_id = parent.get("timeline_id")
    parent_revision_id = parent.get("revision_id")
    if not isinstance(project_id, str) or not project_id:
        raise ShotCompositionProjectionError("parent_revision.project_id must be a non-empty string")
    if not isinstance(timeline_id, str) or not timeline_id:
        raise ShotCompositionProjectionError("parent_revision.timeline_id must be a non-empty string")
    if not isinstance(parent_revision_id, str) or not parent_revision_id:
        raise ShotCompositionProjectionError("parent_revision.revision_id must be a non-empty string")
    payload = _mapping(parent.get("payload"), "parent_revision.payload")
    config_value = _mapping(payload.get("config"), "parent_revision.payload.config")
    registry_value = _effective_parent_registry(payload, config_value)
    payload_clips = payload.get("clips", [])
    config_clips = config_value.get("clips", [])
    for location, clips in (("clips", payload_clips), ("config.clips", config_clips)):
        if not isinstance(clips, list):
            raise ShotCompositionProjectionError(f"parent_revision.payload.{location} must be a list")
    if payload_clips and config_clips and payload_clips != config_clips:
        raise ShotCompositionProjectionError(
            "parent_revision.payload.clips and payload.config.clips disagree"
        )
    ordinary_clips = payload_clips or config_clips
    raw_occurrences = payload.get("occurrences")
    if not isinstance(raw_occurrences, list):
        raise ShotCompositionProjectionError("parent_revision.payload.occurrences must be a list")

    config = copy.deepcopy(dict(config_value))
    registry = copy.deepcopy(dict(registry_value))
    config["clips"] = [
        copy.deepcopy(dict(_mapping(clip, f"parent_revision.payload.clips[{index}]")))
        for index, clip in enumerate(ordinary_clips)
    ]
    if not isinstance(config.get("tracks", []), list):
        raise ShotCompositionProjectionError("parent_revision.payload.config.tracks must be a list")
    assets = registry.setdefault("assets", {})
    if not isinstance(assets, dict):
        raise ShotCompositionProjectionError("parent_revision.payload.registry.assets must be an object")

    shots: dict[tuple[str, str], Mapping[str, Any]] = {}
    for index, raw in enumerate(shot_revisions):
        shot = _mapping(raw, f"shot_revisions[{index}]")
        key = (shot.get("shot_id"), shot.get("revision_id"))
        if not all(isinstance(value, str) and value for value in key):
            raise ShotCompositionProjectionError(
                f"shot_revisions[{index}] must carry shot_id and revision_id"
            )
        if key in shots:
            raise ShotCompositionProjectionError(f"duplicate shot revision {key[0]!r}/{key[1]!r}")
        shots[key] = shot

    internals: dict[str, Mapping[str, Any]] = {}
    for index, raw in enumerate(internal_timeline_revisions):
        internal = _mapping(raw, f"internal_timeline_revisions[{index}]")
        revision_id = internal.get("revision_id")
        if not isinstance(revision_id, str) or not revision_id:
            raise ShotCompositionProjectionError(
                f"internal_timeline_revisions[{index}].revision_id must be a non-empty string"
            )
        if revision_id in internals:
            raise ShotCompositionProjectionError(f"duplicate internal timeline revision {revision_id!r}")
        internals[revision_id] = internal

    occurrences: list[dict[str, Any]] = []
    outputs: list[dict[str, Any]] = []
    seen_occurrences: set[str] = set()
    for index, raw in enumerate(raw_occurrences):
        occurrence = copy.deepcopy(dict(_mapping(raw, f"occurrences[{index}]")))
        occurrence_id = occurrence.get("occurrence_id")
        shot_id = occurrence.get("shot_id")
        shot_revision_id = occurrence.get("shot_revision_id", occurrence.get("revision_id"))
        if not isinstance(occurrence_id, str) or not occurrence_id:
            raise ShotCompositionProjectionError(
                f"occurrences[{index}].occurrence_id must be a non-empty string"
            )
        if occurrence_id in seen_occurrences:
            raise ShotCompositionProjectionError(f"duplicate occurrence {occurrence_id!r}")
        seen_occurrences.add(occurrence_id)
        if not isinstance(shot_id, str) or not isinstance(shot_revision_id, str):
            raise ShotCompositionProjectionError(
                f"occurrences[{index}] must pin shot_id and shot_revision_id"
            )
        shot = shots.get((shot_id, shot_revision_id))
        if shot is None:
            raise ShotCompositionProjectionError(
                f"occurrences[{index}] references missing dependency {shot_id!r}/{shot_revision_id!r}"
            )
        internal_revision_id = shot.get("internal_timeline_revision_id")
        if not isinstance(internal_revision_id, str) or not internal_revision_id:
            shot_payload = _mapping(shot.get("payload"), f"shot_revisions[{index}].payload")
            internal_revision_id = shot_payload.get("internal_timeline_revision_id")
        internal = internals.get(internal_revision_id) if isinstance(internal_revision_id, str) else None
        if internal is None:
            raise ShotCompositionProjectionError(
                f"shot revision {shot_revision_id!r} is missing its pinned internal timeline"
            )
        timeline = _mapping(
            internal.get("payload"),
            f"internal_timeline_revisions[{internal_revision_id!r}].payload",
        )
        if timeline.get("compositing") not in (None, "source_over", "normal"):
            raise ShotCompositionProjectionError("unsupported compositing mode in internal timeline")
        _track_merge(config, timeline)

        occurrence["revision_id"] = shot_revision_id
        occurrence["project_id"] = project_id
        occurrence["timeline_id"] = timeline_id
        occurrence.setdefault("ordinal", index)
        occurrence.setdefault(
            "output_identity", f"{parent_revision_id}:{occurrence_id}"
        )
        occurrence.setdefault(
            "stable_deep_link",
            f"astrid://projects/{project_id}/timelines/{timeline_id}/occurrences/{occurrence_id}",
        )
        start, end, duration = _occurrence_window(occurrence, path=f"occurrences[{index}]")
        occurrence["at"] = start
        occurrence["hold"] = duration
        occurrence["duration_seconds"] = duration
        occurrence["blank"] = not bool(timeline.get("clips"))
        occurrences.append(copy.deepcopy(occurrence))
        outputs.append({
            "occurrence_id": occurrence_id,
            "output_identity": occurrence["output_identity"],
            "stable_deep_link": occurrence["stable_deep_link"],
            "shot_id": shot_id,
            "revision_id": shot_revision_id,
            "internal_timeline_revision_id": internal_revision_id,
            "at_ms": occurrence.get("at_ms", round(start * 1000)),
            "duration_ms": occurrence.get("duration_ms", round(duration * 1000)),
            "blank": occurrence["blank"],
        })

        local_registry = timeline.get("registry", {})
        local_assets = local_registry.get("assets", {}) if isinstance(local_registry, Mapping) else {}
        if not isinstance(local_assets, Mapping):
            raise ShotCompositionProjectionError(
                f"internal_timeline_revisions[{internal_revision_id!r}].payload.registry.assets must be an object"
            )
        asset_ids: dict[str, str] = {}
        for asset_id, entry in local_assets.items():
            if not isinstance(asset_id, str) or not isinstance(entry, Mapping):
                raise ShotCompositionProjectionError("internal registry assets must be named objects")
            projected_id = f"{occurrence_id}:{asset_id}"
            assets[projected_id] = copy.deepcopy(dict(entry))
            asset_ids[asset_id] = projected_id

        speed = _placement_speed(occurrence.get("speed", 1), path=f"occurrences[{index}].speed")
        revision = {
            "shot_id": shot_id,
            "revision_id": shot_revision_id,
            "internal_timeline_revision": {
                "revision_id": internal_revision_id,
                "timeline": timeline,
            },
        }
        for clip_index, raw_clip in enumerate(timeline.get("clips", [])):
            clip = _mapping(
                raw_clip,
                f"internal_timeline_revisions[{internal_revision_id!r}].payload.clips[{clip_index}]",
            )
            projected = _project_clip(
                clip,
                occurrence=occurrence,
                revision=revision,
                occurrence_start=start,
                occurrence_end=end,
                occurrence_speed=speed,
                path=f"internal_timeline_revisions[{internal_revision_id!r}].payload.clips[{clip_index}]",
            )
            if projected is None:
                continue
            for selector in ("asset", "asset_id"):
                selected = projected.get(selector)
                if isinstance(selected, str) and selected in asset_ids:
                    projected[selector] = asset_ids[selected]
            config["clips"].append(projected)

    app = config.setdefault("app", {})
    if not isinstance(app, dict):
        app = {}
        config["app"] = app
    app["astrid_shot_composition"] = {
        "schema_version": 1,
        "project_id": project_id,
        "timeline_id": timeline_id,
        "parent_revision_id": parent_revision_id,
        "occurrences": copy.deepcopy(occurrences),
        "outputs": copy.deepcopy(outputs),
    }
    closure = {
        "parent_revision": copy.deepcopy(dict(parent)),
        "shot_revisions": [copy.deepcopy(dict(row)) for row in shot_revisions],
        "internal_timeline_revisions": [
            copy.deepcopy(dict(row)) for row in internal_timeline_revisions
        ],
    }
    return ShotCompositionProjection(
        config=config,
        registry=registry,
        occurrences=tuple(occurrences),
        outputs=tuple(outputs),
        graph=closure,
    )


project_canonical_shot_composition = project_shot_composition


__all__ = [
    "ShotCompositionProjection",
    "ShotCompositionProjectionError",
    "project_runtime_parent_composition",
    "project_shot_composition",
]
