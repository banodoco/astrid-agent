"""Quality report generation for dataset-build runs."""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from astrid.pack_api import write_json_atomic

from ....shared.items import utc_now_iso


def write_quality_report(
    path: str | Path,
    *,
    items: list[Mapping[str, Any]],
    config: Mapping[str, Any],
    state: Mapping[str, Any],
    budget: Mapping[str, Any] | None = None,
    final_shortfalls: Mapping[str, int] | None = None,
    canonical_manifest: Mapping[str, Any] | None = None,
) -> Path:
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_json_atomic(
        out_path,
        build_quality_report(
            items=items,
            config=config,
            state=state,
            budget=budget,
            final_shortfalls=final_shortfalls,
            canonical_manifest=canonical_manifest,
        ),
    )
    return out_path


def build_quality_report(
    *,
    items: list[Mapping[str, Any]],
    config: Mapping[str, Any],
    state: Mapping[str, Any],
    budget: Mapping[str, Any] | None = None,
    final_shortfalls: Mapping[str, int] | None = None,
    canonical_manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    item_list = [dict(item) for item in items]
    return {
        "schema_version": 1,
        "generated_at": utc_now_iso(),
        "run_status": state.get("status"),
        "dataset_id": config.get("dataset_id") or state.get("run_id"),
        "summary": _summary(item_list, canonical_manifest),
        "source_concentration": _source_concentration(item_list),
        "rights_provenance_warnings": _rights_provenance_warnings(item_list),
        "budget_observed_counts": dict(budget or {}),
        "bucket_counts": _bucket_counts(item_list, config),
        "filter_rejection_breakdowns": _filter_rejection_breakdowns(item_list, state),
        "semantic_scores": _semantic_scores(item_list),
        "caption_validation_failures": list(state.get("caption_validation_failures") or []),
        "top_up_acquisition_results": list(state.get("acquisition_results") or []),
        "final_shortfalls": dict(final_shortfalls or {}),
    }


def _summary(items: list[Mapping[str, Any]], canonical_manifest: Mapping[str, Any] | None) -> dict[str, Any]:
    counts = Counter(str(item.get("review_status") or "pending") for item in items)
    canonical_items = canonical_manifest.get("items") if isinstance(canonical_manifest, Mapping) else None
    return {
        "total_items": len(items),
        "accepted": int(counts.get("accepted", 0)),
        "rejected": int(counts.get("rejected", 0)),
        "pending": int(counts.get("pending", 0)),
        "canonical_items": len(canonical_items) if isinstance(canonical_items, list) else 0,
    }


def _source_concentration(items: list[Mapping[str, Any]]) -> dict[str, Any]:
    counts = Counter(str(item.get("source_id") or "unknown") for item in items)
    total = sum(counts.values())
    sources = [{"source_id": source_id, "count": count} for source_id, count in sorted(counts.items())]
    max_count = max(counts.values(), default=0)
    max_share = (max_count / total) if total else 0.0
    warnings = []
    if total >= 2 and max_share >= 0.8:
        warnings.append({"code": "source_concentration_high", "message": "one source contributes at least 80% of items"})
    return {"total_items": total, "sources": sources, "max_source_share": max_share, "warnings": warnings}


def _rights_provenance_warnings(items: list[Mapping[str, Any]]) -> list[dict[str, str]]:
    warnings: list[dict[str, str]] = []
    for item in items:
        item_id = str(item.get("item_id") or "")
        rights = item.get("rights") if isinstance(item.get("rights"), Mapping) else {}
        rights_status = rights.get("rights_status") if isinstance(rights, Mapping) else None
        if rights_status and rights_status != "verified":
            warnings.append({"item_id": item_id, "code": "rights_not_verified", "message": f"rights_status={rights_status}"})
        if not item.get("source_url"):
            warnings.append({"item_id": item_id, "code": "source_url_missing", "message": "source_url is missing"})
        if not item.get("content_hash"):
            warnings.append({"item_id": item_id, "code": "content_hash_missing", "message": "content_hash is missing"})
    return warnings


def _bucket_counts(items: list[Mapping[str, Any]], config: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    buckets = config.get("buckets") if isinstance(config.get("buckets"), Mapping) else {}
    result: dict[str, dict[str, Any]] = {
        str(bucket): {"target_count": _target_count(target), "accepted": 0, "rejected": 0, "pending": 0}
        for bucket, target in buckets.items()
    }
    for item in items:
        bucket = str(item.get("bucket") or "unbucketed")
        entry = result.setdefault(bucket, {"target_count": 0, "accepted": 0, "rejected": 0, "pending": 0})
        status = str(item.get("review_status") or "pending")
        if status in {"accepted", "rejected"}:
            entry[status] += 1
        else:
            entry["pending"] += 1
    return result


def _target_count(target: Any) -> int:
    try:
        return max(0, int(target.get("target_count", 0) if isinstance(target, Mapping) else target))
    except (TypeError, ValueError):
        return 0


def _filter_rejection_breakdowns(items: list[Mapping[str, Any]], state: Mapping[str, Any]) -> dict[str, Any]:
    by_stage: dict[str, Counter[str]] = defaultdict(Counter)
    for item in items:
        filter_results = item.get("filter_results") if isinstance(item.get("filter_results"), Mapping) else {}
        for stage_id, result in filter_results.items():
            if not isinstance(result, Mapping) or result.get("passed") is not False:
                continue
            reason = str(result.get("reason") or "rejected")
            by_stage[str(stage_id)][reason] += 1
    return {
        "by_stage_reason": {stage_id: dict(counter) for stage_id, counter in sorted(by_stage.items())},
        "stage_stats": dict(state.get("filter_stats") or {}),
    }


def _semantic_scores(items: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    scores: list[dict[str, Any]] = []
    for item in items:
        filter_results = item.get("filter_results") if isinstance(item.get("filter_results"), Mapping) else {}
        for stage_id, result in filter_results.items():
            if not isinstance(result, Mapping) or not str(stage_id).startswith("semantic_"):
                continue
            scores.append(
                {
                    "item_id": str(item.get("item_id") or ""),
                    "stage_id": str(stage_id),
                    "score": result.get("score"),
                    "passed": result.get("passed"),
                    "reason": result.get("reason"),
                }
            )
    return scores
