#!/usr/bin/env python3
"""Refine arrangement dialogue trims before render by re-transcribing clip snippets."""


from __future__ import annotations

from astrid.pack_api import guard_canonical_entrypoint

guard_canonical_entrypoint('editorial.refine')
import argparse
import copy
import json
import subprocess
import tempfile
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Sequence

from astrid.pack_api import build_manifest, write_manifest
from astrid.pack_api import register_outputs
from astrid.pack_api import AstridError
from astrid.pack_api import sha256_file
from astrid.pack_api import ROLE_DURATION_BOUNDS, TOTAL_DURATION_BOUNDS, TRIM_BOUND_EXTENSION_SEC, canonical_timeline_config, compile_arrangement_plan, is_all_generative_arrangement, load_metadata, load_registry, load_timeline, save_arrangement, save_metadata, save_timeline, validate_arrangement_duration_window
from astrid.packs.editorial.actions.refine.src.reviewers.audio_boundary import (
    AudioBoundaryReviewer,
)
from astrid.packs.editorial.actions.refine.src.reviewers.overlay_fit import OverlayFitReviewer
from astrid.packs.editorial.actions.refine.src.reviewers.speaker_flow import SpeakerFlowReviewer
from astrid.packs.editorial.actions.refine.src.reviewers.visual_quality import (
    VisualQualityReviewer,
)
from astrid.packs.editorial.shared.hype import enriched_arrangement
from astrid.packs.editorial.shared.hype.text_match import segments_in_range, token_set_similarity, tokenize
from astrid.packs.video_editing.actions.cut.run import (
    build_metadata_from_arrangement,
    build_multitrack_timeline,
)

from astrid.packs.editorial.shared._common import load_api_key

BOILERPLATE_TOKENS = {"um", "uh"}
BOILERPLATE_BIGRAMS = {("you", "know"), ("i", "mean"), ("sort", "of"), ("kind", "of")}
SENTENCE_END_CHARS = ".!?"
SnippetTranscriber = Callable[[Path | str, float, float], str]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Refine arrangement dialogue trims before Remotion render.")
    add = parser.add_argument
    add("--arrangement", type=Path, required=True)
    add("--pool", type=Path, required=True)
    add("--timeline", type=Path, required=True)
    add("--assets", type=Path, required=True)
    add("--metadata", type=Path, required=True)
    add("--transcript", type=Path, required=True)
    add("--primary-asset")
    add("--out", type=Path, required=True)
    add("--max-iterations", type=int, default=3)
    add("--min-nudge-sec", type=float, default=0.08)
    add("--max-nudge-sec", type=float, default=TRIM_BOUND_EXTENSION_SEC)
    add("--similarity-threshold", type=float, default=0.85)
    add("--skip-whisper", action="store_true")
    add("--env-file", type=Path)
    return parser


def _pool_map(pool: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(entry["id"]): entry for entry in pool.get("entries", [])}


def _joined_text(segments: list[dict[str, Any]]) -> str | None:
    joined = " ".join(str(segment.get("text", "")).strip() for segment in segments if str(segment.get("text", "")).strip()).strip()
    return joined or None


def _first_token(text: str | None) -> str | None:
    tokens = tokenize(text or "")
    return tokens[0] if tokens else None


def _last_token(text: str | None) -> str | None:
    tokens = tokenize(text or "")
    return tokens[-1] if tokens else None


def _ends_sentence(text: str | None) -> bool:
    stripped = (text or "").rstrip()
    return not stripped or stripped[-1] in SENTENCE_END_CHARS


def _resolve_ref(ref: str | None, out_dir: Path) -> Path | None:
    if not ref:
        return None
    path = Path(ref)
    return path if path.is_absolute() else (out_dir / path).resolve()


