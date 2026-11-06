"""Dataset build pipeline phase implementations.

Extracted from `run.py` to keep the orchestrator module
focused on `main()` and `run_pipeline()` adapter glue.
"""

from __future__ import annotations

from astrid.pack_api import pack_root

PACK_ROOT = pack_root('training')

import copy
import json
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from astrid.pack_api import write_json_atomic
from astrid.pack_api import AstridError

from .acquisition import build_acquisition_request
from .caption_providers import caption_candidate
from .config import normalize_filter_stages
from .filter_stages import canonical_source_id
from ...shared.items import (
    deterministic_id,
    make_review_item,
    repo_relative_path,
    sha256_file,
    utc_now_iso,
)
from ...shared.manifest import accepted_items, build_canonical_manifest, write_canonical_manifest
from ...shared.manifest_adapters import get_manifest_adapter
from .review import apply_review_decisions, write_human_review_final, write_review_data
from .services import DatasetRunServices
from .state import read_review_state, write_review_state

REVIEW_UI_ROOT = Path(__file__).resolve().parent / "review_ui"


# ---------------------------------------------------------------------------
# Shared JSON / file I/O helpers
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> Path:
    write_json_atomic(path, payload)
    return path


def _load_candidates(path: Path) -> list[dict[str, Any]]:
    payload = _read_json(path)
    candidates = payload.get("candidates") if isinstance(payload, Mapping) else payload
    if not isinstance(candidates, list):
        raise AstridError(
            f"{path.name} must contain a candidates list",
            recovery_command="check that the candidates JSON file contains a valid candidates array",
        )
    return [dict(candidate) for candidate in candidates if isinstance(candidate, Mapping)]


def _write_filtered_items(path: Path, active: list[dict[str, Any]], rejected: list[dict[str, Any]], *, phase: str) -> Path:
    return _write_json(path, {"phase": phase, "active": active, "rejected": rejected})


