#!/usr/bin/env python3
"""FFmpeg layer compositor and raw rendering-protocol v1 command adapter.

The compositor merges N z-layer segments (possibly overlapping in time) into
one timeline-length video with a single FFmpeg ``overlay`` filtergraph,
bottom-to-top.  Every layer is scaled/padded to the plan canvas and its frame
rate is normalized to the canonical profile; short layers (including z=0) are
padded by the full-length ``color`` base plus ``eof_action=pass`` on every
overlay, so after a layer's frames end the accumulated result below shows
through.  Straight alpha only: alpha inputs are decoded with ``libvpx-vp9``
(the native VP9 decoder drops alpha) and composited with ``overlay``'s default
straight-alpha semantics — never ``alpha=premultiplied``.  The output frame
count is the plan's ``total_frames``, never the sum of per-layer windows
(overlapping layers would double-count).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

# Raw commands run with a sanitized environment and their owning pack as cwd.
# Make the checkout importable when this file is executed directly.
if __package__ in {None, ""}:
    _CHECKOUT_ROOT = Path(__file__).resolve().parents[6]
    if str(_CHECKOUT_ROOT) not in sys.path:
        sys.path.insert(0, str(_CHECKOUT_ROOT))

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

BACKEND_ID = "rendering.ffmpeg-compositor"
BACKEND_VERSION = "1.0.0"
FINALIZER_ID = BACKEND_ID
_CONFIG_KEYS = frozenset({"faststart"})
_SAFE_FILTER_TOKEN = re.compile(r"^[A-Za-z0-9_./+-]+$")


@dataclass(frozen=True)
class _PreparedLayer:
    """One z-layer input prepared for the composite filtergraph."""

    index: int  # ffmpeg input index (0 is the color base)
    z: int
    path: Path
    opacity: float
    alpha: bool
    vp9: bool  # codec is VP9: force libvpx-vp9 decoding when alpha is present
    audio: AudioOwnership
    duration_frames: int  # probed frames at the plan frame rate


Runner = Callable[..., subprocess.CompletedProcess[Any]]


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


def _pixel_format_canonical(value: Any) -> str:
    text = _text(value) or ""
    if text.startswith("yuvj"):
        return "yuv" + text[4:]
    return text


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
            message=f"unsupported target video codec for FFmpeg compositing: {codec}",
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
            message=(f"unsupported {codec} encoder profile for FFmpeg compositing: {profile}"),
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
            message=f"unsupported target audio codec for FFmpeg compositing: {codec}",
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
                "MP4 compositing requires a reciprocal integer video time base; "
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
            message=f"FFmpeg compositor supports canonical MP4 output, not {profile.container!r}",
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
                    f"unsupported encoder level for FFmpeg compositing: {profile.video_level}"
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


def _validate_filter_token(value: str, label: str) -> str:
    if not _SAFE_FILTER_TOKEN.fullmatch(value):
        raise ValueError(f"{label} contains characters FFmpeg cannot safely parse")
    return value


def _layer_has_alpha(probe: MediaProbe, z: int) -> bool:
    """Detect an alpha plane the compositor must preserve.

    The native VP9 decoder drops alpha, and ffprobe itself reports a plain
    ``yuv420p`` for a WebM/VP9 stream whose alpha plane is intact, so a z>0
    VP9 input is treated as alpha regardless of the probed pixel format.
    """
    pixel_format = _text(probe.pixel_format) or ""
    if pixel_format.startswith(("yuva", "rgba", "gbrap")):
        return True
    if z > 0 and _text(probe.video_codec) == "vp9":
        return True
    return False


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


def _duration_frames_from_probe(probe: MediaProbe, profile: RenderProfile) -> int:
    frames = _duration_fraction(probe) * Fraction(*profile.fps_rational)
    return max(1, int(frames + Fraction(1, 2)))


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


def _plan_reasons(plan: Any) -> list[str]:
    """Return honest support reasons for a plan's layer contract."""

    reasons: list[str] = []
    segments = list(plan.segments)
    if not segments:
        reasons.append("plan has no segments; compositor requires at least 2 z layers")
        return reasons
    if any(segment.layer is None for segment in segments):
        reasons.append(
            "plan contains a layer=None segment; compositor requires an explicit "
            "layer on every segment (layer=None plans use rendering.ffmpeg-finalizer)"
        )
    if any(segment.layer is not None and segment.layer.blend != "normal" for segment in segments):
        reasons.append("compositor v1 supports only layer blend 'normal'")
    z_layers = [segment.layer.z for segment in segments if segment.layer is not None]
    if len(set(z_layers)) < 2:
        reasons.append(
            "plan requires at least 2 distinct z layers; single-layer plans use "
            "rendering.ffmpeg-finalizer"
        )
    if len(z_layers) != len(set(z_layers)):
        reasons.append(
            "compositor v1 accepts exactly one segment per z layer; duplicate z "
            "layers are not supported"
        )
    return reasons