def _resolve_asset_path(registry_path: Path, registry: dict[str, Any], asset_key: str) -> Path | str:
    entry = registry.get("assets", {}).get(asset_key, {})
    if not isinstance(entry, dict):
        raise AstridError(
            f"Asset registry entry {asset_key!r} is missing",
            recovery_command="Check that the asset key exists in the registry and the registry file is correctly formed",
        )
    if any(key in entry for key in ("url", "uri", "path", "source_path", "locator", "cache")):
        raise AstridError(
            f"Asset registry entry {asset_key!r} contains an unsupported locator; "
            "refine accepts only host-materialized files",
            recovery_command="Pass the runtime-materialized asset registry produced for this attempt",
        )
    file_value = entry.get("file")
    if not isinstance(file_value, str) or not file_value or not Path(file_value).is_absolute():
        raise AstridError(
            f"Asset registry entry {asset_key!r} is not host-materialized",
            recovery_command="Ensure the attempt registry contains an absolute materialized 'file' path",
        )
    path = Path(file_value).resolve()
    if path.is_symlink() or not path.is_file():
        raise AstridError(
            f"Host-materialized asset for {asset_key!r} is missing: {path}",
            recovery_command="Re-run the task so the runtime host can materialize the asset",
        )
    return path


def _resolve_primary_asset(
    requested: str | None,
    registry: dict[str, Any],
    prior_meta: dict[str, Any],
) -> str | None:
    if requested:
        return requested
    config_snapshot = dict(prior_meta.get("pipeline", {}).get("config_snapshot", {}) or {})
    if "primary_asset" in config_snapshot:
        snapshot_value = config_snapshot["primary_asset"]
        if snapshot_value is None:
            return None
        if isinstance(snapshot_value, str) and snapshot_value.strip():
            return snapshot_value.strip()
    assets = registry.get("assets", {})
    if "main" in assets:
        return "main"
    if len(assets) == 1:
        only_key = next(iter(assets))
        if assets[only_key].get("type") == "audio":
            return None
        return str(only_key)
    raise AstridError(
        "refine requires --primary-asset when the asset registry has multiple keys and metadata lacks pipeline.config_snapshot.primary_asset",
        valid_options=sorted(assets.keys()),
        recovery_command="Pass --primary-asset <key> to specify which asset to use, or add pipeline.config_snapshot.primary_asset to metadata",
    )


def _load_transcript_segments(path: Path) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    segments = payload.get("segments") if isinstance(payload, dict) else payload
    if not isinstance(segments, list):
        raise AstridError(
            f"Transcript payload at {path} must contain a segments list",
            recovery_command="Ensure the transcript file contains a JSON object with a 'segments' list, or pass a valid segments JSON array",
        )
    return [segment for segment in segments if isinstance(segment, dict)]


def _clip_duration(start: float, end: float) -> float:
    return round(float(end) - float(start), 6)


def _arrangement_total_duration(arrangement: dict[str, Any], pool_entries: dict[str, dict[str, Any]]) -> float:
    total = 0.0
    for clip in arrangement.get("clips", []):
        audio_source = clip.get("audio_source")
        if isinstance(audio_source, dict):
            total += _clip_duration(*map(float, audio_source["trim_sub_range"]))
        else:
            visual_source = clip.get("visual_source") or {}
            entry = pool_entries[str(visual_source["pool_id"])]
            total += float(entry["src_end"]) - float(entry["src_start"])
    return round(total, 6)


def _nudge_amount(iteration_idx: int, max_iterations: int, min_nudge_sec: float, max_nudge_sec: float) -> float:
    if max_iterations <= 1:
        return round(max_nudge_sec, 6)
    ratio = min(max(float(iteration_idx) / float(max_iterations - 1), 0.0), 1.0)
    return round(min_nudge_sec + ((max_nudge_sec - min_nudge_sec) * ratio), 6)


