"""Plan and sync narration transcripts from clip-local word anchors."""
from __future__ import annotations

import copy
import re
from collections import defaultdict
from typing import Any

from astrid.sdk import timeline_intent as intent
from astrid.sdk.media_transcript import (
    NARRATION_SOURCE,
    _read_transcript_result,
    _write_transcript_result,
    build_transcript,
)

_SHA = re.compile(r"sha256:[0-9a-f]{64}\Z")
CONVERTED_KEY = "transcriptsConverted"


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
                    current, pin = _read_transcript_result(client, project, media_id, NARRATION_SOURCE)
                    row["status"] = "unchanged" if current and current.get("content") == content else (
                        "would update" if current else "would create")
                    if pin is not None:
                        row["pin"] = pin
                else:
                    row["status"], pin = _write_transcript_result(
                        client, project, media_id, NARRATION_SOURCE, content,
                    )
                    if pin is not None:
                        row["pin"] = pin
            except Exception as exc:  # noqa: BLE001 - containment: a document failure must never undo a published timeline
                row["status"], row["reason"] = "skipped", str(exc)
        report.append(row)
    return report


def is_converted(bundle: Any) -> bool:
    """True once ``timelines transcripts refresh --convert`` has moved every clip's words into media
    transcripts: the stored transcripts are then the only copy, so the publish-time narration sync is off."""
    parent = bundle.get("parent") if hasattr(bundle, "get") else None
    config = parent.get("config") if hasattr(parent, "get") else None
    block = config.get("bundle") if hasattr(config, "get") else None
    mark = block.get(CONVERTED_KEY) if hasattr(block, "get") else None
    return bool(mark and mark.get("source") == NARRATION_SOURCE) if hasattr(mark, "get") else False


def _mark_converted(bundle: dict[str, Any]) -> None:
    config = bundle.setdefault("parent", {}).setdefault("config", {})
    config.setdefault("bundle", {})[CONVERTED_KEY] = {"source": NARRATION_SOURCE}


def _projection(bundle: Any, transcripts: Any) -> dict[str, list[tuple[Any, ...]]]:
    from astrid.sdk.timeline.words import project_words

    rows: dict[str, list[tuple[Any, ...]]] = defaultdict(list)
    for row in project_words(bundle, transcripts, "parent"):
        rows[row["clip_id"]].append((row["occurrence"], row["segment"], row["text"],
                                     round(row["timeline_start"], 3), round(row["timeline_end"], 3)))
    return {clip: sorted(items) for clip, items in rows.items()}


def plan_conversion(tl: Any) -> list[dict[str, Any]]:
    """Read only. What ``refresh --convert`` would do: each media whose clip words become a media transcript.

    Starts from ``plan_narration_transcripts`` and keeps a media only when the words every one of its clips
    would show through the stored transcript are identical (text, line, timeline start and end, each
    placement) to the clip words shown now. A media that fails the check is ``skipped`` with the reason and
    its clips keep their words."""
    plan = plan_narration_transcripts(tl)
    planned = [item for item in plan if item["status"] == "planned"]
    if not planned:
        return plan
    candidate = {item["media_id"]: {NARRATION_SOURCE: {"document_id": None, "version": None, "content": item["content"]}}
                 for item in planned}
    before = _projection(tl.bundle, {})
    stripped = copy.deepcopy(tl.bundle)
    from astrid.sdk.timeline.words import _holder_clips, _occurrences

    clip_ids = {clip for item in planned for clip in item["clips"]}
    for occ in _occurrences(stripped, "parent"):
        for clip in _holder_clips(occ["holder"]):
            if str(clip.get("id")) in clip_ids:
                intent.drop_words(clip)
    after = _projection(stripped, candidate)
    for item in planned:
        if any(before.get(clip) != after.get(clip) for clip in item["clips"]):
            item["status"] = "skipped"
            item["reason"] = ("the stored transcript would show different words than the clips do "
                              "(a clip word outside its media window, or overlapping takes of one file)")
    return plan


def convert_narration_transcripts(tl: Any, client: Any, project: str, *, dry_run: bool = False) -> list[dict[str, Any]]:
    """Convert clip words into ``astrid.narration`` media transcripts, then drop the words from those clips.

    Each converted media's transcript is written, read back and compared before any clip loses its words, so
    a failed write leaves the clips untouched. The clips keep ``segment``, ``text`` and ``gap_after_s``. The
    timeline is marked converted (the publish-time narration sync stops) only when no clip still holds words.
    Nothing is published here: the caller commits ``tl`` once. ``dry_run`` writes and changes nothing."""
    report: list[dict[str, Any]] = []
    done: list[str] = []
    for item in plan_conversion(tl):
        row = {k: v for k, v in item.items() if k != "content"}
        if item["status"] != "planned":
            report.append(row)
            continue
        media_id, content = item["media_id"], item["content"]
        try:
            current, _pin = _read_transcript_result(client, project, media_id, NARRATION_SOURCE)
            same = bool(current and current.get("content") == content)
            if dry_run:
                row["status"] = "would convert" + ("" if same else " (writes the transcript)")
                report.append(row)
                continue
            _write_transcript_result(client, project, media_id, NARRATION_SOURCE, content)
            stored, _pin = _read_transcript_result(client, project, media_id, NARRATION_SOURCE)
            if not stored or stored.get("content") != content:
                raise ValueError("the stored transcript does not read back as written")
        except Exception as exc:  # noqa: BLE001 - keep this media's clip words; the others still convert
            row["status"], row["reason"] = "skipped", f"transcript not stored: {exc}"
            report.append(row)
            continue
        row["status"] = "converted"
        report.append(row)
        done.extend(item["clips"])
    if dry_run:
        return report
    from astrid.sdk.timeline.words import _holder_clips, _occurrences

    keep = set(done)
    for occ in _occurrences(tl.bundle, "parent"):
        for clip in _holder_clips(occ["holder"]):
            if str(clip.get("id")) in keep:
                intent.drop_words(clip)
    tl._mcache = None
    if done and not any(intent.words(c.data) for c in tl.clips()):
        _mark_converted(tl.bundle)
    return report