def support(
    request: RenderRequest | FinalizeRequest,
    *,
    workspace: Path,
) -> SupportReport:
    """Return request-sensitive support evidence for layer compositing."""

    del workspace  # Support is profile/config sensitive; it does not read inputs.
    _faststart, reasons = _config(request.backend_config)
    plan = getattr(request, "plan", None)
    if plan is not None:
        reasons.extend(_plan_reasons(plan))
    profile = plan.profile if plan is not None else getattr(request, "profile", None)
    if profile is None:
        reasons.append("a canonical profile with canvas and frame rate is required")
    else:
        try:
            _validate_target_profile(profile)
        except RendererException as exc:
            reasons.append(exc.error.message)
        except (TypeError, ValueError) as exc:
            reasons.append(str(exc))
    for binary in ("ffmpeg", "ffprobe"):
        if shutil.which(binary) is None:
            reasons.append(f"required binary is unavailable: {binary}")
    if getattr(request, "audio", None) is not None:
        requested_audio = request.audio.value  # type: ignore[union-attr]
    elif plan is not None:
        requested_audio = "rendered" if plan.profile.has_audio else "none"
    else:
        requested_audio = "request-dependent"
    return SupportReport(
        schema_version=SCHEMA_VERSION,
        supported=not reasons,
        reasons=reasons,
        features={
            "layer_compositing": True,
            "straight_alpha": True,
            "short_layer_padding": True,
            "audio_ownership": requested_audio,
        },
        alternatives=[],
        backend=BACKEND_ID,
        backend_version=BACKEND_VERSION,
    )