def transcribe_snippet(asset_path: Path | str, start: float, end: float, client: Any, model: str = "whisper-1", language: str = "en") -> str:
    with tempfile.TemporaryDirectory(prefix="refine-snippet-") as tmp_dir:
        snippet_path = Path(tmp_dir) / "snippet.wav"
        subprocess.run(
            ["ffmpeg", "-y", "-ss", f"{start:.6f}", "-to", f"{end:.6f}", "-i", str(asset_path), "-vn", "-ac", "1", "-ar", "16000", str(snippet_path)],
            check=True,
            capture_output=True,
            text=True,
        )
        with snippet_path.open("rb") as handle:
            response = client.audio.transcriptions.create(model=model, file=handle, response_format="json", language=language)
    if hasattr(response, "text"):
        return str(response.text).strip()
    data = response.model_dump() if hasattr(response, "model_dump") else dict(response)
    return str(data.get("text", "")).strip()


def neighbor_context(segments: list[dict[str, Any]], trim_start: float, trim_end: float) -> tuple[str | None, str | None, bool, bool]:
    overlap = segments_in_range(segments, trim_start, trim_end)
    prev_text = next((str(segment.get("text", "")) for segment in reversed(segments) if float(segment.get("end", 0.0)) <= trim_start), None)
    next_text = next((str(segment.get("text", "")) for segment in segments if float(segment.get("start", 0.0)) >= trim_end), None)
    last_overlap_text = str(overlap[-1].get("text", "")) if overlap else prev_text
    return _last_token(prev_text), _first_token(next_text), _ends_sentence(prev_text), _ends_sentence(last_overlap_text)


def detect_issues(
    expected: str,
    actual: str,
    prev_last_token: str | None,
    next_first_token: str | None,
    start_ends_sentence: bool,
    end_ends_sentence: bool,
    actual_first_token: str | None,
    actual_last_token: str | None,
) -> list[str]:
    expected_tokens = tokenize(expected)
    actual_tokens = tokenize(actual)
    issues: set[str] = set()
    if not actual_tokens:
        return []
    if actual_first_token in BOILERPLATE_TOKENS or tuple(actual_tokens[:2]) in BOILERPLATE_BIGRAMS:
        issues.add("boilerplate_start")
    if actual_last_token in BOILERPLATE_TOKENS or tuple(actual_tokens[-2:]) in BOILERPLATE_BIGRAMS:
        issues.add("boilerplate_end")
    if prev_last_token and actual_first_token and (prev_last_token, actual_first_token) in BOILERPLATE_BIGRAMS:
        issues.add("boilerplate_start")
    if next_first_token and actual_last_token and (actual_last_token, next_first_token) in BOILERPLATE_BIGRAMS:
        issues.add("boilerplate_end")
    if not start_ends_sentence:
        issues.add("mid_sentence_start")
    if next_first_token and not end_ends_sentence:
        issues.add("mid_sentence_end")
    expected_first = expected_tokens[0] if expected_tokens else None
    expected_last = expected_tokens[-1] if expected_tokens else None
    if actual_first_token and expected_first and actual_first_token != expected_first and (expected_first.startswith(actual_first_token) or expected_first.endswith(actual_first_token) or (prev_last_token and (prev_last_token.startswith(actual_first_token) or prev_last_token.endswith(actual_first_token)))):
        issues.add("half_word_start")
    if actual_last_token and expected_last and actual_last_token != expected_last and (expected_last.startswith(actual_last_token) or expected_last.endswith(actual_last_token) or (next_first_token and (next_first_token.startswith(actual_last_token) or next_first_token.endswith(actual_last_token)))):
        issues.add("half_word_end")
    return sorted(issues)


