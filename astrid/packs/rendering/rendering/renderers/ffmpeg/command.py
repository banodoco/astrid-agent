"""Pure FFmpeg command builders for the media-spine and text-overlay renderer.

The builders read the immutable request inputs and return argv.  They do not
create directories, write files, or launch subprocesses, which keeps command
construction independently testable from execution and publication.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from astrid.core import timeline
from astrid.core.rendering.contracts import RenderRequest


@dataclass(frozen=True)
class TextOverlaySpec:
    """One caller-rasterized text PNG composited over the video spine.

    ``path`` is the PNG file; ``at``/``end`` are absolute timeline seconds
    bounding the overlay window; ``fade_in``/``fade_out`` are seconds.
    Callers pass overlays already ordered (track array order, then ``at``,
    then clip index); later entries composite on top.
    """

    path: str
    at: float
    end: float
    fade_in: float
    fade_out: float


@dataclass(frozen=True)
class RenderCommandInputs:
    """Resolved, validated inputs used to construct one FFmpeg argv."""

    timeline_path: Path
    assets_path: Path
    output_path: Path
    timeline_data: dict[str, Any]
    registry: dict[str, Any]
    audio_sample_rate: int = 48000
    # Probe-derived evidence from strict support: stream-copy is only
    # permitted when the probe confirms the entire source (never trust
    # registry metadata alone).
    stream_copy_allowed: bool = False
    text_overlays: tuple[TextOverlaySpec, ...] = ()


def timeline_canvas(timeline_data: Mapping[str, Any]) -> tuple[int, int, int]:
    canvas = (
        timeline_data.get("theme_overrides", {})
        .get("visual", {})
        .get("canvas", {})
    )
    return (
        int(canvas.get("width", 1920)),
        int(canvas.get("height", 1080)),
        int(canvas.get("fps", 30)),
    )


def _media_clip_groups(
    timeline_data: Mapping[str, Any],
) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]], list[Mapping[str, Any]]]:
    tracks = {track.get("id"): track for track in timeline_data.get("tracks", [])}
    visual_track_ids = {
        track["id"] for track in tracks.values() if track.get("kind") == "visual"
    }
    audio_track_ids = {
        track["id"] for track in tracks.values() if track.get("kind") == "audio"
    }
    visual = [
        clip for clip in timeline_data.get("clips", [])
        if clip.get("track") in visual_track_ids and clip.get("clipType") == "media"
    ]
    base = sorted(
        [clip for clip in visual if "hold" not in clip],
        key=lambda clip: float(clip.get("at", 0) or 0),
    )
    overlays = sorted(
        [clip for clip in visual if "hold" in clip],
        key=lambda clip: float(clip.get("at", 0) or 0),
    )
    audio = sorted(
        [clip for clip in timeline_data.get("clips", [])
         if clip.get("track") in audio_track_ids and clip.get("clipType") == "media"],
        key=lambda clip: float(clip.get("at", 0) or 0),
    )
    return base, overlays, audio


def _ordered_asset_keys(timeline_data: Mapping[str, Any]) -> list[str]:
    base, overlays, audio = _media_clip_groups(timeline_data)
    keys: list[str] = []
    for clip in [*base, *overlays, *audio]:
        key = str(clip.get("asset") or "")
        if key and key not in keys:
            keys.append(key)
    return keys


def clip_duration_seconds(clip: Mapping[str, Any]) -> float:
    clip_id = clip.get("id")

    def number(value: Any, label: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"Clip {clip_id!r} {label} must be a finite number")
        result = float(value)
        if not math.isfinite(result):
            raise ValueError(f"Clip {clip_id!r} {label} must be a finite number")
        return result

    if "hold" in clip:
        if "from" in clip or "to" in clip:
            raise ValueError(
                f"Clip {clip_id!r} static hold cannot also declare source bounds"
            )
        hold = number(clip.get("hold"), "hold")
        if hold <= 0:
            raise ValueError(f"Clip {clip_id!r} hold must be positive")
        return hold

    start = number(clip.get("from", 0), "from")
    if "to" not in clip:
        raise ValueError(f"Clip {clip_id!r} must declare a source to bound")
    end = number(clip.get("to"), "to")
    speed = number(clip.get("speed", 1), "speed")
    if speed <= 0:
        raise ValueError(f"Clip {clip_id!r} has non-positive speed {speed}")
    if start < 0 or end <= start:
        raise ValueError(
            f"Clip {clip_id!r} must have positive source bounds with to > from"
        )
    return (end - start) / speed


def validate_ffmpeg_media_timeline(timeline_data: Mapping[str, Any]) -> None:
    """Reject every media-timeline semantic the pure builder would discard."""

    # Local import avoids a module cycle: support owns semantic validation and
    # imports this module only for command construction helpers.
    from astrid.packs.rendering.rendering.renderers.ffmpeg.support import structural_reasons

    reasons = structural_reasons(
        timeline_data,
        allow_audio_reactive=False,
    )
    if reasons:
        raise ValueError(reasons[0])


def _input_path(raw_path: str, workspace: Path) -> Path:
    candidate = Path(raw_path).expanduser()
    return (
        candidate if candidate.is_absolute() else workspace / candidate
    ).resolve()


def _coerce_request(request: RenderRequest | Mapping[str, Any]) -> RenderRequest:
    if isinstance(request, RenderRequest):
        return request
    return RenderRequest.from_dict(request)


def resolve_render_command_inputs(
    request: RenderRequest | Mapping[str, Any],
    workspace: Path,
) -> RenderCommandInputs:
    """Resolve the request's existing input files without mutating anything."""

    normalized = _coerce_request(request)
    root = Path(workspace).resolve()
    timeline_path = _input_path(normalized.timeline_path, root)
    if normalized.assets_registry_path is None:
        raise ValueError("rendering.ffmpeg requires an assets registry")
    assets_path = _input_path(normalized.assets_registry_path, root)
    if not timeline_path.exists():
        raise FileNotFoundError(f"Timeline missing: {timeline_path}")
    if not assets_path.exists():
        raise FileNotFoundError(f"Asset registry missing: {assets_path}")
    timeline_data = json.loads(timeline_path.read_text(encoding="utf-8"))
    if not isinstance(timeline_data, dict):
        raise ValueError("timeline must contain a JSON object")
    registry = timeline.load_registry(assets_path)
    validate_ffmpeg_media_timeline(timeline_data)
    return RenderCommandInputs(
        timeline_path=timeline_path,
        assets_path=assets_path,
        output_path=(root / "outputs" / normalized.output_name).resolve(),
        timeline_data=timeline_data,
        registry=dict(registry),
    )


