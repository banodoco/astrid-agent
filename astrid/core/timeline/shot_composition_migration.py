"""One-time migration of legacy shot shapes into canonical composition.

This module is intentionally the only Astrid code that understands the two
legacy shapes being retired:

* Astrid child clips (``clipType == "shot"`` with a child timeline pointer),
* Astrid App pinned groups (``pinnedShotGroups`` with a list of parent clip IDs).

It is an explicit, read-then-publish operation.  The normal canonical adapter
does not import this module and never interprets either legacy shape.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from .asset_dimensions import canonicalize_asset_dimensions
from .shot_composition import prepare_shot_composition, validate_shot_composition

MIGRATION_SCHEMA_VERSION = 1
MIGRATION_RECEIPT_TYPE = "astrid.shot-composition-migration.v1"
class ShotCompositionMigrationError(RuntimeError):
    """Base class for explicit migration failures."""

    code = "migration_error"


class MigrationAmbiguityError(ShotCompositionMigrationError):
    code = "ambiguous_legacy_group"

    def __init__(
        self,
        message: str,
        *,
        project_id: str,
        timeline_id: str,
        group_id: str,
        recovery: str,
    ) -> None:
        self.project_id = project_id
        self.timeline_id = timeline_id
        self.group_id = group_id
        self.recovery = recovery
        super().__init__(message)

    @property
    def details(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "timeline_id": self.timeline_id,
            "group_id": self.group_id,
            "recovery": self.recovery,
        }


class MigrationSourceChangedError(ShotCompositionMigrationError):
    code = "migration_source_changed"


class MigrationInterruptedError(ShotCompositionMigrationError):
    code = "migration_interrupted"


class MigrationRuntimeUnavailableError(ShotCompositionMigrationError):
    code = "runtime_composition_unavailable"


@dataclass(frozen=True)
class MigrationInventoryItem:
    project_id: str
    timeline_id: str
    document_id: str
    source_fingerprint: str
    astrid_child_count: int
    astrid_group_count: int
    affected: bool
    source_names: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "project_id": self.project_id,
            "timeline_id": self.timeline_id,
            "document_id": self.document_id,
            "source_fingerprint": self.source_fingerprint,
            "astrid_child_count": self.astrid_child_count,
            "astrid_group_count": self.astrid_group_count,
            "affected": self.affected,
            "source_names": list(self.source_names),
        }


@dataclass(frozen=True)
class MigrationInventory:
    schema_version: int
    items: tuple[MigrationInventoryItem, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "items": [item.as_dict() for item in self.items],
        }


@dataclass(frozen=True)
class MigrationPlan:
    """Detached, deterministic work product.  Building a plan is read-only."""

    project_id: str
    timeline_id: str
    document_id: str
    source_fingerprint: str
    migration_id: str
    idempotency_key: str
    graph: Mapping[str, Any]
    publication: Mapping[str, Any]
    identity_mapping: Mapping[str, Any]
    activation_marker: Mapping[str, Any]
    media_digests: tuple[str, ...]
    source_counts: Mapping[str, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": MIGRATION_SCHEMA_VERSION,
            "migration_id": self.migration_id,
            "project_id": self.project_id,
            "timeline_id": self.timeline_id,
            "document_id": self.document_id,
            "source_fingerprint": self.source_fingerprint,
            "idempotency_key": self.idempotency_key,
            "graph": copy.deepcopy(dict(self.graph)),
            "publication": copy.deepcopy(dict(self.publication)),
            "identity_mapping": copy.deepcopy(dict(self.identity_mapping)),
            "activation_marker": copy.deepcopy(dict(self.activation_marker)),
            "media_digests": list(self.media_digests),
            "source_counts": dict(self.source_counts),
        }


class RuntimeCompositionMigrationPort(Protocol):
    """The Runtime composition seam used by the migration.

    The optional ledger/reload methods are discovered at runtime so the same
    plan can be rehearsed against a small fake and activated against a Runtime
    client that exposes the durable migration marker extension.
    """

    def publish_parent_composition(
        self,
        project_id: str,
        timeline_id: str,
        publication: Mapping[str, Any],
        *,
        idempotency_key: str,
    ) -> Any:
        ...


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _content_digest(value: Any) -> str:
    return "sha256:" + hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _identity(prefix: str, *parts: Any) -> str:
    seed = "\x1f".join(str(part) for part in parts)
    return f"{prefix}-{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:32]}"


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ShotCompositionMigrationError(f"{field} must be a non-empty string")
    return value


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ShotCompositionMigrationError(f"{field} must be an object")
    return value


def _copy(value: Any) -> Any:
    return copy.deepcopy(value)


def _canonical_provenance(value: Any) -> Any:
    """Retain useful lineage while removing retired shape discriminators.

    The canonical validator intentionally rejects these keys anywhere in a
    graph.  Migration receipts retain the source fingerprint and identity
    mapping, while this provenance view retains the non-shape source fields.
    """
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_provenance(item)
            for key, item in value.items()
            if key not in {"clipType", "pinnedShotGroups"}
        }
    if isinstance(value, list):
        return [_canonical_provenance(item) for item in value]
    return _copy(value)


def _as_digest(value: Any, fallback: Any) -> str:
    if isinstance(value, str):
        bare = value.removeprefix("sha256:")
        if len(bare) == 64 and all(char in "0123456789abcdef" for char in bare.lower()):
            return "sha256:" + bare.lower()
    return _content_digest(fallback)


def _unwrap_data(value: Any) -> Any:
    if isinstance(value, Mapping) and "data" in value and value.get("ok") is not False:
        return value["data"]
    return value


def _project_id(project: Mapping[str, Any]) -> str:
    return _string(project.get("project_id", project.get("id", project.get("slug"))), "project_id")


def _timeline_id(timeline: Mapping[str, Any]) -> str:
    return _string(timeline.get("timeline_id", timeline.get("id", timeline.get("slug"))), "timeline_id")


def _timeline_config(timeline: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    config_value = timeline.get("config", timeline.get("timeline", timeline))
    config = _copy(_mapping(config_value, "timeline.config"))
    registry_value = timeline.get("registry", config.pop("registry", {}))
    registry = _copy(_mapping(registry_value, "timeline.registry"))
    registry.setdefault("assets", {})
    if not isinstance(registry["assets"], Mapping):
        raise ShotCompositionMigrationError("timeline.registry.assets must be an object")
    return config, registry


def _project_timelines(project: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    timelines = project.get("timelines", [])
    if isinstance(timelines, Mapping):
        return [dict(value, timeline_id=key) if isinstance(value, Mapping) else {"timeline_id": key} for key, value in timelines.items()]
    if isinstance(timelines, Sequence) and not isinstance(timelines, (str, bytes)):
        return [_mapping(value, "project.timelines[]") for value in timelines]
    if "timeline_id" in project or "timeline" in project or "config" in project:
        return [project]
    return []


def _project_rows(source: Any) -> list[dict[str, Any]]:
    """Read a source snapshot without writing it.

    A mapping/sequence is the rehearsal format.  A Runtime-like reader may
    instead expose ``list_projects``, ``list_timelines`` and ``get_timeline``.
    """
    if isinstance(source, Mapping):
        projects = source.get("projects")
        if projects is None:
            projects = [source]
    elif isinstance(source, Sequence) and not isinstance(source, (str, bytes)):
        projects = source
    else:
        if not hasattr(source, "list_projects"):
            raise ShotCompositionMigrationError("source must be a snapshot or read-only source reader")
        listed_projects = source.list_projects()
        projects = listed_projects[0] if isinstance(listed_projects, tuple) and len(listed_projects) == 2 else listed_projects
    rows: list[dict[str, Any]] = []
    for raw_project in projects:
        project = dict(_mapping(raw_project, "projects[]"))
        pid = _project_id(project)
        timelines = _project_timelines(project)
        if hasattr(source, "list_timelines") and not timelines:
            listed = source.list_timelines(pid)
            timelines = list(listed[0] if isinstance(listed, tuple) and len(listed) == 2 else listed)
        for raw_timeline in timelines:
            timeline = dict(_mapping(raw_timeline, "timelines[]"))
            tid = _timeline_id(timeline)
            if hasattr(source, "get_timeline"):
                loaded = source.get_timeline(pid, tid)
                loaded = _unwrap_data(loaded)
                if isinstance(loaded, Mapping):
                    merged = dict(timeline)
                    merged.update(loaded)
                    timeline = merged
            row = dict(project)
            row["project_id"] = pid
            row["timeline"] = timeline
            rows.append(row)
    return rows


def _child_timelines(project_row: Mapping[str, Any], timeline: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    values: Any = timeline.get("child_timelines", timeline.get("children", {}))
    if not values:
        values = project_row.get("child_timelines", {})
    result: dict[str, Mapping[str, Any]] = {}
    if isinstance(values, Mapping):
        for key, value in values.items():
            if isinstance(value, Mapping):
                result[str(key)] = value
    elif isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
        for value in values:
            if isinstance(value, Mapping):
                result[_timeline_id(value)] = value
    return result


def _merge_legacy_registries(
    parent_registry: Mapping[str, Any], child_registry: Mapping[str, Any]
) -> dict[str, Any]:
    """Match legacy expansion's parent-wins asset namespace for a child."""
    merged = _copy(dict(child_registry))
    parent_assets = parent_registry.get("assets", {})
    child_assets = merged.get("assets", {})
    if isinstance(parent_assets, Mapping) and isinstance(child_assets, Mapping):
        assets = _copy(dict(parent_assets))
        assets.update(_copy(dict(child_assets)))
        # Parent entries are authoritative on a colliding document-local key.
        assets.update({key: _copy(value) for key, value in parent_assets.items()})
        merged["assets"] = assets
    return merged


