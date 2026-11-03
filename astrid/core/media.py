"""Canonical shared media-probing helpers.

This is the canonical location for shared media utilities.
Any callers outside ``astrid/core/`` should import from here.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable

from astrid.core.path_utils import has_url_scheme
from astrid.core.subprocess_env import build_child_subprocess_env

Runner = Callable[..., subprocess.CompletedProcess[str]]


def load_rgba(path: str | Path):
    """Load a local image as an owned uint8 RGBA array."""
    import numpy as np
    from PIL import Image

    with Image.open(path) as image:
        return np.asarray(image.convert("RGBA"), dtype=np.uint8).copy()


def outline_integrity(sprite: Any) -> dict[str, Any]:
    """Measure the dark outline fraction along exposed alpha boundary cells."""
    import numpy as np

    alpha = sprite[..., 3] >= 128
    surrounded = np.pad(alpha, 1, constant_values=False)
    boundary = alpha & ~(
        surrounded[:-2, 1:-1] & surrounded[2:, 1:-1]
        & surrounded[1:-1, :-2] & surrounded[1:-1, 2:]
    )
    rgb = sprite[..., :3].astype(np.float64)
    dark = (
        0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]
    ) < 100
    count = int(boundary.sum())
    dark_count = int((boundary & dark).sum())
    return {
        "fraction": round(float(dark_count / count), 4) if count else 0.0,
        "dark_rule": "luminance < 100",
        "dark_boundary_cells": dark_count,
        "boundary_cells": count,
    }


# ---------------------------------------------------------------------------
# MediaProbe – structured ffprobe metadata
# ---------------------------------------------------------------------------


@dataclass
class MediaProbe:
    """Best-effort media metadata extracted via ffprobe.

    All fields are ``None`` when ffprobe is unavailable or fails.
    """

    duration_seconds: float | None = None
    fps: float | None = None
    resolution: str | None = None
    width: int | None = None
    height: int | None = None

    _raw: dict[str, Any] = field(default_factory=dict, repr=False)

    # Exact/profile fields used by rendering.
    fps_rational: tuple[int, int] | None = None
    time_base: tuple[int, int] | None = None
    video_codec: str | None = None
    video_profile: str | None = None
    video_level: str | None = None
    pixel_format: str | None = None
    audio_codec: str | None = None
    audio_sample_rate: int | None = None
    audio_channel_layout: str | None = None
    container: str | None = None
    format_name: str | None = None
    duration_rational: tuple[int, int] | None = None
    video_stream_present: bool | None = None
    audio_stream_present: bool | None = None
    audio_channels: int | None = None
    frames: int | None = None

    @property
    def has_video_stream(self) -> bool:
        if self.video_stream_present is not None:
            return self.video_stream_present
        return self.video_codec is not None or (
            self.width is not None and self.height is not None
        )

    @property
    def has_audio_stream(self) -> bool:
        if self.audio_stream_present is not None:
            return self.audio_stream_present
        return self.audio_codec is not None


class MediaProbeError(RuntimeError):
    """Raised when a fail-closed media probe cannot produce metadata."""


def require_runtime_materialized_file(
    value: str | Path,
    *,
    label: str = "media input",
) -> Path:
    """Validate the only file form accepted by live media executors.

    The neutral runtime owns URL resolution and materializes verified bytes
    before invoking a pack.  Pack code therefore receives an absolute,
    existing regular file and must never download, cache, or resolve a URL.
    """

    text = str(value).strip()
    if not text or has_url_scheme(text):
        raise ValueError(f"{label} must be an absolute runtime-materialized file, not a URL")
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise ValueError(f"{label} must be an absolute runtime-materialized file: {text!r}")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"{label} runtime materialization is unavailable: {path}") from exc
    if not resolved.is_file():
        raise ValueError(f"{label} must be an existing runtime-materialized file: {resolved}")
    return resolved


def _positive_rational(value: Any) -> tuple[int, int] | None:
    """Parse an ffprobe rational without routing through a float."""

    if not isinstance(value, str) or "/" not in value:
        return None
    numerator_text, denominator_text = value.split("/", 1)
    try:
        numerator = int(numerator_text)
        denominator = int(denominator_text)
    except (TypeError, ValueError):
        return None
    if numerator <= 0 or denominator <= 0:
        return None
    rational = Fraction(numerator, denominator)
    return rational.numerator, rational.denominator


def _duration_rational(value: Any) -> tuple[int, int] | None:
    if value is None:
        return None
    try:
        rational = Fraction(str(value))
    except (ValueError, ZeroDivisionError):
        return None
    if rational < 0:
        return None
    return rational.numerator, rational.denominator


def _nonempty_string(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def _int_or_none(value: Any, *, minimum: int = 0) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= minimum else None


def _container_from_format(format_name: str | None, file_path: str | Path) -> str | None:
    if format_name is None:
        return None
    names = {part.strip().lower() for part in format_name.split(",") if part.strip()}
    suffix = Path(file_path).suffix.lower().lstrip(".")
    if suffix in names:
        return suffix
    if "mp4" in names:
        return "mp4"
    if "webm" in names:
        return "webm"
    if "matroska" in names:
        return "matroska"
    if "mov" in names:
        return "mov"
    return sorted(names)[0] if names else None


def _parse_ffprobe_payload(data: dict[str, Any], file_path: str | Path) -> MediaProbe:
    probe = MediaProbe(_raw=data)

    fmt = data.get("format", {})
    if not isinstance(fmt, Mapping):
        fmt = {}
    probe.format_name = _nonempty_string(fmt.get("format_name"))
    probe.container = _container_from_format(probe.format_name, file_path)

    duration_value = fmt.get("duration")
    probe.duration_rational = _duration_rational(duration_value)
    if probe.duration_rational is not None:
        probe.duration_seconds = float(Fraction(*probe.duration_rational))

    streams = data.get("streams", [])
    if not isinstance(streams, list):
        streams = []

    video_stream: Mapping[str, Any] | None = None
    audio_stream: Mapping[str, Any] | None = None
    for stream in streams:
        if not isinstance(stream, Mapping):
            continue
        stream_type = stream.get("codec_type")
        if stream_type == "video" and video_stream is None:
            disposition = stream.get("disposition")
            attached_picture = (
                isinstance(disposition, Mapping)
                and disposition.get("attached_pic") in {1, True, "1"}
            )
            if not attached_picture:
                video_stream = stream
        elif stream_type == "audio" and audio_stream is None:
            audio_stream = stream

    probe.video_stream_present = video_stream is not None
    probe.audio_stream_present = audio_stream is not None

    if video_stream is not None:
        probe.width = _int_or_none(video_stream.get("width"), minimum=1)
        probe.height = _int_or_none(video_stream.get("height"), minimum=1)
        if probe.width is not None and probe.height is not None:
            probe.resolution = f"{probe.width}x{probe.height}"

        fps_value = video_stream.get("avg_frame_rate")
        fps_rational = _positive_rational(fps_value)
        if fps_rational is None:
            fps_rational = _positive_rational(video_stream.get("r_frame_rate"))
        probe.fps_rational = fps_rational
        if fps_rational is not None:
            probe.fps = float(Fraction(*fps_rational))

        probe.time_base = _positive_rational(video_stream.get("time_base"))
        probe.video_codec = _nonempty_string(video_stream.get("codec_name"))
        probe.video_profile = _nonempty_string(video_stream.get("profile"))
        probe.frames = _int_or_none(video_stream.get("nb_frames"), minimum=1)
        level = video_stream.get("level")
        if level is not None and str(level).strip() not in {"", "-99"}:
            probe.video_level = str(level).strip()
        probe.pixel_format = _nonempty_string(video_stream.get("pix_fmt"))

        # Some containers omit format.duration while exposing stream.duration.
        if probe.duration_rational is None:
            probe.duration_rational = _duration_rational(video_stream.get("duration"))
            if probe.duration_rational is not None:
                probe.duration_seconds = float(Fraction(*probe.duration_rational))

    if audio_stream is not None:
        probe.audio_codec = _nonempty_string(audio_stream.get("codec_name"))
        probe.audio_sample_rate = _int_or_none(audio_stream.get("sample_rate"), minimum=1)
        probe.audio_channel_layout = _nonempty_string(
            audio_stream.get("channel_layout")
        )
        probe.audio_channels = _int_or_none(audio_stream.get("channels"), minimum=1)
        if probe.duration_rational is None:
            probe.duration_rational = _duration_rational(audio_stream.get("duration"))
            if probe.duration_rational is not None:
                probe.duration_seconds = float(Fraction(*probe.duration_rational))

    return probe


def _ffprobe_metadata(
    file_path: str | Path,
    *,
    timeout: float,
    strict: bool,
) -> MediaProbe:
    ffprobe_exe = shutil.which("ffprobe")
    if ffprobe_exe is None:
        if strict:
            raise MediaProbeError("ffprobe is not available on PATH")
        return MediaProbe()

    try:
        proc = subprocess.run(
            [
                ffprobe_exe,
                "-v",
                "quiet",
                "-print_format",
                "json",
                "-show_format",
                "-show_streams",
                str(file_path),
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if proc.returncode != 0:
            if strict:
                diagnostic = (proc.stderr or "").strip()
                suffix = f": {diagnostic}" if diagnostic else ""
                raise MediaProbeError(f"ffprobe failed with exit {proc.returncode}{suffix}")
            return MediaProbe()
        data = json.loads(proc.stdout)
        if not isinstance(data, dict):
            raise ValueError("ffprobe JSON root is not an object")
    except (json.JSONDecodeError, subprocess.TimeoutExpired, OSError, ValueError) as exc:
        if strict:
            raise MediaProbeError(f"ffprobe could not inspect {file_path}: {exc}") from exc
        return MediaProbe()

    try:
        return _parse_ffprobe_payload(data, file_path)
    except (TypeError, ValueError, ZeroDivisionError, OverflowError) as exc:
        if strict:
            raise MediaProbeError(
                f"ffprobe returned malformed metadata for {file_path}: {exc}"
            ) from exc
        return MediaProbe()


def ffprobe_metadata(
    file_path: str | Path,
    *,
    timeout: float = 30.0,
) -> MediaProbe:
    """Extract duration, fps, resolution, width, and height via ffprobe.

    Returns a :class:`MediaProbe` with best-effort fields populated.
    If ffprobe is not available or fails, all fields are ``None``.
    """
    return _ffprobe_metadata(file_path, timeout=timeout, strict=False)


def ffprobe_metadata_strict(
    file_path: str | Path,
    *,
    timeout: float = 30.0,
) -> MediaProbe:
    """Return ffprobe metadata or raise :class:`MediaProbeError`.

    Unlike :func:`ffprobe_metadata`, this entry point never converts an
    unavailable binary, failed command, timeout, or malformed payload into an
    all-``None`` probe.  Callers still decide which streams and fields their
    particular artifact contract requires.
    """

    return _ffprobe_metadata(file_path, timeout=timeout, strict=True)


# ---------------------------------------------------------------------------
# Narrow duration probe (preserved for callers who only need seconds)
# ---------------------------------------------------------------------------


def ffprobe_duration_seconds(
    media_path: str | Path,
    *,
    runner: Runner = subprocess.run,
    env: Mapping[str, str] | None = None,
) -> float:
    """Return format duration in seconds using the narrow ffprobe duration probe."""

    result = runner(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(media_path),
        ],
        stdin=subprocess.DEVNULL,
        check=True,
        capture_output=True,
        env=build_child_subprocess_env(explicit_env=env or {}),
        text=True,
    )
    return float(str(result.stdout).strip())
