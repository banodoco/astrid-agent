"""Exact semantic and raw comparison of two immutable authoring closures."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from typing import Any

from .authoring_bundle import AuthoringBundleError
from .authoring_feedback import authoring_change_summary, publication_feedback

_STABLE_ARRAY_KEYS = {
    "clips": ("id",),
    "tracks": ("id",),
    "items": ("item_id", "id"),
    "occurrences": ("occurrence_id",),
}


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AuthoringBundleError(f"{path} must be an object")
    return value


def _bundle_identity(value: Mapping[str, Any], path: str) -> tuple[str, str, str]:
    root = _mapping(value, path)
    base_parent = _mapping(root.get("base_parent"), f"{path}.base_parent")
    project_id = root.get("project_id")
    timeline_id = root.get("timeline_id")
    revision_id = base_parent.get("revision_id")
    if not all(isinstance(item, str) and item for item in (project_id, timeline_id, revision_id)):
        raise AuthoringBundleError(f"{path} is missing project, timeline, or parent revision identity")
    return project_id, timeline_id, revision_id


def _raw_snapshot(bundle: Mapping[str, Any]) -> dict[str, Any]:
    """Read original verified bytes, before authoring normalization."""
    closure = _mapping(bundle.get("immutable_closure"), "bundle.immutable_closure")
    parent = deepcopy(dict(_mapping(closure.get("parent"), "closure.parent")))
    parent_payload = _mapping(parent.get("payload"), "closure.parent.payload")
    occurrences = deepcopy(parent_payload.get("occurrences"))
    if not isinstance(occurrences, list):
        raise AuthoringBundleError("closure.parent.payload.occurrences must be a list")
    shots = _mapping(closure.get("shots"), "closure.shots")
    internals = _mapping(closure.get("internal_timelines"), "closure.internal_timelines")
    per_occurrence = {}
    for row in occurrences:
        shot = deepcopy(dict(_mapping(shots.get(row["shot_revision_id"]), "closure.shot")))
        internal_id = shot.get("internal_timeline_revision_id") or shot["payload"].get("internal_timeline_revision_id")
        internal = deepcopy(dict(_mapping(internals.get(internal_id), "closure.internal_timeline")))
        per_occurrence[row["occurrence_id"]] = {
            "shot_id": shot["shot_id"],
            "shot_revision_id": shot["revision_id"],
            "shot_content_digest": shot["content_digest"],
            "internal_timeline_revision_id": internal["revision_id"],
            "internal_timeline_content_digest": internal["content_digest"],
            "payload": shot.pop("payload"),
            "internal_timeline": internal.pop("payload"),
            "shot_record": shot,
            "internal_timeline_record": internal,
        }
    parent["payload"].pop("occurrences")
    return {"parent": parent, "occurrences": occurrences, "shots": per_occurrence}


def _stable_key(before: list[Any], after: list[Any], name: str) -> str | None:
    for key in _STABLE_ARRAY_KEYS.get(name, ()):
        if name == "items":
            old_ids = [row.get("item_id", row.get("id")) if isinstance(row, Mapping) else None
                       for row in before]
            new_ids = [row.get("item_id", row.get("id")) if isinstance(row, Mapping) else None
                       for row in after]
        else:
            old_ids = [row.get(key) if isinstance(row, Mapping) else None for row in before]
            new_ids = [row.get(key) if isinstance(row, Mapping) else None for row in after]
        if (all(isinstance(identity, str) and identity for identity in old_ids + new_ids)
                and len(set(old_ids)) == len(old_ids) and len(set(new_ids)) == len(new_ids)):
            return key
    return None


def _diff_values(before: Any, after: Any, path: str, changes: list[dict[str, Any]],
                 location: list[Any] | None = None) -> None:
    location = [] if location is None else location

    def emit(kind, child_path, segments, **values):
        changes.append({"path": child_path, "location": deepcopy(segments),
                        "kind": kind, **deepcopy(values)})

    if isinstance(before, Mapping) and isinstance(after, Mapping):
        for key in sorted(before.keys() | after.keys(), key=str):
            child = f"{path}.{key}" if path else str(key)
            segments = location + [key]
            if key not in before:
                emit("added", child, segments, after=after[key])
            elif key not in after:
                emit("removed", child, segments, before=before[key])
            else:
                _diff_values(before[key], after[key], child, changes, segments)
        return
    if isinstance(before, list) and isinstance(after, list):
        array_name = location[-1] if location and isinstance(location[-1], str) else ""
        stable_key = _stable_key(before, after, array_name)
        if stable_key:
            def get_id(row):
                if array_name == "items":
                    return row.get("item_id", row.get("id"))
                return row[stable_key]
            old = {get_id(row): row for row in before}
            new = {get_id(row): row for row in after}
            for identity in sorted(old.keys() - new.keys()):
                emit("removed", f"{path}[{stable_key}={identity}]",
                     location + [{"key": stable_key, "value": identity}], before=old[identity])
            for identity in sorted(new.keys() - old.keys()):
                emit("added", f"{path}[{stable_key}={identity}]",
                     location + [{"key": stable_key, "value": identity}], after=new[identity])
            for identity in sorted(old.keys() & new.keys()):
                _diff_values(old[identity], new[identity], f"{path}[{stable_key}={identity}]",
                             changes, location + [{"key": stable_key, "value": identity}])
            old_order = [get_id(row) for row in before if get_id(row) in new]
            new_order = [get_id(row) for row in after if get_id(row) in old]
            if old_order != new_order:
                emit("reordered", f"{path}.order", location + [{"order": True}],
                     before=old_order, after=new_order)
            return
        for index in range(max(len(before), len(after))):
            child = f"{path}[{index}]"
            if index >= len(before):
                emit("added", child, location + [index], after=after[index])
            elif index >= len(after):
                emit("removed", child, location + [index], before=before[index])
            else:
                _diff_values(before[index], after[index], child, changes, location + [index])
        return
    if before != after:
        emit("changed", path, location, before=before, after=after)


def _display_context(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    """Compact labels from the same verified closure, never current metadata."""
    from .authoring_bundle import _effective_parent_assets
    from .authoring_feedback import _selection

    def timeline_context(timeline):
        def selection(row):
            value = _selection(row, timeline)
            return list(value) if value else None

        return {
            "clips": {row["id"]: {"name": row.get("name"), "track": row.get("track"),
                                     "selection": selection(row)}
                      for row in timeline.get("clips", []) if isinstance(row, Mapping) and row.get("id")},
            "tracks": {row["id"]: {"name": row.get("name"), "kind": row.get("kind")}
                       for row in timeline.get("tracks", []) if isinstance(row, Mapping) and row.get("id")},
        }

    def occurrence_context(snapshot, occurrence_id):
        shot = snapshot["shots"].get(occurrence_id)
        if shot is None:
            return None
        return {"shot_id": shot["shot_id"], "name": shot["payload"].get("name"),
                **timeline_context(shot["internal_timeline"])}

    def parent_context(snapshot):
        payload = snapshot["parent"]["payload"]
        config = payload.get("config", {})
        return timeline_context({
            "clips": payload.get("clips", config.get("clips", [])),
            "tracks": payload.get("tracks", config.get("tracks", [])),
            "registry": {"assets": _effective_parent_assets(payload)},
        })

    return {
        "occurrences": {
            occurrence_id: {"before": occurrence_context(before, occurrence_id),
                            "after": occurrence_context(after, occurrence_id)}
            for occurrence_id in sorted(before["shots"].keys() | after["shots"].keys())
        },
        "parent": {"before": parent_context(before), "after": parent_context(after)},
    }


def _semantic_candidate(before: Mapping[str, Any], after: Mapping[str, Any],
                        raw_before: Mapping[str, Any], raw_after: Mapping[str, Any]) -> dict[str, Any]:
    before_rows = {row["occurrence_id"]: row for row in before["placements"]}
    after_rows = {row["occurrence_id"]: row for row in after["placements"]}
    all_ids = before_rows.keys() | after_rows.keys()
    synthetic = {occurrence_id: f"occurrence:{occurrence_id}" for occurrence_id in all_ids}

    def rows_for(source_rows):
        result = []
        for row in source_rows:
            value = deepcopy(row)
            value["shot_id"] = synthetic[value["occurrence_id"]]
            result.append(value)
        return result

    shot_values = {}
    for occurrence_id in all_ids:
        old_row, new_row = before_rows.get(occurrence_id), after_rows.get(occurrence_id)
        old_shot = raw_before["shots"].get(occurrence_id) if old_row else None
        new_shot = raw_after["shots"].get(occurrence_id) if new_row else None
        baseline = old_shot or new_shot
        current = new_shot or old_shot
        base_payload = deepcopy(baseline["payload"]) if baseline else {}
        payload = deepcopy(current["payload"]) if current else {}
        base_internal = deepcopy(baseline["internal_timeline"]) if baseline else {}
        internal = deepcopy(current["internal_timeline"]) if current else {}
        # Runtime pin IDs identify immutable source records, not authored properties.
        for value in (base_payload, payload):
            value.pop("internal_timeline_revision_id", None)
        shot_values[synthetic[occurrence_id]] = {
            "base_payload": base_payload,
            "payload": payload,
            "base_internal_timeline": base_internal,
            "internal_timeline": internal,
        }

    old_parent = deepcopy(before["parent"])
    new_parent = deepcopy(after["parent"])
    old_parent.pop("occurrences", None)
    new_parent.pop("occurrences", None)
    old_rows = rows_for(before["placements"])
    new_rows = rows_for(after["placements"])
    old_placements = {row["occurrence_id"]: deepcopy(row) for row in old_rows}
    return {
        "project_id": before["project_id"],
        "timeline_id": before["timeline_id"],
        "base_parent": deepcopy(before["base_parent"]),
        "base_parent_payload": old_parent,
        "parent": new_parent,
        "base_placements": old_placements,
        "placements": new_rows,
        "shots": shot_values,
        "source_mapping": {"placements": old_placements, "shots": {}},
    }


def _update(summary: Mapping[str, Any], from_revision: str, to_revision: str,
            project_id: str, timeline_id: str) -> str:
    formatted = publication_feedback(
        summary, {"new_head": to_revision}, project_id=project_id, timeline_id=timeline_id,
    )["update"].splitlines()[0]
    saved_suffix = f" Saved revision {to_revision}."
    if formatted.endswith(saved_suffix):
        formatted = formatted[:-len(saved_suffix)]
    return f"Compared {from_revision} to {to_revision}: {formatted}"


def diff_authoring_revisions(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    """Compare two validated bundles retaining their original immutable closures."""
    before_identity = _bundle_identity(before, "before")
    after_identity = _bundle_identity(after, "after")
    if before_identity[:2] != after_identity[:2]:
        raise AuthoringBundleError("revision comparison requires the same project and timeline")
    raw_before = _raw_snapshot(before)
    raw_after = _raw_snapshot(after)
    changes: list[dict[str, Any]] = []
    _diff_values(raw_before, raw_after, "", changes)
    summary = authoring_change_summary(_semantic_candidate(before, after, raw_before, raw_after))
    from_revision, to_revision = before_identity[2], after_identity[2]
    from .change_intervals import intervals_from_bundles
    intervals = intervals_from_bundles(before, after)
    return {
        "project_id": before_identity[0],
        "timeline_id": before_identity[1],
        "from_revision": from_revision,
        "to_revision": to_revision,
        "complete": True,
        "changes": changes,
        "intervals": intervals,
        "context": _display_context(raw_before, raw_after),
        "raw_change_count": len(changes),
        "summary": summary,
        "update": _update(summary, from_revision, to_revision, before_identity[0], before_identity[1]),
    }


__all__ = ["diff_authoring_revisions"]
