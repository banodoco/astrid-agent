"""Segment-map fusion for stream_content.segment_map."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astrid.pack_api import ffprobe_duration_seconds

SEGMENT_MAP_VERSION = 1
HOLDING_PHRASES = [
    "STARTING SOON",
    "STARTS SOON",
    "WE'LL BE BACK",
    "BE RIGHT BACK",
    "LUNCH BREAK",
    "COFFEE BREAK",
    "SCHEDULE",
    "AGENDA",
    "THANK YOU",
    "BREAK",
]


@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class TranscriptSegment:
    start: float
    end: float
    text: str
    speaker: str | None = None


def fold(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def fmt_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def load_json(path: Path | None) -> Any:
    if path is None:
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_transcript_segments(payload: Any) -> list[TranscriptSegment]:
    raw_segments = payload.get("segments") if isinstance(payload, dict) else payload
    if not isinstance(raw_segments, list):
        return []
    segments: list[TranscriptSegment] = []
    for item in raw_segments:
        if not isinstance(item, dict):
            continue
        try:
            start = float(item.get("start", 0.0))
            end = float(item.get("end", start))
        except (TypeError, ValueError):
            continue
        text = str(item.get("text") or "").strip()
        if end > start:
            speaker = item.get("speaker")
            segments.append(TranscriptSegment(start, end, text, str(speaker) if speaker else None))
    return segments


def normalize_words(payload: Any) -> list[Word]:
    words: list[Word] = []

    def add_word(item: Any) -> None:
        if not isinstance(item, dict):
            return
        try:
            start = float(item.get("start", 0.0))
            end = float(item.get("end", start))
        except (TypeError, ValueError):
            return
        text = str(item.get("word") or item.get("text") or "").strip()
        if end > start and text:
            words.append(Word(start, end, text))

    if isinstance(payload, dict):
        for word in payload.get("words") or []:
            add_word(word)
        for segment in payload.get("segments") or []:
            if isinstance(segment, dict):
                for word in segment.get("words") or []:
                    add_word(word)
    elif isinstance(payload, list):
        for segment in payload:
            if isinstance(segment, dict):
                for word in segment.get("words") or []:
                    add_word(word)

    if words:
        return sorted(words, key=lambda w: (w.start, w.end))

    # Whisper segment-level timestamps are enough for density fallback.
    for segment in normalize_transcript_segments(payload):
        tokens = [t for t in re.split(r"\s+", segment.text) if t]
        if not tokens:
            continue
        step = max((segment.end - segment.start) / len(tokens), 0.001)
        for index, token in enumerate(tokens):
            start = segment.start + index * step
            words.append(Word(start, min(segment.end, start + step), token))
    return sorted(words, key=lambda w: (w.start, w.end))


def normalize_scene_cuts(payload: Any) -> list[float]:
    scenes = payload.get("scenes") if isinstance(payload, dict) else payload
    if not isinstance(scenes, list):
        return []
    cuts: set[float] = set()
    for item in scenes:
        if not isinstance(item, dict):
            continue
        for key in ("start",):
            try:
                value = float(item.get(key, 0.0))
            except (TypeError, ValueError):
                continue
            if value > 0.05:
                cuts.add(round(value, 3))
    return sorted(cuts)


def coalesce_hit_intervals(hits: list[dict[str, Any]], threshold: float) -> list[dict[str, Any]]:
    """Merge adjacent OCR hit timestamps into contiguous intervals.

    This mirrors the lightweight event_talks holding-screen coalescing logic.
    """
    if not hits:
        return []
    sorted_hits = sorted(hits, key=lambda h: h["time"])
    intervals: list[dict[str, Any]] = []
    cur_start = float(sorted_hits[0]["time"])
    cur_end = cur_start
    cur_matched: set[str] = set(sorted_hits[0].get("matched") or [])
    cur_texts = [str(sorted_hits[0].get("text") or "").strip()]

    for hit in sorted_hits[1:]:
        hit_time = float(hit["time"])
        if hit_time - cur_end <= threshold * 1.5:
            cur_end = hit_time
            cur_matched.update(hit.get("matched") or [])
            cur_texts.append(str(hit.get("text") or "").strip())
        else:
            intervals.append(_ocr_interval(cur_start, cur_end, cur_matched, cur_texts))
            cur_start = cur_end = hit_time
            cur_matched = set(hit.get("matched") or [])
            cur_texts = [str(hit.get("text") or "").strip()]

    intervals.append(_ocr_interval(cur_start, cur_end, cur_matched, cur_texts))
    return intervals


def _ocr_interval(start: float, end: float, matched: set[str], texts: list[str]) -> dict[str, Any]:
    label_text = next((t for t in texts if t), "")
    return {
        "start": round(start, 3),
        "end": round(end, 3),
        "start_timecode": fmt_time(start),
        "end_timecode": fmt_time(end),
        "matched": sorted(matched),
        "text": label_text,
    }


def sample_holding_screens(video: Path, work_dir: Path, *, sample_sec: float = 10.0) -> dict[str, Any]:
    """Sample frames and OCR likely holding/title-card screens.

    Lifted from `video_editing.event_talks find-holding-screens`; kept local
    because that orchestrator module is guarded as a runtime entrypoint.
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    if os.environ.get("ASTRID_STREAM_CONTENT_SKIP_OCR"):
        return {
            "video": str(video),
            "sample_sec": sample_sec,
            "hits": [],
            "intervals": [],
            "note": "placeholder - ASTRID_STREAM_CONTENT_SKIP_OCR set",
        }
    if shutil.which("ffmpeg") is None or shutil.which("tesseract") is None:
        return {
            "video": str(video),
            "sample_sec": sample_sec,
            "hits": [],
            "intervals": [],
            "note": "placeholder - ffmpeg/tesseract unavailable",
        }

    duration = ffprobe_duration_seconds(video)
    folded_phrases = [fold(p) for p in HOLDING_PHRASES]
    frames_dir = work_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    hits: list[dict[str, Any]] = []
    t = 0.0
    while t <= duration + 1e-6:
        frame = frames_dir / f"frame_{int(round(t)):06d}.jpg"
        if not frame.is_file():
            subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-ss",
                    f"{t:.3f}",
                    "-i",
                    str(video),
                    "-frames:v",
                    "1",
                    str(frame),
                ],
                check=True,
            )
        text = subprocess.run(
            ["tesseract", str(frame), "stdout", "--psm", "6"],
            check=False,
            capture_output=True,
            text=True,
        ).stdout.strip()
        folded = fold(text)
        matched = [phrase for phrase, folded_phrase in zip(HOLDING_PHRASES, folded_phrases) if folded_phrase in folded]
        if matched:
            hits.append(
                {
                    "time": round(t, 3),
                    "timecode": fmt_time(t),
                    "matched": matched,
                    "text": text,
                    "frame": str(frame),
                }
            )
        t += sample_sec
    return {
        "video": str(video),
        "sample_sec": sample_sec,
        "phrases": HOLDING_PHRASES,
        "hits": hits,
        "intervals": coalesce_hit_intervals(hits, sample_sec),
    }


