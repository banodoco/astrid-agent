#!/usr/bin/env python3
"""FFmpeg finalizer and raw rendering-protocol v1 command adapter.

The protocol path validates every segment in full before starting FFmpeg,
normalizes only incompatible streams, and performs the final concat as a
stream copy.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from astrid.core.foundation.atomic_io import write_json_atomic
from astrid.core.media import MediaProbe, MediaProbeError, ffprobe_metadata_strict
from astrid.core.rendering.artifacts import validate_render_result
from astrid.core.rendering.contracts import (
    SCHEMA_VERSION,
    AudioOwnership,
    FinalizeRequest,
    RenderProfile,
    RenderRequest,
    RenderResult,
    SupportReport,
    VideoArtifact,
)
from astrid.core.rendering.errors import (
    RendererException,
    make_renderer_error,
    raise_invalid_artifact_error,
    raise_unsupported_error,
)
from astrid.packs.rendering.rendering.finalizers.ffmpeg import BACKEND_ID, BACKEND_VERSION

FINALIZER_ID = BACKEND_ID
_CONFIG_KEYS = frozenset({"faststart"})
_VIDEO_TRANSCODE_FIELDS = frozenset(
    {
        "width",
        "height",
        "fps_rational",
        "time_base",
        "video_codec",
        "video_profile",
        "video_level",
        "pixel_format",
    }
)
_AUDIO_TRANSCODE_FIELDS = frozenset(
    {
        "audio_presence",
        "audio_codec",
        "audio_sample_rate",
        "audio_channel_layout",
    }
)
_PROFILE_ANCHOR_BLOCKERS = frozenset(
    {
        "width",
        "height",
        "fps_rational",
        "time_base",
        "video_codec",
        "pixel_format",
    }
)
_SAFE_FILTER_TOKEN = re.compile(r"^[A-Za-z0-9_.+()/-]+$")


@dataclass(frozen=True)
class _PreparedSegment:
    index: int
    path: Path
    profile: RenderProfile
    audio: AudioOwnership
    duration_frames: int


@dataclass(frozen=True)
class _ProfileDifference:
    field: str
    actual: Any
    expected: Any


Runner = Callable[..., subprocess.CompletedProcess[Any]]
Probe = Callable[[str | Path], MediaProbe]


def _input_path(raw_path: str, workspace: Path) -> Path:
    candidate = Path(raw_path).expanduser()
    return (candidate if candidate.is_absolute() else workspace / candidate).resolve()


def _safe_protocol_output_path(workspace: Path, output_name: str) -> Path:
    """Resolve an invocation-owned output without following output symlinks."""

    root = workspace.resolve(strict=True)
    output_dir = root / "outputs"
    if output_dir.is_symlink():
        raise_invalid_artifact_error(
            backend=BACKEND_ID,
            message="finalizer output directory must not be a symlink",
            recovery_command="replace the outputs symlink with an invocation-owned directory",
            details={"output_directory": "outputs"},
        )
    if output_dir.exists() and not output_dir.is_dir():
        raise_invalid_artifact_error(
            backend=BACKEND_ID,
            message="finalizer output parent is not a directory",
            recovery_command="create an invocation-owned outputs directory",
            details={"output_directory": "outputs"},
        )
    output_dir.mkdir(exist_ok=True)
    resolved_dir = output_dir.resolve(strict=True)
    try:
        resolved_dir.relative_to(root)
    except ValueError:
        raise_invalid_artifact_error(
            backend=BACKEND_ID,
            message="finalizer output directory escapes the invocation workspace",
            recovery_command="use an invocation-owned outputs directory",
            details={"output_directory": "outputs"},
        )
    if not resolved_dir.is_dir():
        raise_invalid_artifact_error(
            backend=BACKEND_ID,
            message="finalizer output parent is not a directory",
            recovery_command="create an invocation-owned outputs directory",
            details={"output_directory": "outputs"},
        )

    candidate = resolved_dir / output_name
    if candidate.is_symlink():
        raise_invalid_artifact_error(
            backend=BACKEND_ID,
            message="finalizer output path must not be a symlink",
            recovery_command="remove the output symlink and retry",
            details={"output": output_name},
        )
    if candidate.exists() and not candidate.is_file():
        raise_invalid_artifact_error(
            backend=BACKEND_ID,
            message="finalizer output path is not a regular file",
            recovery_command="choose an unused portable output name",
            details={"output": output_name},
        )
    return candidate


def _profile_without_audio(profile: RenderProfile) -> RenderProfile:
    return RenderProfile(
        width=profile.width,
        height=profile.height,
        fps_rational=profile.fps_rational,
        time_base=profile.time_base,
        container=profile.container,
        video_codec=profile.video_codec,
        video_profile=profile.video_profile,
        video_level=profile.video_level,
        pixel_format=profile.pixel_format,
        audio_codec=None,
        audio_sample_rate=None,
        audio_channel_layout=None,
        duration_tolerance=profile.duration_tolerance,
    )


def _text(value: Any) -> str | None:
    return value.strip().lower() if isinstance(value, str) and value.strip() else None


def _level(value: Any, *, codec: Any = None) -> str | None:
    normalized = _text(value)
    if normalized is None:
        return None
    if normalized.isdigit():
        level_idc = int(normalized)
        normalized_codec = _text(codec)
        divisor = 10 if normalized_codec in {"h264", "avc", "avc1"} else None
        if normalized_codec in {"hevc", "h265"}:
            divisor = 30
        if divisor is not None and level_idc >= divisor:
            quotient = Fraction(level_idc, divisor)
            if quotient.denominator == 1:
                return f"{quotient.numerator}.0"
            return f"{float(quotient):.1f}"
    return normalized


def _same_value(
    field: str,
    actual: Any,
    expected: Any,
    *,
    codec: Any = None,
) -> bool:
    if field in {"fps_rational", "time_base"}:
        try:
            return Fraction(*actual) == Fraction(*expected)
        except (TypeError, ValueError, ZeroDivisionError):
            return False
    if field == "video_level":
        return _level(actual, codec=codec) == _level(expected, codec=codec)
    if field == "pixel_format":
        # ffmpeg's deprecated yuvj* names are full-range variants of the
        # standard yuv* formats (e.g. yuvj420p == yuv420p); treat them as
        # equivalent so the finalizer accepts real encoder output and can
        # normalize it to the canonical profile.
        return _pixel_format_canonical(actual) == _pixel_format_canonical(expected)
    if field in {
        "container",
        "video_codec",
        "video_profile",
        "audio_codec",
        "audio_channel_layout",
    }:
        return _text(actual) == _text(expected)
    return actual == expected


def _pixel_format_canonical(value: Any) -> str:
    text = _text(value) or ""
    if text.startswith("yuvj"):
        return "yuv" + text[4:]
    return text


def _profile_differences(
    actual: RenderProfile,
    expected: RenderProfile,
) -> list[_ProfileDifference]:
    differences: list[_ProfileDifference] = []
    for field in (
        "width",
        "height",
        "fps_rational",
        "time_base",
        "container",
        "video_codec",
        "pixel_format",
    ):
        actual_value = getattr(actual, field)
        expected_value = getattr(expected, field)
        if not _same_value(field, actual_value, expected_value):
            differences.append(_ProfileDifference(field, actual_value, expected_value))

    # Null profile/level values are deliberately unconstrained in the V1
    # artifact contract.  A concrete canonical value, however, must match.
    for field in ("video_profile", "video_level"):
        expected_value = getattr(expected, field)
        actual_value = getattr(actual, field)
        if expected_value is not None and not _same_value(
            field,
            actual_value,
            expected_value,
            codec=expected.video_codec,
        ):
            differences.append(_ProfileDifference(field, actual_value, expected_value))

    if actual.has_audio != expected.has_audio:
        differences.append(
            _ProfileDifference(
                "audio_presence",
                "present" if actual.has_audio else "absent",
                "present" if expected.has_audio else "absent",
            )
        )
    elif expected.has_audio:
        for field in (
            "audio_codec",
            "audio_sample_rate",
            "audio_channel_layout",
        ):
            actual_value = getattr(actual, field)
            expected_value = getattr(expected, field)
            if not _same_value(field, actual_value, expected_value):
                differences.append(_ProfileDifference(field, actual_value, expected_value))
    return differences


def _assembly_profile(
    canonical: RenderProfile,
    segments: Sequence[_PreparedSegment],
) -> RenderProfile:
    """Refine optional H.26x fields so concat inputs share stream metadata."""

    eligible = [
        segment
        for segment in segments
        if not any(
            difference.field in _PROFILE_ANCHOR_BLOCKERS
            for difference in _profile_differences(segment.profile, canonical)
        )
    ]
    video_profile = canonical.video_profile
    video_level = canonical.video_level
    if video_profile is None:
        video_profile = next(
            (
                segment.profile.video_profile
                for segment in eligible
                if segment.profile.video_profile is not None
            ),
            None,
        )
    if video_level is None:
        video_level = next(
            (
                segment.profile.video_level
                for segment in eligible
                if segment.profile.video_level is not None
            ),
            None,
        )
    if video_profile == canonical.video_profile and video_level == canonical.video_level:
        return canonical
    return replace(
        canonical,
        video_profile=video_profile,
        video_level=video_level,
    )


def _format_value(value: Any) -> str:
    if isinstance(value, tuple) and len(value) == 2:
        return f"{value[0]}/{value[1]}"
    if value is None:
        return "null"
    return str(value)


def _normalization_record(
    index: int,
    difference: _ProfileDifference,
) -> str:
    return (
        f"segment[{index}] {difference.field}: "
        f"{_format_value(difference.actual)} -> "
        f"{_format_value(difference.expected)}"
    )


def _validate_filter_token(value: str, label: str) -> str:
    if not _SAFE_FILTER_TOKEN.fullmatch(value):
        raise ValueError(f"{label} contains characters FFmpeg cannot safely parse")
    return value


def _video_encoder(codec: str) -> str:
    normalized = _text(codec)
    encoders = {
        "h264": "libx264",
        "hevc": "libx265",
        "mpeg4": "mpeg4",
        "vp9": "libvpx-vp9",
        "av1": "libaom-av1",
    }
    if normalized not in encoders:
        raise_unsupported_error(
            backend=BACKEND_ID,
            message=f"unsupported target video codec for FFmpeg finalization: {codec}",
            recovery_command="select an FFmpeg-encodable canonical video profile",
            details={"video_codec": codec},
        )
    return encoders[normalized]


def _encoder_profile(codec: str, profile: str) -> str:
    normalized_codec = _text(codec)
    normalized_profile = _text(profile)
    assert normalized_profile is not None
    mapping: dict[str, str]
    if normalized_codec == "h264":
        mapping = {
            "baseline": "baseline",
            "constrained baseline": "baseline",
            "main": "main",
            "high": "high",
            "high 10": "high10",
            "high 10 intra": "high10",
            "high 4:2:2": "high422",
            "high 4:2:2 intra": "high422",
            "high 4:4:4": "high444",
            "high 4:4:4 predictive": "high444",
            "high 4:4:4 intra": "high444",
        }
    elif normalized_codec == "hevc":
        mapping = {
            "main": "main",
            "main 10": "main10",
            "main still picture": "mainstillpicture",
        }
    else:
        mapping = {}
    encoded = mapping.get(normalized_profile)
    if encoded is None:
        raise_unsupported_error(
            backend=BACKEND_ID,
            message=(f"unsupported {codec} encoder profile for FFmpeg finalization: {profile}"),
            recovery_command="select a supported canonical video profile",
            details={"video_codec": codec, "video_profile": profile},
        )
    return encoded


def _audio_encoder(codec: str) -> str:
    normalized = _text(codec)
    encoders = {
        "aac": "aac",
        "mp3": "libmp3lame",
        "opus": "libopus",
        "ac3": "ac3",
        "flac": "flac",
        "pcm_s16le": "pcm_s16le",
        "pcm_s24le": "pcm_s24le",
    }
    if normalized not in encoders:
        raise_unsupported_error(
            backend=BACKEND_ID,
            message=f"unsupported target audio codec for FFmpeg finalization: {codec}",
            recovery_command="select an FFmpeg-encodable canonical audio profile",
            details={"audio_codec": codec},
        )
    return encoders[normalized]


def _mp4_timescale(profile: RenderProfile) -> int:
    time_base = Fraction(*profile.time_base)
    reciprocal = 1 / time_base
    if reciprocal.denominator != 1:
        raise_unsupported_error(
            backend=BACKEND_ID,
            message=(
                "MP4 finalization requires a reciprocal integer video time base; "
                f"received {profile.time_base[0]}/{profile.time_base[1]}"
            ),
            recovery_command="select an MP4 profile supported by the configured renderer",
            details={"time_base": list(profile.time_base)},
        )
    return reciprocal.numerator


def _validate_target_profile(profile: RenderProfile) -> None:
    if _text(profile.container) != "mp4":
        raise_unsupported_error(
            backend=BACKEND_ID,
            message=f"FFmpeg finalizer supports canonical MP4 output, not {profile.container!r}",
            recovery_command="select a finalizer supporting the canonical container",
            details={"container": profile.container},
        )
    _video_encoder(profile.video_codec)
    if profile.video_profile is not None:
        _encoder_profile(profile.video_codec, profile.video_profile)
    if profile.video_level is not None:
        normalized_level = _level(
            profile.video_level,
            codec=profile.video_codec,
        )
        if (
            _text(profile.video_codec) not in {"h264", "hevc"}
            or normalized_level is None
            or re.fullmatch(r"[1-9][0-9]*(?:\.[0-9]+)?", normalized_level) is None
        ):
            raise_unsupported_error(
                backend=BACKEND_ID,
                message=(
                    f"unsupported encoder level for FFmpeg finalization: {profile.video_level}"
                ),
                recovery_command="select a supported canonical video level",
                details={
                    "video_codec": profile.video_codec,
                    "video_level": profile.video_level,
                },
            )
    if profile.has_audio:
        assert profile.audio_codec is not None
        _audio_encoder(profile.audio_codec)
    _mp4_timescale(profile)
    _validate_filter_token(profile.pixel_format, "pixel_format")
    if profile.audio_channel_layout is not None:
        _validate_filter_token(profile.audio_channel_layout, "audio_channel_layout")


def build_normalize_command(
    segment: _PreparedSegment,
    output_path: Path,
    *,
    target_profile: RenderProfile,
    differences: Sequence[_ProfileDifference],
    faststart: bool,
) -> list[str]:
    """Build one segment-normalization command without touching the filesystem."""

    fields = {difference.field for difference in differences}
    video_transcode = bool(fields & _VIDEO_TRANSCODE_FIELDS)
    audio_transcode = bool(fields & _AUDIO_TRANSCODE_FIELDS)
    synthesize_audio = target_profile.has_audio and not segment.profile.has_audio
    fps = f"{target_profile.fps_rational[0]}/{target_profile.fps_rational[1]}"
    time_base = f"{target_profile.time_base[0]}/{target_profile.time_base[1]}"
    # When the video stream is re-encoded, the container duration must match
    # the planned frame window: an audio track padded past the last video
    # frame (Remotion's --enforce-audio-track rounds up to the AAC frame
    # grid) would otherwise extend the container, making ffprobe's
    # avg_frame_rate read frames/duration below the canonical rate.  Trimming
    # the output to the exact video duration keeps stream copy probes honest.
    video_seconds = Fraction(segment.duration_frames, 1) / Fraction(*target_profile.fps_rational)

    argv = [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-i",
        str(segment.path),
    ]
    if synthesize_audio:
        assert target_profile.audio_sample_rate is not None
        assert target_profile.audio_channel_layout is not None
        argv.extend(
            [
                "-f",
                "lavfi",
                "-i",
                (
                    "anullsrc="
                    f"sample_rate={target_profile.audio_sample_rate}:"
                    f"channel_layout={target_profile.audio_channel_layout}"
                ),
            ]
        )
    argv.extend(["-map", "0:v:0"])
    if video_transcode:
        filters = ["setpts=PTS-STARTPTS"]
        if fields & {"width", "height"}:
            filters.extend(
                [
                    (
                        f"scale={target_profile.width}:{target_profile.height}:"
                        "force_original_aspect_ratio=decrease"
                    ),
                    (f"pad={target_profile.width}:{target_profile.height}:(ow-iw)/2:(oh-ih)/2"),
                ]
            )
        if "fps_rational" in fields:
            filters.append(f"fps={fps}")
        if "time_base" in fields:
            filters.append(f"settb=expr={time_base}")
        if "pixel_format" in fields:
            filters.append(f"format={target_profile.pixel_format}")
        argv.extend(["-vf", ",".join(filters)])
        encoder = _video_encoder(target_profile.video_codec)
        argv.extend(["-c:v", encoder, "-r:v", fps, "-fps_mode", "cfr"])
        if encoder in {"libx264", "libx265"}:
            argv.extend(["-preset", "veryfast", "-crf", "20"])
        if target_profile.video_profile is not None:
            argv.extend(
                [
                    "-profile:v",
                    _encoder_profile(
                        target_profile.video_codec,
                        target_profile.video_profile,
                    ),
                ]
            )
        if target_profile.video_level is not None:
            argv.extend(
                [
                    "-level:v",
                    _level(
                        target_profile.video_level,
                        codec=target_profile.video_codec,
                    )
                    or target_profile.video_level,
                ]
            )
        argv.extend(["-pix_fmt", target_profile.pixel_format])
    else:
        argv.extend(["-c:v", "copy"])

    if target_profile.has_audio:
        assert target_profile.audio_codec is not None
        assert target_profile.audio_sample_rate is not None
        assert target_profile.audio_channel_layout is not None
        argv.extend(["-map", "1:a:0" if synthesize_audio else "0:a:0"])
        if audio_transcode:
            audio_filter = (
                "asetpts=PTS-STARTPTS,"
                f"aformat=sample_rates={target_profile.audio_sample_rate}:"
                f"channel_layouts={target_profile.audio_channel_layout}"
            )
            argv.extend(
                [
                    "-af",
                    audio_filter,
                    "-c:a",
                    _audio_encoder(target_profile.audio_codec),
                ]
            )
        else:
            argv.extend(["-c:a", "copy"])
    else:
        argv.append("-an")

    if synthesize_audio:
        argv.append("-shortest")
    elif video_transcode:
        # Re-encoded video pins the exact planned frame count; trim the
        # (copied or re-encoded) audio so the container duration matches the
        # video frames instead of the padded AAC grid.
        argv.extend(["-t", str(float(video_seconds))])

    argv.extend(
        [
            "-video_track_timescale",
            str(_mp4_timescale(target_profile)),
        ]
    )
    if faststart:
        argv.extend(["-movflags", "+faststart"])
    argv.extend(["-f", "mp4", str(output_path)])
    return argv


def build_concat_command(
    list_path: Path,
    output_path: Path,
    *,
    target_profile: RenderProfile,
    faststart: bool,
) -> list[str]:
    """Build the final concat-demuxer stream-copy command."""

    argv = [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_path),
        "-map",
        "0:v:0",
    ]
    if target_profile.has_audio:
        argv.extend(["-map", "0:a:0"])
    else:
        argv.append("-an")
    argv.extend(
        [
            "-c",
            "copy",
            "-video_track_timescale",
            str(_mp4_timescale(target_profile)),
        ]
    )
    if faststart:
        argv.extend(["-movflags", "+faststart"])
    argv.extend(["-f", "mp4", str(output_path)])
    return argv


def _concat_file_line(path: Path) -> str:
    # FFmpeg's concat demuxer uses shell-like single-quote escaping even though
    # the command itself is never run through a shell.
    resolved = str(path.resolve())
    if "\n" in resolved or "\r" in resolved:
        raise ValueError("FFmpeg concat input paths must not contain CR or LF")
    escaped = resolved.replace("'", "'\\''")
    return f"file '{escaped}'"


def _run_checked(runner: Runner, argv: list[str]) -> None:
    runner(argv, check=True)


def _assemble_prepared_segments(
    segments: Sequence[_PreparedSegment],
    output_path: Path,
    *,
    target_profile: RenderProfile,
    faststart: bool,
    runner: Runner,
) -> list[str]:
    """Normalize incompatible segments and atomically assemble the output."""

    if not segments:
        raise ValueError("at least one segment is required for finalization")
    _validate_target_profile(target_profile)
    output_path = output_path.absolute()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    normalization: list[str] = []

    with TemporaryDirectory(
        prefix=f".{output_path.name}.ffmpeg-finalizer-",
        dir=str(output_path.parent),
    ) as tmp_text:
        tmp_dir = Path(tmp_text)
        concat_paths: list[Path] = []
        for segment in segments:
            differences = _profile_differences(segment.profile, target_profile)
            if not differences:
                concat_paths.append(segment.path)
                continue
            normalization.extend(
                _normalization_record(segment.index, difference) for difference in differences
            )
            normalized_path = tmp_dir / "normalized" / f"segment-{segment.index:04d}.mp4"
            normalized_path.parent.mkdir(parents=True, exist_ok=True)
            command = build_normalize_command(
                segment,
                normalized_path,
                target_profile=target_profile,
                differences=differences,
                faststart=faststart,
            )
            _run_checked(runner, command)
            if not normalized_path.is_file() or normalized_path.stat().st_size <= 0:
                raise_invalid_artifact_error(
                    backend=BACKEND_ID,
                    message=(f"FFmpeg did not produce normalized segment[{segment.index}]"),
                    recovery_command="rerun finalization in a fresh invocation workspace",
                    details={"segment_index": segment.index},
                )
            concat_paths.append(normalized_path)

        list_path = tmp_dir / "segments.ffconcat"
        list_path.write_text(
            "ffconcat version 1.0\n"
            + "\n".join(_concat_file_line(path) for path in concat_paths)
            + "\n",
            encoding="utf-8",
        )
        staged_output = tmp_dir / "final" / output_path.name
        staged_output.parent.mkdir(parents=True, exist_ok=True)
        _run_checked(
            runner,
            build_concat_command(
                list_path,
                staged_output,
                target_profile=target_profile,
                faststart=faststart,
            ),
        )
        if not staged_output.is_file() or staged_output.stat().st_size <= 0:
            raise_invalid_artifact_error(
                backend=BACKEND_ID,
                message="FFmpeg did not produce a finalized video",
                recovery_command="rerun finalization in a fresh invocation workspace",
                details={"output": output_path.name},
            )
        os.replace(staged_output, output_path)
    return normalization


def _duration_fraction(probe: MediaProbe) -> Fraction:
    if probe.duration_rational is not None:
        try:
            duration = Fraction(*probe.duration_rational)
        except (TypeError, ValueError, ZeroDivisionError) as exc:
            raise MediaProbeError("ffprobe returned an invalid rational duration") from exc
    elif probe.duration_seconds is not None:
        try:
            duration = Fraction(str(probe.duration_seconds))
        except (ValueError, ZeroDivisionError) as exc:
            raise MediaProbeError("ffprobe returned an invalid duration") from exc
    else:
        raise MediaProbeError("ffprobe did not report media duration")
    if duration <= 0:
        raise MediaProbeError("ffprobe reported a non-positive media duration")
    return duration


def _required_probe_value(value: Any, label: str) -> Any:
    if value is None:
        raise MediaProbeError(f"ffprobe did not report {label}")
    return value


def _layout_from_probe(probe: MediaProbe) -> str | None:
    if probe.audio_channel_layout is not None:
        return probe.audio_channel_layout
    return {
        1: "mono",
        2: "stereo",
        6: "5.1",
        8: "7.1",
    }.get(probe.audio_channels)


def _profile_from_probe(
    probe: MediaProbe,
    *,
    ownership: AudioOwnership,
    duration_tolerance: int,
) -> RenderProfile:
    if not probe.has_video_stream:
        raise MediaProbeError("media has no video stream")
    if ownership is AudioOwnership.RENDERED and not probe.has_audio_stream:
        raise MediaProbeError("media has no required audio stream")
    if ownership is not AudioOwnership.RENDERED and probe.has_audio_stream:
        raise MediaProbeError(f"visual-only {ownership.value} media unexpectedly contains audio")
    return RenderProfile(
        width=_required_probe_value(probe.width, "video width"),
        height=_required_probe_value(probe.height, "video height"),
        fps_rational=_required_probe_value(probe.fps_rational, "video frame rate"),
        time_base=_required_probe_value(probe.time_base, "video time base"),
        container=_required_probe_value(probe.container, "container"),
        video_codec=_required_probe_value(probe.video_codec, "video codec"),
        video_profile=probe.video_profile,
        video_level=probe.video_level,
        pixel_format=_required_probe_value(probe.pixel_format, "pixel format"),
        audio_codec=(
            _required_probe_value(probe.audio_codec, "audio codec")
            if ownership is AudioOwnership.RENDERED
            else None
        ),
        audio_sample_rate=(
            _required_probe_value(probe.audio_sample_rate, "audio sample rate")
            if ownership is AudioOwnership.RENDERED
            else None
        ),
        audio_channel_layout=(
            _required_probe_value(_layout_from_probe(probe), "audio channel layout")
            if ownership is AudioOwnership.RENDERED
            else None
        ),
        duration_tolerance=duration_tolerance,
    )


def _config(mapping: Mapping[str, Any]) -> tuple[bool, list[str]]:
    config = dict(mapping.get(BACKEND_ID, {}))
    reasons: list[str] = []
    unknown = sorted(set(config) - _CONFIG_KEYS)
    if unknown:
        reasons.append(f"unknown {BACKEND_ID} configuration: {', '.join(unknown)}")
    faststart = config.get("faststart", True)
    if not isinstance(faststart, bool):
        reasons.append("faststart must be a boolean")
        faststart = True
    return faststart, reasons


def support(request: RenderRequest, *, workspace: Path) -> SupportReport:
    """Return request-sensitive support evidence for canonical finalization."""

    del workspace  # Support is profile/config sensitive; it does not read inputs.
    _faststart, reasons = _config(request.backend_config)
    if request.profile is not None:
        try:
            _validate_target_profile(request.profile)
        except RendererException as exc:
            reasons.append(exc.error.message)
        except (TypeError, ValueError) as exc:
            reasons.append(str(exc))
    for binary in ("ffmpeg", "ffprobe"):
        if shutil.which(binary) is None:
            reasons.append(f"required binary is unavailable: {binary}")
    requested_audio = request.audio.value if request.audio is not None else "request-dependent"
    return SupportReport(
        schema_version=SCHEMA_VERSION,
        supported=not reasons,
        reasons=reasons,
        features={
            "strict_probe": True,
            "stream_copy": True,
            "profile_normalization": True,
            "preserves_attachments": True,
            "audio_ownership": requested_audio,
        },
        alternatives=[],
        backend=BACKEND_ID,
        backend_version=BACKEND_VERSION,
    )


def _final_audio_ownership(request: FinalizeRequest) -> AudioOwnership:
    ownerships = [artifact.audio for artifact in request.artifacts]
    for index, ownership in enumerate(ownerships):
        if ownership is None:
            raise_invalid_artifact_error(
                backend=BACKEND_ID,
                message=f"segment[{index}] does not declare audio ownership",
                recovery_command="rerender the segment with explicit audio ownership",
                details={"segment_index": index},
            )

    if request.plan.profile.has_audio:
        if all(ownership is AudioOwnership.PASSTHROUGH for ownership in ownerships):
            return AudioOwnership.PASSTHROUGH
        if any(ownership is AudioOwnership.RENDERED for ownership in ownerships) and all(
            ownership in {AudioOwnership.RENDERED, AudioOwnership.NONE} for ownership in ownerships
        ):
            return AudioOwnership.RENDERED
        raise_invalid_artifact_error(
            backend=BACKEND_ID,
            message=(
                "canonical audio requires rendered/visual-only segments or a "
                "uniform passthrough segment set"
            ),
            recovery_command="rerender passthrough segments with one consistent ownership mode",
            details={
                "audio_ownership": [
                    ownership.value if ownership is not None else None for ownership in ownerships
                ]
            },
        )

    if any(ownership is AudioOwnership.PASSTHROUGH for ownership in ownerships):
        raise_invalid_artifact_error(
            backend=BACKEND_ID,
            message="passthrough audio is incompatible with a visual-only canonical profile",
            recovery_command="use audio_ownership='none' for visual-only segments",
            details={"canonical_audio": False},
        )
    return AudioOwnership.NONE


def _preflight_segments(
    request: FinalizeRequest,
    *,
    workspace: Path,
) -> list[_PreparedSegment]:
    prepared: list[_PreparedSegment] = []
    canonical_fps = Fraction(*request.plan.profile.fps_rational)
    tolerance = request.plan.profile.duration_tolerance
    for index, (artifact, plan_segment) in enumerate(
        zip(request.artifacts, request.plan.segments, strict=True)
    ):
        if artifact.audio is None:
            raise_invalid_artifact_error(
                backend=BACKEND_ID,
                message=f"segment[{index}] does not declare audio ownership",
                recovery_command="rerender the segment with explicit audio ownership",
                details={"segment_index": index},
            )
        segment_result = RenderResult(
            schema_version=SCHEMA_VERSION,
            video=artifact,
            audio_ownership=artifact.audio,
        )
        # Validate against the segment's own declared/probed profile.  Its
        # differences from the plan profile are legitimate normalization work.
        validate_render_result(
            segment_result,
            expected_profile=artifact.profile,
            workspace_root=workspace,
        )

        artifact_seconds = Fraction(artifact.duration_frames, 1) / Fraction(
            *artifact.profile.fps_rational
        )
        # Frame-count authority: a backend may declare a duration inflated by
        # audio-extended container padding (Remotion's always-rendered audio).
        # The video stream's ACTUAL frame count is authoritative.
        try:
            artifact_path = Path(artifact.path)
            if not artifact_path.is_absolute():
                artifact_path = workspace / artifact_path
            if artifact_path.is_file():
                probe = ffprobe_metadata_strict(artifact_path)
                if probe.frames is not None and probe.frames > 0:
                    artifact_seconds = Fraction(probe.frames, 1) / Fraction(
                        *artifact.profile.fps_rational
                    )
        except Exception:  # noqa: BLE001 - probe is best-effort
            pass
        planned_seconds = Fraction(plan_segment.window.duration_frames, 1) / canonical_fps
        delta_frames = abs(artifact_seconds - planned_seconds) * canonical_fps
        if delta_frames > tolerance:
            raise_invalid_artifact_error(
                backend=BACKEND_ID,
                message=(f"segment[{index}] duration does not match its planned frame window"),
                recovery_command="rerender the exact planned segment window and retry",
                details={
                    "segment_index": index,
                    "declared_duration_frames": artifact.duration_frames,
                    "planned_duration_frames": plan_segment.window.duration_frames,
                    "canonical_delta_frames": [
                        delta_frames.numerator,
                        delta_frames.denominator,
                    ],
                    "tolerance_frames": tolerance,
                },
            )

        try:
            media_probe = ffprobe_metadata_strict(_input_path(artifact.path, workspace))
            probed_profile = _profile_from_probe(
                media_probe,
                ownership=artifact.audio,
                duration_tolerance=artifact.profile.duration_tolerance,
            )
        except (MediaProbeError, OSError, RuntimeError, TypeError, ValueError) as exc:
            raise_invalid_artifact_error(
                backend=BACKEND_ID,
                message=f"segment[{index}] strict media probe failed: {exc}",
                recovery_command="rerender the segment as a valid canonical media artifact",
                details={"segment_index": index},
            )

        prepared.append(
            _PreparedSegment(
                index=index,
                path=_input_path(artifact.path, workspace),
                profile=probed_profile,
                audio=artifact.audio,
                duration_frames=plan_segment.window.duration_frames,
            )
        )
    return prepared


def _probe_normalized_segments(
    prepared: Sequence[_PreparedSegment],
    *,
    target_profile: RenderProfile,
) -> RenderProfile:
    """Strictly probe every normalized segment before final assembly."""

    probed: list[_PreparedSegment] = []
    for segment in prepared:
        try:
            probe = ffprobe_metadata_strict(segment.path)
            profile = _profile_from_probe(
                probe,
                ownership=segment.audio,
                duration_tolerance=target_profile.duration_tolerance,
            )
        except (MediaProbeError, OSError, RuntimeError, TypeError, ValueError) as exc:
            raise_invalid_artifact_error(
                backend=BACKEND_ID,
                message=(f"normalized segment[{segment.index}] could not be validated: {exc}"),
                recovery_command="rerun finalization in a fresh invocation workspace",
                details={"segment_index": segment.index},
            )
        actual_frames = _duration_fraction(probe) * Fraction(*target_profile.fps_rational)
        duration_delta = abs(actual_frames - segment.duration_frames)
        if duration_delta > target_profile.duration_tolerance:
            raise_invalid_artifact_error(
                backend=BACKEND_ID,
                message=(
                    f"normalized segment[{segment.index}] duration does not match "
                    "its planned frame window"
                ),
                recovery_command="rerun finalization in a fresh invocation workspace",
                details={
                    "segment_index": segment.index,
                    "expected_duration_frames": segment.duration_frames,
                    "actual_duration_frames": [
                        actual_frames.numerator,
                        actual_frames.denominator,
                    ],
                    "delta_frames": [
                        duration_delta.numerator,
                        duration_delta.denominator,
                    ],
                    "tolerance_frames": target_profile.duration_tolerance,
                },
            )
        probed.append(replace(segment, profile=profile))

    effective_profile = _assembly_profile(target_profile, probed)
    for segment in probed:
        differences = _profile_differences(segment.profile, effective_profile)
        if differences:
            raise_invalid_artifact_error(
                backend=BACKEND_ID,
                message=(
                    f"normalized segment[{segment.index}] does not match the canonical profile"
                ),
                recovery_command="rerun finalization in a fresh invocation workspace",
                details={
                    "segment_index": segment.index,
                    "mismatches": [
                        _normalization_record(segment.index, difference)
                        for difference in differences
                    ],
                },
            )
    return effective_profile


def _validate_concat_output(
    output_path: Path,
    *,
    total_frames: int,
    target_profile: RenderProfile,
    ownership: AudioOwnership,
) -> None:
    """Probe the final stream-copied concat without strict per-field equality.

    A concat demuxer stream-copy merges per-segment AAC grids, so the video
    stream's ``avg_frame_rate`` reads frames/duration slightly below the
    canonical rate (e.g. 204800/20521 for 20 frames at 10fps).  The planned
    frame count and the structural profile are authoritative; the strict
    :func:`validate_render_result` probe would reject otherwise-correct
    output on that rounding alone.
    """
    try:
        probe = ffprobe_metadata_strict(output_path)
    except (MediaProbeError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise_invalid_artifact_error(
            backend=BACKEND_ID,
            message=f"final concat could not be probed: {exc}",
            recovery_command="rerun finalization in a fresh invocation workspace",
            details={"error_type": type(exc).__name__},
        )
    if not probe.has_video_stream:
        raise_invalid_artifact_error(
            backend=BACKEND_ID,
            message="final concat has no video stream",
            recovery_command="rerun finalization in a fresh invocation workspace",
        )
    frames = _duration_fraction(probe) * Fraction(*target_profile.fps_rational)
    if abs(frames - total_frames) > target_profile.duration_tolerance:
        raise_invalid_artifact_error(
            backend=BACKEND_ID,
            message="final concat frame count does not match the planned total",
            recovery_command="rerun finalization in a fresh invocation workspace",
            details={
                "planned_total_frames": total_frames,
                "probed_total_frames": [
                    frames.numerator,
                    frames.denominator,
                ],
                "tolerance_frames": target_profile.duration_tolerance,
            },
        )
    if probe.width != target_profile.width or probe.height != target_profile.height:
        raise_invalid_artifact_error(
            backend=BACKEND_ID,
            message="final concat resolution does not match the canonical profile",
            recovery_command="rerun finalization in a fresh invocation workspace",
            details={
                "expected": [target_profile.width, target_profile.height],
                "actual": [probe.width, probe.height],
            },
        )
    if probe.video_codec and _text(probe.video_codec) != _text(target_profile.video_codec):
        raise_invalid_artifact_error(
            backend=BACKEND_ID,
            message="final concat video codec does not match the canonical profile",
            recovery_command="rerun finalization in a fresh invocation workspace",
            details={
                "expected": target_profile.video_codec,
                "actual": probe.video_codec,
            },
        )
    if ownership is AudioOwnership.RENDERED and not probe.has_audio_stream:
        raise_invalid_artifact_error(
            backend=BACKEND_ID,
            message="final concat is missing the required audio stream",
            recovery_command="rerun finalization in a fresh invocation workspace",
        )
    if ownership is not AudioOwnership.RENDERED and probe.has_audio_stream:
        raise_invalid_artifact_error(
            backend=BACKEND_ID,
            message="final concat unexpectedly contains an audio stream",
            recovery_command="rerun finalization in a fresh invocation workspace",
        )


def finalize(
    request: FinalizeRequest,
    *,
    workspace: Path,
    runner: Runner | None = None,
) -> RenderResult:
    """Preflight, normalize, concatenate, and validate one render plan."""

    execute = subprocess.run if runner is None else runner
    if request.plan.finalizer.id != BACKEND_ID:
        raise ValueError(
            f"finalize request selects {request.plan.finalizer.id!r}, not {BACKEND_ID!r}"
        )
    faststart, config_reasons = _config(request.backend_config)
    if config_reasons:
        raise ValueError("; ".join(config_reasons))

    # This loop completes in full before the first assembly subprocess starts.
    prepared = _preflight_segments(request, workspace=workspace)
    ownership = _final_audio_ownership(request)
    target_profile = (
        request.plan.profile
        if ownership is AudioOwnership.RENDERED
        else _profile_without_audio(request.plan.profile)
    )
    assembly_profile = _assembly_profile(target_profile, prepared)
    _validate_target_profile(assembly_profile)
    output_path = _safe_protocol_output_path(workspace, request.output_name)
    total_frames = sum(segment.window.duration_frames for segment in request.plan.segments)
    recovery_tmp = TemporaryDirectory(
        prefix=f".{output_path.name}.ffmpeg-finalizer-recovery-",
        dir=str(output_path.parent),
    )
    previous_output = (
        Path(recovery_tmp.name) / "previous-output.mp4" if output_path.is_file() else None
    )
    published = False
    assembly_started = False
    try:
        if previous_output is not None:
            shutil.copy2(output_path, previous_output)
        with TemporaryDirectory(
            prefix=f".{output_path.name}.ffmpeg-finalizer-normalize-",
            dir=str(output_path.parent),
        ) as normalized_tmp_text:
            normalized_tmp = Path(normalized_tmp_text)
            normalized_prepared: list[_PreparedSegment] = []
            normalization: list[str] = []
            for segment in prepared:
                differences = _profile_differences(segment.profile, assembly_profile)
                if not differences:
                    normalized_prepared.append(segment)
                    continue
                normalization.extend(
                    _normalization_record(segment.index, difference) for difference in differences
                )
                normalized_path = normalized_tmp / f"segment-{segment.index:04d}.mp4"
                normalized_path.parent.mkdir(parents=True, exist_ok=True)
                _run_checked(
                    execute,
                    build_normalize_command(
                        segment,
                        normalized_path,
                        target_profile=assembly_profile,
                        differences=differences,
                        faststart=faststart,
                    ),
                )
                if not normalized_path.is_file() or normalized_path.stat().st_size <= 0:
                    raise_invalid_artifact_error(
                        backend=BACKEND_ID,
                        message=(f"FFmpeg did not produce normalized segment[{segment.index}]"),
                        recovery_command="rerun finalization in a fresh invocation workspace",
                        details={"segment_index": segment.index},
                    )
                normalized_prepared.append(
                    _PreparedSegment(
                        index=segment.index,
                        path=normalized_path,
                        profile=assembly_profile,
                        audio=ownership,
                        duration_frames=segment.duration_frames,
                    )
                )
            effective_profile = _probe_normalized_segments(
                normalized_prepared,
                target_profile=assembly_profile,
            )
            for segment in prepared:
                if not _profile_differences(segment.profile, assembly_profile):
                    continue
                existing = set(normalization)
                for difference in _profile_differences(
                    segment.profile,
                    effective_profile,
                ):
                    record = _normalization_record(segment.index, difference)
                    if record not in existing:
                        normalization.append(record)
                        existing.add(record)
            normalized_prepared = [
                replace(segment, profile=effective_profile) for segment in normalized_prepared
            ]
            # The prepared list now has a uniform canonical profile, so this
            # call performs only the concat-demuxer stream-copy assembly.
            assembly_started = True
            extra_normalization = _assemble_prepared_segments(
                normalized_prepared,
                output_path,
                target_profile=effective_profile,
                faststart=faststart,
                runner=execute,
            )
            published = True
            normalization.extend(extra_normalization)
        video = VideoArtifact.from_file(
            path=output_path,
            workspace_root=workspace,
            profile=effective_profile,
            duration_frames=total_frames,
            audio=ownership,
            attachments=request.expected_attachments,
        )
        result = RenderResult(
            schema_version=SCHEMA_VERSION,
            video=video,
            audio_ownership=ownership,
            backend_fragments={
                BACKEND_ID: {
                    "finalizer_kind": "ffmpeg",
                    "finalizer_version": BACKEND_VERSION,
                    "segment_count": len(prepared),
                    "stream_copied_segments": [
                        segment.index
                        for segment in prepared
                        if not _profile_differences(segment.profile, assembly_profile)
                    ],
                    "normalized_segments": [
                        segment.index
                        for segment in prepared
                        if _profile_differences(segment.profile, assembly_profile)
                    ],
                    "audio_mode": ownership.value,
                }
            },
            normalization=normalization,
            logs=[],
            metadata=request.metadata,
        )
        request.validate_final_result(result)
        _validate_concat_output(
            output_path,
            total_frames=total_frames,
            target_profile=request.plan.profile,
            ownership=ownership,
        )
        return result
    except BaseException:
        if assembly_started:
            if previous_output is not None and previous_output.is_file():
                os.replace(previous_output, output_path)
            elif published or output_path.exists():
                output_path.unlink(missing_ok=True)
        raise
    finally:
        recovery_tmp.cleanup()


def _load_finalize_request(path: Path) -> FinalizeRequest:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise TypeError("finalize request must contain a JSON object")
    return FinalizeRequest.from_dict(payload)


def _load_support_request(path: Path) -> RenderRequest:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise TypeError("support request must contain a JSON object")
    return RenderRequest.from_dict(payload).for_backend(BACKEND_ID)


def _write_failure(result_path: Path, exc: BaseException, *, kind: str) -> None:
    if isinstance(exc, RendererException):
        error_kind = exc.error.kind
        message = exc.error.message
        recovery = exc.error.recovery_command
        details = exc.error.details
    else:
        error_kind = kind
        message = str(exc) or type(exc).__name__
        recovery = None
        details = {"error_type": type(exc).__name__}
    error = make_renderer_error(
        error_kind,
        backend=BACKEND_ID,
        message=message,
        recovery_command=recovery,
        details=details,
    )
    write_json_atomic(result_path, error.to_dict())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("verb", choices=("finalize", "support"))
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        request_path = args.request.resolve(strict=True)
        result_path = args.result.resolve()
        if request_path == result_path:
            raise ValueError("--request and --result must be different paths")
        request: FinalizeRequest | RenderRequest
        if args.verb == "finalize":
            request = _load_finalize_request(request_path)
        else:
            request = _load_support_request(request_path)
    except (
        OSError,
        ValueError,
        TypeError,
        json.JSONDecodeError,
        RendererException,
    ) as exc:
        _write_failure(args.result.resolve(), exc, kind="protocol")
        return 0

    try:
        workspace = request_path.parent
        response: RenderResult | SupportReport
        if args.verb == "support":
            assert isinstance(request, RenderRequest)
            response = support(request, workspace=workspace)
        else:
            assert isinstance(request, FinalizeRequest)
            response = finalize(request, workspace=workspace)
        write_json_atomic(result_path, response.to_dict())
    except RendererException as exc:
        _write_failure(result_path, exc, kind=exc.error.kind)
    except FileNotFoundError as exc:
        _write_failure(result_path, exc, kind="binary_missing")
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        _write_failure(result_path, exc, kind="protocol")
    except subprocess.TimeoutExpired as exc:
        _write_failure(result_path, exc, kind="timeout")
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        _write_failure(result_path, exc, kind="internal")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BACKEND_ID",
    "BACKEND_VERSION",
    "FINALIZER_ID",
    "build_concat_command",
    "build_normalize_command",
    "finalize",
    "main",
    "support",
]