def propose_nudge(
    issues: list[str],
    trim_start: float,
    trim_end: float,
    entry: dict[str, Any],
    role: str,
    iteration_idx: int,
    *,
    max_iterations: int,
    min_nudge_sec: float,
    max_nudge_sec: float,
) -> dict[str, Any] | None:
    src_start, src_end = float(entry["src_start"]), float(entry["src_end"])
    min_duration, max_duration = ROLE_DURATION_BOUNDS[role]
    nudge = _nudge_amount(iteration_idx, max_iterations, min_nudge_sec, max_nudge_sec)
    for issue in issues:
        new_start, new_end = trim_start, trim_end
        if issue in {"half_word_start", "boilerplate_start"}:
            new_start = trim_start + nudge
        elif issue in {"half_word_end", "boilerplate_end"}:
            new_end = trim_end - nudge
        elif issue == "mid_sentence_start":
            new_start = trim_start - nudge
        elif issue == "mid_sentence_end":
            new_end = trim_end + nudge
        new_start = max(src_start - TRIM_BOUND_EXTENSION_SEC, min(new_start, src_end + TRIM_BOUND_EXTENSION_SEC))
        new_end = max(src_start - TRIM_BOUND_EXTENSION_SEC, min(new_end, src_end + TRIM_BOUND_EXTENSION_SEC))
        duration = _clip_duration(new_start, new_end)
        if new_end <= new_start or duration < min_duration or duration > max_duration:
            continue
        if new_start == trim_start and new_end == trim_end:
            continue
        return {"issue": issue, "trim_before": [round(trim_start, 6), round(trim_end, 6)], "trim_after": [round(new_start, 6), round(new_end, 6)], "nudge": nudge}
    return None


def _regen_args(args: argparse.Namespace, prior_meta: dict[str, Any]) -> SimpleNamespace:
    out_dir = args.out.resolve()
    source_meta = dict(prior_meta.get("sources", {}).get(args.primary_asset, {}) or {})
    config_snapshot = dict(prior_meta.get("pipeline", {}).get("config_snapshot", {}) or {})
    scenes_ref = _resolve_ref(source_meta.get("scenes_ref"), out_dir)
    transcript_ref = _resolve_ref(source_meta.get("transcript_ref"), out_dir)
    quality_zones_ref = None
    if scenes_ref is not None:
        quality_zones_ref = scenes_ref.parent / "quality_zones.json"
    elif transcript_ref is not None:
        quality_zones_ref = transcript_ref.parent / "quality_zones.json"
    return SimpleNamespace(
        scenes=scenes_ref,
        transcript=args.transcript.resolve(),
        quality_zones=quality_zones_ref,
        shots=_resolve_ref(source_meta.get("shots_ref"), out_dir),
        arrangement=args.arrangement.resolve(),
        renderer=str(config_snapshot.get("renderer") or "rendering.remotion"),
        out=out_dir,
    )


def _ordered_issues(issues: list[str], total_duration: float) -> list[str]:
    shrink = [issue for issue in issues if issue.startswith("half_word") or issue.startswith("boilerplate")]
    extend = [issue for issue in issues if issue.startswith("mid_sentence")]
    min_total, max_total = TOTAL_DURATION_BOUNDS
    return shrink + extend if total_duration >= max_total - 1.0 else extend + shrink if total_duration <= min_total + 1.0 else shrink + extend


def _evaluate_clip(transcriber: SnippetTranscriber | None, asset_path: Path | str, trim_start: float, trim_end: float, expected: str) -> tuple[str, float | None]:
    if transcriber is None:
        return "", None
    actual = transcriber(asset_path, trim_start, trim_end)
    return actual, token_set_similarity(expected=expected, actual=actual)


def _narrow_clip_transcript_texts(metadata: dict[str, Any], arrangement: dict[str, Any], pool_entries: dict[str, dict[str, Any]], transcript_segments: list[dict[str, Any]]) -> None:
    clips_meta = metadata.setdefault("clips", {})
    for clip in sorted(arrangement.get("clips", []), key=lambda item: int(item["order"])):
        audio_source = clip.get("audio_source")
        if not isinstance(audio_source, dict):
            continue
        entry = pool_entries.get(str(audio_source.get("pool_id")))
        if not entry or entry.get("category") != "dialogue":
            continue
        trim_start, trim_end = map(float, audio_source["trim_sub_range"])
        clips_meta.setdefault(f"clip_a_{int(clip['order'])}", {})["source_transcript_text"] = _joined_text(segments_in_range(transcript_segments, trim_start, trim_end))


