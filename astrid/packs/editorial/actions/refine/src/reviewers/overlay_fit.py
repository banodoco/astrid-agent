from __future__ import annotations

from astrid.pack_api import MAX_VISUAL_HOLD_RATIO, MIN_OVERLAY_COVERAGE_SEC
from astrid.packs.editorial.actions.refine.src.reviewers import Reviewer
from astrid.packs.editorial.shared.hype import enriched_arrangement


class OverlayFitReviewer(Reviewer):
    name = "overlay_fit"

    def review(self, enriched: enriched_arrangement.EnrichedArrangement) -> list[enriched_arrangement.ReviewerFinding]:
        findings: list[enriched_arrangement.ReviewerFinding] = []
        for clip in enriched.clips:
            visual_source = clip.clip.get("visual_source")
            if not isinstance(visual_source, dict) or visual_source.get("role") != "overlay":
                continue
            overlay_entry = clip.visual_pool_entry
            audio_source = clip.clip.get("audio_source") or {}
            if not overlay_entry or not isinstance(audio_source, dict):
                continue
            trim_range = audio_source.get("trim_sub_range") or []
            if len(trim_range) != 2:
                continue
            slot_duration = max(0.0, float(trim_range[1]) - float(trim_range[0]))
            overlay_source_duration = max(0.0, float(overlay_entry["src_end"]) - float(overlay_entry["src_start"]))
            overlay_duration = min(slot_duration, overlay_source_duration)
            hold_ratio = max(0.0, slot_duration - overlay_duration) / slot_duration if slot_duration > 0 else 0.0
            if overlay_duration >= MIN_OVERLAY_COVERAGE_SEC and hold_ratio <= MAX_VISUAL_HOLD_RATIO:
                continue
            findings.append(
                enriched_arrangement.ReviewerFinding(
                    clip_order=clip.order,
                    clip_uuid=clip.uuid,
                    code="overlay_insufficient_coverage",
                    severity=enriched_arrangement.FindingSeverity.FLAG,
                    message=(
                        f"Overlay covers {overlay_duration:.2f}s of {slot_duration:.2f}s "
                        f"(hold ratio {hold_ratio:.1%})"
                    ),
                    reviewer=self.name,
                )
            )
        return findings
