"""Shared item helpers for ``training.dataset_build``."""

from __future__ import annotations

from astrid.pack_api import checkout_root

CHECKOUT_ROOT = checkout_root()

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from astrid.pack_api import utc_now_iso as _utc_now_iso

UNKNOWN_RIGHTS = {
    "license": "unknown",
    "attribution": "",
    "restrictions": [],
    "rights_status": "unknown",
}


def utc_now_iso() -> str:
    return _utc_now_iso()


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def stable_json_sha256(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def config_hash(config: Mapping[str, Any]) -> str:
    return stable_json_sha256(config)


def deterministic_id(*parts: object, prefix: str | None = None, length: int = 16) -> str:
    raw = "\x1f".join(str(part) for part in parts)
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:length]
    return f"{prefix}_{digest}" if prefix else digest


def repo_relative_path(path: str | Path, *, repo_root: Path = CHECKOUT_ROOT) -> str:
    resolved = Path(path).expanduser().resolve()
    root = repo_root.expanduser().resolve()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return str(resolved)


def explicit_rights(rights: Mapping[str, Any] | None = None) -> dict[str, Any]:
    payload = dict(UNKNOWN_RIGHTS)
    if rights:
        payload.update({key: value for key, value in rights.items() if value is not None})
    if not payload.get("rights_status"):
        payload["rights_status"] = "unknown"
    payload.setdefault("restrictions", [])
    return payload


def make_candidate_item(
    *,
    source_type: str,
    source_id: str,
    source_url: str,
    media_path: str | Path,
    media_type: str = "video",
    source_metadata: Mapping[str, Any] | None = None,
    rights: Mapping[str, Any] | None = None,
    content_hash: str | None = None,
    acquired_at: str | None = None,
    duration_s: float | None = None,
    clip_start_s: float | None = None,
    clip_end_s: float | None = None,
    scene_index: int | None = None,
    derived_from: Mapping[str, Any] | None = None,
    repo_root: Path = CHECKOUT_ROOT,
) -> dict[str, Any]:
    item: dict[str, Any] = {
        "source_type": source_type,
        "source_id": source_id,
        "source_url": source_url,
        "source_metadata": dict(source_metadata or {}),
        "rights": explicit_rights(rights),
        "content_hash": content_hash or sha256_file(media_path),
        "acquired_at": acquired_at or utc_now_iso(),
        "media_path": repo_relative_path(media_path, repo_root=repo_root),
        "media_type": media_type,
    }
    if duration_s is not None:
        item["duration_s"] = duration_s
    if clip_start_s is not None:
        item["clip_start_s"] = clip_start_s
    if clip_end_s is not None:
        item["clip_end_s"] = clip_end_s
    if scene_index is not None:
        item["scene_index"] = scene_index
    if derived_from is not None:
        item["derived_from"] = dict(derived_from)
    return item


def make_review_item(
    candidate: Mapping[str, Any],
    *,
    item_id: str | None = None,
    bucket: str | None = None,
    caption: Mapping[str, Any] | None = None,
    caption_file: str | Path | None = None,
    filter_results: Mapping[str, Any] | None = None,
    review_status: str = "pending",
    repo_root: Path = CHECKOUT_ROOT,
) -> dict[str, Any]:
    review_item = dict(candidate)
    review_item["item_id"] = item_id or deterministic_id(
        candidate.get("source_type", ""),
        candidate.get("source_id", ""),
        candidate.get("scene_index", ""),
        prefix="item",
    )
    review_item["rights"] = explicit_rights(review_item.get("rights"))
    review_item["review_status"] = review_status
    if bucket is not None:
        review_item["bucket"] = bucket
    if caption is not None:
        review_item["caption"] = dict(caption)
    if caption_file is not None:
        review_item["caption_file"] = repo_relative_path(caption_file, repo_root=repo_root)
    if filter_results is not None:
        review_item["filter_results"] = dict(filter_results)
    return review_item