def refine_arrangement(
    enriched: enriched_arrangement.EnrichedArrangement,
    registry_path: Path,
    registry: dict[str, Any],
    args: argparse.Namespace,
    transcriber: SnippetTranscriber | None,
) -> dict[str, Any]:
    dialogue_clips = [
        clip for clip in enriched.clips
        if clip.audio_pool_entry and clip.audio_pool_entry.get("category") == "dialogue" and isinstance(clip.clip.get("audio_source"), dict)
    ]
    auto_fix_by_order: dict[int, dict[str, Any]] = {}
    rejected_nudges: list[dict[str, Any]] = []
    latest_flag_findings: dict[str, list[enriched_arrangement.ReviewerFinding]] = {
        "visual_quality": [],
        "speaker_flow": [],
        "overlay_fit": [],
    }
    iterations_run = 0
    converged = not dialogue_clips
    if transcriber is None and dialogue_clips:
        iterations_run = 1
        for clip in dialogue_clips:
            entry = clip.audio_pool_entry or {}
            rejected_nudges.append(
                {
                    "order": clip.order,
                    "pool_id": str(entry.get("id") or ""),
                    "iteration": 1,
                    "reason": "whisper_skipped",
                    "message": "Audio boundary refinement skipped because whisper transcription was disabled.",
                }
            )
        latest_flag_findings = _run_flag_reviewers(enriched)
        converged = False
    elif dialogue_clips:
        final_audio_findings: list[enriched_arrangement.ReviewerFinding] = []
        for iteration_idx in range(args.max_iterations):
            iterations_run = iteration_idx + 1
            audio_reviewer = AudioBoundaryReviewer(
                transcriber=transcriber,
                similarity_threshold=args.similarity_threshold,
                min_nudge_sec=args.min_nudge_sec,
                max_nudge_sec=args.max_nudge_sec,
                iteration_idx=iteration_idx,
                max_iterations=args.max_iterations,
            )
            final_audio_findings = audio_reviewer.review(enriched)
            latest_flag_findings = _run_flag_reviewers(enriched)
            auto_fixes = [finding for finding in final_audio_findings if finding.severity is enriched_arrangement.FindingSeverity.AUTO_FIX]
            audio_flags = [finding for finding in final_audio_findings if finding.severity is enriched_arrangement.FindingSeverity.FLAG]
            for finding in audio_flags:
                _append_rejected_nudge(rejected_nudges, finding, enriched, iteration_idx + 1)
            if not auto_fixes:
                if audio_flags and iteration_idx < args.max_iterations - 1:
                    continue
                converged = not audio_flags
                break
            for finding in auto_fixes:
                _merge_audio_auto_fix(auto_fix_by_order, finding)
            enriched_arrangement.apply_auto_fixes(enriched, _sanitized_auto_fixes(auto_fixes))
        else:
            final_audio_findings = AudioBoundaryReviewer(
                transcriber=transcriber,
                similarity_threshold=args.similarity_threshold,
                min_nudge_sec=args.min_nudge_sec,
                max_nudge_sec=args.max_nudge_sec,
                iteration_idx=max(args.max_iterations - 1, 0),
                max_iterations=args.max_iterations,
            ).review(enriched)
            latest_flag_findings = _run_flag_reviewers(enriched)
            final_audio_flags = [finding for finding in final_audio_findings if finding.severity is enriched_arrangement.FindingSeverity.FLAG]
            for finding in final_audio_flags:
                _append_rejected_nudge(rejected_nudges, finding, enriched, args.max_iterations)
            converged = not final_audio_findings
    else:
        latest_flag_findings = _run_flag_reviewers(enriched)

    _refresh_audio_auto_fixes(auto_fix_by_order, enriched, registry_path, registry, transcriber)
    audio_auto_fixes = [auto_fix_by_order[order] for order in sorted(auto_fix_by_order)]
    return {
        "iterations_run": iterations_run,
        "converged": converged,
        "auto_fixes": {"audio_boundary": audio_auto_fixes},
        "flags": {
            "visual_quality": [_flag_entry(finding) for finding in latest_flag_findings["visual_quality"]],
            "speaker_flow": [_flag_entry(finding) for finding in latest_flag_findings["speaker_flow"]],
            "overlay_fit": [_flag_entry(finding) for finding in latest_flag_findings["overlay_fit"]],
        },
        "rejected_nudges": rejected_nudges,
        "per_clip": [copy.deepcopy(entry) for entry in audio_auto_fixes],
    }


