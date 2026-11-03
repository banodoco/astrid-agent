"""Attempt-local asset helpers for the cut worker.

The generic host materializes admitted runtime objects into the fenced attempt
directory before invoking a pack.  This module turns those local files into a
render registry for the attempt; it has no runtime client, workspace lookup, URL
fetcher, or project-storage fallback.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from astrid.pack_api import AstridError
from astrid.pack_api import require_runtime_materialized_file
from astrid.pack_api import CARRY_FORWARD_SOURCE_FIELDS, AssetRegistry

_PRESERVED_REGISTRY_FIELDS = (
    "origin",
    "etag",
    "content_sha256",
    "thumbnailUrl",
    "derivedFrom",
    "generationId",
    "variantId",
)


def _lookup_probe_asset():
    """Keep the established test/adapter seam through the run facade."""
    run_module = sys.modules.get("astrid.packs.video_editing.actions.cut.run")
    if run_module is not None:
        return run_module.probe_asset
    from .probe import probe_asset

    return probe_asset


def resolve_materialized_asset_paths(args: Any) -> dict[str, Path]:
    """Resolve only host-materialized file inputs supplied to this attempt."""
    paths: dict[str, Path] = {}
    for raw_entry in getattr(args, "asset", None) or []:
        if not isinstance(raw_entry, str) or "=" not in raw_entry:
            raise AstridError(f"Invalid --asset value {raw_entry!r}: expected KEY=PATH")
        key, raw_path = raw_entry.split("=", 1)
        if not key or not raw_path:
            raise AstridError(f"Invalid --asset value {raw_entry!r}: expected KEY=PATH")
        if key in paths:
            raise AstridError(f"Duplicate asset key {key!r} in --asset")
        try:
            paths[key] = require_runtime_materialized_file(
                raw_path, label=f"asset {key!r}"
            )
        except ValueError as exc:
            raise AstridError(str(exc)) from exc

    for key, value, flag in (
        ("main", getattr(args, "video", None), "--video"),
        ("rant", getattr(args, "audio", None), "--audio"),
    ):
        if value is None or (key == "rant" and getattr(args, "video", None) is not None):
            continue
        if key in paths:
            raise AstridError(f"Duplicate asset key {key!r}")
        try:
            paths[key] = require_runtime_materialized_file(value, label=flag)
        except ValueError as exc:
            raise AstridError(str(exc)) from exc
    return paths


def _carry_forward(entry: dict[str, Any], existing: Any) -> None:
    if not isinstance(existing, dict):
        return
    for field in _PRESERVED_REGISTRY_FIELDS:
        value = existing.get(field)
        if value not in (None, ""):
            entry[field] = value


def build_attempt_registry(
    asset_paths: dict[str, Path],
    existing_registry: AssetRegistry,
    prior_meta: dict[str, Any] | None,
) -> tuple[AssetRegistry, dict[str, dict[str, Any]]]:
    """Probe materialized files and build a render-only attempt registry."""
    registry: AssetRegistry = {"assets": {}}
    sources_meta: dict[str, dict[str, Any]] = {}
    existing_assets = existing_registry.get("assets", {})
    prior_sources = (prior_meta or {}).get("sources", {})

    for key, path in asset_paths.items():
        try:
            resolved = require_runtime_materialized_file(path, label=f"asset {key!r}")
        except ValueError as exc:
            raise AstridError(str(exc)) from exc
        prior = prior_sources.get(key, {}) if isinstance(prior_sources, dict) else {}
        if not isinstance(prior, dict):
            prior = {}
        sources_meta[key] = {
            field: prior[field]
            for field in CARRY_FORWARD_SOURCE_FIELDS
            if field in prior
        }
        existing = existing_assets.get(key) if isinstance(existing_assets, dict) else None
        cache_hit = (
            isinstance(existing, dict)
            and existing.get("file") == str(resolved)
            and existing.get("duration") is not None
            and (
                existing.get("type") == "audio"
                or (
                    existing.get("resolution") not in (None, "")
                    and existing.get("fps") is not None
                )
            )
        )
        if cache_hit:
            entry: dict[str, Any] = {
                "file": str(resolved),
                "duration": existing["duration"],
                "type": existing.get("type", "video"),
            }
            if entry["type"] != "audio":
                entry["resolution"] = existing["resolution"]
                entry["fps"] = existing["fps"]
        else:
            probed = _lookup_probe_asset()(resolved)
            media_type = probed.get("type", "video")
            entry = {
                "file": str(resolved),
                "duration": probed["duration"],
                "type": media_type,
            }
            if media_type != "audio":
                entry["resolution"] = probed["resolution"]
                entry["fps"] = probed["fps"]
            if probed.get("codec") is not None:
                sources_meta[key]["codec"] = probed["codec"]
        _carry_forward(entry, existing)
        registry["assets"][key] = entry
    return registry, sources_meta