def _overlap(start: float, end: float, item_start: float, item_end: float) -> float:
    return max(0.0, min(end, item_end) - max(start, item_start))


def _interval_for_time(intervals: list[dict[str, Any]], start: float, end: float, sample_sec: float) -> dict[str, Any] | None:
    best: dict[str, Any] | None = None
    best_overlap = 0.0
    for interval in intervals:
        istart = float(interval.get("start", 0.0))
        iend = min(float(interval.get("end", istart)) + sample_sec, end + sample_sec)
        overlap = _overlap(start, end, istart, iend)
        if overlap > best_overlap:
            best = interval
            best_overlap = overlap
    return best if best_overlap > 0 else None


def _first_transcript_label(segments: list[TranscriptSegment], start: float, end: float) -> str:
    for segment in segments:
        if segment.text and _overlap(start, end, segment.start, segment.end) > 0:
            return re.sub(r"\s+", " ", segment.text).strip()[:140]
    return ""


def _scene_cut_count(cuts: list[float], start: float, end: float) -> int:
    return sum(1 for cut in cuts if start <= cut < end)


def build_segment_map(
    *,
    video: Path,
    transcript_path: Path | None = None,
    scenes_path: Path | None = None,
    ocr_work_dir: Path | None = None,
) -> dict[str, Any]:
    duration = ffprobe_duration_seconds(video)
    transcript_payload = load_json(transcript_path)
    scene_payload = load_json(scenes_path)
    words = normalize_words(transcript_payload)
    transcript_segments = normalize_transcript_segments(transcript_payload)
    cuts = normalize_scene_cuts(scene_payload)
    if transcript_segments:
        duration = max(duration, max(segment.end for segment in transcript_segments))
    if words:
        duration = max(duration, max(word.end for word in words))

    ocr = sample_holding_screens(video, ocr_work_dir or (video.parent / ".stream_content_ocr"))
    sample_sec = float(ocr.get("sample_sec") or 10.0)
    ocr_intervals = list(ocr.get("intervals") or [])

    bins: list[dict[str, Any]] = []
    word_index = 0
    t = 0.0
    while t < duration - 1e-6:
        start = round(t, 3)
        end = round(min(duration, t + 1.0), 3)
        win = max(end - start, 0.001)
        while word_index < len(words) and words[word_index].end <= start:
            word_index += 1
        word_count = 0
        scan_index = word_index
        while scan_index < len(words) and words[scan_index].start < end:
            word = words[scan_index]
            if _overlap(start, end, word.start, word.end) > 0:
                word_count += 1
            scan_index += 1
        scene_cuts = _scene_cut_count(cuts, start, end)
        ocr_interval = _interval_for_time(ocr_intervals, start, end, sample_sec)

        if ocr_interval is not None:
            kind = "holding"
            label = _label_from_ocr(ocr_interval)
            confidence = 0.86
        elif transcript_payload is not None:
            words_per_sec = word_count / win
            if word_count == 0:
                kind = "screening" if scene_cuts >= 2 else "dead_air"
                label = "Visual screening" if kind == "screening" else "Low/no speech"
                confidence = 0.66 if kind == "screening" else 0.72
            elif words_per_sec < 0.25 and scene_cuts >= 2:
                kind = "screening"
                label = _first_transcript_label(transcript_segments, start, end) or "Visual screening"
                confidence = 0.62
            else:
                kind = "content"
                label = _first_transcript_label(transcript_segments, start, end)
                confidence = min(0.95, 0.68 + min(words_per_sec / 3.0, 0.25))
        else:
            kind = "screening" if scene_cuts >= 2 else "content"
            label = "Visual screening" if kind == "screening" else "Content"
            confidence = 0.46 if kind == "content" else 0.58

        bins.append(
            {
                "start": start,
                "end": end,
                "kind": kind,
                "label": label,
                "confidence": round(confidence, 3),
                "signals": {
                    "word_count": word_count,
                    "words_per_sec": round(word_count / win, 3),
                    "scene_cuts": scene_cuts,
                    "ocr_hits": 1 if ocr_interval is not None else 0,
                    "ocr_label": _label_from_ocr(ocr_interval) if ocr_interval else "",
                },
            }
        )
        t = end

    return {
        "version": SEGMENT_MAP_VERSION,
        "source": str(video),
        "duration": round(duration, 3),
        "segments": merge_segments(bins, transcript_segments),
    }


