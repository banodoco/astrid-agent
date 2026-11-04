"""ffprobe media-probing helpers for the cut executor.

Extracted from ``run.py``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from astrid.pack_api import AstridError

_FFPROBE_VERBOSE = False


def parse_ffprobe_fps(value: Any, *, path: Path | str) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if not isinstance(value, str) or not value:
        raise AstridError(f"ffprobe did not return fps for {path}")
    if "/" in value:
        numerator_text, denominator_text = value.split("/", 1)
        numerator = float(numerator_text)
        denominator = float(denominator_text)
        if denominator == 0:
            raise AstridError(f"ffprobe returned invalid fps {value!r} for {path}")
        return numerator / denominator
    return float(value)


def probe_asset(path: Path | str) -> dict[str, Any]:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=codec_name,width,height,avg_frame_rate,r_frame_rate",
            "-of",
            "json",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AstridError(f"Invalid ffprobe JSON for {path}: {exc.msg}") from exc

    streams = payload.get("streams")
    if not isinstance(streams, list):
        raise AstridError(f"ffprobe did not return streams for {path}")
    stream = next((item for item in streams if isinstance(item, dict) and item.get("width") and item.get("height")), None)
    kind = "video"
    if stream is None:
        stream = next((item for item in streams if isinstance(item, dict) and isinstance(item.get("codec_name"), str)), None)
        kind = "audio"
    if stream is None:
        raise AstridError(f"ffprobe did not return a usable stream for {path}")
    format_info = payload.get("format")
    if not isinstance(format_info, dict):
        raise AstridError(f"ffprobe did not return format metadata for {path}")

    try:
        duration = float(format_info["duration"])
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
    except (KeyError, TypeError, ValueError) as exc:
        raise AstridError(f"ffprobe returned incomplete metadata for {path}") from exc

    codec = stream.get("codec_name")
    if not isinstance(codec, str) or not codec:
        raise AstridError(f"ffprobe did not return a codec for {path}")

    if kind == "video":
        fps_source = stream.get("avg_frame_rate")
        if fps_source in (None, "", "0/0"):
            fps_source = stream.get("r_frame_rate")
        fps = parse_ffprobe_fps(fps_source, path=path)
        resolution = f"{width}x{height}"
    else:
        fps = 0.0
        resolution = ""
    return {
        "type": kind,
        "duration": duration,
        "resolution": resolution,
        "fps": fps,
        "codec": codec,
    }


def probe_video_duration(video_path: Path) -> float:
    from astrid.pack_api import ffprobe_duration_seconds

    return ffprobe_duration_seconds(video_path)
