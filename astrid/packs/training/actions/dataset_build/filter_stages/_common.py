"""Shared helpers for dataset-build filter stages."""

from __future__ import annotations

from astrid.pack_api import pack_root

PACK_ROOT = pack_root('training')

import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any


from ....shared.items import deterministic_id


def canonical_source_id(item: Mapping[str, Any]) -> str:
    """Return the source identity used by source-level filters and resume state."""

    derived_from = item.get("derived_from")
    if isinstance(derived_from, Mapping):
        source_id = _non_empty_str(derived_from.get("source_id"))
        if source_id is not None:
            return source_id

    source_id = _non_empty_str(item.get("source_id"))
    if source_id is not None:
        return source_id

    fallback_parts = _source_fallback_parts(item)
    return deterministic_id(*fallback_parts, prefix="source") if fallback_parts else ""


def build_filter_stats(
    *,
    stage_id: str,
    stage_order: int,
    items_in: int,
    items_passed: int,
    items_rejected: int,
    started: float,
    rejection_reasons: Mapping[str, int] | None = None,
    warnings: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Build a schema-valid FilterStats payload."""

    return {
        "stage_id": stage_id,
        "stage_order": stage_order,
        "items_in": items_in,
        "items_passed": items_passed,
        "items_rejected": items_rejected,
        "rejection_reasons": dict(rejection_reasons or {}),
        "duration_ms": round((time.perf_counter() - started) * 1000, 3),
        "warnings": list(warnings or []),
    }


def record_warning(warnings: list[str], message: str) -> None:
    """Append a non-empty warning once, preserving first-seen order."""

    if message and message not in warnings:
        warnings.append(message)


def increment_reason(reasons: dict[str, int], reason: str) -> None:
    reasons[reason] = reasons.get(reason, 0) + 1


def with_filter_result(
    item: Mapping[str, Any],
    stage_id: str,
    *,
    passed: bool,
    reason: str,
    score: float | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a copy of item with a stage result under filter_results."""

    updated = dict(item)
    filter_results = dict(updated.get("filter_results") or {})
    result: dict[str, Any] = {"passed": passed, "reason": reason}
    if score is not None:
        result["score"] = score
    if extra:
        result.update(dict(extra))
    filter_results[stage_id] = result
    updated["filter_results"] = filter_results
    updated.setdefault("review_status", "pending")
    return updated


def pass_item(
    item: Mapping[str, Any],
    stage_id: str,
    *,
    reason: str = "",
    score: float | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return with_filter_result(item, stage_id, passed=True, reason=reason, score=score, extra=extra)


def reject_item(
    item: Mapping[str, Any],
    stage_id: str,
    *,
    reason: str,
    score: float | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    updated = with_filter_result(item, stage_id, passed=False, reason=reason, score=score, extra=extra)
    updated["review_status"] = "rejected"
    return updated


def nested_metadata(item: Mapping[str, Any], path: str | Sequence[str], default: Any = None) -> Any:
    """Read a nested value from item/source_metadata using dotted or sequence paths."""

    keys = path.split(".") if isinstance(path, str) else list(path)
    if not keys:
        return default

    roots: list[Any] = [item]
    metadata = item.get("source_metadata")
    if isinstance(metadata, Mapping):
        roots.append(metadata)

    for root in roots:
        value = _nested_get(root, keys, default)
        if value is not default:
            return value
    return default


def resolve_media_path(
    item: Mapping[str, Any],
    *,
    repo_root: Path = PACK_ROOT,
    required: bool = False,
    must_exist: bool = False,
) -> Path | None:
    """Resolve item media_path, without probing or touching media."""

    raw = item.get("media_path")
    if raw is None:
        raw = nested_metadata(item, "media_path")
    if raw is None:
        if required:
            raise ValueError(f"item {item.get('item_id') or item.get('source_id') or '<unknown>'} has no media_path")
        return None

    path = Path(str(raw)).expanduser()
    if not path.is_absolute():
        path = repo_root / path
    resolved = path.resolve()
    if must_exist and not resolved.exists():
        if required:
            raise FileNotFoundError(resolved)
        return None
    return resolved


def _nested_get(root: Any, keys: Sequence[str], default: Any) -> Any:
    current = root
    for key in keys:
        if not isinstance(current, Mapping) or key not in current:
            return default
        current = current[key]
    return current


def _non_empty_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _source_fallback_parts(item: Mapping[str, Any]) -> list[str]:
    metadata = item.get("source_metadata")
    parts: list[str] = []
    if isinstance(metadata, Mapping):
        for key in ("source_id", "asset_id", "video_id", "path", "file", "url"):
            value = _non_empty_str(metadata.get(key))
            if value is not None:
                parts.append(value)
    for key in ("media_path", "source_url", "content_hash"):
        value = _non_empty_str(item.get(key))
        if value is not None:
            parts.append(value)
    return parts
