from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from astrid.pack_api import ROLE_DURATION_BOUNDS, TOTAL_DURATION_BOUNDS, TRIM_BOUND_EXTENSION_SEC, load_registry
from astrid.packs.editorial.actions.refine.src.reviewers import Reviewer
from astrid.packs.editorial.shared.hype import enriched_arrangement
from astrid.packs.editorial.shared.hype.text_match import segments_in_range, token_set_similarity, tokenize

BOILERPLATE_TOKENS = {"um", "uh"}
BOILERPLATE_BIGRAMS = {("you", "know"), ("i", "mean"), ("sort", "of"), ("kind", "of")}
SENTENCE_END_CHARS = ".!?"
SnippetTranscriber = Callable[[Path | str, float, float], str]


class AudioBoundaryReviewer(Reviewer):
    name = "audio_boundary"

    def __init__(
        self,
        *,
        transcriber: SnippetTranscriber | None,
        similarity_threshold: float = 0.85,
        min_nudge_sec: float = 0.08,
        max_nudge_sec: float = TRIM_BOUND_EXTENSION_SEC,
        iteration_idx: int = 0,
        max_iterations: int = 3,
    ) -> None:
        self._transcriber = transcriber
        self._similarity_threshold = similarity_threshold
        self._min_nudge_sec = min_nudge_sec
        self._max_nudge_sec = max_nudge_sec
        self._iteration_idx = iteration_idx
        self._max_iterations = max_iterations

    def review(self, enriched: enriched_arrangement.EnrichedArrangement) -> list[enriched_arrangement.ReviewerFinding]:
        if self._transcriber is None:
            return []
        registry = _load_registry(enriched.run_dir / "hype.assets.json")
        current_total = _arrangement_total_duration(enriched)
        findings: list[enriched_arrangement.ReviewerFinding] = []
        for clip in enriched.clips:
            entry = clip.audio_pool_entry
            audio_source = clip.clip.get("audio_source")
            if not entry or entry.get("category") != "dialogue" or not isinstance(audio_source, dict):
                continue
            trim_start, trim_end = map(float, audio_source["trim_sub_range"])
            expected = enriched_arrangement.expected_text_for_clip(clip)
            actual = self._transcriber(_resolve_asset_path(enriched.run_dir, registry, clip.asset_key), trim_start, trim_end)
            similarity = token_set_similarity(expected=expected, actual=actual)
            issues = _detect_issues(
                clip.transcript_segments,
                trim_start,
                trim_end,
                actual,
                actual_tokens=tokenize(actual),
            )
            if similarity >= self._similarity_threshold and not issues:
                continue
            ordered = _ordered_issues(issues, current_total)
            proposal = _propose_nudge(
                ordered,
                trim_start,
                trim_end,
                entry,
                str((clip.clip.get("visual_source") or {}).get("role") or "primary"),
                iteration_idx=self._iteration_idx,
                max_iterations=self._max_iterations,
                min_nudge_sec=self._min_nudge_sec,
                max_nudge_sec=self._max_nudge_sec,
            )
            if proposal is None:
                findings.append(
                    enriched_arrangement.ReviewerFinding(
                        clip_order=clip.order,
                        clip_uuid=clip.uuid,
                        code="no_valid_nudge",
                        severity=enriched_arrangement.FindingSeverity.FLAG,
                        message=f"Unable to propose a valid trim nudge for issues={ordered or ['similarity_only']}",
                        reviewer=self.name,
                    )
                )
                continue
            candidate_total = round(
                current_total - _clip_duration(trim_start, trim_end) + _clip_duration(*proposal["trim_after"]),
                6,
            )
            min_total, max_total = TOTAL_DURATION_BOUNDS
            if candidate_total < min_total or candidate_total > max_total:
                findings.append(
                    enriched_arrangement.ReviewerFinding(
                        clip_order=clip.order,
                        clip_uuid=clip.uuid,
                        code="no_valid_nudge",
                        severity=enriched_arrangement.FindingSeverity.FLAG,
                        message=f"Proposed trim would violate total duration bounds: {candidate_total}",
                        reviewer=self.name,
                    )
                )
                continue
            current_total = candidate_total
            findings.append(
                enriched_arrangement.ReviewerFinding(
                    clip_order=clip.order,
                    clip_uuid=clip.uuid,
                    code=str(proposal["issue"]),
                    severity=enriched_arrangement.FindingSeverity.AUTO_FIX,
                    message=f"Adjust dialogue trim to resolve {proposal['issue']}",
                    reviewer=self.name,
                    proposed_patch={
                        "trim_after": proposal["trim_after"],
                        "trim_before": proposal["trim_before"],
                        "pool_id": str(entry["id"]),
                        "issues_resolved": [proposal["issue"]],
                        "similarity_before": round(similarity, 6),
                        "similarity_after": round(similarity, 6),
                        "source_transcript_text_before": _joined_text(segments_in_range(clip.transcript_segments, trim_start, trim_end)),
                        "source_transcript_text_after": _joined_text(segments_in_range(clip.transcript_segments, *proposal["trim_after"])),
                    },
                )
            )
        return findings


def _load_registry(path: Path) -> dict[str, Any]:
    return dict(load_registry(path))


