"""Render review: a contact sheet sampled from the rendered video, with loudness and silences.

The timeline contact sheet shows the timeline's cards. This module reads the
rendered file itself: one frame every N seconds on a contact sheet, plus the
integrated loudness, true peak and silences of its audio, all through ffmpeg
(ebur128 and silencedetect). It writes render-contact.png and render-review.json
and returns the finding lines an agent reads.

ffmpeg here has no drawtext, so frame labels are drawn with Pillow.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Sequence

DEFAULT_EVERY_S = 5.0
MAX_SAMPLES = 120
TILE_WIDTH = 320
SILENCE_NOISE_DB = -50.0
SILENCE_MIN_S = 1.0
TRUE_PEAK_CEILING_DB = -1.0
CONTACT_NAME = "render-contact.png"
REVIEW_NAME = "render-review.json"


class RenderReviewError(RuntimeError):
    """The rendered video could not be sampled or measured."""


def sample_times(duration: float, every: float) -> list[float]:
    """Frame times at 0, every, 2*every ... before the end, capped at MAX_SAMPLES."""
    if not every or every <= 0:
        raise ValueError("every must be a positive number of seconds")
    if duration <= 0:
        return [0.0]
    times: list[float] = []
    step = 0
    while len(times) < MAX_SAMPLES:
        t = round(step * every, 3)
        if t >= duration - 1e-6:
            break
        times.append(t)
        step += 1
    return times or [0.0]


_DURATION = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_AUDIO_STREAM = re.compile(r"Stream #\d+:\d+.*?:\s*Audio:")


def probe(video: Path, ffmpeg: str) -> tuple[float, bool]:
    """Return (duration seconds, has audio) from ffmpeg's stream banner."""
    proc = subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-i", str(video)], capture_output=True, text=True)
    banner = proc.stderr or ""
    match = _DURATION.search(banner)
    if not match:
        raise RenderReviewError(f"cannot read the duration of {video.name}")
    hours, minutes, seconds = match.groups()
    duration = int(hours) * 3600 + int(minutes) * 60 + float(seconds)
    return duration, bool(_AUDIO_STREAM.search(banner))


def parse_loudness(text: str) -> dict[str, Any]:
    """Integrated loudness, LRA and true peak from ebur128's summary block."""
    out: dict[str, Any] = {"integrated_lufs": None, "lra_lu": None, "true_peak_dbtp": None}
    integrated = re.search(r"Integrated loudness:.*?I:\s+(-?\d+(?:\.\d+)?)\s+LUFS", text, re.S)
    if integrated:
        out["integrated_lufs"] = float(integrated.group(1))
    lra = re.search(r"Loudness range:.*?LRA:\s+(\d+(?:\.\d+)?)\s+LU", text, re.S)
    if lra:
        out["lra_lu"] = float(lra.group(1))
    peak = re.search(r"True peak:.*?Peak:\s+(-?\d+(?:\.\d+)?)\s+dBFS", text, re.S)
    if peak:
        out["true_peak_dbtp"] = float(peak.group(1))
    return out


_SILENCE_START = re.compile(r"silence_start:\s*(-?\d+(?:\.\d+)?)")
_SILENCE_END = re.compile(r"silence_end:\s*(-?\d+(?:\.\d+)?)\s*\|\s*silence_duration:\s*(\d+(?:\.\d+)?)")


def parse_silences(text: str) -> list[dict[str, float]]:
    """Pair silencedetect's start and end lines, in order."""
    starts = [float(m.group(1)) for m in _SILENCE_START.finditer(text)]
    ends = [(float(m.group(1)), float(m.group(2))) for m in _SILENCE_END.finditer(text)]
    silences = []
    for index, (end, duration) in enumerate(ends):
        start = starts[index] if index < len(starts) else max(0.0, end - duration)
        silences.append({"start": round(max(0.0, start), 3), "end": round(end, 3), "duration": round(duration, 3)})
    if len(starts) > len(ends):  # silence still open at end of file
        start = starts[-1]
        silences.append({"start": round(start, 3), "end": None, "duration": None})
    return silences


