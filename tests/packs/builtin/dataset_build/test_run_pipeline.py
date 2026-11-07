from __future__ import annotations

import copy
import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
import yaml

from astrid.core.contracts.errors import AstridError
from astrid.packs.training.actions.dataset_build import phases as dataset_phases
from astrid.packs.training.actions.dataset_build import run as dataset_run
from astrid.packs.training.actions.dataset_build.source_providers.local_folder import (
    LocalFolderSourceProvider,
)
from astrid.packs.training.actions.dataset_build.state import (
    make_initial_state,
    read_review_state,
    set_status,
    write_review_state,
)
from astrid.packs.training.shared.items import config_hash, make_candidate_item
from tests.fixtures.dataset_build_child_invoker import FakeChildInvoker, install_child_invoker


def _config(tmp_path: Path, media_dir: Path) -> Path:
    config = {
        "schema_version": 1,
        "media_type": "video",
        "dataset_id": "fixture-run",
        "sources": [{"provider": "local_folder", "config": {"path": str(media_dir)}}],
        "buckets": {"wide": {"target_count": 1}},
        "clip_config": {"min_duration_s": 1.0, "max_duration_s": 10.0, "max_scenes_per_source": 1},
        "caption": {"provider": "visual_understand", "prompt_template": "Caption fixture."},
        "filters": {"duration": {"enabled": True, "min_s": 1.0, "max_s": 10.0}},
        "review": {"enabled": True},
        "manifest": {"adapter": "ai-toolkit-ltx"},
        "budgets": {"max_api_calls": 1, "max_estimated_cost_usd": 1.0, "providers": {}},
        "output": {"run_dir": str(tmp_path / "ignored")},
        "extensions": {"fixture_mode": True},
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return path


def _patch_local_provider(monkeypatch, media_file: Path) -> None:
    def acquire(self: LocalFolderSourceProvider, config: dict[str, Any]):
        yield {
            **make_candidate_item(
                source_type="local_folder",
                source_id="source-1",
                source_url=media_file.as_uri(),
                media_path=media_file,
                media_type="video",
                source_metadata={"resolution": {"width": 64, "height": 64}},
                duration_s=5.0,
                clip_start_s=0.0,
                clip_end_s=5.0,
                scene_index=0,
            ),
            "item_id": "clip-a",
        }

    monkeypatch.setattr(LocalFolderSourceProvider, "acquire", acquire)


def _patch_local_provider_many(monkeypatch, media_file: Path, count: int) -> None:
    def acquire(self: LocalFolderSourceProvider, config: dict[str, Any]):
        for index in range(count):
            yield {
                **make_candidate_item(
                    source_type="local_folder",
                    source_id=f"source-{index + 1}",
                    source_url=media_file.as_uri(),
                    media_path=media_file,
                    media_type="video",
                    source_metadata={"resolution": {"width": 64, "height": 64}},
                    duration_s=5.0,
                    clip_start_s=0.0,
                    clip_end_s=5.0,
                    scene_index=index,
                ),
                "item_id": f"clip-{index}",
            }

    monkeypatch.setattr(LocalFolderSourceProvider, "acquire", acquire)


def _patch_round_provider(monkeypatch, media_file: Path) -> list[dict[str, Any]]:
    seen_requests: list[dict[str, Any]] = []

    def acquire(self: LocalFolderSourceProvider, config: dict[str, Any]):
        request = dict(config.get("acquisition_request") or {})
        seen_requests.append(request)
        round_index = int(request.get("round_index", 0))
        item_id = f"clip-{round_index}"
        source_id = f"source-{round_index}"
        excluded_candidates = set(request.get("exclude_candidate_ids") or [])
        excluded_sources = set(request.get("exclude_source_ids") or [])
        if item_id in excluded_candidates or source_id in excluded_sources:
            return
        yield {
            **make_candidate_item(
                source_type="local_folder",
                source_id=source_id,
                source_url=media_file.as_uri(),
                media_path=media_file,
                media_type="video",
                source_metadata={"resolution": {"width": 64, "height": 64}},
                duration_s=5.0,
                clip_start_s=0.0,
                clip_end_s=5.0,
                scene_index=round_index,
            ),
            "item_id": item_id,
        }

    monkeypatch.setattr(LocalFolderSourceProvider, "acquire", acquire)
    return seen_requests


def _patch_local_provider_per_file(monkeypatch, media_files: list[Path]) -> None:
    def acquire(self: LocalFolderSourceProvider, config: dict[str, Any]):
        for index, media_file in enumerate(media_files):
            yield {
                **make_candidate_item(
                    source_type="local_folder",
                    source_id=f"source-{index + 1}",
                    source_url=media_file.as_uri(),
                    media_path=media_file,
                    media_type="video",
                    source_metadata={"resolution": {"width": 64, "height": 64}},
                    duration_s=5.0,
                    clip_start_s=0.0,
                    clip_end_s=5.0,
                    scene_index=index,
                ),
                "item_id": f"clip-{index}",
            }

    monkeypatch.setattr(LocalFolderSourceProvider, "acquire", acquire)


def test_dataset_build_cli_runs_noninteractive_pipeline_into_requested_out_dir(tmp_path: Path, monkeypatch) -> None:
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    media_file = media_dir / "source.mp4"
    media_file.write_bytes(b"not-a-real-video-but-fixture-provider-is-patched")
    _patch_local_provider(monkeypatch, media_file)
    config_path = _config(tmp_path, media_dir)
    decisions_path = tmp_path / "decisions.json"
    decisions_path.write_text(
        json.dumps(
            [
                {
                    "item_id": "clip-a",
                    "decision": "accept",
                    "edited_caption": "Edited from non-interactive review.",
                    "reviewed_at": "2026-05-21T00:00:00Z",
                }
            ]
        ),
        encoding="utf-8",
    )
    out_dir = tmp_path / "run"
    original_set_status = dataset_run.set_status
    statuses: list[str] = []

    def recording_set_status(path, status, **kwargs):
        statuses.append(status)
        return original_set_status(path, status, **kwargs)

    monkeypatch.setattr(dataset_run, "set_status", recording_set_status)

    exit_code = dataset_run.main(["--config", str(config_path), "--out", str(out_dir), "--review-decisions", str(decisions_path)])

    assert exit_code == 0
    assert statuses == ["acquiring", "filtering", "preview_ready", "captioning", "reviewing", "finalized"]
    assert (out_dir / "review_data.json").is_file()
    assert (out_dir / "review_state.json").is_file()
    assert (out_dir / "candidates.json").is_file()
    assert (out_dir / "work_preview.json").is_file()
    assert (out_dir / "filtered_items.json").is_file()
    assert (out_dir / "review_server" / "human_review.final.json").is_file()
    assert (out_dir / "final.manifest.json").is_file()
    assert (out_dir / "ai-toolkit-ltx.manifest.json").is_file()
    assert not (media_dir / "clip-a.caption.json").exists()
    round_dir = out_dir / "rounds" / "0"
    for relative_path in [
        "review_state.json",
        "candidates.json",
        "work_preview.json",
        "filtered_items.json",
        "review_data.json",
        "review_server/human_review.final.json",
    ]:
        assert (round_dir / relative_path).is_file()
        assert json.loads((round_dir / relative_path).read_text(encoding="utf-8")) == json.loads(
            (out_dir / relative_path).read_text(encoding="utf-8")
        )

    state = json.loads((out_dir / "review_state.json").read_text(encoding="utf-8"))
    assert state["status"] == "finalized"
    assert state["submitted"] is True
    assert state["quality_report"] == str(out_dir / "quality_report.json")
    canonical = json.loads((out_dir / "final.manifest.json").read_text(encoding="utf-8"))
    assert [item["item_id"] for item in canonical["items"]] == ["clip-a"]
    assert canonical["items"][0]["caption"]["text"] == "Edited from non-interactive review."
    adapter = json.loads((out_dir / "ai-toolkit-ltx.manifest.json").read_text(encoding="utf-8"))
    assert adapter["clips"][0]["clip_file"].endswith("/run/clips/clip-a.mp4")
    assert adapter["clips"][0]["caption_file"].endswith("/run/clips/clip-a.caption.json")
    assert (out_dir / "clips" / "clip-a.mp4").is_file()
    assert json.loads((out_dir / "clips" / "clip-a.caption.json").read_text(encoding="utf-8"))["text"] == "Edited from non-interactive review."
    preview = json.loads((out_dir / "work_preview.json").read_text(encoding="utf-8"))
    assert preview["phase"] == "post_deterministic_filters"
    assert preview["active_item_count"] == 1
    assert preview["rejected_item_count"] == 0
    assert preview["planned_caption_calls"] == 1
    assert preview["enabled_model_backed_stages"] == []
    report = json.loads((out_dir / "quality_report.json").read_text(encoding="utf-8"))
    assert report["run_status"] == "finalized"
    assert report["summary"]["accepted"] == 1
    assert report["bucket_counts"]["wide"]["accepted"] == 1


def test_dataset_build_pipeline_marks_state_failed_on_runtime_error(tmp_path: Path, monkeypatch) -> None:
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    media_file = media_dir / "source.mp4"
    media_file.write_bytes(b"fixture")
    _patch_local_provider(monkeypatch, media_file)
    parsed = dataset_run.load_dataset_config(_config(tmp_path, media_dir))

    try:
        dataset_run.run_pipeline(parsed, tmp_path / "run", review_decisions_path=tmp_path / "missing-decisions.json")
    except FileNotFoundError:
        pass
    else:
        raise AssertionError("missing review decisions should fail the run")

    state = json.loads((tmp_path / "run" / "review_state.json").read_text(encoding="utf-8"))
    assert state["status"] == "failed"
    assert state["error"]["type"] == "FileNotFoundError"


def test_run_pipeline_blocks_manifest_export_when_accepted_caption_validation_fails(tmp_path: Path, monkeypatch) -> None:
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    media_file = media_dir / "source.mp4"
    media_file.write_bytes(b"fixture")
    _patch_local_provider(monkeypatch, media_file)
    config_path = _config(tmp_path, media_dir)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["caption"]["validation"] = {"text_pattern": "^APPROVED:"}
    config_path.write_text(json.dumps(config), encoding="utf-8")
    parsed = dataset_run.load_dataset_config(config_path)
    decisions_path = tmp_path / "decisions.json"
    decisions_path.write_text(
        json.dumps(
            [
                {
                    "item_id": "clip-a",
                    "decision": "accept",
                    "reviewed_at": "2026-05-21T00:00:00Z",
                }
            ]
        ),
        encoding="utf-8",
    )

    summary = dataset_run.run_pipeline(parsed, tmp_path / "run", review_decisions_path=decisions_path)

    assert summary["state_status"] == "failed"
    assert summary["canonical_manifest"] is None
    assert summary["adapter_manifest"] is None
    assert not (tmp_path / "run" / "final.manifest.json").exists()
    assert summary["caption_validation_failures"][0]["code"] == "caption_text_pattern_mismatch"
    state = read_review_state(tmp_path / "run" / "review_state.json")
    assert state["error"]["stage"] == "caption_validation"
    assert state["caption_validation_failures"][0]["item_id"] == "clip-a"
    review_data = json.loads((tmp_path / "run" / "review_data.json").read_text(encoding="utf-8"))
    validation = review_data["items"][0]["caption_validation"]
    assert validation["valid"] is False
    assert validation["failures"][0]["code"] == "caption_text_pattern_mismatch"
    report = json.loads((tmp_path / "run" / "quality_report.json").read_text(encoding="utf-8"))
    assert report["run_status"] == "failed"
    assert report["caption_validation_failures"][0]["code"] == "caption_text_pattern_mismatch"


def test_dataset_build_top_up_round_fills_bucket_shortfall_with_novel_candidates(tmp_path: Path, monkeypatch) -> None:
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    media_file = media_dir / "source.mp4"
    media_file.write_bytes(b"fixture")
    seen_requests = _patch_round_provider(monkeypatch, media_file)
    config_path = _config(tmp_path, media_dir)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["buckets"] = {"wide": {"target_count": 2}}
    config["review"]["top_up"] = {"max_rounds": 2}
    config_path.write_text(json.dumps(config), encoding="utf-8")
    parsed = dataset_run.load_dataset_config(config_path)
    decisions_path = tmp_path / "decisions.json"
    decisions_path.write_text(
        json.dumps(
            {
                "rounds": {
                    "0": [{"item_id": "clip-0", "decision": "accept", "reviewed_at": "2026-05-21T00:00:00Z"}],
                    "1": [{"item_id": "clip-1", "decision": "accept", "reviewed_at": "2026-05-21T00:00:01Z"}],
                }
            }
        ),
        encoding="utf-8",
    )

    summary = dataset_run.run_pipeline(parsed, tmp_path / "run", review_decisions_path=decisions_path)

    assert summary["state_status"] == "finalized"
    assert summary["accepted"] == 2
    assert [request["round_index"] for request in seen_requests] == [0, 1]
    assert seen_requests[1]["target_shortfalls"] == {"wide": 1}
    assert "clip-0" in seen_requests[1]["exclude_candidate_ids"]
    assert "source-0" in seen_requests[1]["exclude_source_ids"]
    out_dir = tmp_path / "run"
    assert [candidate["item_id"] for candidate in json.loads((out_dir / "candidates.json").read_text(encoding="utf-8"))["candidates"]] == ["clip-0"]
    assert [candidate["item_id"] for candidate in json.loads((out_dir / "rounds" / "1" / "candidates.json").read_text(encoding="utf-8"))["candidates"]] == ["clip-1"]
    assert json.loads((out_dir / "rounds" / "1" / "review_data.json").read_text(encoding="utf-8"))["items"][0]["item_id"] == "clip-1"
    state = read_review_state(out_dir / "review_state.json")
    assert state["top_up_rounds"] == 1
    assert state["buckets"]["wide"]["accepted"] == 2


def test_dataset_build_top_up_requires_round_aware_noninteractive_decisions(tmp_path: Path, monkeypatch) -> None:
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    media_file = media_dir / "source.mp4"
    media_file.write_bytes(b"fixture")
    _patch_round_provider(monkeypatch, media_file)
    config_path = _config(tmp_path, media_dir)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["buckets"] = {"wide": {"target_count": 2}}
    config["review"]["top_up"] = {"max_rounds": 1}
    config_path.write_text(json.dumps(config), encoding="utf-8")
    parsed = dataset_run.load_dataset_config(config_path)
    decisions_path = tmp_path / "decisions.json"
    decisions_path.write_text(
        json.dumps([{"item_id": "clip-0", "decision": "accept", "reviewed_at": "2026-05-21T00:00:00Z"}]),
        encoding="utf-8",
    )

    with pytest.raises(AstridError, match=r"round 1"):
        dataset_run.run_pipeline(parsed, tmp_path / "run", review_decisions_path=decisions_path)

    out_dir = tmp_path / "run"
    assert read_review_state(out_dir / "review_state.json")["status"] == "failed"
    assert not (out_dir / "final.manifest.json").exists()
    assert not (out_dir / "ai-toolkit-ltx.manifest.json").exists()


def test_dataset_build_unmet_shortfalls_after_max_top_up_rounds_do_not_export_manifests(tmp_path: Path, monkeypatch) -> None:
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    media_file = media_dir / "source.mp4"
    media_file.write_bytes(b"fixture")

    def acquire(self: LocalFolderSourceProvider, config: dict[str, Any]):
        request = dict(config.get("acquisition_request") or {})
        if int(request.get("round_index", 0)) > 0:
            self.last_acquisition_result = {
                "provider": "local_folder",
                "round_index": int(request.get("round_index", 0)),
                "limit_hint": request.get("limit_hint"),
                "considered": 0,
                "yielded": 0,
                "skipped_processed": 0,
                "skipped_excluded": 0,
                "skipped_duplicate_media": 0,
                "no_new_candidates": True,
                "reason": "no_new_candidates",
            }
            return
        self.last_acquisition_result = {
            "provider": "local_folder",
            "round_index": 0,
            "limit_hint": request.get("limit_hint"),
            "considered": 1,
            "yielded": 1,
            "skipped_processed": 0,
            "skipped_excluded": 0,
            "skipped_duplicate_media": 0,
            "no_new_candidates": False,
            "reason": "candidates_yielded",
        }
        yield {
            **make_candidate_item(
                source_type="local_folder",
                source_id="source-0",
                source_url=media_file.as_uri(),
                media_path=media_file,
                media_type="video",
                source_metadata={"resolution": {"width": 64, "height": 64}},
                duration_s=5.0,
                clip_start_s=0.0,
                clip_end_s=5.0,
                scene_index=0,
            ),
            "item_id": "clip-0",
        }

    monkeypatch.setattr(LocalFolderSourceProvider, "acquire", acquire)
    config_path = _config(tmp_path, media_dir)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["buckets"] = {"wide": {"target_count": 2}}
    config["review"]["top_up"] = {"max_rounds": 1}
    config_path.write_text(json.dumps(config), encoding="utf-8")
    parsed = dataset_run.load_dataset_config(config_path)
    decisions_path = tmp_path / "decisions.json"
    decisions_path.write_text(
        json.dumps(
            {
                "rounds": {
                    "0": [{"item_id": "clip-0", "decision": "accept", "reviewed_at": "2026-05-21T00:00:00Z"}],
                    "1": [],
                }
            }
        ),
        encoding="utf-8",
    )

    summary = dataset_run.run_pipeline(parsed, tmp_path / "run", review_decisions_path=decisions_path)

    out_dir = tmp_path / "run"
    assert summary["state_status"] == "failed"
    assert summary["bucket_shortfalls"] == {"wide": 1}
    assert summary["canonical_manifest"] is None
    assert summary["adapter_manifest"] is None
    assert not (out_dir / "final.manifest.json").exists()
    assert not (out_dir / "ai-toolkit-ltx.manifest.json").exists()
    report = json.loads((out_dir / "quality_report.json").read_text(encoding="utf-8"))
    assert report["run_status"] == "failed"
    assert report["final_shortfalls"] == {"wide": 1}
    assert [result["round_index"] for result in report["top_up_acquisition_results"]] == [0, 1]


def test_dataset_build_resume_uses_existing_review_state_authoritatively(tmp_path: Path, monkeypatch) -> None:
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    media_file = media_dir / "source.mp4"
    media_file.write_bytes(b"fixture")
    _patch_local_provider(monkeypatch, media_file)
    config_path = _config(tmp_path, media_dir)
    parsed = dataset_run.load_dataset_config(config_path)
    out_dir = tmp_path / "run"
    out_dir.mkdir()
    config = copy.deepcopy(parsed.data)
    config.setdefault("output", {})["run_dir"] = str(out_dir.resolve())
    existing = make_initial_state(
        run_id="fixture-run",
        writer_id="training.dataset_build",
        config_hash=config_hash(config),
        buckets=config.get("buckets"),
        status="preview_ready",
    )
    existing["processed_source_ids"] = ["source-before-resume"]
    written = write_review_state(out_dir / "review_state.json", existing, now="2026-05-21T00:00:00Z")
    decisions_path = tmp_path / "decisions.json"
    decisions_path.write_text(
        json.dumps([{"item_id": "clip-a", "decision": "accept", "reviewed_at": "2026-05-21T00:00:01Z"}]),
        encoding="utf-8",
    )

    summary = dataset_run.run_pipeline(parsed, out_dir, review_decisions_path=decisions_path)

    state = read_review_state(out_dir / "review_state.json")
    assert summary["state_status"] == "finalized"
    assert state["state_version"] > written["state_version"]
    assert state["processed_source_ids"] == ["source-before-resume", "source-1"]
    assert summary["quality_report"].endswith("/run/quality_report.json")


def test_run_pipeline_threads_services_through_filter_caption_and_human_review(tmp_path: Path, monkeypatch) -> None:
    run_dir = tmp_path / "run"
    media_dir = run_dir / "clips"
    media_dir.mkdir(parents=True)
    media_file = media_dir / "source.mp4"
    media_file.write_bytes(b"fixture")
    _patch_local_provider(monkeypatch, media_file)
    config_path = _config(tmp_path, media_dir)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["extensions"] = {"fixture_mode": False}
    config["filters"] = {
        "stages": [
            {"stage_id": "duration_filter", "config": {"min_s": 1.0, "max_s": 10.0}},
            {
                "stage_id": "bucket_judge_filter",
                "config": {
                    "enabled": True,
                    "provider": "visual_understand",
                    "buckets": ["wide"],
                    "out_dir": str(tmp_path / "judges"),
                },
            },
        ]
    }
    config["review"] = {"enabled": True}
    config["budgets"] = {
        "max_api_calls": 2,
        "max_estimated_cost_usd": 1.0,
        "providers": {},
        "delegation": {"max_derived_bytes": 4294967296},
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")
    parsed = dataset_run.load_dataset_config(config_path)
    calls: list[str] = []

    def respond(capability: str, inputs: Mapping[str, Any]) -> dict[str, Any]:
        if "response_schema" in inputs:
            calls.append("filter")
            return {"accept": True, "bucket": "wide", "reason": "usable", "score": 0.9}
        calls.append("caption")
        return {"text": "Service caption.", "schema_version": 1, "confidence": 0.8, "model": "stub"}

    install_child_invoker(monkeypatch, FakeChildInvoker(respond, attempt_root=run_dir))
    monkeypatch.setattr(dataset_phases, "_human_review_argv", lambda: ["review-capability-stub"])

    def human_review_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append("human_review")
        state_path = Path(cmd[cmd.index("--state") + 1])
        out_path = Path(cmd[cmd.index("--out") + 1])
        state = read_review_state(state_path)
        decision = {
            "item_id": "clip-a",
            "decision": "accept",
            "reject_reason": None,
            "edited_caption": None,
            "reviewed_at": "2026-05-21T00:00:01Z",
            "state_version": state["state_version"],
        }
        state["review_decisions"] = {"clip-a": decision}
        state["submitted"] = True
        write_review_state(state_path, state)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps({"review_decisions": {"clip-a": decision}, "submitted": True}), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    services = dataset_run.DatasetRunServices.from_config(
        parsed.data,
        human_review_runner=human_review_runner,
    )

    summary = dataset_run.run_pipeline(parsed, run_dir, services=services)

    assert summary["state_status"] == "finalized"
    assert summary["accepted"] == 1
    assert summary["quality_report"].endswith("/run/quality_report.json")
    assert calls == ["filter", "caption", "human_review"]
    assert services.budget_tracker.as_dict()["total_api_calls"] == 2
    assert services.budget_tracker.as_dict()["provider_calls"] == {
        "bucket_judge.visual_understand": 1,
        "caption.visual_understand": 1,
    }


def _run_child_pipeline(
    tmp_path: Path,
    monkeypatch,
    filter_stages: list[dict[str, Any]],
    respond,
    *,
    clip_bytes: list[bytes],
) -> tuple[dict[str, Any], Path]:
    run_dir = tmp_path / "run"
    media_dir = run_dir / "clips"
    media_dir.mkdir(parents=True)
    media_files = []
    for index, payload in enumerate(clip_bytes):
        media_file = media_dir / f"source-{index}.mp4"
        media_file.write_bytes(payload)
        media_files.append(media_file)
    _patch_local_provider_per_file(monkeypatch, media_files)
    config_path = _config(tmp_path, media_dir)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["extensions"] = {"fixture_mode": False}
    config["filters"] = {"stages": filter_stages}
    config["review"] = {"enabled": False, "top_up": {"max_rounds": 0}}
    config["budgets"] = {
        "max_api_calls": 10,
        "max_estimated_cost_usd": 1.0,
        "providers": {},
        "delegation": {"max_derived_bytes": 4294967296},
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")
    parsed = dataset_run.load_dataset_config(config_path)
    install_child_invoker(monkeypatch, FakeChildInvoker(respond, attempt_root=run_dir))
    services = dataset_run.DatasetRunServices.from_config(parsed.data, human_review_runner=subprocess.run)
    return dataset_run.run_pipeline(parsed, run_dir, services=services), run_dir


def test_run_pipeline_exercises_transcript_and_near_duplicate_filters_with_child_invoker(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []

    def respond(capability: str, inputs: Mapping[str, Any]) -> dict[str, Any]:
        if capability == "editorial.transcribe":
            calls.append("transcript")
            return {"text": "keep this clip", "segments": [{"text": "keep this clip"}]}
        calls.append("caption")
        return {"text": "Caption.", "schema_version": 1, "confidence": 0.9, "model": "stub"}

    summary, run_dir = _run_child_pipeline(
        tmp_path,
        monkeypatch,
        [
            {
                "stage_id": "transcript_keyword_filter",
                "config": {
                    "allowlist": ["keep"],
                    "out_dir": str(tmp_path / "transcripts"),
                },
            },
            {
                "stage_id": "near_duplicate_filter",
                "config": {
                    "fixture_hashes": {
                        "clip-0": ["0000000000000000"],
                        "clip-1": ["ffffffffffffffff"],
                    },
                    "hamming_threshold": 3,
                    "out_dir": str(tmp_path / "frames"),
                },
            },
        ],
        respond,
        clip_bytes=[b"fixture-0", b"fixture-1"],
    )

    assert summary["state_status"] == "finalized"
    assert summary["accepted"] == 2
    assert calls.count("transcript") == 2
    assert calls.count("caption") == 2
    filtered = json.loads((run_dir / "filtered_items.json").read_text(encoding="utf-8"))
    assert [item["item_id"] for item in filtered["active"]] == ["clip-0", "clip-1"]
    assert filtered["rejected"] == []


def test_run_pipeline_exercises_semantic_filters_with_child_invoker(tmp_path: Path, monkeypatch) -> None:
    calls: list[str] = []

    def respond(capability: str, inputs: Mapping[str, Any]) -> dict[str, Any]:
        if capability == "understanding.video_understand":
            calls.append("semantic_video")
            return {"accept": True, "reason": "useful", "score": 0.9, "details": {"stub": True}}
        if "response_schema" in inputs:
            calls.append("semantic_visual")
            return {"accept": True, "reason": "useful", "score": 0.9, "details": {"stub": True}}
        calls.append("caption")
        return {"text": "Caption.", "schema_version": 1, "confidence": 0.9, "model": "stub"}

    summary, run_dir = _run_child_pipeline(
        tmp_path,
        monkeypatch,
        [
            {
                "stage_id": "semantic_visual_filter",
                "config": {
                    "prompt_template": "Keep useful clips. Prior feedback: {feedback_hints}",
                    "out_dir": str(tmp_path / "semantic_visual"),
                },
            },
            {
                "stage_id": "semantic_video_filter",
                "config": {
                    "prompt_template": "Keep useful motion. Prior feedback: {feedback_hints}",
                    "out_dir": str(tmp_path / "semantic_video"),
                },
            },
            {
                "stage_id": "near_duplicate_filter",
                "config": {
                    "fixture_hashes": {
                        "clip-0": ["0000000000000000"],
                        "clip-1": ["ffffffffffffffff"],
                    },
                    "hamming_threshold": 3,
                    "out_dir": str(tmp_path / "frames"),
                },
            },
        ],
        respond,
        clip_bytes=[b"fixture-0", b"fixture-1"],
    )

    assert summary["state_status"] == "finalized"
    assert summary["accepted"] == 2
    assert calls.count("semantic_visual") == 2
    assert calls.count("semantic_video") == 2
    assert calls.count("caption") == 2
    filtered = json.loads((run_dir / "filtered_items.json").read_text(encoding="utf-8"))
    assert [item["item_id"] for item in filtered["active"]] == ["clip-0", "clip-1"]
    assert filtered["rejected"] == []


@pytest.mark.parametrize(
    "resume_status",
    ["initializing", "acquiring", "failed", "filtering", "preview_ready", "captioning", "reviewing", "finalized"],
)
def test_dataset_build_resume_statuses_load_existing_checkpoints_without_reacquiring(
    tmp_path: Path,
    monkeypatch,
    resume_status: str,
) -> None:
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    media_file = media_dir / "source.mp4"
    media_file.write_bytes(b"fixture")
    _patch_local_provider(monkeypatch, media_file)
    config_path = _config(tmp_path, media_dir)
    parsed = dataset_run.load_dataset_config(config_path)
    decisions_path = tmp_path / "decisions.json"
    decisions_path.write_text(
        json.dumps([{"item_id": "clip-a", "decision": "accept", "reviewed_at": "2026-05-21T00:00:01Z"}]),
        encoding="utf-8",
    )
    out_dir = tmp_path / "run"
    dataset_run.run_pipeline(parsed, out_dir, review_decisions_path=decisions_path)
    state_path = out_dir / "review_state.json"
    assert read_review_state(state_path)["processed_source_ids"] == ["source-1"]

    if resume_status == "finalized":
        set_status(state_path, "finalized")
        manifest_mtime = (out_dir / "final.manifest.json").stat().st_mtime_ns
        state_version = read_review_state(state_path)["state_version"]
    else:
        set_status(state_path, resume_status)
        manifest_mtime = None
        state_version = None

    def fail_acquire(self: LocalFolderSourceProvider, config: dict[str, Any]):
        raise AssertionError(f"resume from {resume_status} should load checkpoints instead of acquiring")
        yield

    monkeypatch.setattr(LocalFolderSourceProvider, "acquire", fail_acquire)

    summary = dataset_run.run_pipeline(parsed, out_dir, review_decisions_path=decisions_path)

    state = read_review_state(state_path)
    assert summary["state_status"] == "finalized"
    assert state["processed_source_ids"] == ["source-1"]
    if resume_status == "finalized":
        assert state["state_version"] == state_version
        assert (out_dir / "final.manifest.json").stat().st_mtime_ns == manifest_mtime


def test_dataset_build_resume_rejects_config_hash_mismatch_without_mutating_state(tmp_path: Path, monkeypatch) -> None:
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    media_file = media_dir / "source.mp4"
    media_file.write_bytes(b"fixture")
    _patch_local_provider(monkeypatch, media_file)
    parsed = dataset_run.load_dataset_config(_config(tmp_path, media_dir))
    out_dir = tmp_path / "run"
    out_dir.mkdir()
    existing = make_initial_state(
        run_id="fixture-run",
        writer_id="training.dataset_build",
        config_hash="different-config",
        status="preview_ready",
    )
    written = write_review_state(out_dir / "review_state.json", existing, now="2026-05-21T00:00:00Z")

    try:
        dataset_run.run_pipeline(parsed, out_dir, review_decisions_path=None)
    except dataset_run.ResumeConfigMismatchError:
        pass
    else:
        raise AssertionError("config hash mismatch should fail before mutating state")

    state = read_review_state(out_dir / "review_state.json")
    assert state["state_version"] == written["state_version"]
    assert state["status"] == "preview_ready"
    assert state.get("error") is None


def test_acquire_candidates_embeds_initial_and_top_up_request_in_provider_config(tmp_path: Path, monkeypatch) -> None:
    media = tmp_path / "media.mp4"
    media.write_bytes(b"candidate")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    state = make_initial_state(
        run_id="fixture-run",
        writer_id="training.dataset_build",
        config_hash="hash",
        status="acquiring",
    )
    state["processed_source_ids"] = ["processed-source"]
    write_review_state(run_dir / "review_state.json", state)
    seen_configs: list[dict[str, Any]] = []

    class Provider:
        def acquire(self, config: dict[str, Any]):
            seen_configs.append(config)
            yield {
                **make_candidate_item(
                    source_type="local_folder",
                    source_id="source-1",
                    source_url=media.as_uri(),
                    media_path=media,
                    media_type="video",
                    source_metadata={"resolution": {"width": 64, "height": 64}},
                    duration_s=5.0,
                ),
                "item_id": "clip-a",
            }

    monkeypatch.setattr(dataset_run, "get_source_provider", lambda provider_id, **kwargs: Provider())
    config = {"sources": [{"provider": "local_folder", "config": {"path": str(tmp_path)}}]}

    candidates = dataset_run._acquire_candidates(
        config,
        run_dir,
        source_provider_kwargs=None,
        acquisition_request={
            "round_index": 1,
            "target_shortfalls": {"wide": 2},
            "exclude_candidate_ids": ["clip-before"],
            "exclude_source_ids": ["source-before"],
            "exclude_media_hashes": ["hash-before"],
        },
    )

    request = seen_configs[0]["acquisition_request"]
    assert candidates[0]["source_id"] == "source-1"
    assert request["round_index"] == 1
    assert request["target_shortfalls"] == {"wide": 2}
    assert request["limit_hint"] == 2
    assert request["exclude_candidate_ids"] == ["clip-before"]
    assert request["exclude_source_ids"] == ["source-before"]
    assert request["exclude_media_hashes"] == ["hash-before"]
    assert request["processed_source_ids"] == ["processed-source"]
    assert seen_configs[0]["processed_source_ids"] == ["processed-source"]


def test_dataset_build_skip_review_finalizes_without_auto_accepting_pending_items(tmp_path: Path, monkeypatch) -> None:
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    media_file = media_dir / "source.mp4"
    media_file.write_bytes(b"fixture")
    _patch_local_provider(monkeypatch, media_file)
    parsed = dataset_run.load_dataset_config(_config(tmp_path, media_dir))

    def fail_human_review(*args: Any, **kwargs: Any):
        raise AssertionError("--skip-review should not launch human_review")

    summary = dataset_run.run_pipeline(parsed, tmp_path / "run", skip_review=True, human_review_runner=fail_human_review)

    assert summary["accepted"] == 0
    assert summary["canonical_manifest"] is None
    assert summary["adapter_manifest"] is None
    assert not (tmp_path / "run" / "review_server" / "human_review.final.json").exists()
    assert not (tmp_path / "run" / "final.manifest.json").exists()
    state = read_review_state(tmp_path / "run" / "review_state.json")
    assert state["status"] == "reviewing"
    assert state["quality_report"].endswith("/run/quality_report.json")
    report = json.loads((tmp_path / "run" / "quality_report.json").read_text(encoding="utf-8"))
    assert report["run_status"] == "reviewing"
    assert report["summary"]["pending"] == 1
    assert report["final_shortfalls"] == {"wide": 1}
    assert state["submitted"] is False


def test_dataset_build_review_sampling_marks_items_without_dropping_unsampled_provenance(tmp_path: Path, monkeypatch) -> None:
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    media_file = media_dir / "source.mp4"
    media_file.write_bytes(b"fixture")
    _patch_local_provider_many(monkeypatch, media_file, 3)
    config_path = _config(tmp_path, media_dir)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["review"] = {"enabled": True, "sampling": {"mode": "top_n", "sample_count": 1}}
    config["budgets"]["max_api_calls"] = 10
    config_path.write_text(json.dumps(config), encoding="utf-8")
    parsed = dataset_run.load_dataset_config(config_path)

    summary = dataset_run.run_pipeline(parsed, tmp_path / "run", skip_review=True)

    assert summary["accepted"] == 0
    review_data = json.loads((tmp_path / "run" / "review_data.json").read_text(encoding="utf-8"))
    items = review_data["items"]
    assert [item["item_id"] for item in items] == ["clip-0", "clip-1", "clip-2"]
    assert [item["review_status"] for item in items] == ["pending", "pending", "pending"]
    assert [item["review_sampled"]["sampled"] for item in items] == [True, False, False]
    assert {item["review_sampled"]["mode"] for item in items} == {"top_n"}
    assert {item["review_sampled"]["sample_count"] for item in items} == {1}
    assert items[0]["review_sampled"]["rank"] == 1


def test_dataset_build_review_disabled_accepts_all_without_human_review_compatibility(tmp_path: Path, monkeypatch) -> None:
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    media_file = media_dir / "source.mp4"
    media_file.write_bytes(b"fixture")
    _patch_local_provider(monkeypatch, media_file)
    config_path = _config(tmp_path, media_dir)
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["review"] = {"enabled": False}
    config_path.write_text(json.dumps(config), encoding="utf-8")
    parsed = dataset_run.load_dataset_config(config_path)

    def fail_human_review(*args: Any, **kwargs: Any):
        raise AssertionError("review.enabled=false should not launch human_review")

    summary = dataset_run.run_pipeline(parsed, tmp_path / "run", human_review_runner=fail_human_review)

    assert summary["accepted"] == 1
    state = read_review_state(tmp_path / "run" / "review_state.json")
    assert state["submitted"] is True
    assert (tmp_path / "run" / "review_server" / "human_review.final.json").is_file()


def test_dataset_build_review_only_uses_existing_review_checkpoint_without_reprocessing(tmp_path: Path, monkeypatch) -> None:
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    media_file = media_dir / "source.mp4"
    media_file.write_bytes(b"fixture")
    _patch_local_provider(monkeypatch, media_file)
    config_path = _config(tmp_path, media_dir)
    parsed = dataset_run.load_dataset_config(config_path)
    decisions_path = tmp_path / "decisions.json"
    decisions_path.write_text(
        json.dumps([{"item_id": "clip-a", "decision": "accept", "reviewed_at": "2026-05-21T00:00:01Z"}]),
        encoding="utf-8",
    )
    out_dir = tmp_path / "run"
    dataset_run.run_pipeline(parsed, out_dir, review_decisions_path=decisions_path)
    set_status(out_dir / "review_state.json", "reviewing")

    def fail_acquire(self: LocalFolderSourceProvider, config: dict[str, Any]):
        raise AssertionError("--review-only should not acquire sources")
        yield

    def fail_caption(*args: Any, **kwargs: Any):
        raise AssertionError("--review-only should not caption")

    monkeypatch.setattr(LocalFolderSourceProvider, "acquire", fail_acquire)
    summary = dataset_run.run_pipeline(parsed, out_dir, review_decisions_path=decisions_path, review_only=True, caption_runner=fail_caption)

    assert summary["state_status"] == "finalized"
    assert summary["accepted"] == 1


def test_dataset_build_cli_skip_review_then_review_only_finalizes_without_reacquiring(tmp_path: Path, monkeypatch) -> None:
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    media_file = media_dir / "source.mp4"
    media_file.write_bytes(b"fixture")
    _patch_local_provider(monkeypatch, media_file)
    config_path = _config(tmp_path, media_dir)
    decisions_path = tmp_path / "decisions.json"
    decisions_path.write_text(
        json.dumps([{"item_id": "clip-a", "decision": "accept", "reviewed_at": "2026-05-21T00:00:01Z"}]),
        encoding="utf-8",
    )
    out_dir = tmp_path / "run"

    skip_exit = dataset_run.main(["--config", str(config_path), "--out", str(out_dir), "--skip-review"])

    assert skip_exit == 0
    skipped_state = read_review_state(out_dir / "review_state.json")
    assert skipped_state["status"] == "reviewing"
    assert skipped_state["submitted"] is False
    assert (out_dir / "review_data.json").is_file()
    assert not (out_dir / "final.manifest.json").exists()

    def fail_acquire(self: LocalFolderSourceProvider, config: dict[str, Any]):
        raise AssertionError("--review-only CLI should use review_data.json instead of acquiring")
        yield

    monkeypatch.setattr(LocalFolderSourceProvider, "acquire", fail_acquire)
    review_only_exit = dataset_run.main(
        [
            "--config",
            str(config_path),
            "--out",
            str(out_dir),
            "--review-decisions",
            str(decisions_path),
            "--review-only",
        ]
    )

    assert review_only_exit == 0
    final_state = read_review_state(out_dir / "review_state.json")
    assert final_state["status"] == "finalized"
    assert final_state["submitted"] is True
    assert (out_dir / "final.manifest.json").is_file()


def test_dataset_build_cli_resume_from_candidates_checkpoint_without_reacquiring_processed_sources(tmp_path: Path, monkeypatch) -> None:
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    media_file = media_dir / "source.mp4"
    media_file.write_bytes(b"fixture")
    _patch_local_provider(monkeypatch, media_file)
    config_path = _config(tmp_path, media_dir)
    decisions_path = tmp_path / "decisions.json"
    decisions_path.write_text(
        json.dumps([{"item_id": "clip-a", "decision": "accept", "reviewed_at": "2026-05-21T00:00:01Z"}]),
        encoding="utf-8",
    )
    out_dir = tmp_path / "run"
    first_exit = dataset_run.main(["--config", str(config_path), "--out", str(out_dir), "--review-decisions", str(decisions_path)])
    assert first_exit == 0
    assert read_review_state(out_dir / "review_state.json")["processed_source_ids"] == ["source-1"]
    set_status(out_dir / "review_state.json", "acquiring")

    def fail_acquire(self: LocalFolderSourceProvider, config: dict[str, Any]):
        raise AssertionError("resume should load candidates.json instead of reacquiring completed sources")
        yield

    monkeypatch.setattr(LocalFolderSourceProvider, "acquire", fail_acquire)
    resume_exit = dataset_run.main(["--config", str(config_path), "--out", str(out_dir), "--review-decisions", str(decisions_path)])

    assert resume_exit == 0
    state = read_review_state(out_dir / "review_state.json")
    assert state["status"] == "finalized"
    assert state["processed_source_ids"] == ["source-1"]


def test_dataset_build_dry_run_prints_plan_without_creating_artifacts(tmp_path: Path, capsys) -> None:
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    config_path = _config(tmp_path, media_dir)
    out_dir = tmp_path / "dry-run-output"

    exit_code = dataset_run.main(["--config", str(config_path), "--out", str(out_dir), "--dry-run"])

    assert exit_code == 0
    assert not out_dir.exists()
    payload = json.loads(capsys.readouterr().out)
    assert payload["orchestrator"] == "training.dataset_build"
    assert [stage["stage_id"] for stage in payload["filter_stages"]] == ["duration_filter"]
    assert payload["fixture_mode"] is True
    assert payload["budget_limits"]["max_api_calls"] == 1
    assert payload["skip_review"] is False
    assert payload["review_only"] is False
    assert payload["quality_report"] == str((out_dir / "quality_report.json").resolve())


def test_dataset_build_cli_preflights_no_spend_api_config_before_run_dir_creation(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    config = {
        "schema_version": 1,
        "media_type": "video",
        "dataset_id": "no-spend-preflight",
        "sources": [{"provider": "youtube", "config": {"source_urls": ["https://example.invalid/video"]}}],
        "buckets": {"wide": {"target_count": 1, "search_queries": ["generic training clip"]}},
        "clip_config": {"min_duration_s": 1.0, "max_duration_s": 10.0, "max_scenes_per_source": 1},
        "caption": {"provider": "visual_understand", "prompt_template": "Caption."},
        "filters": {"duration": {"enabled": True, "min_s": 1.0, "max_s": 10.0}},
        "review": {"enabled": False},
        "manifest": {"adapter": "ai-toolkit-ltx"},
        "budgets": {
            "max_api_calls": 0,
            "max_estimated_cost_usd": 1.0,
            "providers": {"caption.visual_understand": {"max_calls": 0}},
        },
        "output": {"run_dir": str(tmp_path / "ignored")},
        "extensions": {},
    }
    config_path = tmp_path / "api-config.yaml"
    config_path.write_text(yaml.safe_dump(config), encoding="utf-8")
    out_dir = tmp_path / "must-not-exist"

    with pytest.raises(AstridError) as raised:
        dataset_run.main(["--config", str(config_path), "--out", str(out_dir)])
    assert "budgets.max_api_calls must be positive" in raised.value.cause
    assert not out_dir.exists()


def test_human_review_command_requires_the_editorial_pack_when_absent(tmp_path: Path) -> None:
    with pytest.raises(AstridError, match="requires pack 'editorial'"):
        dataset_phases._run_human_review(
            tmp_path, tmp_path / "data.json", tmp_path / "state.json", lambda *a, **k: None
        )