def _resolve_asset_path(run_dir: Path, registry: dict[str, Any], asset_key: str) -> Path | str:
    entry = registry.get("assets", {}).get(asset_key, {})
    if not isinstance(entry, dict):
        raise ValueError(f"Asset registry entry {asset_key!r} is missing")
    if any(key in entry for key in ("url", "uri", "path", "source_path", "locator", "cache")):
        raise ValueError(
            f"Asset registry entry {asset_key!r} contains an unsupported locator; "
            "the audio reviewer accepts only host-materialized files"
        )
    file_value = entry.get("file")
    if not isinstance(file_value, str) or not file_value or not Path(file_value).is_absolute():
        raise ValueError(f"Asset registry entry {asset_key!r} is not host-materialized")
    path = Path(file_value).resolve()
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Host-materialized asset for {asset_key!r} is missing: {path}")
    return path


def _joined_text(segments: list[dict[str, Any]]) -> str | None:
    joined = " ".join(str(segment.get("text", "")).strip() for segment in segments if str(segment.get("text", "")).strip()).strip()
    return joined or None


def _arrangement_total_duration(enriched: enriched_arrangement.EnrichedArrangement) -> float:
    total = 0.0
    for clip in enriched.clips:
        audio_source = clip.clip.get("audio_source")
        if isinstance(audio_source, dict):
            total += _clip_duration(*map(float, audio_source["trim_sub_range"]))
        elif clip.visual_pool_entry:
            total += float(clip.visual_pool_entry["src_end"]) - float(clip.visual_pool_entry["src_start"])
    return round(total, 6)


def _clip_duration(start: float, end: float) -> float:
    return round(float(end) - float(start), 6)


def _nudge_amount(iteration_idx: int, max_iterations: int, min_nudge_sec: float, max_nudge_sec: float) -> float:
    if max_iterations <= 1:
        return round(max_nudge_sec, 6)
    ratio = min(max(float(iteration_idx) / float(max_iterations - 1), 0.0), 1.0)
    return round(min_nudge_sec + ((max_nudge_sec - min_nudge_sec) * ratio), 6)


def _ordered_issues(issues: list[str], total_duration: float) -> list[str]:
    shrink = [issue for issue in issues if issue == "boilerplate_start"]
    extend = [issue for issue in issues if issue.startswith("mid_sentence")]
    min_total, max_total = TOTAL_DURATION_BOUNDS
    if total_duration >= max_total - 1.0:
        return shrink + extend
    if total_duration <= min_total + 1.0:
        return extend + shrink
    return shrink + extend


def _detect_issues(
    transcript_segments: list[dict[str, Any]],
    trim_start: float,
    trim_end: float,
    actual: str,
    *,
    actual_tokens: list[str],
) -> list[str]:
    issues: set[str] = set()
    overlap = segments_in_range(transcript_segments, trim_start, trim_end)
    prev_text = next((str(segment.get("text", "")) for segment in reversed(transcript_segments) if float(segment.get("end", 0.0)) <= trim_start), None)
    next_text = next((str(segment.get("text", "")) for segment in transcript_segments if float(segment.get("start", 0.0)) >= trim_end), None)
    prev_last = tokenize(prev_text or "")
    next_first = tokenize(next_text or "")
    actual_first = actual_tokens[0] if actual_tokens else None
    last_overlap = str(overlap[-1].get("text", "")) if overlap else prev_text
    if actual_first in BOILERPLATE_TOKENS or tuple(actual_tokens[:2]) in BOILERPLATE_BIGRAMS:
        issues.add("boilerplate_start")
    if prev_last and actual_first and (prev_last[-1], actual_first) in BOILERPLATE_BIGRAMS:
        issues.add("boilerplate_start")
    if not _ends_sentence(prev_text):
        issues.add("mid_sentence_start")
    if next_first and not _ends_sentence(last_overlap):
        issues.add("mid_sentence_end")
    return sorted(issues)


def _ends_sentence(text: str | None) -> bool:
    stripped = (text or "").rstrip()
    return not stripped or stripped[-1] in SENTENCE_END_CHARS


def _propose_nudge(
    issues: list[str],
    trim_start: float,
    trim_end: float,
    entry: dict[str, Any],
    role: str,
    *,
    iteration_idx: int,
    max_iterations: int,
    min_nudge_sec: float,
    max_nudge_sec: float,
) -> dict[str, Any] | None:
    src_start, src_end = float(entry["src_start"]), float(entry["src_end"])
    min_duration, max_duration = ROLE_DURATION_BOUNDS[role]
    nudge = _nudge_amount(iteration_idx, max_iterations, min_nudge_sec, max_nudge_sec)
    for issue in issues:
        new_start, new_end = trim_start, trim_end
        if issue == "boilerplate_start":
            new_start = trim_start + nudge
        elif issue == "mid_sentence_start":
            new_start = trim_start - nudge
        elif issue == "mid_sentence_end":
            new_end = trim_end + nudge
        new_start = max(src_start - TRIM_BOUND_EXTENSION_SEC, min(new_start, src_end + TRIM_BOUND_EXTENSION_SEC))
        new_end = max(src_start - TRIM_BOUND_EXTENSION_SEC, min(new_end, src_end + TRIM_BOUND_EXTENSION_SEC))
        duration = _clip_duration(new_start, new_end)
        if new_end <= new_start or duration < min_duration or duration > max_duration:
            continue
        return {"issue": issue, "trim_before": [round(trim_start, 6), round(trim_end, 6)], "trim_after": [round(new_start, 6), round(new_end, 6)]}
    return None
