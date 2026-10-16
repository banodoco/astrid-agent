"""Duplicate a timeline into a new one, with new shot and occurrence ids.

Shot ids are global per project, so a duplicate can never reuse the source's
shot ids. The copy is built the way ``production/scripts/build_timeline.py``
builds a candidate: an authoring shot per source placement, each with its own
internal timeline, registry and placement row, published as one revision of
the new (empty) timeline.

Pure parts (``id_map``, ``copy_content``) take and return plain dicts; only
``duplicate_timeline`` talks to the runtime.
"""
from __future__ import annotations

import copy
import uuid
from collections.abc import Iterable, Mapping
from typing import Any

from astrid.core.timeline.astrid_ns import migrate_bundle
from astrid.sdk.timeline_checkout import Checkout, TimelineEditError
from astrid.sdk.timeline_editing import add_authoring_shot

__all__ = ["duplicate_timeline", "id_map", "copy_content"]

# Payload fields that point at the source's own runtime records. They are not
# copied: the new shot gets its own records on the next publish.
_SOURCE_BOUND_PAYLOAD_KEYS = ("text_bindings", "internal_timeline_revision_id")


def _common_prefix(values: list[str]) -> str:
    if not values:
        return ""
    prefix = values[0]
    for value in values[1:]:
        while not value.startswith(prefix):
            prefix = prefix[:-1]
    # Cut back to a '-' boundary so suffixes stay whole words.
    return prefix[: prefix.rfind("-") + 1] if "-" in prefix else ""


def id_map(old_ids: Iterable[str], new_slug: str) -> dict[str, str]:
    """Map each old id to ``<new_slug>-<suffix>``, where suffix is the id after the ids' shared prefix.

    ``["p-ch01-a", "p-ch02-b"]`` with slug ``dup`` gives ``{"p-ch01-a": "dup-ch01-a", "p-ch02-b": "dup-ch02-b"}``.
    A single id keeps its whole text as the suffix. Old ids must be unique; the result is injective.
    """
    ids = [str(i) for i in old_ids]
    if len(set(ids)) != len(ids):
        raise TimelineEditError("cannot map duplicate source ids")
    prefix = _common_prefix(ids) if len(ids) > 1 else ""
    mapping: dict[str, str] = {}
    for old in ids:
        suffix = old[len(prefix):] or old
        mapping[old] = f"{new_slug}-{suffix}"
    if len(set(mapping.values())) != len(mapping):
        raise TimelineEditError("id mapping is not one-to-one; choose another slug")
    return mapping


def _remap_values(value: Any, mapping: Mapping[str, str]) -> Any:
    """Replace every string that is exactly a mapped id (values only, never substrings)."""
    if isinstance(value, str):
        return mapping.get(value, value)
    if isinstance(value, list):
        return [_remap_values(v, mapping) for v in value]
    if isinstance(value, dict):
        return {k: _remap_values(v, mapping) for k, v in value.items()}
    return value


def copy_content(source: Mapping[str, Any], target: Mapping[str, Any], *, new_slug: str) -> tuple[dict[str, Any], list[str]]:
    """Return a copy of ``target`` (keeping its provenance and identity) holding ``source``'s content.

    Shots, placements and occurrences get new ids (``id_map``). Returns ``(bundle, notes)``
    where notes say what was not carried over.
    """
    notes: list[str] = []
    placements = [dict(row) for row in source.get("placements") or []]
    shot_ids = [str(row["shot_id"]) for row in placements]
    occurrence_ids = [str(row["occurrence_id"]) for row in placements]
    shot_map = id_map(shot_ids, new_slug)
    occ_map = id_map(occurrence_ids, f"occ-{new_slug}")
    ids = {**shot_map, **occ_map}

    out = copy.deepcopy(dict(target))
    src_parent = source.get("parent") or {}
    out["parent"] = {
        **dict(out.get("parent") or {}),
        "config": copy.deepcopy(src_parent.get("config") or {}),
        "registry": _remap_values(copy.deepcopy(src_parent.get("registry") or {}), ids),
        "clips": _remap_values(copy.deepcopy(src_parent.get("clips") or []), ids),
    }
    out["shots"] = {}
    out["placements"] = []
    out["source_mapping"] = {"placements": {}, "shots": {}}

    text_bindings = 0
    for row in placements:
        old_shot = str(row["shot_id"])
        new_shot = shot_map[old_shot]
        src_shot = (source.get("shots") or {}).get(old_shot)
        if not isinstance(src_shot, Mapping):
            raise TimelineEditError(f"source shot {old_shot!r} has no content")
        # Seed the shot the way build_timeline does (add_authoring_shot). Its base_* stays the empty seed
        # and the current content is the copy: that difference is what makes the compiler materialize it.
        shot = add_authoring_shot(out, shot_id=new_shot, occurrence_id=occ_map[str(row["occurrence_id"])],
                                  start_ms=(row.get("placement") or {}).get("start_ms", 0))
        payload = copy.deepcopy(dict(src_shot.get("payload") or src_shot.get("base_payload") or {}))
        text_bindings += len(payload.get("text_bindings") or [])
        for key in _SOURCE_BOUND_PAYLOAD_KEYS:
            payload.pop(key, None)
        payload["text_bindings"] = []
        shot["payload"] = payload
        shot["internal_timeline"] = _remap_values(copy.deepcopy(dict(src_shot.get("internal_timeline") or {})), shot_map)
        new_row = _remap_values(copy.deepcopy(row), ids)
        new_row["shot_id"] = new_shot
        new_row["occurrence_id"] = occ_map[str(row["occurrence_id"])]
        out["placements"][-1] = new_row  # replaces the placement add_authoring_shot appended
        out["source_mapping"]["placements"][new_row["occurrence_id"]] = copy.deepcopy(new_row)

    if text_bindings:
        from astrid.sdk.timeline_checkout import Checkout

        bound = Checkout(copy.deepcopy(out)).narration()
        if len(bound) == len(out.get("shots") or {}):
            notes.append(f"narration: bound again from each line's script text ({len(bound)} shots)")
        else:
            notes.append(f"{text_bindings} narration text binding(s) not copied (these lines declare no script text); "
                         "the words themselves are copied in each VO clip's app.words")
    migrate_bundle(out)  # the source may be an old revision: the copy carries intent under app.astrid
    return out, notes