def _label_from_ocr(interval: dict[str, Any] | None) -> str:
    if not interval:
        return ""
    text = str(interval.get("text") or "").strip()
    if text:
        return re.sub(r"\s+", " ", text)[:140]
    matched = interval.get("matched") or []
    return ", ".join(str(item).title() for item in matched) if matched else "Holding screen"


def merge_segments(bins: list[dict[str, Any]], transcript_segments: list[TranscriptSegment]) -> list[dict[str, Any]]:
    if not bins:
        return []
    merged: list[dict[str, Any]] = []
    current = dict(bins[0])
    current["signals"] = dict(current["signals"])
    confidences = [float(current["confidence"])]

    for item in bins[1:]:
        if item["kind"] == current["kind"] and abs(float(item["start"]) - float(current["end"])) < 0.01:
            current["end"] = item["end"]
            if not current.get("label") or current["label"] in {"Low/no speech", "Content", "Visual screening"}:
                current["label"] = item.get("label") or current.get("label", "")
            for key in ("word_count", "scene_cuts", "ocr_hits"):
                current["signals"][key] = int(current["signals"].get(key, 0)) + int(item["signals"].get(key, 0))
            current["signals"]["words_per_sec"] = round(
                current["signals"]["word_count"] / max(float(current["end"]) - float(current["start"]), 0.001),
                3,
            )
            if item["signals"].get("ocr_label") and not current["signals"].get("ocr_label"):
                current["signals"]["ocr_label"] = item["signals"]["ocr_label"]
            confidences.append(float(item["confidence"]))
            current["confidence"] = round(sum(confidences) / len(confidences), 3)
        else:
            _finalize_label(current, transcript_segments)
            merged.append(current)
            current = dict(item)
            current["signals"] = dict(current["signals"])
            confidences = [float(current["confidence"])]

    _finalize_label(current, transcript_segments)
    merged.append(current)
    return merged


def _finalize_label(segment: dict[str, Any], transcript_segments: list[TranscriptSegment]) -> None:
    if segment.get("kind") in {"content", "screening"}:
        label = _first_transcript_label(transcript_segments, float(segment["start"]), float(segment["end"]))
        if label:
            segment["label"] = label
    if not segment.get("label"):
        segment["label"] = str(segment.get("kind", "segment")).replace("_", " ").title()


def write_segment_map(payload: dict[str, Any], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