def _load_filtered_items(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    payload = _read_json(path)
    if not isinstance(payload, Mapping):
        raise AstridError(
            f"{path.name} must contain filtered item groups",
            recovery_command="verify the filtered items JSON file has the expected structure with active and rejected lists",
        )
    active = payload.get("active")
    rejected = payload.get("rejected")
    if not isinstance(active, list) or not isinstance(rejected, list):
        raise AstridError(
            f"{path.name} must contain active and rejected lists",
            recovery_command="verify the filtered items JSON file contains both active and rejected arrays",
        )
    return [dict(item) for item in active if isinstance(item, Mapping)], [dict(item) for item in rejected if isinstance(item, Mapping)]


def _load_review_data(path: Path) -> list[dict[str, Any]]:
    payload = _read_json(path)
    items = payload.get("items") if isinstance(payload, Mapping) else payload
    if not isinstance(items, list):
        raise AstridError(
            f"{path.name} must contain an items list",
            recovery_command="verify the review data JSON file contains an items array",
        )
    return [dict(item) for item in items if isinstance(item, Mapping)]


# ---------------------------------------------------------------------------
# Shared item / state helpers
# ---------------------------------------------------------------------------


def _update_state(path: Path, updates: Mapping[str, Any]) -> dict[str, Any]:
    state = read_review_state(path)
    state.update(copy.deepcopy(dict(updates)))
    return write_review_state(path, state)


def _item_id(item: Mapping[str, Any]) -> str:
    if item.get("item_id"):
        return str(item["item_id"])
    return deterministic_id(item.get("source_type", ""), item.get("source_id", ""), item.get("scene_index", ""), prefix="item")


def _default_bucket(config: Mapping[str, Any]) -> str | None:
    buckets = config.get("buckets")
    if isinstance(buckets, Mapping) and len(buckets) == 1:
        return next(iter(buckets))
    return None


def _source_provider_label(config: Mapping[str, Any]) -> str | None:
    providers = [str(source.get("provider")) for source in config.get("sources", []) if isinstance(source, Mapping)]
    return providers[0] if len(set(providers)) == 1 and providers else ",".join(providers) if providers else None


def _normalize_decision(value: Any) -> str:
    if value in {"accepted", "accept", True}:
        return "accept"
    if value in {"rejected", "reject", False}:
        return "reject"
    return "pending"


# ---------------------------------------------------------------------------
# Acquisition phase
# ---------------------------------------------------------------------------


def _record_processed_sources(state_path: Path, candidates: list[Mapping[str, Any]]) -> dict[str, Any]:
    state = read_review_state(state_path)
    seen = [str(source_id) for source_id in state.get("processed_source_ids") or [] if str(source_id)]
    seen_set = set(seen)
    changed = False
    for candidate in candidates:
        source_id = canonical_source_id(candidate)
        if source_id and source_id not in seen_set:
            seen.append(source_id)
            seen_set.add(source_id)
            changed = True
    if not changed:
        return state
    state["processed_source_ids"] = seen
    return write_review_state(state_path, state)


def _record_acquisition_results(state_path: Path, acquisition_results: list[Any]) -> dict[str, Any]:
    state = read_review_state(state_path)
    existing = [dict(result) for result in state.get("acquisition_results") or [] if isinstance(result, Mapping)]
    for result in acquisition_results:
        if isinstance(result, Mapping):
            existing.append(dict(result))
    state["acquisition_results"] = existing
    return write_review_state(state_path, state)


def _acquire_candidates(
    config: Mapping[str, Any],
    run_dir: Path,
    *,
    source_provider_kwargs: Mapping[str, Any] | None,
    acquisition_request: Mapping[str, Any] | None = None,
    state_path_override: Path | None = None,
) -> list[dict[str, Any]]:
    state_path = state_path_override or (run_dir / "review_state.json")
    processed_source_ids = read_review_state(state_path).get("processed_source_ids") if state_path.is_file() else []
    request = build_acquisition_request(processed_source_ids=processed_source_ids, acquisition_request=acquisition_request)
    candidates: list[dict[str, Any]] = []
    acquisition_results: list[dict[str, Any]] = []
    for index, source in enumerate(config.get("sources", []) or []):
        provider_id = str(source["provider"])
        provider_config = dict(source.get("config") or {})
        provider_config.setdefault("dataset_config", config)
        provider_config.setdefault("out_dir", str(run_dir / "sources" / f"{index:02d}_{provider_id}"))
        provider_config.setdefault("acquisition_request", copy.deepcopy(request))
        provider_config.setdefault("processed_source_ids", list(request["processed_source_ids"]))
        provider_config.setdefault("exclude_candidate_ids", list(request["exclude_candidate_ids"]))
        provider_config.setdefault("exclude_source_ids", list(request["exclude_source_ids"]))
        provider_config.setdefault("exclude_media_hashes", list(request["exclude_media_hashes"]))
        provider_config.setdefault("feedback_hints", list(request.get("feedback_hints") or []))
        provider_config.setdefault("limit_hint", request["limit_hint"])
        # Late import through the run facade so monkeypatch.setattr on
        # dataset_run.get_source_provider still intercepts.
        from astrid.packs.training.actions.dataset_build import run as _dataset_run
        _get_sp = _dataset_run.get_source_provider
        provider = _get_sp(provider_id, **dict(source_provider_kwargs or {}))
        for candidate in provider.acquire(provider_config):
            candidates.append(_confine_candidate_media(candidate, run_dir))
        result = getattr(provider, "last_acquisition_result", None)
        if isinstance(result, Mapping):
            acquisition_results.append(dict(result))
    if isinstance(config, dict):
        config["_acquisition_results"] = acquisition_results
    return candidates


def _confine_candidate_media(candidate: Mapping[str, Any], run_dir: Path) -> dict[str, Any]:
    item = dict(candidate)
    source_path = Path(str(item["media_path"])).expanduser()
    if not source_path.is_absolute():

        source_path = (PACK_ROOT / source_path).resolve()
    clips_dir = run_dir / "clips"
    clips_dir.mkdir(parents=True, exist_ok=True)
    stem = _item_id(item)
    target = clips_dir / f"{stem}{source_path.suffix or '.mp4'}"
    if source_path.resolve() != target.resolve():
        shutil.copy2(source_path, target)
    item["media_path"] = repo_relative_path(target)
    item["content_hash"] = sha256_file(target)
    _assert_under(target, run_dir)
    return item


def _phase_acquire_candidates(
    config: Mapping[str, Any],
    run_dir: Path,
    *,
    paths: Mapping[str, Path],
    state_path: Path,
    source_provider_kwargs: Mapping[str, Any] | None,
    acquisition_request: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    candidates = _acquire_candidates(config, run_dir, source_provider_kwargs=source_provider_kwargs, acquisition_request=acquisition_request)
    _write_json(paths["candidates"], {"candidates": candidates})
    _record_processed_sources(state_path, candidates)
    if isinstance(config, Mapping):
        acquisition_results = config.get("_acquisition_results")
        if isinstance(acquisition_results, list) and acquisition_results:
            _record_acquisition_results(state_path, acquisition_results)
    return candidates


def _phase_prepare_review_items(candidates: list[dict[str, Any]], config: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [
        make_review_item(candidate, item_id=_item_id(candidate), bucket=_default_bucket(config))
        for candidate in candidates
    ]


# ---------------------------------------------------------------------------
# Filtering phase
# ---------------------------------------------------------------------------


def _enabled_filter_stages(config: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    stages = normalize_filter_stages(config).get("filters", {}).get("stages", [])
    return [stage for stage in stages if isinstance(stage, Mapping) and stage.get("enabled") is True]


def _apply_filters(items: list[dict[str, Any]], config: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    services = DatasetRunServices.from_config(config)
    return _apply_filter_stages(items, _enabled_filter_stages(config), model_backed=None, services=services)


def _apply_filter_stages(
    items: list[dict[str, Any]],
    stages: list[Mapping[str, Any]],
    *,
    model_backed: bool | None,
    services: DatasetRunServices,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    filter_stats: dict[str, Any] = {}
    rejected: list[dict[str, Any]] = []
    active = items
    for stage in stages:
        if model_backed is not None and bool(stage.get("model_backed")) is not model_backed:
            continue
        stage_id = str(stage.get("stage_id", ""))
        stage_config = stage.get("config") if isinstance(stage.get("config"), Mapping) else {}
        filter_stage = _make_filter_stage(stage_id, services)
        result = filter_stage.apply(active, {}, _stage_runtime_config(stage_config, services))
        active = result.passed
        rejected.extend(result.rejected)
        filter_stats[result.stats["stage_id"]] = result.stats
    return active, rejected, filter_stats


def _make_filter_stage(stage_id: str, services: DatasetRunServices):
    kwargs: dict[str, Any] = {}
    runner = services.filter_stage_runners.get(stage_id)
    if runner is not None:
        kwargs["runner"] = runner
    return services.filter_stage_factory(stage_id, **kwargs)


def _stage_runtime_config(stage_config: Mapping[str, Any], services: DatasetRunServices) -> dict[str, Any]:
    runtime_config = dict(stage_config)
    runtime_config.setdefault("budget_tracker", services.budget_tracker)
    runtime_config.setdefault("clock", services.clock)
    runtime_config.setdefault("sleep", services.sleep)
    runtime_config["child_work_meter"] = services.child_work_meter
    runtime_config["attempt_output_root"] = services.attempt_output_root
    if services.artifact_helpers:
        runtime_config.setdefault("artifact_helpers", dict(services.artifact_helpers))
    return runtime_config


def _write_work_preview(
    path: Path,
    config: Mapping[str, Any],
    active_items: list[dict[str, Any]],
    rejected_items: list[dict[str, Any]],
    filter_stats: Mapping[str, Any],
    stages: list[Mapping[str, Any]],
) -> Path:
    model_backed_stages = [
        {
            "stage_id": str(stage.get("stage_id")),
            "enabled": bool(stage.get("enabled", True)),
            "expensive": bool(stage.get("expensive")),
        }
        for stage in stages
        if bool(stage.get("model_backed"))
    ]
    preview = {
        "schema_version": 1,
        "phase": "post_deterministic_filters",
        "active_item_count": len(active_items),
        "rejected_item_count": len(rejected_items),
        "filter_rejected_counts": {
            str(stage_id): int(stats.get("items_rejected", 0))
            for stage_id, stats in filter_stats.items()
            if isinstance(stats, Mapping)
        },
        "filter_warning_counts": {
            str(stage_id): len(stats.get("warnings") or [])
            for stage_id, stats in filter_stats.items()
            if isinstance(stats, Mapping)
        },
        "planned_caption_calls": len(active_items),
        "enabled_model_backed_stages": model_backed_stages,
        "budget_limits": _config_budget_limits(config),
        "fixture_mode": _config_fixture_mode(config),
        "expensive_spend_disabled": _config_expensive_spend_disabled(config),
    }
    _write_json(path, preview)
    return path


def _phase_deterministic_filters(
    review_items: list[dict[str, Any]],
    config: Mapping[str, Any],
    paths: Mapping[str, Path],
    stages: list[Mapping[str, Any]],
    services: DatasetRunServices,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    services.attempt_output_root = paths["work_preview"].parent.resolve()
    active_items, rejected_items, filter_stats = _apply_filter_stages(review_items, stages, model_backed=False, services=services)
    _write_work_preview(paths["work_preview"], config, active_items, rejected_items, filter_stats, stages)
    return active_items, rejected_items, filter_stats


def _phase_model_backed_filters(
    active_items: list[dict[str, Any]],
    config: Mapping[str, Any],
    stages: list[Mapping[str, Any]],
    services: DatasetRunServices,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    output_config = config.get("output") if isinstance(config.get("output"), Mapping) else {}
    run_dir = output_config.get("run_dir")
    if isinstance(run_dir, (str, Path)) and str(run_dir):
        services.attempt_output_root = Path(run_dir).expanduser().resolve()
    return _apply_filter_stages(active_items, stages, model_backed=True, services=services)


# ---------------------------------------------------------------------------
# Captioning phase
# ---------------------------------------------------------------------------


def _caption_items(items: list[dict[str, Any]], config: Mapping[str, Any], run_dir: Path, services: DatasetRunServices) -> list[dict[str, Any]]:
    caption_config = dict(config.get("caption") or {"provider": "visual_understand"})
    if (caption_config.get("provider") or "visual_understand") in {"visual_understand", "video_understand"}:
        caption_config["child_work_meter"] = services.child_work_meter
        caption_config["attempt_output_root"] = services.attempt_output_root
    caption_config.setdefault("out_dir", str(run_dir / "captions"))
    caption_config.setdefault("budget_tracker", services.budget_tracker)
    caption_config.setdefault("clock", services.clock)
    caption_config.setdefault("sleep", services.sleep)
    if services.artifact_helpers:
        caption_config.setdefault("artifact_helpers", dict(services.artifact_helpers))
    extensions = config.get("extensions") or {}
    if isinstance(extensions, Mapping) and extensions.get("fixture_caption_dir") and "fixture_dir" not in caption_config:
        caption_config["fixture_dir"] = extensions["fixture_caption_dir"]
    if extensions.get("fixture_mode") is True:
        caption_config["fixture_mode"] = True
    captioned: list[dict[str, Any]] = []
    for item in items:
        if services.caption_runner is None:
            result, sidecar = caption_candidate(item, caption_config)
        else:
            result, sidecar = caption_candidate(item, caption_config, runner=services.caption_runner)
        updated = dict(item)
        updated["caption"] = {
            "text": result.text,
            "schema_version": result.schema_version,
            "confidence": result.confidence,
            "model": result.model,
        }
        if result.raw_response is not None:
            updated["caption"]["raw_response"] = result.raw_response
        updated["caption_file"] = repo_relative_path(sidecar)
        captioned.append(updated)
    return captioned


def _phase_caption_items(items: list[dict[str, Any]], config: Mapping[str, Any], run_dir: Path, services: DatasetRunServices) -> list[dict[str, Any]]:
    return _caption_items(items, config, run_dir, services)


# ---------------------------------------------------------------------------
# Review phase
# ---------------------------------------------------------------------------


def _apply_review_sampling(items: list[dict[str, Any]], config: Mapping[str, Any]) -> list[dict[str, Any]]:
    review_config = config.get("review") if isinstance(config.get("review"), Mapping) else {}
    sampling_config = review_config.get("sampling") if isinstance(review_config, Mapping) else None
    if not isinstance(sampling_config, Mapping):
        return [copy.deepcopy(item) for item in items]

    mode = str(sampling_config.get("mode") or "all")
    sample_count = _sampling_count(sampling_config, default=len(items))
    ordered_item_ids = [str(item.get("item_id") or _item_id(item)) for item in items]
    if mode == "top_n":
        sampled_item_ids = set(ordered_item_ids[:sample_count])
    elif mode == "random_sample":
        ranked = sorted(
            ordered_item_ids,
            key=lambda item_id: deterministic_id("review_sampling", item_id, prefix="sample"),
        )
        sampled_item_ids = set(ranked[:sample_count])
    else:
        mode = "all"
        sampled_item_ids = set(ordered_item_ids)
        sample_count = len(ordered_item_ids)

    sampled_rank = {
        item_id: rank
        for rank, item_id in enumerate(
            [item_id for item_id in ordered_item_ids if item_id in sampled_item_ids],
            start=1,
        )
    }
    marked: list[dict[str, Any]] = []
    for item in items:
        updated = copy.deepcopy(item)
        item_id = str(updated.get("item_id") or _item_id(updated))
        marker: dict[str, Any] = {
            "sampled": item_id in sampled_item_ids,
            "mode": mode,
            "sample_count": sample_count,
        }
        if item_id in sampled_rank:
            marker["rank"] = sampled_rank[item_id]
        updated["review_sampled"] = marker
        marked.append(updated)
    return marked


def _sampling_count(sampling_config: Mapping[str, Any], *, default: int) -> int:
    raw = sampling_config.get("sample_count", default)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return default


def _phase_prepare_review_data(paths: Mapping[str, Path], all_items: list[dict[str, Any]], config: Mapping[str, Any]) -> Path:
    return write_review_data(paths["review_data"], _apply_review_sampling(all_items, config))


def _run_human_review(run_dir: Path, review_data_path: Path, state_path: Path, runner, *,
                      media_root: Path | None = None, services: DatasetRunServices | None = None,
                      round_index: int = 0) -> None:
    if runner is subprocess.run:
        from .review_assets import run_review
        from .state import SCHEMAS_ROOT

        if services is None or services.attempt_output_root is None:
            raise ValueError("Human Review requires the trusted parent attempt output root")
        run_review(root=services.attempt_output_root, run_dir=(media_root or run_dir / "clips").parent,
                   output_dir=run_dir, data_path=review_data_path, state_path=state_path,
                   ui_root=REVIEW_UI_ROOT, schema_root=SCHEMAS_ROOT,
                   meter=services.child_work_meter, round_index=round_index)
        return
    # Preserve the existing explicitly injected offline runner adapter. This
    # path submits no public child and cannot qualify for review lifetime credit.
    out_path = run_dir / "review_server" / "human_review.final.json"
    media_dir = media_root or run_dir / "clips"
    cmd = [
        sys.executable,
        "-X",
        "utf8",
        "-m",
        "astrid.packs.editorial.actions.human_review.run",
        "--html",
        str(REVIEW_UI_ROOT),
        "--data",
        str(review_data_path),
        "--state",
        str(state_path),
        "--out",
        str(out_path),
        "--serve",
        f"/media={media_dir}",
        "--no-open",
    ]
    runner(cmd, check=True)


def _load_review_decisions(path: str | Path, *, round_index: int = 0) -> dict[str, dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    rounds = payload.get("rounds") if isinstance(payload, Mapping) else None
    if isinstance(rounds, Mapping) and str(round_index) in rounds:
        raw = rounds[str(round_index)]
    elif round_index > 0:
        if not isinstance(rounds, Mapping) or str(round_index) not in rounds:
            raise AstridError(
                f"non-interactive top-up round {round_index} requires review decisions JSON with rounds.{round_index}",
                recovery_command=f"add a rounds.{round_index} key to the review decisions JSON file",
            )
    else:
        raw = payload.get("review_decisions", payload.get("decisions", payload.get("revisions", payload))) if isinstance(payload, Mapping) else payload
    decisions: dict[str, dict[str, Any]] = {}
    if isinstance(raw, Mapping):
        iterable = raw.items()
    elif isinstance(raw, list):
        iterable = ((entry.get("item_id"), entry) for entry in raw if isinstance(entry, Mapping))
    else:
        raise AstridError(
            "review decisions JSON must be an object or list",
            recovery_command="ensure the review decisions JSON file contains either a JSON object or a JSON array",
        )
    for item_id, decision in iterable:
        if item_id is None or not isinstance(decision, Mapping):
            continue
        normalized = _normalize_decision(decision.get("decision", decision.get("review_status", "pending")))
        decisions[str(item_id)] = {
            "item_id": str(item_id),
            "decision": normalized,
            "reject_reason": decision.get("reject_reason"),
            "edited_caption": decision.get("edited_caption"),
            "reviewed_at": decision.get("reviewed_at") or utc_now_iso(),
            "state_version": int(decision.get("state_version", 0)),
        }
        if decision.get("reviewer_id"):
            decisions[str(item_id)]["reviewer_id"] = str(decision["reviewer_id"])
    return decisions


def _require_review_decisions(decisions: Mapping[str, Any], required_item_ids: set[str], *, round_index: int) -> None:
    missing = sorted(item_id for item_id in required_item_ids if item_id not in decisions)
    if missing:
        raise AstridError(
            f"round {round_index} review decisions missing required item_ids: {', '.join(missing)}",
            recovery_command=f"add review decisions for the missing item_ids to the round {round_index} decisions",
        )


def _accept_all_decisions(items: list[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    return {
        str(item["item_id"]): {
            "item_id": str(item["item_id"]),
            "decision": "accept",
            "reject_reason": None,
            "edited_caption": None,
            "reviewed_at": utc_now_iso(),
            "state_version": 0,
        }
        for item in items
    }


def _merge_review_decisions(path: Path, decisions: Mapping[str, Mapping[str, Any]], *, submitted: bool) -> dict[str, Any]:
    state = read_review_state(path)
    merged = dict(state.get("review_decisions") or {})
    for item_id, decision in decisions.items():
        merged[str(item_id)] = copy.deepcopy(dict(decision))
    state["review_decisions"] = merged
    state["submitted"] = submitted
    return write_review_state(path, state)


def _phase_human_review(
    run_dir: Path,
    state_path: Path,
    review_data_path: Path,
    captioned: list[dict[str, Any]],
    config: Mapping[str, Any],
    *,
    review_decisions_path: str | Path | None,
    services: DatasetRunServices,
    skip_review: bool,
    round_index: int = 0,
    review_output_dir: Path | None = None,
    required_decision_item_ids: set[str] | None = None,
) -> None:
    output_dir = review_output_dir or run_dir
    if review_decisions_path is not None:
        decisions = _load_review_decisions(review_decisions_path, round_index=round_index)
        _require_review_decisions(decisions, required_decision_item_ids or set(), round_index=round_index)
        _merge_review_decisions(state_path, decisions, submitted=True)
        write_human_review_final(output_dir, {"review_decisions": decisions, "submitted": True, "round_index": round_index})
    elif skip_review:
        return
    elif (config.get("review") or {}).get("enabled", True):
        _run_human_review(output_dir, review_data_path, state_path, services.human_review_runner,
                          media_root=run_dir / "clips", services=services, round_index=round_index)
    else:
        decisions = _accept_all_decisions(captioned)
        _merge_review_decisions(state_path, decisions, submitted=True)
        write_human_review_final(output_dir, {"review_decisions": decisions, "submitted": True, "round_index": round_index})


# ---------------------------------------------------------------------------
# Finalization phase
# ---------------------------------------------------------------------------


def _phase_finalize_manifests(
    run_dir: Path,
    state_path: Path,
    all_items: list[dict[str, Any]],
    config: Mapping[str, Any],
    *,
    paths: Mapping[str, Path],
    dataset_id: str,
    items_are_reviewed: bool = False,
) -> tuple[Path, Path, dict[str, Any]]:
    if items_are_reviewed:
        final_items = [copy.deepcopy(item) for item in all_items]
    else:
        reviewed_state = read_review_state(state_path)
        final_items = apply_review_decisions(all_items, reviewed_state)
    manifest_config = config.get("manifest") or {}
    adapter_id = str(manifest_config.get("adapter") or "ai-toolkit-ltx")
    canonical = build_canonical_manifest(
        final_items,
        dataset_id=dataset_id,
        source_provider=_source_provider_label(config),
        manifest_adapter=adapter_id,
        bucket_targets=config.get("buckets"),
    )
    canonical_path = write_canonical_manifest(paths["canonical_manifest"], canonical)
    adapter = get_manifest_adapter(
        adapter_id,
        out_path=paths["adapter_manifest"],
        source_manifest=canonical_path,
    )
    adapter_path = adapter.export(accepted_items(canonical["items"]))
    return canonical_path, adapter_path, canonical


def _finalized_summary(run_dir: Path, paths: Mapping[str, Path], state_path: Path) -> dict[str, Any]:
    final_state = read_review_state(state_path)
    canonical = _read_json(paths["canonical_manifest"]) if paths["canonical_manifest"].is_file() else {"items": []}
    return {
        "run_dir": str(run_dir),
        "work_preview": str(paths["work_preview"]),
        "review_data": str(paths["review_data"]),
        "review_state": str(state_path),
        "canonical_manifest": str(paths["canonical_manifest"]),
        "adapter_manifest": str(paths["adapter_manifest"]),
        "quality_report": str(paths["quality_report"]),
        "accepted": len(canonical.get("items") or []) if isinstance(canonical, Mapping) else 0,
        "state_status": final_state["status"],
        "state_version": final_state["state_version"],
    }


def _assert_under(path: Path, root: Path) -> None:
    path.resolve().relative_to(root.resolve())


# ---------------------------------------------------------------------------
# Config helpers used by _write_work_preview
# ---------------------------------------------------------------------------


def _config_budget_limits(config: Mapping[str, Any]) -> dict[str, Any]:
    budgets = config.get("budgets") if isinstance(config.get("budgets"), Mapping) else {}
    return {
        "max_api_calls": budgets.get("max_api_calls"),
        "max_estimated_cost_usd": budgets.get("max_estimated_cost_usd"),
        "providers": copy.deepcopy(dict(budgets.get("providers") or {})),
    }


def _config_expensive_spend_disabled(config: Mapping[str, Any]) -> bool:
    limits = _config_budget_limits(config)
    if limits.get("max_api_calls") == 0 or limits.get("max_estimated_cost_usd") == 0:
        return True
    for provider in limits.get("providers", {}).values():
        if isinstance(provider, Mapping) and provider.get("max_calls") == 0:
            return True
    return False


def _config_fixture_mode(config: Mapping[str, Any]) -> bool:
    extensions = config.get("extensions") or {}
    return isinstance(extensions, Mapping) and extensions.get("fixture_mode") is True