def _probe_layer(
    path: Path,
    *,
    ownership: AudioOwnership,
    index: int,
) -> MediaProbe:
    try:
        probe = ffprobe_metadata_strict(path)
    except (MediaProbeError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise_invalid_artifact_error(
            backend=BACKEND_ID,
            message=f"segment[{index}] strict media probe failed: {exc}",
            recovery_command="rerender the segment as a valid canonical media artifact",
            details={"segment_index": index},
        )
    if not probe.has_video_stream:
        raise_invalid_artifact_error(
            backend=BACKEND_ID,
            message=f"segment[{index}] media has no video stream",
            recovery_command="rerender the segment with a video stream",
            details={"segment_index": index},
        )
    if ownership is AudioOwnership.RENDERED and not probe.has_audio_stream:
        raise_invalid_artifact_error(
            backend=BACKEND_ID,
            message=f"segment[{index}] declares rendered audio but has no audio stream",
            recovery_command="rerender the segment with its declared audio track",
            details={"segment_index": index},
        )
    if ownership is not AudioOwnership.RENDERED and probe.has_audio_stream:
        raise_invalid_artifact_error(
            backend=BACKEND_ID,
            message=(f"visual-only segment[{index}] unexpectedly contains an audio stream"),
            recovery_command="rerender the segment without an audio track",
            details={"segment_index": index},
        )
    return probe


def _preflight_layers(
    request: FinalizeRequest,
    *,
    workspace: Path,
) -> list[_PreparedLayer]:
    plan_reasons = _plan_reasons(request.plan)
    if plan_reasons:
        raise ValueError("; ".join(plan_reasons))
    canonical_fps = Fraction(*request.plan.profile.fps_rational)
    tolerance = request.plan.profile.duration_tolerance
    prepared: list[_PreparedLayer] = []
    for index, (artifact, plan_segment) in enumerate(
        zip(request.artifacts, request.plan.segments, strict=True)
    ):
        assert plan_segment.layer is not None
        layer = plan_segment.layer
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
        validate_render_result(
            segment_result,
            expected_profile=artifact.profile,
            workspace_root=workspace,
        )
        path = _input_path(artifact.path, workspace)
        probe = _probe_layer(
            path,
            ownership=artifact.audio,
            index=index,
        )
        artifact_seconds = Fraction(artifact.duration_frames, 1) / Fraction(
            *artifact.profile.fps_rational
        )
        if probe.frames is not None and probe.frames > 0:
            artifact_seconds = Fraction(probe.frames, 1) / Fraction(*artifact.profile.fps_rational)
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
        prepared.append(
            _PreparedLayer(
                index=index + 1,  # input 0 is the color base
                z=layer.z,
                path=path,
                opacity=layer.opacity,
                alpha=_layer_has_alpha(probe, layer.z),
                vp9=_text(probe.video_codec) == "vp9",
                audio=artifact.audio,
                duration_frames=_duration_frames_from_probe(probe, artifact.profile),
            )
        )
    prepared.sort(key=lambda layer: layer.z)
    return prepared


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
        if ownership is AudioOwnership.PASSTHROUGH:
            raise_invalid_artifact_error(
                backend=BACKEND_ID,
                message=(
                    "passthrough audio is incompatible with compositor output; "
                    "the compositor re-encodes audio to the canonical AAC profile"
                ),
                recovery_command="rerender segments with audio_ownership='rendered' or 'none'",
                details={"audio_ownership": [o.value for o in ownerships]},
            )
    if request.plan.profile.has_audio:
        return AudioOwnership.RENDERED
    return AudioOwnership.NONE


def build_composite_command(
    layers: Sequence[_PreparedLayer],
    output_path: Path,
    *,
    target_profile: RenderProfile,
    total_frames: int,
    ownership: AudioOwnership,
    faststart: bool,
) -> list[str]:
    """Build the single composite command without touching the filesystem.

    Filtergraph (z ascending): a full-length ``color`` base is input [0]; each
    layer is scaled/padded to the canvas, normalized to the canonical frame
    rate, and chained bottom-to-top with ``overlay``.  ``eof_action=pass`` on
    every overlay is the short-layer padding: once a layer's frames end, the
    accumulated result below shows through for the rest of the plan window.
    """

    width = target_profile.width
    height = target_profile.height
    fps = f"{target_profile.fps_rational[0]}/{target_profile.fps_rational[1]}"
    total_seconds = float(Fraction(total_frames, 1) / Fraction(*target_profile.fps_rational))
    argv = [
        "ffmpeg",
        "-hide_banner",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:s={width}x{height}:r={fps}:d={total_seconds}",
    ]
    for layer in layers:
        if layer.alpha and layer.vp9:
            # The native VP9 decoder drops alpha; force the libvpx decoder.
            argv.extend(["-c:v", "libvpx-vp9"])
        argv.extend(["-i", str(layer.path)])
    synthesize_audio = ownership is AudioOwnership.RENDERED and not any(
        layer.audio is AudioOwnership.RENDERED for layer in layers
    )
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

    filters: list[str] = []
    previous = "0:v"
    for position, layer in enumerate(layers):
        prepend = "format=yuva420p," if layer.alpha else ""
        opacity = ""
        if layer.opacity < 1:
            opacity = f",format=rgba,colorchannelmixer=aa={layer.opacity}"
        label = f"t{layer.index}"
        filters.append(
            f"[{layer.index}:v]{prepend}scale={width}:{height}:"
            f"force_original_aspect_ratio=decrease,pad={width}:{height}:"
            f"(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},setpts=PTS-STARTPTS{opacity}[{label}]"
        )
        if position == len(layers) - 1:
            filters.append(
                f"[{previous}][{label}]overlay=0:0:format=auto:eof_action=pass,format=yuv420p[vout]"
            )
        else:
            chain = f"l{layer.index}"
            filters.append(f"[{previous}][{label}]overlay=0:0:format=auto:eof_action=pass[{chain}]")
            previous = chain
    argv.extend(["-filter_complex", ";".join(filters), "-map", "[vout]"])

    if ownership is AudioOwnership.RENDERED:
        assert target_profile.audio_sample_rate is not None
        assert target_profile.audio_channel_layout is not None
        audio_format = (
            f"aformat=sample_rates={target_profile.audio_sample_rate}:"
            f"channel_layouts={target_profile.audio_channel_layout}"
        )
        if synthesize_audio:
            argv.extend(["-map", f"{len(layers) + 1}:a:0"])
        else:
            audio_source = next(layer for layer in layers if layer.audio is AudioOwnership.RENDERED)
            argv.extend(["-map", f"{audio_source.index}:a:0"])
        argv.extend(["-af", f"{audio_format},apad"])
        assert target_profile.audio_codec is not None
        argv.extend(["-c:a", _audio_encoder(target_profile.audio_codec)])
    else:
        argv.append("-an")

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
    argv.extend(["-video_track_timescale", str(_mp4_timescale(target_profile))])
    argv.extend(["-t", str(total_seconds)])
    if faststart:
        argv.extend(["-movflags", "+faststart"])
    argv.extend(["-f", "mp4", str(output_path)])
    return argv


def _run_checked(runner: Runner, argv: list[str]) -> None:
    runner(argv, check=True)


def _validate_compositor_output(
    output_path: Path,
    *,
    total_frames: int,
    target_profile: RenderProfile,
    ownership: AudioOwnership,
) -> None:
    """Probe the final composite against the canonical profile and frame count."""

    try:
        probe = ffprobe_metadata_strict(output_path)
    except (MediaProbeError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise_invalid_artifact_error(
            backend=BACKEND_ID,
            message=f"final composite could not be probed: {exc}",
            recovery_command="rerun finalization in a fresh invocation workspace",
            details={"error_type": type(exc).__name__},
        )
    if not probe.has_video_stream:
        raise_invalid_artifact_error(
            backend=BACKEND_ID,
            message="final composite has no video stream",
            recovery_command="rerun finalization in a fresh invocation workspace",
        )
    frames = _duration_fraction(probe) * Fraction(*target_profile.fps_rational)
    if abs(frames - total_frames) > target_profile.duration_tolerance:
        raise_invalid_artifact_error(
            backend=BACKEND_ID,
            message="final composite frame count does not match the planned total",
            recovery_command="rerun finalization in a fresh invocation workspace",
            details={
                "planned_total_frames": total_frames,
                "probed_total_frames": [frames.numerator, frames.denominator],
                "tolerance_frames": target_profile.duration_tolerance,
            },
        )
    if probe.width != target_profile.width or probe.height != target_profile.height:
        raise_invalid_artifact_error(
            backend=BACKEND_ID,
            message="final composite resolution does not match the canonical profile",
            recovery_command="rerun finalization in a fresh invocation workspace",
            details={
                "expected": [target_profile.width, target_profile.height],
                "actual": [probe.width, probe.height],
            },
        )
    if probe.video_codec and _text(probe.video_codec) != _text(target_profile.video_codec):
        raise_invalid_artifact_error(
            backend=BACKEND_ID,
            message="final composite video codec does not match the canonical profile",
            recovery_command="rerun finalization in a fresh invocation workspace",
            details={
                "expected": target_profile.video_codec,
                "actual": probe.video_codec,
            },
        )
    if _pixel_format_canonical(probe.pixel_format) != _pixel_format_canonical(
        target_profile.pixel_format
    ):
        raise_invalid_artifact_error(
            backend=BACKEND_ID,
            message="final composite pixel format does not match the canonical profile",
            recovery_command="rerun finalization in a fresh invocation workspace",
            details={
                "expected": target_profile.pixel_format,
                "actual": probe.pixel_format,
            },
        )
    if ownership is AudioOwnership.RENDERED:
        if not probe.has_audio_stream:
            raise_invalid_artifact_error(
                backend=BACKEND_ID,
                message="final composite is missing the required audio stream",
                recovery_command="rerun finalization in a fresh invocation workspace",
            )
        if probe.audio_codec and _text(probe.audio_codec) != _text(target_profile.audio_codec):
            raise_invalid_artifact_error(
                backend=BACKEND_ID,
                message="final composite audio codec does not match the canonical profile",
                recovery_command="rerun finalization in a fresh invocation workspace",
                details={
                    "expected": target_profile.audio_codec,
                    "actual": probe.audio_codec,
                },
            )
    elif probe.has_audio_stream:
        raise_invalid_artifact_error(
            backend=BACKEND_ID,
            message="final composite unexpectedly contains an audio stream",
            recovery_command="rerun finalization in a fresh invocation workspace",
        )


def finalize(
    request: FinalizeRequest,
    *,
    workspace: Path,
    runner: Runner | None = None,
) -> RenderResult:
    """Preflight the layer stack, composite it, and validate one render plan."""

    execute = subprocess.run if runner is None else runner
    if request.plan.finalizer.id != BACKEND_ID:
        raise ValueError(
            f"finalize request selects {request.plan.finalizer.id!r}, not {BACKEND_ID!r}"
        )
    faststart, config_reasons = _config(request.backend_config)
    if config_reasons:
        raise ValueError("; ".join(config_reasons))

    # This loop completes in full before the first FFmpeg subprocess starts.
    layers = _preflight_layers(request, workspace=workspace)
    ownership = _final_audio_ownership(request)
    target_profile = (
        request.plan.profile
        if ownership is AudioOwnership.RENDERED
        else _profile_without_audio(request.plan.profile)
    )
    _validate_target_profile(target_profile)
    output_path = _safe_protocol_output_path(workspace, request.output_name)
    # Frame-count authority: overlapping layers must never be summed; the
    # plan's canonical total is the only authority.
    total_frames = request.plan.total_frames

    recovery_tmp = TemporaryDirectory(
        prefix=f".{output_path.name}.ffmpeg-compositor-recovery-",
        dir=str(output_path.parent),
    )
    previous_output = (
        Path(recovery_tmp.name) / "previous-output.mp4" if output_path.is_file() else None
    )
    published = False
    try:
        if previous_output is not None:
            shutil.copy2(output_path, previous_output)
        with TemporaryDirectory(
            prefix=f".{output_path.name}.ffmpeg-compositor-stage-",
            dir=str(output_path.parent),
        ) as stage_text:
            stage_dir = Path(stage_text)
            staged_output = stage_dir / "final" / output_path.name
            staged_output.parent.mkdir(parents=True, exist_ok=True)
            _run_checked(
                execute,
                build_composite_command(
                    layers,
                    staged_output,
                    target_profile=target_profile,
                    total_frames=total_frames,
                    ownership=ownership,
                    faststart=faststart,
                ),
            )
            if not staged_output.is_file() or staged_output.stat().st_size <= 0:
                raise_invalid_artifact_error(
                    backend=BACKEND_ID,
                    message="FFmpeg did not produce a composited video",
                    recovery_command="rerun finalization in a fresh invocation workspace",
                    details={"output": output_path.name},
                )
            os.replace(staged_output, output_path)
            published = True
        video = VideoArtifact.from_file(
            path=output_path,
            workspace_root=workspace,
            profile=target_profile,
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
                    "finalizer_kind": "ffmpeg-compositor",
                    "finalizer_version": BACKEND_VERSION,
                    "layer_count": len(layers),
                    "layers": [
                        {
                            "z": layer.z,
                            "input_index": layer.index,
                            "alpha": layer.alpha,
                            "opacity": layer.opacity,
                            "duration_frames": layer.duration_frames,
                        }
                        for layer in layers
                    ],
                    "audio_mode": ownership.value,
                    "audio_source_z": next(
                        (layer.z for layer in layers if layer.audio is AudioOwnership.RENDERED),
                        None,
                    ),
                }
            },
            normalization=[],
            logs=[],
            metadata=request.metadata,
        )
        request.validate_final_result(result)
        _validate_compositor_output(
            output_path,
            total_frames=total_frames,
            target_profile=request.plan.profile,
            ownership=ownership,
        )
        return result
    except BaseException:
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
    "build_composite_command",
    "finalize",
    "main",
    "support",
]
