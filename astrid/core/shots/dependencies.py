"""Pure fixed-point invalidation for shot-owned derivative descriptors.

The runtime remains the source of truth for shots and media.  This module only
compares caller-supplied read models and returns a deterministic report; it has
no database, filesystem, or timeline-pack dependency.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

_DETERMINISTIC_KINDS = frozenset(
    {"plate", "render_plate", "proxy", "review_proxy", "timeline_asset"}
)
_GENERATIVE_KINDS = frozenset(
    {"transition", "generative_transition", "continuity", "continuity_input"}
)


def _records(value: object) -> list[Mapping[str, Any]]:
    if isinstance(value, Mapping):
        return [item for item in value.values() if isinstance(item, Mapping)]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [item for item in value if isinstance(item, Mapping)]
    return []


def _meta(record: Mapping[str, Any]) -> Mapping[str, Any]:
    value = record.get("metadata")
    return value if isinstance(value, Mapping) else record


def _kind(record: Mapping[str, Any]) -> str:
    metadata = _meta(record)
    return str(
        metadata.get("kind")
        or metadata.get("item_kind")
        or metadata.get("dependency_kind")
        or metadata.get("role")
        or ""
    ).lower()


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _descriptor(record: Mapping[str, Any]) -> dict[str, Any]:
    metadata = _meta(record)
    return {
        "source_item_id": _string(
            metadata.get("source_item_id") or metadata.get("source_item")
        ),
        "source_media_id": _string(
            metadata.get("source_media_id")
            or metadata.get("source_media")
            or metadata.get("input_media_id")
        ),
        "source_content_sha256": _string(
            metadata.get("source_content_sha256")
            or metadata.get("source_hash")
            or metadata.get("source_content_hash")
            or metadata.get("input_content_sha256")
        ),
    }


def _actual_media_hashes(media: object) -> dict[str, str]:
    result: dict[str, str] = {}
    for record in _records(media):
        media_id = _string(record.get("id") or record.get("media_id"))
        content_hash = _string(record.get("content_hash") or record.get("content_sha256"))
        if media_id is not None and content_hash is not None:
            result[media_id] = content_hash
    return result


def _endpoint_mismatch(
    record: Mapping[str, Any],
    *,
    media_hashes: Mapping[str, str],
    superseded_media_ids: set[str],
    active_media_id: str | None,
) -> tuple[str, str, str] | None:
    metadata = _meta(record)
    for endpoint in ("from", "to", "start", "end", "parent", "input"):
        media_id = _string(
            metadata.get(f"{endpoint}_media_id")
            or metadata.get(f"{endpoint}_media")
        )
        expected_hash = _string(
            metadata.get(f"{endpoint}_content_sha256")
            or metadata.get(f"{endpoint}_content_hash")
            or metadata.get(f"{endpoint}_hash")
        )
        if media_id is None:
            continue
        if media_id in superseded_media_ids and active_media_id is not None:
            return f"{endpoint}_media_id", media_id, active_media_id
        if expected_hash is not None and media_hashes.get(media_id) != expected_hash:
            return f"{endpoint}_content_sha256", expected_hash, str(media_hashes.get(media_id))
    return None


def _mismatch(
    descriptor: Mapping[str, Any],
    *,
    active_item_id: str | None,
    item_by_id: Mapping[str, Mapping[str, Any]],
    stale_ids: set[str],
    media_hashes: Mapping[str, str],
) -> tuple[str, str, str] | None:
    expected_item = descriptor.get("source_item_id")
    expected_media = descriptor.get("source_media_id")
    expected_hash = descriptor.get("source_content_sha256")
    if expected_item is not None:
        source = item_by_id.get(str(expected_item))
        if source is None:
            return "source_item_id", str(expected_item), "missing"
        if str(expected_item) in stale_ids:
            return "source_item_id", str(expected_item), "stale"
        if (
            _kind(source) == "primary_visual"
            and active_item_id is not None
            and expected_item != active_item_id
        ):
            return "source_item_id", str(expected_item), active_item_id
    if expected_media is not None and expected_hash is not None:
        actual_hash = media_hashes.get(str(expected_media))
        if actual_hash != expected_hash:
            return "source_content_sha256", str(expected_hash), str(actual_hash)
    return None


def _entry(
    record: Mapping[str, Any], *, reason: str, field: str, expected: str, actual: str
) -> dict[str, Any]:
    item_id = _string(record.get("id") or record.get("item_id"))
    result: dict[str, Any] = {
        "kind": _kind(record),
        "reason": reason,
        "field": field,
        "expected": expected,
        "actual": actual,
    }
    if item_id is not None:
        result["item_id"] = item_id
    media_id = _string(record.get("media_id"))
    if media_id is not None:
        result["media_id"] = media_id
    return result


def analyze_invalidation(
    shot_items: Sequence[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]] = (),
    media: Sequence[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]] = (),
    timeline_assets: Sequence[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]] = (),
    *,
    media_relations: Sequence[Mapping[str, Any]] | Mapping[str, Mapping[str, Any]] = (),
) -> dict[str, list[dict[str, Any]]]:
    """Classify frozen derivatives without mutating or persisting anything.

    Deterministic records are evaluated to a fixed point, so a proxy remains
    stale even when it appears before the stale plate it consumes.  The final
    report is sorted by stable identity and kind, independent of input order.
    """
    items = _records(shot_items)
    relations = list(_records(media_relations))
    for media_record in _records(media):
        relations.extend(_records(media_record.get("relations")))
    media_hashes = _actual_media_hashes(media)
    active_primary = next(
        (
            str(item.get("id") or item.get("item_id"))
            for item in items
            if _kind(item) == "primary_visual" and _meta(item).get("status") == "primary"
        ),
        None,
    )
    active_media_id = next(
        (
            _string(item.get("media_id"))
            for item in items
            if str(item.get("id") or item.get("item_id")) == active_primary
        ),
        None,
    )
    superseded_media_ids = {
        media_id
        for item in items
        if _kind(item) == "primary_visual" and _meta(item).get("status") == "superseded"
        for media_id in [_string(item.get("media_id"))]
        if media_id is not None
    }
    item_by_id = {
        str(item.get("id") or item.get("item_id")): item
        for item in items
        if _string(item.get("id") or item.get("item_id")) is not None
    }

    stale_ids: set[str] = set()
    while True:
        before = len(stale_ids)
        for item in items:
            if _kind(item) not in _DETERMINISTIC_KINDS:
                continue
            mismatch = _mismatch(
                _descriptor(item),
                active_item_id=active_primary,
                item_by_id=item_by_id,
                stale_ids=stale_ids,
                media_hashes=media_hashes,
            ) or _endpoint_mismatch(
                item,
                media_hashes=media_hashes,
                superseded_media_ids=superseded_media_ids,
                active_media_id=active_media_id,
            )
            if mismatch is not None:
                item_id = _string(item.get("id") or item.get("item_id"))
                if item_id is not None:
                    stale_ids.add(item_id)
        if len(stale_ids) == before:
            break

    report: dict[str, list[dict[str, Any]]] = {
        "stale": [],
        "blocked_on_generation": [],
        "ready_to_compile": [],
        "current": [],
    }
    for item in items:
        kind = _kind(item)
        if kind not in _DETERMINISTIC_KINDS | _GENERATIVE_KINDS:
            continue
        mismatch = _mismatch(
            _descriptor(item),
            active_item_id=active_primary,
            item_by_id=item_by_id,
            stale_ids=stale_ids,
            media_hashes=media_hashes,
        ) or _endpoint_mismatch(
            item,
            media_hashes=media_hashes,
            superseded_media_ids=superseded_media_ids,
            active_media_id=active_media_id,
        )
        if mismatch is None:
            report["current"].append({"kind": kind, "item_id": _string(item.get("id") or item.get("item_id"))})
            continue
        field, expected, actual = mismatch
        bucket = "stale" if kind in _DETERMINISTIC_KINDS else "blocked_on_generation"
        report[bucket].append(
            _entry(
                item,
                reason=(
                    "frozen deterministic source no longer matches current input"
                    if bucket == "stale"
                    else "generative dependency requires explicit regeneration"
                ),
                field=field,
                expected=expected,
                actual=actual,
            )
        )

    seen_relations: set[tuple[str, str, str, str]] = set()
    for relation in relations:
        if relation.get("kind") != "uses_as_input":
            continue
        key = (str(relation.get("from_media_id")), str(relation.get("to_media_id")), str(relation.get("kind")), str(relation.get("ordinal")))
        if key in seen_relations:
            continue
        seen_relations.add(key)
        descriptor = _descriptor(relation)
        source_media = descriptor.get("source_media_id")
        expected_hash = descriptor.get("source_content_sha256")
        mismatch = None
        if source_media in superseded_media_ids and active_media_id is not None:
            mismatch = ("source_media_id", str(source_media), active_media_id)
        elif source_media is not None and expected_hash is not None and media_hashes.get(str(source_media)) != expected_hash:
            mismatch = ("source_content_sha256", str(expected_hash), str(media_hashes.get(str(source_media))))
        if mismatch is not None:
            field, expected, actual = mismatch
            entry = _entry(relation, reason="continuity input requires explicit regeneration", field=field, expected=expected, actual=actual)
            entry.update({"kind": "continuity_input", "from_media_id": relation.get("from_media_id"), "to_media_id": relation.get("to_media_id")})
            report["blocked_on_generation"].append(entry)

    for asset in _records(timeline_assets):
        descriptor = _descriptor(asset)
        media_id = descriptor["source_media_id"] or _string(asset.get("media_id"))
        expected_hash = descriptor["source_content_sha256"] or _string(asset.get("content_sha256") or asset.get("content_hash"))
        mismatch = _mismatch(descriptor, active_item_id=active_primary, item_by_id=item_by_id, stale_ids=stale_ids, media_hashes=media_hashes)
        if mismatch is None and media_id in superseded_media_ids and active_media_id is not None:
            mismatch = ("media_id", str(media_id), active_media_id)
        if mismatch is None and (media_id is None or expected_hash is None or media_hashes.get(media_id) != expected_hash):
            if media_id is not None and expected_hash is not None:
                mismatch = ("content_sha256", expected_hash, str(media_hashes.get(media_id)))
        if mismatch is None:
            report["current"].append({"kind": "timeline_asset", "asset_id": _string(asset.get("id") or asset.get("asset_id"))})
        else:
            field, expected, actual = mismatch
            result = _entry(asset, reason="timeline asset pin no longer matches current media", field=field, expected=expected, actual=actual)
            result["kind"] = "timeline_asset"
            report["stale"].append(result)

    for values in report.values():
        values.sort(key=lambda value: (str(value.get("item_id") or value.get("asset_id") or ""), str(value.get("kind"))))
    return report


__all__ = ["analyze_invalidation"]