def build_filter_graph(
    inputs: RenderCommandInputs,
) -> tuple[list[str], int | None]:
    """Return the filter graph and optional stream-copy input index."""

    timeline_data = inputs.timeline_data
    registry = inputs.registry
    width, height, fps = timeline_canvas(timeline_data)
    tracks = {
        track.get("id"): track for track in timeline_data.get("tracks", [])
    }
    video_clips, overlay_clips, audio_clips = _media_clip_groups(timeline_data)
    if not video_clips:
        raise ValueError("ffmpeg engine needs at least one visual media clip")

    asset_keys: list[str] = []
    for clip in [*video_clips, *overlay_clips, *audio_clips]:
        asset_key = str(clip.get("asset") or "")
        if not asset_key:
            raise ValueError(f"Clip {clip.get('id')!r} has no asset")
        if asset_key not in registry["assets"]:
            raise ValueError(
                f"Clip {clip.get('id')!r} references unknown asset "
                f"{asset_key!r}"
            )
        if asset_key not in asset_keys:
            asset_keys.append(asset_key)

    asset_index = {
        asset_key: index for index, asset_key in enumerate(asset_keys)
    }
    filters: list[str] = []
    video_labels: list[str] = []
    copy_video_input: int | None = None
    if len(video_clips) == 1 and not overlay_clips:
        clip = video_clips[0]
        asset_key = str(clip["asset"])
        entry = registry["assets"][asset_key]
        source_duration = entry.get("duration")
        source_resolution = entry.get("resolution")
        source_fps = entry.get("fps")
        start = float(clip.get("from", 0) or 0)
        end = float(clip.get("to", start) or start)
        at = float(clip.get("at", 0) or 0)
        full_duration = (
            isinstance(source_duration, (int, float))
            and abs((end - start) - float(source_duration)) < 0.05
        )
        same_resolution = source_resolution == f"{width}x{height}"
        same_fps = (
            isinstance(source_fps, (int, float))
            and not isinstance(source_fps, bool)
            and math.isfinite(float(source_fps))
            and abs(float(source_fps) - fps) < 1e-6
        )
        no_visual_adjustments = not any(
            key in clip
            for key in (
                "x",
                "y",
                "width",
                "height",
                "cropTop",
                "cropBottom",
                "cropLeft",
                "cropRight",
                "effects",
                "transition",
            )
        )
        if (
            inputs.stream_copy_allowed
            and not inputs.text_overlays
            and at == 0
            and start == 0
            and full_duration
            and same_resolution
            and same_fps
            and no_visual_adjustments
        ):
            copy_video_input = asset_index[asset_key]
    if copy_video_input is None:
        for index, clip in enumerate(video_clips):
            inp = asset_index[str(clip["asset"])]
            start = float(clip.get("from", 0) or 0)
            end = float(clip.get("to", start) or start)
            label = f"v{index}"
            filters.append(
                f"[{inp}:v]trim=start={start:.6f}:end={end:.6f},"
                "setpts=PTS-STARTPTS,"
                f"scale={width}:{height}:force_original_aspect_ratio=decrease,"
                f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
                f"fps={fps},format=yuv420p[{label}]"
            )
            video_labels.append(f"[{label}]")
        concat_label = "vbase" if overlay_clips else "vout"
        filters.append("".join(video_labels) + f"concat=n={len(video_labels)}:v=1:a=0[{concat_label}]")
        spine = concat_label
        if overlay_clips:
            overlay = overlay_clips[0]
            overlay_input = asset_index[str(overlay["asset"])]
            overlay_duration = clip_duration_seconds(overlay)
            filters.append(
                f"[{overlay_input}:v]loop=loop=-1:size=1:start=0,"
                f"trim=duration={overlay_duration:.6f},setpts=PTS-STARTPTS,"
                f"fps={fps},scale={width}:{height}:flags=lanczos,"
                "format=rgba[voverlay]"
            )
            filters.append("[vbase][voverlay]overlay=0:0:format=auto[vout]")
            spine = "vout"
        for k, overlay in enumerate(inputs.text_overlays):
            # ffmpeg's fade filter treats duration=0 as nb_frames (default
            # 25), so each side is emitted only when its duration is
            # positive; a no-envelope overlay composites instantly.
            steps = [f"[{len(asset_keys) + k}:v]format=rgba"]
            if overlay.fade_in > 0:
                steps.append(
                    f"fade=t=in:st={overlay.at:.6f}:d={overlay.fade_in:.6f}:alpha=1"
                )
            if overlay.fade_out > 0:
                steps.append(
                    f"fade=t=out:st={overlay.end - overlay.fade_out:.6f}:"
                    f"d={overlay.fade_out:.6f}:alpha=1"
                )
            filters.append(",".join(steps) + f"[ov{k}]")
            spine_out = (
                "vout" if k == len(inputs.text_overlays) - 1 else f"vout{k + 1}"
            )
            filters.append(
                f"[{spine}][ov{k}]overlay=0:0:"
                f"enable='between(t,{overlay.at:.6f},{overlay.end:.6f})':"
                f"format=auto[{spine_out}]"
            )
            spine = spine_out

    from astrid.core.timeline.audio_envelope import compile_legacy, ffmpeg_expression
    from astrid.packs.rendering.rendering.renderers.ffmpeg.support import effective_gain

    fps = timeline_canvas(timeline_data)[2]
    envelopes = [compile_legacy(clip, fps) for clip in audio_clips]
    if any(envelope is not None for envelope in envelopes):
        # Per-sample evaluation after source trim/resampling, before placement.
        # Mixing is unnormalised: base track/clip gains retain their meaning.
        labels = []
        for index, (clip, envelope) in enumerate(zip(audio_clips, envelopes)):
            inp = asset_index[str(clip["asset"])]
            start, end = float(clip.get("from", 0)), float(clip["to"])
            base = effective_gain(tracks[str(clip["track"])], clip)
            expression = ffmpeg_expression(envelope) if envelope is not None else "1"
            delay = math.floor(float(clip.get("at", 0)) * inputs.audio_sample_rate + 0.5)
            label = f"env{index}"
            filters.append(
                f"[{inp}:a]atrim=start={start:.9f}:end={end:.9f},asetpts=PTS-STARTPTS,"
                f"aformat=sample_rates={inputs.audio_sample_rate}:channel_layouts=stereo,"
                f"aeval=exprs='val(ch)*({base:.17g})*({expression})':c=same,"
                f"adelay={delay}S:all=1[{label}]"
            )
            labels.append(f"[{label}]")
        duration = max(float(clip.get("at", 0)) + clip_duration_seconds(clip) for clip in video_clips)
        filters.append("".join(labels) + f"amix=inputs={len(labels)}:normalize=0:dropout_transition=0,"
                       f"apad=whole_len={math.floor(duration * inputs.audio_sample_rate + 0.5)},"
                       f"atrim=end_sample={math.floor(duration * inputs.audio_sample_rate + 0.5)}[aout]")
        return filters, copy_video_input

    audio_labels: list[str] = []
    cursor = 0.0
    audio_index = 0
    for clip in audio_clips:
        at = float(clip.get("at", 0))
        if at > cursor + 1e-9:
            duration = at - cursor
            label = f"a{audio_index}"
            filters.append(
                f"anullsrc=r={inputs.audio_sample_rate}:cl=stereo,"
                f"atrim=duration={duration:.6f}[{label}]"
            )
            audio_labels.append(f"[{label}]")
            audio_index += 1
        inp = asset_index[str(clip["asset"])]
        start = float(clip.get("from", 0))
        end = float(clip.get("to"))
        track = tracks[str(clip["track"])]
        from astrid.packs.rendering.rendering.renderers.ffmpeg.support import effective_gain

        volume = effective_gain(track, clip)
        label = f"a{audio_index}"
        filters.append(
            f"[{inp}:a]atrim=start={start:.6f}:end={end:.6f},"
            "asetpts=PTS-STARTPTS,"
            f"aformat=sample_rates={inputs.audio_sample_rate}:channel_layouts=stereo,"
            f"volume={volume:.6f}[{label}]"
        )
        audio_labels.append(f"[{label}]")
        cursor = at + clip_duration_seconds(clip)
        audio_index += 1

    if audio_clips:
        visual_duration = max(
            float(clip.get("at", 0)) + clip_duration_seconds(clip)
            for clip in video_clips
        )
        if visual_duration > cursor + 1e-9:
            duration = visual_duration - cursor
            label = f"a{audio_index}"
            filters.append(
                f"anullsrc=r={inputs.audio_sample_rate}:cl=stereo,"
                f"atrim=duration={duration:.6f}[{label}]"
            )
            audio_labels.append(f"[{label}]")
        filters.append(
            "".join(audio_labels)
            + f"concat=n={len(audio_labels)}:v=0:a=1[aout]"
        )
    return filters, copy_video_input