def duplicate_timeline(project: str, source: str, *, slug: str | None = None, client: Any = None) -> dict[str, Any]:
    """Create a timeline with a copy of ``source``'s head (new shot ids) and publish it.

    Returns ``{"timeline_id", "slug", "source", "shots", "old_head", "new_head", "notes"}``.
    If the copy fails after the timeline exists, the error names that timeline so it can be archived.
    """
    if client is not None:
        return _duplicate(client, project, source, slug=slug)
    from astrid.sdk import AstridClient

    with AstridClient.open_from_launcher(start_pack_host=False) as opened:
        return _duplicate(opened, project, source, slug=slug)


def _duplicate(client: Any, project: str, source: str, *, slug: str | None) -> dict[str, Any]:
    src = Checkout.open(project, source, client=client)
    report = src.check()  # all or nothing: refuse an invalid head BEFORE creating anything
    if not report.valid:
        raise TimelineEditError(f"not duplicating {source}: its head is not valid ("
                                + "; ".join((report.blocking or report.problems or ["invalid"])[:3]) + "); nothing was created")
    created = client.timelines.create_empty(
        project=src.bundle["project_id"],
        timeline_id=slug or None,
        config=copy.deepcopy(dict((src.bundle.get("parent") or {}).get("config") or {})),
        duplicate_source={
            "timeline_id": src.bundle.get("timeline_id"),
            "expected_revision_id": (src.bundle.get("base_parent") or {}).get("revision_id"),
        },
        idempotency_key=f"dup-{uuid.uuid4().hex}",
    )
    if not created.ok:
        error = getattr(created.error, "message", created.error)
        raise TimelineEditError(f"could not create the timeline: {error}")
    row = created.data if isinstance(created.data, Mapping) else {}
    new_id = str(row.get("timeline_id") or slug or "")
    if not new_id:
        raise TimelineEditError("the runtime created a timeline but returned no id")
    try:
        dst = Checkout.open(project, new_id, client=client)
        bundle, notes = copy_content(src.bundle, dst.bundle, new_slug=slug or new_id)
        dst.bundle = bundle
        dst.notes.extend(notes)
        report = dst.check()
        if not report.valid:
            raise TimelineEditError("the copy is invalid: " + "; ".join(report.problems or [str(report)]))
        receipt = dst.publish(f"duplicate of {source}", client=client,
                              idempotency_key=f"dup-{uuid.uuid4().hex}")
    except Exception as exc:
        archived = client.timelines.archive(project, new_id)  # all or nothing: the empty timeline goes too
        if getattr(archived, "ok", False):
            raise TimelineEditError(f"{exc} (nothing kept: the new timeline {new_id!r} was archived again)") from exc
        raise TimelineEditError(f"{exc} (the empty timeline {new_id!r} was created and could not be archived: "
                                f"`timelines archive {new_id} --project {project}`)") from exc
    return {
        "timeline_id": new_id,
        "slug": slug,
        "source": source,
        "shots": len(bundle["shots"]),
        "old_head": receipt.get("old_head"),
        "new_head": receipt.get("new_head"),
        "notes": list(dst.notes),
    }
