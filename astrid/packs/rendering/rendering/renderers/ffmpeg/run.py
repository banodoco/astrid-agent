#!/usr/bin/env python3
"""FFmpeg renderer and raw rendering-protocol v1 command adapter."""

from __future__ import annotations

import argparse
import json
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import replace
from fractions import Fraction
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from astrid.core import timeline
from astrid.core.foundation.atomic_io import write_json_atomic
from astrid.core.foundation.paths import REPO_ROOT
from astrid.core.media import MediaProbe, MediaProbeError, ffprobe_metadata_strict
from astrid.core.rendering.artifacts import validate_render_result
from astrid.core.rendering.contracts import (
    SCHEMA_VERSION,
    AudioOwnership,
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
from astrid.packs.rendering.rendering.renderers.ffmpeg import audio_reactive_colour
from astrid.packs.rendering.rendering.renderers.ffmpeg.command import (
    TextOverlaySpec,
    build_render_command,
    timeline_canvas,
)
from astrid.packs.rendering.rendering.renderers.ffmpeg.support import (
    ALTERNATIVE_BACKENDS,
    BACKEND_ID,
    BACKEND_VERSION,
)
from astrid.packs.rendering.rendering.renderers.ffmpeg.support import (
    support as strict_support,
)
from astrid.packs.rendering.rendering.renderers.remotion import run as remotion_backend
from astrid.packs.rendering.rendering.renderers.ffmpeg.text import (
    _parse_fades,
    _text_window,
    rasterize_text_clip,
)

def _input_path(raw_path: str, workspace: Path) -> Path:
    candidate = Path(raw_path).expanduser()
    return (candidate if candidate.is_absolute() else workspace / candidate).resolve()


def _text_overlay_specs(
    timeline_data: Mapping[str, Any],
    *,
    rasterize_dir: Path,
) -> tuple[TextOverlaySpec, ...]:
    """Rasterize every text clip and return the caller-ordered overlay specs.

    Ordering follows TextOverlaySpec's contract: track array order, then
    ``at``, then clip index; later entries composite on top. Fades come from
    ``_parse_fades`` — the same reader strict support already ran — and
    windows from ``_text_window``. No text clips -> ``()``.
    """
    if not any(
        isinstance(clip, Mapping) and clip.get("clipType") == "text"
        for clip in timeline_data.get("clips", [])
    ):
        return ()
    width, height, _fps = timeline_canvas(timeline_data)
    track_order = {
        str(track.get("id")): index
        for index, track in enumerate(timeline_data.get("tracks", []))
        if isinstance(track, Mapping)
    }
    text_clips = sorted(
        (
            (index, clip)
            for index, clip in enumerate(timeline_data.get("clips", []))
            if isinstance(clip, Mapping) and clip.get("clipType") == "text"
        ),
        key=lambda item: (
            track_order.get(str(item[1].get("track")), len(track_order)),
            float(item[1].get("at", 0) or 0),
            item[0],
        ),
    )
    specs: list[TextOverlaySpec] = []
    for order, (_index, clip) in enumerate(text_clips):
        dest = rasterize_dir / f"text-{order}.png"
        rasterize_text_clip(clip, width, height, dest)
        at, end = _text_window(clip)
        fade_in, fade_out = _parse_fades(clip.get("effects"))
        specs.append(
            TextOverlaySpec(
                path=str(dest),
                at=at,
                end=end,
                fade_in=fade_in,
                fade_out=fade_out,
            )
        )
    return tuple(specs)


def support(request: RenderRequest, *, workspace: Path) -> SupportReport:
    """Load request files and delegate to the fail-closed evaluator."""

    if request.metadata.get("review") is not None:
        return _support_load_failure("Review overlay requires rendering.remotion or rendering.threejs")
    timeline_path = _input_path(request.timeline_path, workspace)
    if request.assets_registry_path is None:
        return _support_load_failure("rendering.ffmpeg requires an assets registry")
    assets_path = _input_path(request.assets_registry_path, workspace)
    try:
        timeline_data = json.loads(timeline_path.read_text(encoding="utf-8"))
        if not isinstance(timeline_data, dict):
            raise ValueError("timeline must contain a JSON object")
        from astrid.core.rendering.compiled_audio import attach_compiled_audio
        timeline_data = attach_compiled_audio(timeline_data, request.compiled_audio, request.timeline_authority)
        assets = timeline.load_registry(assets_path)
    except Exception as exc:  # noqa: BLE001 - support reports normalize adapter failures
        return _support_load_failure(str(exc) or type(exc).__name__)

    localized = replace(
        request,
        timeline_path=str(timeline_path),
        assets_registry_path=str(assets_path),
    )
    return strict_support(
        localized,
        timeline_data,
        assets,
        probe=ffprobe_metadata_strict,
    )


def _support_load_failure(reason: str) -> SupportReport:
    return SupportReport(
        schema_version=SCHEMA_VERSION,
        supported=False,
        reasons=[reason],
        features={
            "media_only": False,
            "full_timeline": True,
            "windows": False,
            "sequential_audio": True,
            "audio_reactive_colour": False,
            "text_overlay": False,
            "fade_envelope": False,
            "whole_media": False,
            "whole_media_optimization": False,
            "stream_copy": False,
            "static_image_overlay": False,
            "audio_ownership": AudioOwnership.NONE.value,
        },
        alternatives=list(ALTERNATIVE_BACKENDS),
        backend=BACKEND_ID,
        backend_version=BACKEND_VERSION,
    )


def _required(value: Any, label: str) -> Any:
    if value is None:
        raise RuntimeError(f"ffprobe did not report {label}")
    return value


def _profile_from_probe(
    probe: MediaProbe,
    ownership: AudioOwnership,
) -> RenderProfile:
    if not probe.has_video_stream:
        raise RuntimeError("ffprobe did not report a video stream")
    if ownership is AudioOwnership.RENDERED and not probe.has_audio_stream:
        raise RuntimeError("rendering.ffmpeg media output did not contain its rendered audio")
    if ownership is not AudioOwnership.RENDERED and probe.has_audio_stream:
        raise RuntimeError("rendering.ffmpeg visual-only output unexpectedly contained audio")
    audio_layout = probe.audio_channel_layout
    if audio_layout is None and probe.audio_channels == 2:
        audio_layout = "stereo"
    elif audio_layout is None and probe.audio_channels == 1:
        audio_layout = "mono"
    return RenderProfile(
        width=_required(probe.width, "video width"),
        height=_required(probe.height, "video height"),
        fps_rational=_required(probe.fps_rational, "video frame rate"),
        time_base=_required(probe.time_base, "video time base"),
        container=_required(probe.container, "container"),
        video_codec=_required(probe.video_codec, "video codec"),
        video_profile=probe.video_profile,
        video_level=probe.video_level,
        pixel_format=_required(probe.pixel_format, "pixel format"),
        audio_codec=(
            _required(probe.audio_codec, "audio codec")
            if ownership is AudioOwnership.RENDERED
            else None
        ),
        audio_sample_rate=(
            _required(probe.audio_sample_rate, "audio sample rate")
            if ownership is AudioOwnership.RENDERED
            else None
        ),
        audio_channel_layout=(
            _required(audio_layout, "audio channel layout")
            if ownership is AudioOwnership.RENDERED
            else None
        ),
        duration_tolerance=1,
    )


def _duration_frames(probe: MediaProbe, profile: RenderProfile) -> int:
    if probe.duration_rational is not None:
        duration = Fraction(*probe.duration_rational)
    elif probe.duration_seconds is not None:
        duration = Fraction(str(probe.duration_seconds))
    else:
        raise RuntimeError("ffprobe did not report a video duration")
    frames = duration * Fraction(*profile.fps_rational)
    return max(1, int(frames + Fraction(1, 2)))


def _protocol_render(
    request: RenderRequest,
    *,
    workspace: Path,
) -> RenderResult:
    report = support(request, workspace=workspace)
    if not report.supported:
        raise_unsupported_error(
            backend=BACKEND_ID,
            message="FFmpeg does not support this render request",
            recovery_command="resolve the reported support reasons and retry",
            details={"reasons": report.reasons, "features": report.features},
        )

    ownership = AudioOwnership(str(report.features["audio_ownership"]))
    timeline_path = _input_path(request.timeline_path, workspace)
    if request.assets_registry_path is None:
        raise ValueError("rendering.ffmpeg requires an assets registry")
    assets_path = _input_path(request.assets_registry_path, workspace)
    specialization_spec: audio_reactive_colour.AudioReactiveColourSpec | None = None
    if report.features.get("audio_reactive_colour") is True:
        timeline_data = json.loads(timeline_path.read_text(encoding="utf-8"))
        registry = timeline.load_registry(assets_path)
        specialization_spec = audio_reactive_colour.match_and_validate(
            timeline_data,
            registry,
            assets_path,
        )
        if specialization_spec is None:
            raise RuntimeError(
                "audio-reactive support evidence did not produce a specialization spec"
            )

    outputs_dir = workspace / "outputs"
    output_path = outputs_dir / request.output_name
    outputs_dir.mkdir(parents=True, exist_ok=True)
    output_path.unlink(missing_ok=True)
    try:
        if specialization_spec is not None:
            audio_reactive_colour.render(specialization_spec, output_path)
        else:
            with TemporaryDirectory(
                prefix="astrid-ffmpeg-text-overlays-"
            ) as overlay_tmp:
                timeline_data = json.loads(
                    timeline_path.read_text(encoding="utf-8")
                )
                text_overlays = _text_overlay_specs(
                    timeline_data, rasterize_dir=Path(overlay_tmp)
                )
                subprocess.run(
                    build_render_command(
                        request, workspace, text_overlays=text_overlays
                    ),
                    check=True,
                )
        try:
            probe = ffprobe_metadata_strict(output_path)
            probed_profile = _profile_from_probe(probe, ownership)
        except (MediaProbeError, RuntimeError) as exc:
            raise_invalid_artifact_error(
                backend=BACKEND_ID,
                message=f"FFmpeg output could not be validated: {exc}",
                recovery_command=("rerun rendering.ffmpeg in a fresh invocation workspace"),
                details={"error_type": type(exc).__name__},
            )
        declared_profile = request.profile or probed_profile
        duration_frames = _duration_frames(probe, declared_profile)
        backend_provenance = remotion_backend._render_provenance_payload(
            project_dir=REPO_ROOT / "remotion",
            composition_id="TimelineComposition",
            theme_path=None,
            active_theme=None,
            registry_state=remotion_backend._effective_registry_state(None),
            stage_summary={"root": None, "effects": []},
        )
        fragment: dict[str, Any] = {
            "renderer": "ffmpeg",
            "renderer_version": BACKEND_VERSION,
            "support_evidence": report.features,
            **backend_provenance,
        }
        if specialization_spec is not None:
            markers = [
                {
                    "frame": event.frame,
                    "color": event.color,
                    "id": event.event_id,
                }
                for event in specialization_spec.events
            ]
            specialization_fragment = {
                "id": audio_reactive_colour.ADAPTER_ID,
                "markers": markers,
                "event_count": len(specialization_spec.events),
                "frame_count": specialization_spec.total_frames,
                "fps": specialization_spec.fps,
                "marker_sha256": specialization_spec.marker_sha256,
            }
            fragment["specialization"] = specialization_fragment
        video = VideoArtifact.from_file(
            path=output_path,
            workspace_root=workspace,
            profile=declared_profile,
            duration_frames=duration_frames,
            audio=ownership,
        )
        result = RenderResult(
            schema_version=SCHEMA_VERSION,
            video=video,
            audio_ownership=ownership,
            backend_fragments={BACKEND_ID: fragment},
            normalization=[],
            logs=[],
            metadata=request.metadata,
        )
        validate_render_result(
            result,
            expected_profile=declared_profile,
            workspace_root=workspace,
        )
        return result
    except BaseException:
        output_path.unlink(missing_ok=True)
        raise


def _load_request(path: Path) -> RenderRequest:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise TypeError("render request must contain a JSON object")
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
    parser.add_argument("verb", choices=("render", "support"))
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--result", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        request_path = args.request.resolve(strict=True)
        result_path = args.result.resolve()
        if request_path == result_path:
            raise ValueError("--request and --result must be different paths")
        request = _load_request(request_path)
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
            response = support(request, workspace=workspace)
        else:
            response = _protocol_render(request, workspace=workspace)
        write_json_atomic(result_path, response.to_dict())
    except RendererException as exc:
        _write_failure(result_path, exc, kind=exc.error.kind)
    except FileNotFoundError as exc:
        _write_failure(result_path, exc, kind="binary_missing")
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        _write_failure(result_path, exc, kind="protocol")
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
    "main",
    "support",
]