def _has_audio_clips(timeline_data: Mapping[str, Any]) -> bool:
    tracks = {
        track.get("id"): track
        for track in timeline_data.get("tracks", [])
        if isinstance(track, Mapping)
    }
    return any(
        isinstance(clip, Mapping)
        and clip.get("clipType") == "media"
        and tracks.get(clip.get("track"), {}).get("kind") == "audio"
        for clip in timeline_data.get("clips", [])
    )


def _asset_input_argv(inputs: RenderCommandInputs) -> list[str]:
    timeline_data = inputs.timeline_data
    registry = inputs.registry
    asset_keys = _ordered_asset_keys(timeline_data)

    argv: list[str] = []
    for asset_key in asset_keys:
        entry = registry["assets"][asset_key]
        file_value = entry.get("file")
        if not isinstance(file_value, str) or not file_value:
            raise ValueError(
                "ffmpeg engine requires local file assets; "
                f"{asset_key!r} has no file"
            )
        asset_path = Path(file_value)
        if not asset_path.is_absolute():
            asset_path = (inputs.assets_path.parent / asset_path).resolve()
        kind = str(entry.get("type", "")).lower()
        is_still = kind in {"image", "still", "image/png", "image/jpeg", "image/webp"}
        if not is_still:
            is_still = asset_path.suffix.lower() in {
                ".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"
            }
        if is_still:
            # Image files are one-frame sources.  Loop only typed stills and
            # bound the input to the latest authored clip end; media clips
            # retain ordinary finite source semantics.
            image_clips = [
                clip
                for clip in timeline_data.get("clips", [])
                if isinstance(clip, Mapping)
                and clip.get("clipType") == "media"
                and clip.get("asset") == asset_key
            ]
            end = max(
                clip_duration_seconds(clip)
                for clip in image_clips
            )
            argv.extend(["-loop", "1", "-t", f"{end:.6f}", "-i", str(asset_path)])
        else:
            argv.extend(["-i", str(asset_path)])
    for overlay in inputs.text_overlays:
        argv.extend(
            ["-loop", "1", "-t", f"{overlay.end:.6f}", "-i", str(overlay.path)]
        )
    return argv