def _run_flag_reviewers(enriched: enriched_arrangement.EnrichedArrangement) -> dict[str, list[enriched_arrangement.ReviewerFinding]]:
    reviewers = [VisualQualityReviewer(), SpeakerFlowReviewer(), OverlayFitReviewer()]
    return {reviewer.name: reviewer.review(enriched) for reviewer in reviewers}


def _merge_audio_auto_fix(auto_fix_by_order: dict[int, dict[str, Any]], finding: enriched_arrangement.ReviewerFinding) -> None:
    patch = dict(finding.proposed_patch or {})
    order = int(finding.clip_order)
    existing = auto_fix_by_order.get(order)
    issues = list(patch.get("issues_resolved") or [finding.code])
    if existing is None:
        auto_fix_by_order[order] = {
            "order": order,
            "uuid": finding.clip_uuid,
            "pool_id": str(patch.get("pool_id") or ""),
            "trim_before": list(patch.get("trim_before") or []),
            "trim_after": list(patch.get("trim_after") or []),
            "issues_resolved": issues,
            "similarity_before": patch.get("similarity_before"),
            "similarity_after": patch.get("similarity_after"),
            "source_transcript_text_before": patch.get("source_transcript_text_before"),
            "source_transcript_text_after": patch.get("source_transcript_text_after"),
        }
        return
    existing["trim_after"] = list(patch.get("trim_after") or existing["trim_after"])
    existing["issues_resolved"] = sorted(set(existing["issues_resolved"]) | set(issues))
    existing["similarity_after"] = patch.get("similarity_after", existing["similarity_after"])
    existing["source_transcript_text_after"] = patch.get("source_transcript_text_after", existing["source_transcript_text_after"])
    if not existing["pool_id"] and patch.get("pool_id") is not None:
        existing["pool_id"] = str(patch["pool_id"])


def _sanitized_auto_fixes(
    findings: list[enriched_arrangement.ReviewerFinding],
) -> list[enriched_arrangement.ReviewerFinding]:
    sanitized: list[enriched_arrangement.ReviewerFinding] = []
    for finding in findings:
        trim_after = (finding.proposed_patch or {}).get("trim_after")
        if trim_after is None:
            continue
        sanitized.append(
            enriched_arrangement.ReviewerFinding(
                clip_order=finding.clip_order,
                clip_uuid=finding.clip_uuid,
                code=finding.code,
                severity=finding.severity,
                message=finding.message,
                reviewer=finding.reviewer,
                proposed_patch={"trim_after": list(trim_after)},
            )
        )
    return sanitized


def _append_rejected_nudge(
    rejected_nudges: list[dict[str, Any]],
    finding: enriched_arrangement.ReviewerFinding,
    enriched: enriched_arrangement.EnrichedArrangement,
    iteration: int,
) -> None:
    clip = enriched.clips_by_order.get(int(finding.clip_order))
    pool_id = ""
    if clip and clip.audio_pool_entry and clip.audio_pool_entry.get("id") is not None:
        pool_id = str(clip.audio_pool_entry["id"])
    reason = "total_duration_bounds" if "total duration bounds" in finding.message else "no_valid_nudge"
    rejected_nudges.append(
        {
            "order": int(finding.clip_order),
            "pool_id": pool_id,
            "iteration": iteration,
            "reason": reason,
            "message": finding.message,
        }
    )