def measure_audio(video: Path, ffmpeg: str) -> dict[str, Any]:
    """Loudness and silences of the whole file's first audio stream."""
    audio_filter = f"ebur128=peak=true,silencedetect=noise={SILENCE_NOISE_DB}dB:d={SILENCE_MIN_S}"
    proc = subprocess.run(
        [ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-i", str(video), "-map", "0:a:0", "-af", audio_filter, "-f", "null", "-"],
        capture_output=True, text=True,
    )
    if proc.returncode:
        raise RenderReviewError(f"audio measurement failed: {(proc.stderr or '')[-800:]}")
    result = parse_loudness(proc.stderr)
    result["silences"] = parse_silences(proc.stderr)
    return result


def sample_frames(video: Path, times: Sequence[float], frames_dir: Path, ffmpeg: str) -> list[Path]:
    frames_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for index, t in enumerate(times):
        target = frames_dir / f"frame-{index:03d}.png"
        proc = subprocess.run(
            [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-ss", f"{t:.3f}", "-i", str(video),
             "-frames:v", "1", "-vf", f"scale={TILE_WIDTH}:-2", "-y", str(target)],
            capture_output=True, text=True,
        )
        if proc.returncode or not target.is_file():
            raise RenderReviewError(f"frame at {t:.2f} s failed: {(proc.stderr or '')[-400:]}")
        paths.append(target)
    return paths


def compose_contact(frames: Sequence[Path], times: Sequence[float], out_path: Path, *, columns: int = 6) -> Path:
    """One PNG grid of the sampled frames, each labelled with its time."""
    from PIL import Image, ImageDraw

    tiles = [Image.open(path).convert("RGB") for path in frames]
    tile_w = max(tile.width for tile in tiles)
    tile_h = max(tile.height for tile in tiles)
    label_h = 18
    gutter = 8
    columns = max(1, min(columns, len(tiles)))
    rows = -(-len(tiles) // columns)
    sheet = Image.new("RGB", (columns * (tile_w + gutter) + gutter, rows * (tile_h + label_h + gutter) + gutter), (17, 24, 39))
    draw = ImageDraw.Draw(sheet)
    for index, (tile, t) in enumerate(zip(tiles, times)):
        x = gutter + (index % columns) * (tile_w + gutter)
        y = gutter + (index // columns) * (tile_h + label_h + gutter)
        draw.text((x, y), f"{t:.1f} s", fill=(140, 224, 208))
        sheet.paste(tile, (x, y + label_h))
    sheet.save(out_path)
    return out_path


def findings(review: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    lufs = review.get("integrated_lufs")
    peak = review.get("true_peak_dbtp")
    if lufs is not None:
        lra = review.get("lra_lu")
        lra_text = f", LRA {lra:.1f} LU" if lra is not None else ""
        lines.append(f"LOUDNESS integrated {lufs:.1f} LUFS, true peak {peak:.1f} dBTP{lra_text}")
    else:
        lines.append("LOUDNESS no audio stream in the render")
    if peak is not None and peak > TRUE_PEAK_CEILING_DB:
        lines.append(f"PEAK true peak {peak:.1f} dBTP is above {TRUE_PEAK_CEILING_DB:.0f} dBTP")
    for silence in review.get("silences") or []:
        if silence.get("end") is None:
            lines.append(f"SILENCE {silence['start']:.2f}s to end of file (below {SILENCE_NOISE_DB:.0f} dB)")
        else:
            lines.append(f"SILENCE {silence['start']:.2f}–{silence['end']:.2f}s ({silence['duration']:.2f} s, below {SILENCE_NOISE_DB:.0f} dB)")
    return lines[:24]


def review_render(video: Path, out_root: Path, *, every: float = DEFAULT_EVERY_S, ffmpeg: str | None = None, clock=None) -> dict[str, Any]:
    """Sample the render every `every` seconds, measure its audio, and write the review files."""
    binary = ffmpeg or shutil.which("ffmpeg")
    if not binary:
        raise RenderReviewError("ffmpeg is required for the render review; install ffmpeg")
    video = Path(video)
    out_root = Path(out_root)
    out_root.mkdir(parents=True, exist_ok=True)
    duration, has_audio = probe(video, binary)
    times = sample_times(duration, every)
    frames_dir = out_root / "render-frames"
    try:
        frames = sample_frames(video, times, frames_dir, binary)
        timeline_times = [clock.timeline_seconds(t) for t in times] if clock else times
        contact = compose_contact(frames, timeline_times, out_root / CONTACT_NAME)
    finally:
        shutil.rmtree(frames_dir, ignore_errors=True)
    audio = measure_audio(video, binary) if has_audio else {"integrated_lufs": None, "lra_lu": None, "true_peak_dbtp": None, "silences": []}
    if clock:
        for silence in audio["silences"]:
            silence["start"] = clock.timeline_seconds(silence["start"])
            if silence["end"] is not None:
                silence["end"] = clock.timeline_seconds(silence["end"])
    review: dict[str, Any] = {
        "video": video.name,
        "duration_s": round(duration, 3),
        "every_s": float(every),
        "sample_times_s": timeline_times,
        "contact": contact.name,
        **{key: audio[key] for key in ("integrated_lufs", "lra_lu", "true_peak_dbtp", "silences")},
    }
    review["findings"] = findings(review)
    (out_root / REVIEW_NAME).write_text(json.dumps(review, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return review


__all__ = ["DEFAULT_EVERY_S", "RenderReviewError", "findings", "parse_loudness", "parse_silences", "review_render", "sample_times"]