def build_render_command_from_inputs(inputs: RenderCommandInputs) -> list[str]:
    """Return FFmpeg argv for already-resolved, strictly supported inputs."""
    filters, copy_video_input = build_filter_graph(inputs)
    has_audio = _has_audio_clips(inputs.timeline_data)
    return [
        "ffmpeg",
        "-hide_banner",
        "-y",
        *_asset_input_argv(inputs),
        *(["-filter_complex", ";".join(filters)] if filters else []),
        "-map",
        (
            f"{copy_video_input}:v:0"
            if copy_video_input is not None
            else "[vout]"
        ),
        *(["-map", "[aout]"] if has_audio else []),
        "-c:v",
        "copy" if copy_video_input is not None else "libx264",
        *(
            ["-preset", "veryfast", "-crf", "20"]
            if copy_video_input is None
            else []
        ),
        *(["-pix_fmt", "yuv420p"] if copy_video_input is None else []),
        *(
            ["-c:a", "aac", "-b:a", "192k"]
            if has_audio
            else ["-an"]
        ),
        "-movflags",
        "+faststart",
        str(inputs.output_path),
    ]


def build_render_command(
    request: RenderRequest | Mapping[str, Any],
    workspace: Path,
    *,
    text_overlays: tuple[TextOverlaySpec, ...] = (),
) -> list[str]:
    """Build FFmpeg argv for ``workspace/outputs/<request.output_name>``.

    Stream-copy is permitted only when strict support's probe evidence says
    the whole source is compatible (never trust registry metadata alone).
    Text overlays are caller-provided rasterized PNG specs; nothing is
    rasterized or fade-parsed here.
    """
    inputs = resolve_render_command_inputs(request, workspace)
    try:
        from astrid.core.rendering.contracts import RenderRequest
        from astrid.packs.rendering.rendering.renderers.ffmpeg.support import support

        normalized_request = (
            request
            if isinstance(request, RenderRequest)
            else RenderRequest.from_dict(request)
        )
        report = support(
            normalized_request,
            inputs.timeline_data,
            inputs.registry,
        )
        stream_copy_allowed = (
            report.supported and bool(report.features.get("stream_copy"))
        )
    except Exception:
        stream_copy_allowed = False
    inputs = replace(
        inputs,
        stream_copy_allowed=stream_copy_allowed,
        text_overlays=text_overlays,
    )
    return build_render_command_from_inputs(inputs)


__all__ = [
    "RenderCommandInputs",
    "TextOverlaySpec",
    "build_filter_graph",
    "build_render_command",
    "build_render_command_from_inputs",
    "clip_duration_seconds",
    "resolve_render_command_inputs",
    "timeline_canvas",
    "validate_ffmpeg_media_timeline",
]