def _shot_records(project_row: Mapping[str, Any], timeline: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    values: Any = timeline.get("shots", project_row.get("shots", {}))
    result: dict[str, Mapping[str, Any]] = {}
    if isinstance(values, Mapping):
        for key, value in values.items():
            if isinstance(value, Mapping):
                result[str(key)] = value
    elif isinstance(values, Sequence) and not isinstance(values, (str, bytes)):
        for value in values:
            if isinstance(value, Mapping):
                sid = value.get("shot_id", value.get("id"))
                if isinstance(sid, str):
                    result[sid] = value
    return result


def _legacy_groups(config: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    groups = config.get("pinnedShotGroups", [])
    if groups is None:
        return []
    if not isinstance(groups, list):
        raise ShotCompositionMigrationError("legacy pinnedShotGroups must be a list")
    return [_mapping(group, "pinnedShotGroups[]") for group in groups]


def _legacy_child_clips(config: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    clips = config.get("clips", [])
    if not isinstance(clips, list):
        raise ShotCompositionMigrationError("legacy timeline clips must be a list")
    return [clip for clip in clips if isinstance(clip, Mapping) and clip.get("clipType") == "shot"]


def _source_fingerprint(project_row: Mapping[str, Any], timeline: Mapping[str, Any]) -> str:
    return _content_digest({"project": _copy(project_row), "timeline": _copy(timeline)})


def inventory_legacy_shot_compositions(source: Any) -> MigrationInventory:
    """Inventory affected projects/timelines without calling a write seam."""
    items: list[MigrationInventoryItem] = []
    for row in _project_rows(source):
        timeline = _mapping(row["timeline"], "timeline")
        config, _ = _timeline_config(timeline)
        children = _legacy_child_clips(config)
        groups = _legacy_groups(config)
        names = tuple(
            str(value)
            for value in (timeline.get("name"), timeline.get("slug"))
            if isinstance(value, str) and value
        )
        tid = _timeline_id(timeline)
        document_id = str(timeline.get("document_id", tid))
        items.append(
            MigrationInventoryItem(
                project_id=_project_id(row),
                timeline_id=tid,
                document_id=document_id,
                source_fingerprint=_source_fingerprint(row, timeline),
                astrid_child_count=len(children),
                astrid_group_count=len(groups),
                affected=bool(children or groups),
                source_names=names,
            )
        )
    return MigrationInventory(MIGRATION_SCHEMA_VERSION, tuple(items))


def _ensure_unique(
    seen: dict[str, str], value: str, *, project_id: str, timeline_id: str, kind: str
) -> None:
    previous = seen.get(value)
    if previous is not None:
        raise MigrationAmbiguityError(
            f"{kind} identity {value!r} occurs more than once",
            project_id=project_id,
            timeline_id=timeline_id,
            group_id=value,
            recovery="Give each legacy group/clip a unique stable ID, then rerun rehearsal.",
        )
    seen[value] = value


def _require_legacy_group_contiguous(
    *,
    project_id: str,
    timeline_id: str,
    group_id: str,
    clip_ids: list[str],
    clips: list[Mapping[str, Any]],
) -> list[int]:
    if not clip_ids or len(set(clip_ids)) != len(clip_ids):
        raise MigrationAmbiguityError(
            f"legacy group {group_id!r} must contain unique clip IDs",
            project_id=project_id,
            timeline_id=timeline_id,
            group_id=group_id,
            recovery="Repair clipIds so every selected parent clip appears exactly once, then rerun rehearsal.",
        )
    positions = [index for index, clip in enumerate(clips) if clip.get("id") in clip_ids]
    if len(positions) != len(clip_ids) or positions != list(range(min(positions), max(positions) + 1)):
        raise MigrationAmbiguityError(
            f"legacy group {group_id!r} is interleaved or references a missing parent clip",
            project_id=project_id,
            timeline_id=timeline_id,
            group_id=group_id,
            recovery="Make the group's clipIds a contiguous authored range (or split it into separate groups), then rerun rehearsal.",
        )
    if [clips[index].get("id") for index in positions] != clip_ids:
        raise MigrationAmbiguityError(
            f"legacy group {group_id!r} does not preserve authored clip order",
            project_id=project_id,
            timeline_id=timeline_id,
            group_id=group_id,
            recovery="Order clipIds exactly as the parent timeline, then rerun rehearsal.",
        )
    return positions


def _clip_duration_ms(clip: Mapping[str, Any]) -> int:
    speed = float(clip.get("speed", 1))
    if speed <= 0:
        raise ShotCompositionMigrationError(f"clip {clip.get('id', '?')} has invalid speed")
    if "hold" in clip:
        duration = float(clip["hold"]) / speed
    else:
        duration = (float(clip.get("to", 0)) - float(clip.get("from", 0))) / speed
    if duration <= 0:
        raise ShotCompositionMigrationError(f"clip {clip.get('id', '?')} has non-positive duration")
    return max(1, round(duration * 1000))


def _source_audio(value: Any, *, project_id: str, fallback_key: str) -> dict[str, Any]:
    audio = _copy(value) if isinstance(value, Mapping) else {"value": _copy(value)}
    digest = _as_digest(audio.get("digest", audio.get("content_digest")), audio)
    return {
        "track_id": str(audio.get("track_id", f"audio-{fallback_key}")),
        "object_id": str(audio.get("object_id", audio.get("media_id", f"audio-object-{fallback_key}"))),
        "digest": digest,
        "scope": {"project_id": project_id, **(_copy(audio.get("scope", {})) if isinstance(audio.get("scope"), Mapping) else {})},
        "source": audio,
    }


def _source_assets(registry: Mapping[str, Any], *, project_id: str) -> tuple[list[dict[str, Any]], tuple[str, ...]]:
    raw_assets = registry.get("assets", {})
    if not isinstance(raw_assets, Mapping):
        return [], ()
    assets: list[dict[str, Any]] = []
    media: set[str] = set()
    for asset_id in sorted(raw_assets):
        entry = raw_assets[asset_id]
        if not isinstance(entry, Mapping):
            continue
        digest = _as_digest(
            entry.get("digest", entry.get("content_digest", entry.get("sha256"))),
            entry,
        )
        object_id = str(entry.get("object_id", entry.get("media_id", digest)))
        assets.append({
            "asset_id": str(asset_id),
            "object_id": object_id,
            "digest": digest,
            "scope": {"project_id": project_id, **(_copy(entry.get("scope", {})) if isinstance(entry.get("scope"), Mapping) else {})},
            "role": str(entry.get("role", entry.get("type", "asset"))),
            "source": _copy(entry),
        })
        # The object/media identity is the dependency; ``digest`` may be a
        # legacy record digest for the asset descriptor itself and need not be
        # a Runtime object.  Preserve that field in the payload, but only
        # claim an actual SHA-256 object as managed media.
        if object_id.startswith("sha256:") and len(object_id) == 71:
            media.add(object_id)
    return assets, tuple(sorted(media))


def _source_generation_inputs(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, Mapping):
        values = list(value.values())
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        values = list(value)
    else:
        values = []
    result: list[dict[str, Any]] = []
    for ordinal, item in enumerate(values):
        raw = _copy(item) if isinstance(item, Mapping) else {"value": _copy(item)}
        result.append({
            "input_id": str(raw.get("input_id", raw.get("id", f"input-{ordinal}"))),
            "object_id": str(raw.get("object_id", raw.get("media_id", raw.get("id", f"input-object-{ordinal}")))),
            "digest": _as_digest(raw.get("digest", raw.get("content_digest", raw.get("sha256"))), raw),
            "ordinal": ordinal,
            "role": str(raw.get("role", "generation_input")),
            "source": raw,
        })
    return result


def _legacy_shot_payload(
    *,
    project_id: str,
    shot_record: Mapping[str, Any],
    source_unit: Mapping[str, Any],
    internal_payload: Mapping[str, Any],
    assets: Sequence[Mapping[str, Any]],
    audio: Mapping[str, Any],
    generation_inputs: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    metadata = _copy(shot_record.get("metadata", {})) if isinstance(shot_record.get("metadata"), Mapping) else {}
    for key in ("name", "title", "description", "aspect_ratio"):
        if key in shot_record and key not in metadata:
            metadata[key] = _copy(shot_record[key])
    if source_unit.get("name") and "name" not in metadata:
        metadata["name"] = _copy(source_unit["name"])
    source_fields = {
        key: _canonical_provenance(value)
        for key, value in shot_record.items()
        if key not in {"metadata", "items", "images", "pools", "alternatives", "selected_variants", "provenance", "generation_inputs", "audio", "audio_bindings", "text_bindings"}
    }
    payload = {
        "metadata": metadata,
        "items": _copy(shot_record.get("items", shot_record.get("images", []))),
        "pools": _copy(shot_record.get("pools", [])),
        "alternatives": _copy(shot_record.get("alternatives", [])),
        "selected_variants": _copy(shot_record.get("selected_variants", shot_record.get("selectedVariants", {}))),
        "provenance": {
            **(_copy(shot_record.get("provenance", {})) if isinstance(shot_record.get("provenance"), Mapping) else {}),
            "migration": _copy(source_unit["provenance"]),
            "legacy_source": source_fields,
        },
        "generation_inputs": _copy(generation_inputs),
        "audio_bindings": _copy(shot_record.get("audio_bindings", [])),
        "text_bindings": _copy(shot_record.get("text_bindings", [])),
        "audio": _copy(audio),
        "assets": _copy(assets),
        "internal_timeline": _copy(internal_payload),
        "project_id": project_id,
    }
    return payload


def _internal_payload(config: Mapping[str, Any], registry: Mapping[str, Any]) -> dict[str, Any]:
    if any(
        isinstance(clip, Mapping) and clip.get("clipType") == "shot"
        for clip in config.get("clips", [])
        if isinstance(config.get("clips", []), list)
    ):
        raise ShotCompositionMigrationError("nested legacy shot composition is unsupported")
    payload = _copy(dict(config))
    canonical_registry = _copy(registry)
    assets = canonical_registry.get("assets")
    if isinstance(assets, Mapping):
        canonical_registry["assets"] = {
            asset_id: canonicalize_asset_dimensions(asset_id, entry)
            if isinstance(entry, Mapping) else entry
            for asset_id, entry in assets.items()
        }
    payload["registry"] = canonical_registry
    payload.setdefault("tracks", [])
    payload.setdefault("clips", [])
    payload.setdefault("effects", [])
    payload.setdefault("audio", [])
    payload.setdefault("layout", {})
    payload.setdefault("assets", _copy(canonical_registry.get("assets", {})))
    return payload


def _occurrence(
    *,
    occurrence_id: str,
    document_id: str,
    shot_id: str,
    revision_id: str,
    at_ms: int,
    duration_ms: int,
    track: Any,
    source_offset: Any,
    speed: Any,
    source: Mapping[str, Any],
) -> dict[str, Any]:
    track_value = str(track or "video")
    provenance = _copy(source.get("provenance", {})) if isinstance(source.get("provenance"), Mapping) else {}
    provenance["legacy_source"] = _canonical_provenance(dict(source))
    return {
        "occurrence_id": occurrence_id,
        "parent_document_id": document_id,
        "shot_id": shot_id,
        "revision_id": revision_id,
        "ordinal": 0,
        "at_ms": max(0, at_ms),
        "duration_ms": max(1, duration_ms),
        "placement": {"start_ms": max(0, at_ms), "track": track_value},
        "source_offset": _copy(source_offset),
        "speed": _copy(speed),
        "track": track_value,
        "transform": _copy(source.get("transform", {})) if isinstance(source.get("transform", {}), Mapping) else {},
        "gain": source.get("gain", 1),
        "muted": bool(source.get("muted", source.get("mute", False))),
        "provenance": provenance,
        "name": source.get("name"),
    }


def plan_shot_composition_migration(
    source: Any,
    *,
    project_id: str | None = None,
    timeline_id: str | None = None,
    migration_id: str | None = None,
) -> MigrationPlan | tuple[MigrationPlan, ...]:
    """Build one or more deterministic plans from legacy source snapshots."""
    plans: list[MigrationPlan] = []
    seen_project_timeline: set[tuple[str, str]] = set()
    for row in _project_rows(source):
        pid = _project_id(row)
        timeline = _mapping(row["timeline"], "timeline")
        tid = _timeline_id(timeline)
        if project_id is not None and pid != project_id:
            continue
        if timeline_id is not None and tid != timeline_id:
            continue
        if (pid, tid) in seen_project_timeline:
            raise ShotCompositionMigrationError(f"duplicate source timeline {pid}/{tid}")
        seen_project_timeline.add((pid, tid))
        config, registry = _timeline_config(timeline)
        child_clips = _legacy_child_clips(config)
        groups = _legacy_groups(config)
        if not child_clips and not groups:
            continue
        document_id = str(timeline.get("document_id", tid))
        fingerprint = _source_fingerprint(row, timeline)
        mid = migration_id or _identity("shot-composition-migration", pid, tid, fingerprint)
        # Runtime idempotency keys are HTTP header values.  Keep the
        # deterministic migration identity, but use only the characters
        # accepted by Runtime's transport validator.
        key = f"astrid-shot-composition-migration-{mid}"
        children = _child_timelines(row, timeline)
        shots = _shot_records(row, timeline)
        clips = config.get("clips", [])
        if not isinstance(clips, list):
            raise ShotCompositionMigrationError("legacy timeline clips must be a list")

        occurrences: list[dict[str, Any]] = []
        revisions: dict[tuple[str, str], dict[str, Any]] = {}
        internal_revisions: dict[tuple[str, str], dict[str, Any]] = {}
        mapping: dict[str, Any] = {"projects": {}, "timelines": {}, "shots": {}, "occurrences": {}}
        occurrence_seen: dict[str, str] = {}
        source_counts = {"astrid_child_clips": len(child_clips), "astrid_pinned_groups": len(groups)}

        def register_unit(
            *,
            source_key: str,
            source_unit: Mapping[str, Any],
            shot_id: str,
            child_config: Mapping[str, Any],
            child_registry: Mapping[str, Any],
            shot_record: Mapping[str, Any],
            parent_at_ms: int,
            parent_duration_ms: int,
            track: Any,
            source_offset: Any,
            speed: Any,
            occurrence_source: Mapping[str, Any],
        ) -> None:
            internal = _internal_payload(child_config, child_registry)
            internal_digest = _content_digest(internal)
            internal_id = _identity("internal-timeline-revision", pid, tid, shot_id, internal_digest)
            internal_key = (tid, internal_id)
            internal_revisions.setdefault(internal_key, {
                "timeline_id": tid,
                "revision_id": internal_id,
                "payload": internal,
                "content_digest": internal_digest,
            })
            assets, media = _source_assets(child_registry, project_id=pid)
            raw_audio = shot_record.get("audio", shot_record.get("audio_bindings", internal.get("audio", [])))
            audio = _source_audio(raw_audio, project_id=pid, fallback_key=shot_id)
            generation_inputs = _source_generation_inputs(shot_record.get("generation_inputs", []))
            source_unit_with_provenance = {
                "kind": source_unit.get("kind", "legacy"),
                "source_key": source_key,
                "source_id": source_unit.get("source_id", source_key),
                "source_fingerprint": fingerprint,
                "source_name": source_unit.get("name", shot_record.get("name")),
            }
            shot_payload = _legacy_shot_payload(
                project_id=pid,
                shot_record=shot_record,
                source_unit={"provenance": source_unit_with_provenance},
                internal_payload=internal,
                assets=assets,
                audio=audio,
                generation_inputs=generation_inputs,
            )
            # Runtime adds this immutable dependency identity before hashing a
            # shot payload.  Include it here so the supplied digest is the
            # digest of exactly the bytes Runtime will persist.
            shot_payload["internal_timeline_revision_id"] = internal_id
            shot_digest = _content_digest(shot_payload)
            shot_revision_id = _identity("shot-revision", pid, shot_id, shot_digest)
            shot_key = (shot_id, shot_revision_id)
            revisions.setdefault(shot_key, {
                "shot_id": shot_id,
                "revision_id": shot_revision_id,
                "internal_timeline_revision_id": internal_id,
                "payload": shot_payload,
                "content_digest": shot_digest,
            })
            occurrence_id = str(occurrence_source.get("occurrence_id", occurrence_source.get("id", source_key)))
            _ensure_unique(occurrence_seen, occurrence_id, project_id=pid, timeline_id=tid, kind="occurrence")
            occurrence = _occurrence(
                occurrence_id=occurrence_id,
                document_id=document_id,
                shot_id=shot_id,
                revision_id=shot_revision_id,
                at_ms=parent_at_ms,
                duration_ms=parent_duration_ms,
                track=track,
                source_offset=source_offset,
                speed=speed,
                source=occurrence_source,
            )
            occurrence["_source_index"] = min(
                (index for index, clip in enumerate(clips) if isinstance(clip, Mapping) and clip.get("id") == occurrence_source.get("id")),
                default=len(clips),
            )
            occurrences.append(occurrence)
            mapping["shots"][source_key] = {
                "shot_id": shot_id,
                "revision_id": shot_revision_id,
                "internal_timeline_revision_id": internal_id,
                "content_digest": shot_digest,
            }
            mapping["occurrences"][occurrence_id] = {
                "source_id": source_key,
                "occurrence_id": occurrence_id,
                "shot_id": shot_id,
                "revision_id": shot_revision_id,
            }
            mapping.setdefault("media", []).extend(media)

        for clip in child_clips:
            params = _mapping(clip.get("params"), f"legacy child clip {clip.get('id', '?')}.params")
            shot_id = _string(params.get("shot_id"), "legacy child clip shot_id")
            child_id = _string(params.get("timeline_document_id"), "legacy child clip timeline_document_id")
            child = children.get(child_id)
            if child is None:
                raise ShotCompositionMigrationError(
                    f"child timeline {child_id!r} for shot {shot_id!r} is unavailable; inventory must include it"
                )
            child_config, child_registry = _timeline_config(child)
            child_registry = _merge_legacy_registries(registry, child_registry)
            shot_record = shots.get(shot_id, {})
            register_unit(
                source_key=f"astrid:{clip.get('id', shot_id)}",
                source_unit={"kind": "astrid_child_timeline", "source_id": str(clip.get("id", shot_id)), "name": clip.get("name")},
                shot_id=shot_id,
                child_config=child_config,
                child_registry=child_registry,
                shot_record=shot_record,
                parent_at_ms=round(float(clip.get("at", 0)) * 1000),
                parent_duration_ms=_clip_duration_ms(clip),
                track=clip.get("track", params.get("track")),
                source_offset=params.get("source_offset", params.get("source_offset_ms", 0)),
                speed=clip.get("speed", params.get("speed", 1)),
                occurrence_source={**_copy(dict(clip)), "occurrence_id": params.get("occurrence_id", clip.get("occurrence_id", clip.get("id", shot_id)))},
            )

        group_clip_owner: dict[str, str] = {}
        group_seen: set[str] = set()
        for group_index, group in enumerate(groups):
            group_id = str(group.get("group_id", group.get("id", group.get("shotId", f"group-{group_index}"))))
            if group_id in group_seen:
                raise MigrationAmbiguityError(
                    f"legacy group {group_id!r} occurs more than once",
                    project_id=pid,
                    timeline_id=tid,
                    group_id=group_id,
                    recovery="Give each pinned group a unique stable ID, then rerun rehearsal.",
                )
            group_seen.add(group_id)
            clip_ids_value = group.get("clipIds", group.get("clip_ids", []))
            if not isinstance(clip_ids_value, list):
                raise MigrationAmbiguityError(
                    f"legacy group {group_id!r} has no clipIds list",
                    project_id=pid,
                    timeline_id=tid,
                    group_id=group_id,
                    recovery="Add an ordered clipIds list to the pinned group, then rerun rehearsal.",
                )
            clip_ids = [_string(value, f"group {group_id}.clipIds[]") for value in clip_ids_value]
            positions = _require_legacy_group_contiguous(project_id=pid, timeline_id=tid, group_id=group_id, clip_ids=clip_ids, clips=[_mapping(c, "clips[]") for c in clips])
            child_ids = {str(clip.get("id")) for clip in child_clips}
            if child_ids.intersection(clip_ids):
                raise MigrationAmbiguityError(
                    f"legacy group {group_id!r} overlaps an Astrid child-shot clip",
                    project_id=pid,
                    timeline_id=tid,
                    group_id=group_id,
                    recovery="Choose one legacy owner for the overlapping parent clip, then rerun rehearsal.",
                )
            if any(clip_id in group_clip_owner for clip_id in clip_ids):
                raise MigrationAmbiguityError(
                    f"legacy group {group_id!r} overlaps another pinned group",
                    project_id=pid,
                    timeline_id=tid,
                    group_id=group_id,
                    recovery="Remove overlapping membership or split the groups before migration.",
                )
            for clip_id in clip_ids:
                group_clip_owner[clip_id] = group_id
            selected = [_mapping(clips[index], "clips[]") for index in positions]
            child_config = {key: _copy(value) for key, value in config.items() if key != "pinnedShotGroups"}
            child_config["clips"] = []
            start = min(float(clip.get("at", 0)) for clip in selected)
            ends = []
            for clip in selected:
                duration_ms = _clip_duration_ms(clip)
                ends.append(float(clip.get("at", 0)) + duration_ms / 1000)
                child_clip = _copy(dict(clip))
                child_clip["at"] = float(clip.get("at", 0)) - start
                child_config["clips"].append(child_clip)
            duration_ms = max(1, round((max(ends) - start) * 1000))
            shot_id = str(group.get("shotId", group.get("shot_id", group_id)))
            shot_record_value = group.get("shot", shots.get(shot_id, {}))
            shot_record = dict(_mapping(shot_record_value, f"legacy group {group_id}.shot") if shot_record_value else {})
            for field in ("name", "metadata", "items", "images", "pools", "alternatives", "selected_variants", "selectedVariants", "provenance", "generation_inputs", "audio", "audio_bindings", "text_bindings"):
                if field in group and field not in shot_record:
                    shot_record[field] = _copy(group[field])
            first = selected[0]
            occurrence_source = {**_copy(dict(group)), "id": group_id, "occurrence_id": group.get("occurrence_id", group_id), "name": group.get("name", shot_record.get("name"))}
            register_unit(
                source_key=f"app:{group_id}",
                source_unit={"kind": "astrid_pinned_group", "source_id": group_id, "name": group.get("name")},
                shot_id=shot_id,
                child_config=child_config,
                child_registry=registry,
                shot_record=shot_record,
                parent_at_ms=round(start * 1000),
                parent_duration_ms=duration_ms,
                track=group.get("track", first.get("track")),
                source_offset=group.get("source_offset", 0),
                speed=group.get("speed", 1),
                occurrence_source=occurrence_source,
            )

        occurrences.sort(key=lambda row: (row.pop("_source_index", len(clips)), row["occurrence_id"]))
        for ordinal, occurrence in enumerate(occurrences):
            occurrence["ordinal"] = ordinal
        parent_config = _copy(config)
        parent_config.pop("pinnedShotGroups", None)
        parent_config["clips"] = [
            _copy(clip)
            for index, clip in enumerate(clips)
            if isinstance(clip, Mapping) and not (
                clip.get("clipType") == "shot" or clip.get("id") in group_clip_owner
            )
        ]
        # The canonical graph deliberately contains no legacy shape.  It is
        # validated before any Runtime call, including on dry-run.
        parent_revision_id = _identity("parent-composition-revision", pid, tid, fingerprint)
        primary_head_id = str(timeline.get("head_revision_id", timeline.get("composition_head_revision_id", timeline.get("revision_id", _identity("legacy-head", pid, tid, fingerprint)))))
        parent_document = {
            "config": parent_config,
            "registry": _copy(registry),
            "clips": _copy(parent_config.get("clips", [])),
            "occurrences": _copy(occurrences),
        }
        marker = {
            "schema_version": MIGRATION_SCHEMA_VERSION,
            "migration_id": mid,
            "state": "active",
            "source_fingerprint": fingerprint,
            "activated_by": "astrid.explicit_shot_composition_migration",
        }
        parent_document["migration_activation"] = marker
        graph = {
            "schema_version": 1,
            "project": {"project_id": pid, "document_id": document_id, "role": "project"},
            "primary_timeline": {"document_id": document_id, "role": "primary_timeline", "head": {"revision_id": primary_head_id, "content_digest": _content_digest({"source": fingerprint, "timeline": tid})}},
            "shot_revisions": [],
            "occurrences": _copy(occurrences),
            "cases": {"missing_dependency": {"shot_id": "__missing__", "revision_id": "__missing__", "expected": "missing_dependency"}, "stale_write_rejection": {"expected_head_revision_id": primary_head_id, "submitted_head_revision_id": primary_head_id, "expected_status": 409}},
        }
        for shot in sorted(revisions.values(), key=lambda value: (value["shot_id"], value["revision_id"])):
            internal = internal_revisions[(tid, shot["internal_timeline_revision_id"])]
            graph["shot_revisions"].append({
                "shot_id": shot["shot_id"],
                "revision_id": shot["revision_id"],
                "document_role": "shot_revision",
                "content_digest": shot["content_digest"],
                "internal_timeline_revision": {"revision_id": internal["revision_id"], "content_digest": internal["content_digest"], "timeline": {"tracks": _copy(internal["payload"].get("tracks", [])), "clips": _copy(internal["payload"].get("clips", []))}},
                "audio": _copy(shot["payload"]["audio"]),
                "timing": {"duration_ms": next(item["duration_ms"] for item in occurrences if item["shot_id"] == shot["shot_id"] and item["revision_id"] == shot["revision_id"])},
                "provenance": _copy(shot["payload"]["provenance"]),
                "dependencies": [],
                "assets": _copy(shot["payload"].get("assets", [])),
                "generation_inputs": _copy(shot["payload"].get("generation_inputs", [])),
            })
        graph = prepare_shot_composition(graph)
        media = tuple(sorted(set(mapping.get("media", []))))
        publication_occurrences = []
        for occurrence in occurrences:
            runtime_occurrence = {key: _copy(occurrence[key]) for key in ("occurrence_id", "shot_id", "revision_id", "placement", "source_offset", "duration_ms", "speed", "track", "transform", "gain", "muted", "provenance")}
            runtime_occurrence["shot_revision_id"] = runtime_occurrence.pop("revision_id")
            publication_occurrences.append(runtime_occurrence)
        publication_parent = {
            **_copy(parent_document),
            "occurrences": publication_occurrences,
        }
        publication = {
            "project_id": pid,
            "timeline_id": tid,
            "expected_head": timeline.get("head_revision_id", timeline.get("composition_head_revision_id")),
            "parent_revision_id": parent_revision_id,
            "content_digest": _content_digest(publication_parent),
            "parent_composition": publication_parent,
            "internal_timeline_revisions": sorted(internal_revisions.values(), key=lambda value: (value["timeline_id"], value["revision_id"])),
            "shot_revisions": sorted(revisions.values(), key=lambda value: (value["shot_id"], value["revision_id"])),
            "dependency_manifest": {
                "shots": [{"shot_id": item["shot_id"], "revision_id": item["revision_id"], "internal_timeline_revision_id": item["internal_timeline_revision_id"], "content_digest": item["content_digest"]} for item in sorted(revisions.values(), key=lambda value: (value["shot_id"], value["revision_id"]))],
                "internal_timelines": [{"timeline_id": item["timeline_id"], "revision_id": item["revision_id"], "content_digest": item["content_digest"]} for item in sorted(internal_revisions.values(), key=lambda value: (value["timeline_id"], value["revision_id"]))],
                "media": [{"media_id": digest, "content_digest": digest} for digest in media],
            },
            "migration": {"migration_id": mid, "source_fingerprint": fingerprint, "activation_marker": marker, "identity_mapping": mapping},
        }
        mapping["projects"][pid] = {"project_id": pid}
        mapping["timelines"][tid] = {"timeline_id": tid, "document_id": document_id, "parent_revision_id": parent_revision_id}
        plans.append(MigrationPlan(pid, tid, document_id, fingerprint, mid, key, graph, publication, mapping, marker, media, source_counts))
    if project_id is not None or timeline_id is not None:
        if not plans:
            raise ShotCompositionMigrationError("no affected source timeline matched the requested identity")
        return plans[0]
    return tuple(plans)


def rehearse_shot_composition_migration(source: Any, **kwargs: Any) -> dict[str, Any]:
    """Return deterministic plans and counts without any Runtime mutation."""
    planned = plan_shot_composition_migration(source, **kwargs)
    plans = (planned,) if isinstance(planned, MigrationPlan) else planned
    return {
        "receipt_type": MIGRATION_RECEIPT_TYPE,
        "mode": "dry_run",
        "schema_version": MIGRATION_SCHEMA_VERSION,
        "plans": [plan.as_dict() for plan in plans],
        "inventory": inventory_legacy_shot_compositions(source).as_dict(),
    }


def _read_prior_migration(writer: Any, migration_id: str) -> Mapping[str, Any] | None:
    for name in ("get_migration_receipt", "load_migration_receipt", "get_shot_composition_migration"):
        method = getattr(writer, name, None)
        if method is not None:
            value = _unwrap_data(method(migration_id))
            return value if isinstance(value, Mapping) else None
    return None


def _verify_reload(writer: Any, plan: MigrationPlan, publication_result: Any) -> dict[str, Any]:
    result = _unwrap_data(publication_result)
    new_head = result.get("new_head", result.get("revision_id", plan.publication["parent_revision_id"])) if isinstance(result, Mapping) else plan.publication["parent_revision_id"]
    reload_result: Any = None
    method = getattr(writer, "get_project_parent_composition_revision", None)
    if method is not None:
        reload_result = _unwrap_data(method(plan.project_id, plan.timeline_id, str(new_head)))
    else:
        method = getattr(writer, "reload_canonical_composition", None)
        if method is not None:
            reload_result = _unwrap_data(method(plan.project_id, plan.timeline_id, str(new_head)))
    if isinstance(reload_result, Mapping):
        payload = reload_result.get("payload", reload_result)
        if isinstance(payload, Mapping) and isinstance(payload.get("canonical_graph"), Mapping):
            validate_shot_composition(payload["canonical_graph"])
        if reload_result.get("revision_id") not in (None, str(new_head)):
            raise MigrationInterruptedError("Runtime reload returned a different parent revision")
    return {"verified": True, "revision_id": str(new_head), "reload": copy.deepcopy(reload_result)}


def migrate_shot_compositions(
    source: Any,
    writer: RuntimeCompositionMigrationPort,
    *,
    project_id: str | None = None,
    timeline_id: str | None = None,
    migration_id: str | None = None,
    dry_run: bool = False,
) -> dict[str, Any] | tuple[dict[str, Any], ...]:
    """Activate a planned migration through Runtime's publication/CAS seam.

    The operation never calls legacy timeline/shot mutation services.  Replay
    uses the same Runtime idempotency key and returns the prior durable marker
    when the Runtime exposes its migration ledger.
    """
    planned = plan_shot_composition_migration(source, project_id=project_id, timeline_id=timeline_id, migration_id=migration_id)
    plans = (planned,) if isinstance(planned, MigrationPlan) else planned
    if dry_run:
        return tuple(rehearse_shot_composition_migration(source, project_id=project_id, timeline_id=timeline_id, migration_id=migration_id)["plans"])
    receipts: list[dict[str, Any]] = []
    for plan in plans:
        prior = _read_prior_migration(writer, plan.migration_id)
        if prior is not None:
            prior_fingerprint = prior.get("source_fingerprint")
            if prior_fingerprint != plan.source_fingerprint:
                raise MigrationSourceChangedError(
                    f"migration {plan.migration_id} was recorded for a different source fingerprint"
                )
            replay_receipt = dict(prior)
            replay_receipt["replayed"] = True
            receipts.append(replay_receipt)
            continue
        try:
            stage = getattr(writer, "stage_shot_composition_migration", None)
            if stage is not None:
                stage(plan.migration_id, plan.source_fingerprint, plan.identity_mapping)
            publish = getattr(writer, "publish_parent_composition", None)
            if publish is None:
                raise MigrationRuntimeUnavailableError(
                    "Runtime must expose publish_parent_composition; no local compatibility writer is allowed"
                )
            publication_result = publish(
                plan.project_id,
                plan.timeline_id,
                plan.publication,
                idempotency_key=plan.idempotency_key,
            )
            reload_evidence = _verify_reload(writer, plan, publication_result)
            activation = getattr(writer, "activate_shot_composition_migration", None)
            if activation is not None:
                activation(plan.migration_id, plan.activation_marker, plan.identity_mapping, publication_result)
            receipt = {
                "receipt_type": MIGRATION_RECEIPT_TYPE,
                "schema_version": MIGRATION_SCHEMA_VERSION,
                "migration_id": plan.migration_id,
                "project_id": plan.project_id,
                "timeline_id": plan.timeline_id,
                "document_id": plan.document_id,
                "source_fingerprint": plan.source_fingerprint,
                "status": "activated",
                "replayed": False,
                "idempotency_key": plan.idempotency_key,
                "activation_marker": copy.deepcopy(dict(plan.activation_marker)),
                "identity_mapping": copy.deepcopy(dict(plan.identity_mapping)),
                "managed_media": {"digests": list(plan.media_digests), "deduplicated": True, "created": 0, "reused": len(plan.media_digests)},
                "publication": copy.deepcopy(_unwrap_data(publication_result)),
                "reload": reload_evidence,
            }
            record = getattr(writer, "record_shot_composition_migration", None)
            if record is not None:
                recorded = _unwrap_data(record(plan.migration_id, receipt))
                if isinstance(recorded, Mapping):
                    receipt = dict(recorded)
            receipts.append(receipt)
        except MigrationRuntimeUnavailableError:
            raise
        except Exception as exc:
            if getattr(exc, "status", None) == 409 or getattr(exc, "code", None) in {"stale_write", "conflict", "idempotency_conflict"}:
                raise
            raise MigrationInterruptedError(
                f"migration {plan.migration_id} did not finish; retry the same migration ID to replay the Runtime idempotency key: {exc}"
            ) from exc
    return receipts[0] if isinstance(planned, MigrationPlan) else tuple(receipts)


__all__ = [
    "MIGRATION_RECEIPT_TYPE",
    "MIGRATION_SCHEMA_VERSION",
    "MigrationAmbiguityError",
    "MigrationInventory",
    "MigrationInventoryItem",
    "MigrationInterruptedError",
    "MigrationPlan",
    "MigrationRuntimeUnavailableError",
    "MigrationSourceChangedError",
    "RuntimeCompositionMigrationPort",
    "ShotCompositionMigrationError",
    "inventory_legacy_shot_compositions",
    "migrate_shot_compositions",
    "plan_shot_composition_migration",
    "rehearse_shot_composition_migration",
]
