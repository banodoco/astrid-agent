"""Canonical manifest helpers shared by Training actions."""

from __future__ import annotations

from astrid.pack_api import pack_root

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import jsonschema
from referencing import Registry, Resource

from .items import repo_relative_path, utc_now_iso

SCHEMAS_ROOT = pack_root("training") / "actions" / "dataset_build" / "schemas"


def accepted_items(items: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(item) for item in items if item.get("review_status") == "accepted"]


def build_canonical_manifest(
    items: list[Mapping[str, Any]],
    *,
    dataset_id: str,
    source_provider: str | None = None,
    manifest_adapter: str = "ai-toolkit-ltx",
    bucket_targets: Mapping[str, int | Mapping[str, Any]] | None = None,
    created_at: str | None = None,
) -> dict[str, Any]:
    accepted = accepted_items(items)
    manifest: dict[str, Any] = {
        "manifest_version": 1,
        "dataset_id": dataset_id,
        "media_type": "video",
        "created_at": created_at or utc_now_iso(),
        "manifest_adapter": manifest_adapter,
        "items": accepted,
        "stats": _stats(items, bucket_targets=bucket_targets),
    }
    if source_provider is not None:
        manifest["source_provider"] = source_provider
    validate_schema(manifest, "manifest.schema.json")
    return manifest


def write_canonical_manifest(path: str | Path, manifest: Mapping[str, Any]) -> Path:
    validate_schema(manifest, "manifest.schema.json")
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return out_path


def validate_schema(payload: Mapping[str, Any], schema_name: str) -> None:
    schema = json.loads((SCHEMAS_ROOT / schema_name).read_text(encoding="utf-8"))
    jsonschema.Draft7Validator(schema, registry=_schema_registry()).validate(payload)


def _stats(items: list[Mapping[str, Any]], *, bucket_targets: Mapping[str, int | Mapping[str, Any]] | None) -> dict[str, Any]:
    accepted = [item for item in items if item.get("review_status") == "accepted"]
    rejected = [item for item in items if item.get("review_status") == "rejected"]
    pending = [item for item in items if item.get("review_status", "pending") == "pending"]
    stats: dict[str, Any] = {
        "total_accepted": len(accepted),
        "total_rejected": len(rejected),
        "total_pending": len(pending),
        "total_sources": len({item.get("source_id") for item in items if item.get("source_id")}),
        "buckets": _bucket_stats(accepted, bucket_targets=bucket_targets),
        "total_duration_s": round(sum(float(item.get("duration_s") or 0.0) for item in accepted), 3),
        "estimated_cost_usd": 0.0,
    }
    return stats


def _bucket_stats(items: list[Mapping[str, Any]], *, bucket_targets: Mapping[str, int | Mapping[str, Any]] | None) -> dict[str, Any]:
    buckets: dict[str, dict[str, int]] = {}
    if bucket_targets:
        for bucket, target in bucket_targets.items():
            if isinstance(target, Mapping):
                target_count = int(target.get("target_count", 0))
            else:
                target_count = int(target)
            buckets[str(bucket)] = {"accepted": 0, "target": target_count, "shortfall": target_count}
    for item in items:
        bucket = str(item.get("bucket") or "unbucketed")
        entry = buckets.setdefault(bucket, {"accepted": 0, "target": 0, "shortfall": 0})
        entry["accepted"] += 1
        entry["shortfall"] = max(0, entry.get("target", 0) - entry["accepted"])
    return buckets


def repo_path(path: str | Path) -> str:
    return repo_relative_path(path)


def _schema_registry() -> Registry:
    registry = Registry()
    for path in SCHEMAS_ROOT.glob("*.schema.json"):
        schema = json.loads(path.read_text(encoding="utf-8"))
        resource = Resource.from_contents(schema)
        registry = registry.with_resource(path.name, resource)
        if "$id" in schema:
            registry = registry.with_resource(schema["$id"], resource)
    return registry
