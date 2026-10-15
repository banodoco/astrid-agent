"""Readable, lossless presentation for exact authoring revision diffs."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

_HASH = re.compile(r"^sha256:[0-9a-fA-F]{64}$")
_ABSENT = object()
_BOOKKEEPING_FIELDS = {
    "parent": {"project_id", "timeline_id", "revision_id", "content_digest", "created_at"},
    "shot_record": {"project_id", "shot_id", "revision_id", "content_digest",
                    "internal_timeline_revision_id", "created_at"},
    "internal_timeline_record": {"project_id", "timeline_id", "revision_id",
                                 "content_digest", "created_at"},
}
_DIRECT_BOOKKEEPING = {
    "shot_revision_id", "shot_content_digest", "internal_timeline_revision_id",
    "internal_timeline_content_digest",
}
_MEDIA_FIELDS = {"asset", "asset_id", "media_id", "object_id"}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2)


def _safe_label(value: Any) -> str:
    if not isinstance(value, str):
        value = str(value)
    return json.dumps(value, ensure_ascii=False)[1:-1]


def _identity(value: Any) -> str:
    if isinstance(value, Mapping):
        if value.get("order") is True:
            return "order"
        return f"[{_safe_label(value.get('key', 'key'))}={_safe_label(value.get('value', '?'))}]"
    return _safe_label(value)


def _location(change: Mapping[str, Any]) -> list[Any] | None:
    value = change.get("location")
    if isinstance(value, list) and all(isinstance(item, (str, int, Mapping)) for item in value):
        return value
    return None


def _classify(location: list[Any] | None) -> str:
    if not location:
        return "authored"
    # Only exact source-record/header paths are bookkeeping. User-owned maps
    # named metadata/content_digest or arbitrary *_id fields remain visible.
    if len(location) == 2 and location[0] == "parent" and isinstance(location[1], str) \
            and location[1] in _BOOKKEEPING_FIELDS["parent"]:
        return "bookkeeping"
    if len(location) >= 3 and location[0] == "occurrences":
        if isinstance(location[1], Mapping) and location[1].get("key") == "occurrence_id" and location[2] in {
            "shot_revision_id", "revision_id",
        }:
            return "bookkeeping"
    if len(location) >= 3 and location[0] == "shots":
        if isinstance(location[2], str) and location[2] in _DIRECT_BOOKKEEPING and len(location) == 3:
            return "bookkeeping"
        if len(location) == 4 and isinstance(location[2], str) \
                and location[2] in {"shot_record", "internal_timeline_record"}:
            if location[3] in _BOOKKEEPING_FIELDS[location[2]]:
                return "bookkeeping"
        if len(location) == 4 and location[2] == "payload" and location[3] == "internal_timeline_revision_id":
            return "bookkeeping"
    if location[0] == "parent" and len(location) == 2 and isinstance(location[1], str) \
            and location[1] in {"project_id", "timeline_id"}:
        return "bookkeeping"

    # Media references are grouped by their explicit owner path. Do not infer
    # that matching item and clip values represent the same selection.
    field = location[-1] if location else None
    if isinstance(field, str) and location[0] in {"shots", "parent"}:
        tail = location[2:] if location[0] == "shots" else location[1:]
        if field in _MEDIA_FIELDS:
            if len(tail) == 4 and tail[0] == "internal_timeline" and tail[1] == "clips" \
                    and isinstance(tail[2], Mapping) and tail[2].get("key") == "id" \
                    and tail[3] == field:
                return "media"
            if (location[0] == "parent" and
                    ((len(tail) == 3 and tail[0] == "clips" and isinstance(tail[1], Mapping)
                      and tail[1].get("key") == "id") or
                     (len(tail) == 4 and tail[0] == "config" and tail[1] == "clips"
                      and isinstance(tail[2], Mapping) and tail[2].get("key") == "id"))):
                return "media"
            if len(tail) == 5 and tail[0] == "internal_timeline" and tail[1] == "registry" \
                    and tail[2] == "assets" and isinstance(tail[3], str) \
                    and field in {"media_id", "object_id"}:
                return "media"
            if len(tail) == 4 and tail[0] == "payload" and tail[1] in {"items", "audio_bindings"} \
                    and isinstance(tail[2], Mapping) and tail[2].get("key") in {"item_id", "id", "track_id"} \
                    and field in {"media_id", "object_id"}:
                return "media"
            if len(tail) == 4 and tail[0] == "registry" and tail[1] == "assets" \
                    and isinstance(tail[2], str) \
                    and field in {"media_id", "object_id"}:
                return "media"
    return "authored"


def _path(change: Mapping[str, Any]) -> str:
    value = change.get("path")
    if isinstance(value, str) and value:
        return _safe_label(value)
    location = _location(change)
    if location is None:
        return "<property path unavailable>"
    out = []
    for item in location:
        if isinstance(item, Mapping):
            out.append(_identity(item))
        elif isinstance(item, int):
            out.append(f"[{item}]")
        else:
            out.append(_safe_label(item))
    return ".".join(out)


def _value(value: Any, *, shorten_ids: bool = False) -> str:
    if value is _ABSENT:
        return "<absent>"
    if shorten_ids and isinstance(value, str) and _HASH.fullmatch(value):
        return f"{value[:19]}… [ID shortened]"
    return _json(value)


def _change_values(change: Mapping[str, Any], *, shorten_ids: bool = False) -> tuple[str, str]:
    values = (
        _value(change.get("before", _ABSENT), shorten_ids=shorten_ids),
        _value(change.get("after", _ABSENT), shorten_ids=shorten_ids),
    )
    if shorten_ids and values[0] == values[1] and change.get("before") != change.get("after"):
        return _change_values(change)
    return values


def _selector_segment(location: list[Any], collection: str, key: str) -> str | None:
    index = None
    if collection == "clips":
        if location and location[0] == "shots" and len(location) >= 5 \
                and location[2] == "internal_timeline" and location[3] == "clips":
            index = 3
        elif location and location[0] == "parent" and len(location) >= 4:
            if location[1] == "clips":
                index = 1
            elif len(location) >= 5 and location[1] == "config" and location[2] == "clips":
                index = 2
    else:
        try:
            index = location.index(collection)
        except ValueError:
            index = None
    if index is None:
        return None
    if index + 1 >= len(location):
        return None
    selector = location[index + 1]
    if isinstance(selector, Mapping) and selector.get("key") == key:
        return str(selector.get("value"))
    return None


def _occurrence_id(location: list[Any] | None) -> str | None:
    if not location:
        return None
    if location[0] == "shots" and len(location) > 1:
        return str(location[1])
    if location[0] == "occurrences" and len(location) > 1:
        segment = location[1]
        if isinstance(segment, Mapping) and segment.get("key") == "occurrence_id":
            return str(segment.get("value"))
    return None


def _field_label(location: list[Any]) -> str:
    clip_id = _selector_segment(location, "clips", "id")
    if clip_id is not None:
        index = 3 if location[0] == "shots" else (1 if location[1] == "clips" else 2)
        suffix = location[index + 2:]  # collection + stable clip selector
        keys = [_identity(part) if isinstance(part, Mapping) else _safe_label(part) for part in suffix]
        if keys and keys[0] == "rect":
            relative = ".".join(keys)
            tail = ".".join(keys[1:])
            return f"Rectangle {tail} ({relative})" if tail else f"Rectangle ({relative})"
        labels = {"at": "Start", "hold": "Hold", "speed": "Speed", "volume": "Volume"}
        relative = ".".join(keys)
        leaf = keys[-1] if keys else "property"
        return f"{labels.get(leaf, leaf)} ({relative})"
    if location and location[0] == "shots" and len(location) >= 4 \
            and location[2] in {"payload", "internal_timeline"}:
        relative = location[3:]
        return ".".join(_identity(part) if isinstance(part, Mapping) else _safe_label(part)
                        for part in relative)
    return _path({"location": location})


def _context_side(value: Any) -> Mapping[str, Any] | None:
    return value if isinstance(value, Mapping) else None


def _display_name(before: Mapping[str, Any] | None, after: Mapping[str, Any] | None,
                  name_key: str, id_key: str, fallback: str) -> str:
    left = before.get(name_key) if before else None
    right = after.get(name_key) if after else None
    stable = (after or before or {}).get(id_key, fallback)
    if left and right and left != right:
        return f"{_safe_label(left)} → {_safe_label(right)} ({_safe_label(stable)})"
    if left or right:
        return f"{_safe_label(right or left)} ({_safe_label(stable)})"
    return _safe_label(stable)


def _selection(value: Any) -> tuple[str, str] | None:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) >= 2:
        media_id, kind = value[0], value[1]
        if isinstance(media_id, str) and media_id and isinstance(kind, str) and kind:
            return media_id, kind
    return None


def _selection_identity(value: Any) -> str | None:
    selected = _selection(value)
    return selected[0] if selected else None


def _context_clips(context: Mapping[str, Any], occurrence_id: str | None,
                   parent: bool = False) -> dict[str, tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]]:
    if parent:
        sides = context.get("parent")
    else:
        occurrences = context.get("occurrences", {})
        occurrence = occurrences.get(occurrence_id) if isinstance(occurrences, Mapping) else None
        sides = occurrence
    if not isinstance(sides, Mapping):
        return {}
    old_side, new_side = _context_side(sides.get("before")), _context_side(sides.get("after"))
    old_clips = old_side.get("clips", {}) if old_side else {}
    new_clips = new_side.get("clips", {}) if new_side else {}
    if not isinstance(old_clips, Mapping) or not isinstance(new_clips, Mapping):
        return {}
    result = {}
    for clip_id in old_clips.keys() | new_clips.keys():
        old, new = old_clips.get(clip_id), new_clips.get(clip_id)
        old = old if isinstance(old, Mapping) else None
        new = new if isinstance(new, Mapping) else None
        result[str(clip_id)] = (old, new)
    return result


def _track_label(context: Mapping[str, Any], occurrence_id: str | None,
                 parent: bool, clip: Mapping[str, Any] | None) -> str | None:
    if clip is None:
        return None
    track_ref = clip.get("track")
    if track_ref is None:
        return None
    sides = context.get("parent") if parent else (
        context.get("occurrences", {}).get(occurrence_id)
        if isinstance(context.get("occurrences"), Mapping) else None
    )
    if not isinstance(sides, Mapping):
        return _safe_label(track_ref)
    for side_key in ("after", "before"):
        side = sides.get(side_key)
        tracks = side.get("tracks", {}) if isinstance(side, Mapping) else {}
        track = tracks.get(track_ref) if isinstance(tracks, Mapping) else None
        if isinstance(track, Mapping):
            track_name = track.get("name")
            track_id = track.get("id", track_ref)
            return (f"{_safe_label(track_name)} ({_safe_label(track_id)})" if track_name
                    else _safe_label(track_id))
    return _safe_label(track_ref)


def _render_overview(data: Mapping[str, Any], classified: Mapping[str, list[Mapping[str, Any]]],
                     *, include_properties: bool = True) -> list[str]:
    summary = data.get("summary", {})
    context = data.get("context", {})
    if not isinstance(summary, Mapping):
        summary = {}
    if not isinstance(context, Mapping):
        context = {}
    project_id = _safe_label(data.get("project_id", "<project>"))
    timeline_id = _safe_label(data.get("timeline_id", "<timeline>"))
    from_revision = _safe_label(data.get("from_revision", "<from>"))
    to_revision = _safe_label(data.get("to_revision", "<to>"))
    lines = [f"{project_id} / {timeline_id}",
             f"{from_revision} → {to_revision} — "
             f"{'complete' if data.get('complete') else 'incomplete'} pinned composition comparison"]
    update = data.get("update")
    if isinstance(update, str) and update:
        # The semantic owner supplies the counts and wording; this view only
        # removes its revision-pair prefix, which is already in the header.
        prefix = f"Compared {data.get('from_revision')} to {data.get('to_revision')}: "
        summary_counts = summary.get("media_selections", {})
        has_semantic = (any(counts.get("updated", 0) or counts.get("removed", 0)
                            for counts in summary_counts.values() if isinstance(counts, Mapping))
                        or any(summary.get("timing_changes", {}).values())
                        or any(summary.get("occurrence_changes", {}).values())
                        or bool(summary.get("text_changes"))
                        or bool(summary.get("other_properties_changed")))
        if not has_semantic and classified["authored"]:
            lines.extend(["", "Other properties changed (listed below)."])
        elif not has_semantic and classified["bookkeeping"] and data.get("changes"):
            lines.extend(["", "No authored changes; revision/reference bookkeeping changed."])
        else:
            lines.extend(["", _safe_label(update.removeprefix(prefix))])
    elif not data.get("changes"):
        lines.extend(["", "No changes."])
    else:
        lines.extend(["", "Revision comparison summary unavailable."])

    authored = classified["authored"]
    media_rows = classified["media"]
    bookkeeping = classified["bookkeeping"]
    occurrence_groups: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    timeline_groups: list[Mapping[str, Any]] = []
    ungrouped: list[Mapping[str, Any]] = []
    for change in authored:
        location = _location(change)
        occurrence_id = _occurrence_id(location)
        if occurrence_id:
            occurrence_groups[occurrence_id].append(change)
        elif location and location[0] == "parent":
            timeline_groups.append(change)
        else:
            ungrouped.append(change)

    for change in media_rows:
        occurrence_id = _occurrence_id(_location(change))
        if occurrence_id:
            occurrence_groups.setdefault(occurrence_id, [])

    for occurrence_id, changes in occurrence_groups.items():
        occurrence_context = context.get("occurrences", {}).get(occurrence_id, {}) \
            if isinstance(context.get("occurrences"), Mapping) else {}
        old_side = _context_side(occurrence_context.get("before")) if isinstance(occurrence_context, Mapping) else None
        new_side = _context_side(occurrence_context.get("after")) if isinstance(occurrence_context, Mapping) else None
        heading = _display_name(old_side, new_side, "name", "shot_id", occurrence_id)
        shot_ids = [side.get("shot_id") for side in (old_side, new_side) if side and side.get("shot_id")]
        shot_id = str(shot_ids[-1]) if shot_ids else occurrence_id
        lines.extend(["", f"{heading} — occurrence {_safe_label(occurrence_id)}, shot {_safe_label(shot_id)}"])
        by_clip: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
        other_changes = []
        for change in changes:
            location = _location(change) or []
            clip_id = _selector_segment(location, "clips", "id")
            if clip_id is not None:
                by_clip[clip_id].append(change)
            else:
                other_changes.append(change)
        clip_context = _context_clips(context, occurrence_id)
        changed_selection_ids = {
            clip_id for clip_id, (old, new) in clip_context.items()
            if _selection_identity(old.get("selection") if old else None)
            != _selection_identity(new.get("selection") if new else None)
        }
        for clip_id in sorted(by_clip.keys() | changed_selection_ids):
            clip_rows = by_clip.get(clip_id, [])
            old_clip, new_clip = clip_context.get(clip_id, (None, None))
            display = _display_name(old_clip, new_clip, "name", "id", clip_id)
            track = _track_label(context, occurrence_id, False, new_clip or old_clip)
            prefix = "Image clip" if any(_selection((side or {}).get("selection")) and
                                         _selection((side or {}).get("selection"))[1] == "image"
                                         for side in (old_clip, new_clip)) else "Clip"
            heading_line = f"  {prefix} {display}"
            if track:
                heading_line += f" — track {track}"
            lines.append(heading_line)
            old_selection = _selection(old_clip.get("selection") if old_clip else None)
            new_selection = _selection(new_clip.get("selection") if new_clip else None)
            if ((old_selection[0] if old_selection else None) != (new_selection[0] if new_selection else None)
                    and (old_selection or new_selection)):
                kind = (new_selection or old_selection)[1]
                before_id = old_selection[0] if old_selection else _ABSENT
                after_id = new_selection[0] if new_selection else _ABSENT
                short_before = _value(before_id, shorten_ids=True)
                short_after = _value(after_id, shorten_ids=True)
                if old_selection and new_selection and short_before == short_after and old_selection[0] != new_selection[0]:
                    short_before, short_after = _json(old_selection[0]), _json(new_selection[0])
                if include_properties:
                    lines.append(f"    Selected {_safe_label(kind)}: {short_before} → {short_after}")
            for change in clip_rows if include_properties else ():
                before_text, after_text = _change_values(change)
                label = _field_label(_location(change) or [])
                lines.extend(_indented_property(label, before_text, after_text, "    "))
        if other_changes and include_properties:
            lines.append("  Other shot properties")
            for change in other_changes:
                lines.extend(_indented_property(_field_label(_location(change) or []), *_change_values(change), "    "))

    if timeline_groups and include_properties:
        lines.extend(["", "Timeline-level properties"])
        for change in timeline_groups:
            lines.extend(_indented_property(_path(change), *_change_values(change), "  "))
    if ungrouped and include_properties:
        lines.extend(["", "Other properties"])
        for change in ungrouped:
            lines.extend(_indented_property(_path(change), *_change_values(change), "  "))

    if media_rows:
        references = []
        for change in media_rows:
            location = _location(change) or []
            occurrence_id = _occurrence_id(location)
            label = _media_owner(location)
            owner = f"occurrence {_safe_label(occurrence_id)}: " if occurrence_id else "timeline: "
            before_text, after_text = _change_values(change, shorten_ids=True)
            references.append(owner + label + f": {before_text} → {after_text}")
        lines.extend(["", "Media reference data also changed:"])
        lines.extend(f"  {reference}" for reference in dict.fromkeys(references))
        if any("items" in (_location(change) or []) for change in media_rows):
            lines.append("  Item references remain at shot scope; no item-to-clip binding is assumed.")

    raw_count = data.get("raw_change_count", len(data.get("changes", [])))
    lines.extend(["", f"{raw_count} raw entr{'y' if raw_count == 1 else 'ies'} total."])
    if bookkeeping:
        lines.append(f"Revision/reference bookkeeping: {len(bookkeeping)} raw entr{'y' if len(bookkeeping) == 1 else 'ies'}; see full details.")
    diff_command = ["python3", "-m", "astrid", "timelines", "diff", str(data.get("timeline_id", "<timeline>")),
                    "--project", str(data.get("project_id", "<project>")),
                    "--from-revision", str(data.get("from_revision", "<from>")),
                    "--to-revision", str(data.get("to_revision", "<to>"))]
    import shlex
    lines.extend([f"Full details: repeat with {shlex.join(diff_command + ['--format', 'details'])}",
                  f"Exact machine data: repeat with {shlex.join(diff_command + ['--json'])}"])
    return lines


def _media_owner(location: list[Any]) -> str:
    asset = _selector_segment(location, "assets", "asset_id")
    # Runtime asset registries are maps, so the asset alias is a plain key.
    if asset is None and "assets" in location:
        index = location.index("assets")
        if index + 1 < len(location) and isinstance(location[index + 1], str):
            asset = location[index + 1]
    item = _selector_segment(location, "items", "item_id")
    if item is None and "items" in location:
        index = location.index("items")
        if index + 1 < len(location) and isinstance(location[index + 1], Mapping):
            item = str(location[index + 1].get("value"))
    if "items" in location:
        return f"item {_safe_label(item or '<item identity unavailable>')} (shot-level reference)"
    if "assets" in location:
        return f"asset {_safe_label(asset or '<asset alias unavailable>')}"
    if "clips" in location:
        clip = _selector_segment(location, "clips", "id")
        return f"clip selector {_safe_label(clip or '<clip identity unavailable>')}"
    return _path({"location": location})


def _indented_property(label: str, before: str, after: str, indent: str) -> list[str]:
    return [f"{indent}{label}", f"{indent}  before: {before}", f"{indent}  after:  {after}"]


def _render_details(data: Mapping[str, Any], classified: Mapping[str, list[Mapping[str, Any]]]) -> list[str]:
    lines = _render_overview(data, classified, include_properties=False)
    lines.extend(["", "Complete changed-field details follow. Counts are raw entries; values and IDs are full."])
    labels = {
        "authored": "Authored property details",
        "media": "Selected-media reference details",
        "bookkeeping": "Revision/hash bookkeeping",
    }
    for category in ("authored", "media", "bookkeeping"):
        rows = classified[category]
        lines.extend(["", f"{labels[category]} ({len(rows)} raw entr{'y' if len(rows) == 1 else 'ies'})"])
        if not rows:
            lines.append("  None.")
        for change in rows:
            before, after = _change_values(change)
            lines.extend([f"  {change.get('kind', 'changed')} {_path(change)}",
                          f"    location: {json.dumps(_location(change), ensure_ascii=False)}",
                          f"    before: {before}", f"    after:  {after}"])
    return lines


def _render_legacy(data: Mapping[str, Any], mode: str) -> str:
    del mode
    return "Diff result — structured locations unavailable.\n" + _json(data)


def format_authoring_revision_diff(data: Mapping[str, Any], *, mode: str = "readable") -> str:
    """Present an exact diff without removing any raw change row.

    ``readable`` groups authored fields by occurrence and clip; ``details``
    appends every raw row once in complete, untruncated form.
    """
    if mode not in {"readable", "details"}:
        raise ValueError("mode must be 'readable' or 'details'")
    if not isinstance(data, Mapping):
        raise TypeError("data must be an object")
    required = ("project_id", "timeline_id", "from_revision", "to_revision", "complete", "changes")
    if any(field not in data for field in required):
        return _render_legacy(data, mode)
    changes = data.get("changes", [])
    if not isinstance(changes, list):
        raise ValueError("diff changes must be a list")
    if any(not _location(change) for change in changes if isinstance(change, Mapping)):
        return _render_legacy(data, mode)
    classified = {category: [] for category in ("authored", "media", "bookkeeping")}
    for change in changes:
        if not isinstance(change, Mapping):
            classified["authored"].append({"kind": "changed", "path": "<raw change>",
                                            "before": _ABSENT, "after": change,
                                            "location": []})
            continue
        category = _classify(_location(change))
        classified[category].append(change)
    lines = _render_overview(data, classified)
    if mode == "details":
        lines = _render_details(data, classified)
    if isinstance(data.get('intervals'), Mapping):
        from .change_intervals import interval_lines
        lines.extend(('', *interval_lines(data['intervals'])))
    return "\n".join(lines)


__all__ = ["format_authoring_revision_diff"]
