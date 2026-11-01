"""Plan and sync narration transcripts from clip-local word anchors."""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from astrid.sdk import timeline_intent as intent
from astrid.sdk.media_transcript import (
    NARRATION_SOURCE,
    build_transcript,
    read_transcript,
    write_transcript,
)

_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")


def plan_narration_transcripts(tl: Any) -> list[dict[str, Any]]:
    """Read only. Clip words are local; source time is ``from + local * speed``."""
    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    skipped: list[dict[str, Any]] = []
    for clip in tl.clips():
        data = clip.data
        segment = intent.line(data)
        if clip.track != "vo" and not segment:
            continue
        if hasattr(tl, "bundle"):
            shot = (tl.bundle.get("shots") or {}).get(clip.shot_id) or {}
            assets = (((shot.get("internal_timeline") or {}).get("registry") or {}).get("assets") or {})
        else:  # small fixture timelines can expose only the registry accessor
            assets = tl._registry(clip.shot_id)
        asset = assets.get(clip.asset or "") or {}
        media_id = asset.get("media_id") or asset.get("content_sha256")
        label = f"clip {clip.id}"
        if not isinstance(media_id, str) or not _SHA.fullmatch(media_id):
            skipped.append({"label": label, "status": "skipped", "reason": "missing sha256 media id"})
            continue
        words = intent.words(data)
        if not words:
            skipped.append({"label": label, "status": "skipped", "reason": "no words"})
            continue
        try:
            source_start = float(data.get("from") or 0)
            speed = float(data.get("speed") or 1)
            if source_start < 0 or speed <= 0:
                raise ValueError("invalid source window or speed")
            timed = [(round(source_start + float(w[0]) * speed, 6),
                      round(source_start + float(w[1]) * speed, 6), str(w[2])) for w in words]
            if any(a < 0 or b < a or not text.strip() for a, b, text in timed):
                raise ValueError("invalid timed words")
        except (TypeError, ValueError, IndexError) as exc:
            skipped.append({"label": label, "status": "skipped", "reason": str(exc)})
            continue
        groups[media_id].append({"segment": segment or clip.id, "text": intent.line_text(data),
                                 "words": timed, "clip": clip.id})
    plan: list[dict[str, Any]] = []
    for media_id, pieces in sorted(groups.items()):
        segments = {piece["segment"] for piece in pieces}
        if len(segments) != 1:
            plan.append({"label": media_id, "status": "skipped", "reason": "conflicting duplicates of the same media: different line segments"})
            continue
        declared = {p["text"] for p in pieces if p["text"] is not None}
        if len(declared) > 1:
            plan.append({"label": media_id, "status": "skipped", "reason": "conflicting duplicates of the same media: different line text"})
            continue
        by_start: dict[float, tuple[float, float, str]] = {}
        conflict = False
        for piece in pieces:
            for word in piece["words"]:
                previous = by_start.setdefault(word[0], word)
                if previous != word:
                    conflict = True
        if conflict:
            plan.append({"label": media_id, "status": "skipped", "reason": "conflicting duplicates of the same media: word timing/text"})
            continue
        words = sorted(by_start.values())
        segment = next(iter(segments))
        text = next(iter(declared)) if declared else " ".join(w[2] for w in words)
        plan.append({"label": media_id, "media_id": media_id, "status": "planned",
                     "clips": [p["clip"] for p in pieces],
                     "content": build_transcript(media_id, segment, text, words)})
    return plan + skipped


def sync_narration_transcripts(tl: Any, client: Any, project: str, *, dry_run: bool = False) -> list[dict[str, Any]]:
    report = []
    for item in plan_narration_transcripts(tl):
        row = {k: v for k, v in item.items() if k != "content"}
        if item["status"] == "planned":
            media_id, content = item["media_id"], item["content"]
            try:
                if dry_run:
                    current = read_transcript(client, project, media_id, NARRATION_SOURCE)
                    row["status"] = "unchanged" if current and current.get("content") == content else (
                        "would update" if current else "would create")
                else:
                    row["status"] = write_transcript(client, project, media_id, NARRATION_SOURCE, content)
            except Exception as exc:  # noqa: BLE001 - containment: a document failure must never undo a published timeline
                row["status"], row["reason"] = "skipped", str(exc)
        report.append(row)
    return report
