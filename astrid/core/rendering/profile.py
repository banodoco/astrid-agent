"""Canonical render-profile resolution for timeline rendering."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping, Sequence
from fractions import Fraction
from pathlib import Path
from typing import Any

from astrid.core.theme import builtin_theme, load_runtime_theme
from astrid.core.timeline import Timeline

from .contracts import AudioOwnership, RenderProfile

_DEFAULT_CANVAS = {"width": 1920, "height": 1080, "fps": 30}


def _load_mapping(value: Any, *, label: str) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (str, Path)):
        path = Path(value)
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError(f"{label} must contain a JSON object")
        return data
    to_config = getattr(value, "to_config", None)
    if callable(to_config):
        data = to_config()
        if isinstance(data, Mapping):
            return dict(data)
    raise TypeError(f"{label} must be a mapping, JSON path, or Timeline")


def _timeline_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, Timeline):
        return dict(value.to_config())
    return _load_mapping(value, label="timeline")


def _asset_mapping(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    return _load_mapping(value, label="assets registry")


def _deep_merge_theme(base: Mapping[str, Any], overlay: Mapping[str, Any]) -> dict[str, Any]:
    """Merge runtime theme data with timeline visual overrides."""

    result: dict[str, Any] = dict(base)
    for key, value in overlay.items():
        existing = result.get(key)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            merged_block: dict[str, Any] = dict(existing)
            for sub_key, sub_value in value.items():
                existing_sub = merged_block.get(sub_key)
                if isinstance(existing_sub, Mapping) and isinstance(sub_value, Mapping):
                    inner = dict(existing_sub)
                    inner.update(sub_value)
                    merged_block[sub_key] = inner
                else:
                    merged_block[sub_key] = sub_value
            result[key] = merged_block
        else:
            result[key] = value
    return result


def _resolve_merged_theme(
    timeline: Mapping[str, Any],
    *,
    theme: Mapping[str, Any] | str | Path | None,
) -> dict[str, Any]:
    # Runtime render truth is either the pinned document supplied by the host
    # or this built-in value.
    overrides = timeline.get("theme_overrides")
    override_mapping = overrides if isinstance(overrides, Mapping) else {}

    if isinstance(theme, Mapping):
        return _deep_merge_theme(theme, override_mapping)

    if theme is not None:
        if isinstance(theme, (str, Path)):
            return _deep_merge_theme(load_runtime_theme(theme), override_mapping)
        raise TypeError("theme must be a validated mapping or runtime-materialized theme.json")
    return _deep_merge_theme(builtin_theme(), override_mapping)


def explicit_timeline_canvas(timeline: Mapping[str, Any]) -> Mapping[str, Any] | None:
    output = timeline.get('output')
    if not isinstance(output, Mapping) or 'resolution' not in output:
        return None
    match = re.fullmatch(r'([1-9][0-9]*)[xX]([1-9][0-9]*)', str(output['resolution']))
    if not match or 'fps' not in output:
        raise ValueError('timeline output resolution and fps must define a valid canvas')
    return {'width': int(match[1]), 'height': int(match[2]), 'fps': output['fps']}


def _remotion_canvas(
    timeline: Mapping[str, Any], merged_theme: Mapping[str, Any]
) -> Mapping[str, Any]:
    """Return the canvas selected by ``remotion/src/Root.tsx::getCanvas``.

    Root selects the *whole* override canvas before the resolved theme canvas.
    A partial override consequently falls back to Remotion's field defaults,
    not to the missing fields in the base theme.  Mirroring that edge is
    important: this profile is the contract for what Remotion actually emits.
    """

    explicit = explicit_timeline_canvas(timeline)
    if explicit is not None:
        return explicit
    overrides = timeline.get("theme_overrides")
    if isinstance(overrides, Mapping):
        visual = overrides.get("visual")
        if isinstance(visual, Mapping) and isinstance(visual.get("canvas"), Mapping):
            return visual["canvas"]
    visual = merged_theme.get("visual")
    if isinstance(visual, Mapping) and isinstance(visual.get("canvas"), Mapping):
        return visual["canvas"]
    return _DEFAULT_CANVAS


def _positive_dimension(value: Any, *, default: int, label: str) -> int:
    candidate = default if value is None else value
    if isinstance(candidate, bool) or not isinstance(candidate, (int, float)):
        raise TypeError(f"canvas {label} must be a positive integer")
    if isinstance(candidate, float) and not candidate.is_integer():
        raise ValueError(f"canvas {label} must be a positive integer")
    result = int(candidate)
    if result <= 0:
        raise ValueError(f"canvas {label} must be a positive integer")
    return result


def _fps_fraction(value: Any) -> Fraction:
    if isinstance(value, bool):
        raise TypeError("canvas fps must be a positive number or rational")
    if isinstance(value, str):
        try:
            fps = Fraction(value)
        except (ValueError, ZeroDivisionError) as exc:
            raise ValueError(f"invalid canvas fps {value!r}") from exc
    elif isinstance(value, Mapping):
        if value.keys() != {"numerator", "denominator"}:
            raise ValueError("canvas fps rational must contain numerator and denominator")
        numerator, denominator = value.get("numerator"), value.get("denominator")
        if type(numerator) is not int or type(denominator) is not int:
            raise TypeError("canvas fps rational must contain integer numerator and denominator")
        if numerator <= 0 or denominator <= 0:
            raise ValueError("canvas fps rational components must be positive")
        fps = Fraction(numerator, denominator)
    elif (
        isinstance(value, Sequence)
        and not isinstance(value, (str, bytes))
        and len(value) == 2
    ):
        numerator, denominator = value
        if type(numerator) is not int or type(denominator) is not int:
            raise TypeError("canvas fps rational must contain two integers")
        try:
            fps = Fraction(numerator, denominator)
        except ZeroDivisionError as exc:
            raise ValueError("canvas fps denominator must be positive") from exc
    elif isinstance(value, int):
        fps = Fraction(value, 1)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canvas fps must be finite")
        # Decimal text is the authored value.  Fraction(float) would preserve
        # the binary approximation and make the wire profile drift.
        fps = Fraction(str(value))
    else:
        raise TypeError("canvas fps must be a positive number or rational")
    if fps <= 0:
        raise ValueError("canvas fps must be positive")
    return fps


def _mp4_time_base(fps: Fraction) -> tuple[int, int]:
    """Mirror FFmpeg's MP4 video-track timescale selection.

    Integer rates are repeatedly doubled until the timescale is at least
    10,000 (24 -> 12,288; 30 -> 15,360).  NTSC-style rationals already carry
    a large numerator (30000/1001 -> 30,000).
    """

    timescale = fps.numerator
    while timescale < 10_000:
        timescale *= 2
    return 1, timescale


def _coerce_audio_ownership(value: AudioOwnership | str | None) -> AudioOwnership | None:
    if value is None or isinstance(value, AudioOwnership):
        return value
    if isinstance(value, str):
        try:
            return AudioOwnership(value)
        except ValueError as exc:
            raise ValueError(
                "audio_ownership must be one of: rendered, passthrough, none"
            ) from exc
    raise TypeError("audio_ownership must be an AudioOwnership value or string")


def _has_referenced_audio(
    timeline: Mapping[str, Any], assets: Mapping[str, Any] | None
) -> bool:
    tracks = timeline.get("tracks")
    clips = timeline.get("clips")
    if not isinstance(tracks, list) or not isinstance(clips, list):
        return False
    audio_tracks = {
        track.get("id")
        for track in tracks
        if isinstance(track, Mapping) and track.get("kind") == "audio"
    }
    if not audio_tracks:
        return False

    registered_assets: Mapping[str, Any] | None = None
    if isinstance(assets, Mapping):
        candidates = assets.get("assets")
        if isinstance(candidates, Mapping):
            registered_assets = candidates

    for clip in clips:
        if not isinstance(clip, Mapping) or clip.get("track") not in audio_tracks:
            continue
        if clip.get("clipType", "media") != "media":
            continue
        asset_id = clip.get("asset")
        if not isinstance(asset_id, str) or not asset_id:
            continue
        if registered_assets is None or asset_id in registered_assets:
            return True
    return False


def resolve_render_profile(
    timeline: Mapping[str, Any] | str | Path | Timeline,
    assets: Mapping[str, Any] | str | Path | None = None,
    *,
    theme: Mapping[str, Any] | str | Path | None = None,
    audio_ownership: AudioOwnership | str | None = None,
    duration_tolerance: int = 1,
) -> RenderProfile:
    """Resolve the canonical profile shared by planning and finalization.

    Canvas selection deliberately mirrors Remotion's metadata calculation.
    The encoder target remains backend-neutral but matches Astrid's canonical
    MP4 output: H.264/yuv420p and, when audio is rendered, AAC 48 kHz stereo.
    """

    timeline_data = _timeline_mapping(timeline)
    assets_data = _asset_mapping(assets)
    merged_theme = _resolve_merged_theme(
        timeline_data,
        theme=theme,
    )
    canvas = _remotion_canvas(timeline_data, merged_theme)
    width = _positive_dimension(canvas.get("width"), default=1920, label="width")
    height = _positive_dimension(canvas.get("height"), default=1080, label="height")
    fps = _fps_fraction(canvas.get("fps", 30))

    ownership = _coerce_audio_ownership(audio_ownership)
    if ownership is None:
        ownership = (
            AudioOwnership.RENDERED
            if _has_referenced_audio(timeline_data, assets_data)
            else AudioOwnership.NONE
        )
    rendered_audio = ownership is AudioOwnership.RENDERED

    return RenderProfile(
        width=width,
        height=height,
        fps_rational=(fps.numerator, fps.denominator),
        time_base=_mp4_time_base(fps),
        container="mp4",
        video_codec="h264",
        video_profile=None,
        video_level=None,
        pixel_format="yuv420p",
        audio_codec="aac" if rendered_audio else None,
        audio_sample_rate=48_000 if rendered_audio else None,
        audio_channel_layout="stereo" if rendered_audio else None,
        duration_tolerance=duration_tolerance,
    )


__all__ = ["resolve_render_profile"]