def _refresh_audio_auto_fixes(
    auto_fix_by_order: dict[int, dict[str, Any]],
    enriched: enriched_arrangement.EnrichedArrangement,
    registry_path: Path,
    registry: dict[str, Any],
    transcriber: SnippetTranscriber | None,
) -> None:
    for order, entry in auto_fix_by_order.items():
        clip = enriched.clips_by_order.get(order)
        if clip is None:
            continue
        audio_source = clip.clip.get("audio_source") or {}
        trim_range = audio_source.get("trim_sub_range") or []
        if len(trim_range) == 2:
            entry["trim_after"] = [round(float(value), 6) for value in trim_range]
            entry["source_transcript_text_after"] = _joined_text(segments_in_range(clip.transcript_segments, *map(float, trim_range)))
        if clip.audio_pool_entry and clip.audio_pool_entry.get("id") is not None:
            entry["pool_id"] = str(clip.audio_pool_entry["id"])
        if transcriber is None or not clip.audio_pool_entry:
            continue
        expected = enriched_arrangement.expected_text_for_clip(clip)
        asset_path = _resolve_asset_path(registry_path, registry, clip.asset_key)
        _actual, similarity = _evaluate_clip(transcriber, asset_path, *map(float, entry["trim_after"]), expected)
        if similarity is not None:
            entry["similarity_after"] = round(similarity, 6)


def _flag_entry(finding: enriched_arrangement.ReviewerFinding) -> dict[str, Any]:
    return {
        "order": int(finding.clip_order),
        "uuid": finding.clip_uuid,
        "code": finding.code,
        "message": finding.message,
    }


def write_outputs(enriched: enriched_arrangement.EnrichedArrangement, registry: dict[str, Any], transcript_segments: list[dict[str, Any]], prior_meta: dict[str, Any], args: argparse.Namespace, report: dict[str, Any]) -> None:
    pool_entries = _pool_map(enriched.pool)
    if not is_all_generative_arrangement(enriched.arrangement, enriched.pool):
        validate_arrangement_duration_window(enriched.arrangement)
    save_arrangement(enriched.arrangement, args.arrangement, set(pool_entries))
    compiled_plan = compile_arrangement_plan(enriched.arrangement, enriched.pool)
    provenance = dict(prior_meta.get("pipeline", {}).get("pool_provenance", {}) or {})
    regen_args = _regen_args(args, prior_meta)
    metadata = build_metadata_from_arrangement(
        enriched.arrangement,
        enriched.pool,
        registry,
        dict(prior_meta.get("sources", {}) or {}),
        regen_args,
        args.primary_asset,
        transcript_segments,
        quality_zones_ref=regen_args.quality_zones,
        pool_sha256=str(provenance.get("pool_sha256") or sha256_file(args.pool)),
        arrangement_sha256=sha256_file(args.arrangement),
        brief_sha256=str(provenance.get("brief_sha256") or enriched.arrangement.get("brief_sha256") or ""),
        compiled_plan=compiled_plan,
    )
    _narrow_clip_transcript_texts(metadata, enriched.arrangement, pool_entries, transcript_segments)
    # Preserve the existing timeline's theme reference across the rebuild — refine
    # doesn't change brand, only contents.
    prior_timeline = load_timeline(args.timeline) if args.timeline.exists() else {}
    prior_theme_slug = prior_timeline.get("theme") if isinstance(prior_timeline, dict) else None
    rebuilt = build_multitrack_timeline(
        enriched.arrangement,
        enriched.pool,
        registry,
        args.primary_asset,
        compiled_plan=compiled_plan,
        theme_slug=prior_theme_slug if isinstance(prior_theme_slug, str) and prior_theme_slug else None,
    )
    if isinstance(prior_timeline, dict) and isinstance(prior_timeline.get("theme_overrides"), dict):
        rebuilt.setdefault("theme_overrides", prior_timeline["theme_overrides"])
    canonical_timeline_config(rebuilt)
    save_timeline(rebuilt, args.timeline)
    save_metadata(metadata, args.metadata)
    clips_by_order = {int(clip_id.rsplit("_", 1)[-1]): dict(clip_meta) for clip_id, clip_meta in metadata.get("clips", {}).items() if clip_id.startswith("clip_a_")}
    trim_by_order = {int(clip["order"]): [round(float(value), 6) for value in clip["audio_source"]["trim_sub_range"]] for clip in enriched.arrangement.get("clips", []) if isinstance(clip.get("audio_source"), dict)}
    for clip_entry in report["auto_fixes"]["audio_boundary"]:
        order = int(clip_entry["order"])
        clip_entry["trim_after"] = trim_by_order.get(order, clip_entry["trim_after"])
        clip_entry["source_transcript_text_after"] = clips_by_order.get(order, {}).get("source_transcript_text")
    report["per_clip"] = [copy.deepcopy(entry) for entry in report["auto_fixes"]["audio_boundary"]]
    (args.out / "refine.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    register_outputs(
        stage="refine",
        outputs=[
            ("arrangement", args.arrangement, "Refined arrangement"),
            ("timeline", args.timeline, "Refined timeline"),
            ("metadata", args.metadata, "Refined metadata"),
            ("refinement", args.out / "refine.json", "Refinement report"),
        ],
        metadata={
            "iterations_run": report.get("iterations_run"),
            "converged": report.get("converged"),
            "audio_boundary_fixes": len(report.get("auto_fixes", {}).get("audio_boundary", [])),
        },
    )

    # --- universal result manifest -----------------------
    manifest_path = args.out / "manifest.json"
    manifest = build_manifest(
        kind="refine",
        inputs={
            "arrangement": str(args.arrangement),
            "pool": str(args.pool),
            "timeline": str(args.timeline),
            "assets": str(args.assets),
            "metadata": str(args.metadata),
            "transcript": str(args.transcript),
        },
        outputs=[
            {"path": "refine.json", "type": "file"},
            {"path": args.arrangement.name, "type": "file"},
            {"path": args.timeline.name, "type": "file"},
            {"path": args.metadata.name, "type": "file"},
        ],
        created=datetime.now(timezone.utc).isoformat(),
    )
    write_manifest(manifest_path, manifest)
    # -------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None, *, transcriber: SnippetTranscriber | None = None) -> int:
    args = build_parser().parse_args(argv)
    for name in ("arrangement", "pool", "timeline", "assets", "metadata", "transcript", "out"):
        setattr(args, name, getattr(args, name).resolve())
    args.out.mkdir(parents=True, exist_ok=True)
    registry = load_registry(args.assets)
    prior_meta = load_metadata(args.metadata)
    transcript_segments = _load_transcript_segments(args.transcript)
    args.primary_asset = _resolve_primary_asset(args.primary_asset, registry, prior_meta)
    enriched = enriched_arrangement.load(args.out)
    if transcriber is None and not args.skip_whisper:
        from openai import OpenAI
        transcriber = partial(transcribe_snippet, client=OpenAI(api_key=load_api_key(args.env_file)), model="whisper-1", language="en")
    report = refine_arrangement(enriched, args.assets, registry, args, transcriber)
    write_outputs(enriched, registry, transcript_segments, prior_meta, args, report)
    print(" ".join([f"iterations_run={report['iterations_run']}", f"converged={str(report['converged']).lower()}", f"auto_fixes={len(report['auto_fixes']['audio_boundary'])}", f"refine_json={args.out / 'refine.json'}"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
