"""Fail-closed, request-sensitive support evidence for ``rendering.ffmpeg``.

This module is deliberately read-only.  It validates a decoded timeline and
asset registry, probes every referenced local source, and reports every reason
the FFmpeg renderer cannot preserve the requested semantics.
"""

from __future__ import annotations

import math
import shutil
import struct
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from astrid.core.media import MediaProbe, ffprobe_metadata_strict
from astrid.core.rendering.assets import AssetMaterializer
from astrid.core.rendering.contracts import (
    SCHEMA_VERSION,
    AudioOwnership,
    RenderRequest,
    SupportReport,
)
from astrid.core.rendering.profile import _mp4_time_base
from astrid.core.timeline.audio_envelope import compile_legacy
from astrid.packs.rendering.rendering.renderers.ffmpeg import audio_reactive_colour
from astrid.packs.rendering.rendering.renderers.ffmpeg.text import (
    _parse_color,
    _parse_fades,
    _parse_text_shadow,
    _resolve_font_path,
    _text_window,
    text_wants_bold,
)

BACKEND_ID = "rendering.ffmpeg"
BACKEND_VERSION = "1.1.0"
ALTERNATIVE_BACKENDS = ("rendering.remotion",)

_TRACK_KINDS = frozenset({"visual", "audio"})
_POSITION_KEYS = frozenset({"x", "y", "width", "height"})
_CROP_KEYS = frozenset({"cropTop", "cropBottom", "cropLeft", "cropRight"})
_EFFECT_KEYS = frozenset({"effects", "entrance", "exit", "continuous", "keyframes"})
_TEXT_PARAM_KEYS = frozenset(
    {"anchor", "offsetX", "offsetY", "maxWidth", "textShadow", "weight"}
)
_TIMELINE_EPSILON_SECONDS = 1e-9
_SOURCE_BOUND_TOLERANCE_SECONDS = 0.001


Probe = Callable[[str | Path], MediaProbe]
BinaryResolver = Callable[[str], str | None]


@dataclass(frozen=True)
class _ClipRange:
    clip: Mapping[str, Any]
    at: float
    source_from: float
    source_to: float

    @property
    def duration(self) -> float:
        return self.source_to - self.source_from

    @property
    def end(self) -> float:
        return self.at + self.duration


@dataclass(frozen=True)
class _TimelineRange:
    clip: Mapping[str, Any]
    at: float
    duration: float

    @property
    def end(self) -> float:
        return self.at + self.duration


def _number(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be a finite number")
    return result


def _gain(value: Any, label: str, *, default: float = 1.0) -> float:
    resolved = default if value is None else _number(value, label)
    if not 0.0 <= resolved <= 1.0:
        raise ValueError(f"{label} must be between 0 and 1")
    return resolved


def effective_gain(track: Mapping[str, Any], clip: Mapping[str, Any]) -> float:
    """Return the exact timeline gain for one media clip.

    Track mute is authoritative; otherwise track and clip gains multiply.  A
    missing gain means unity, while malformed or out-of-range values are
    rejected instead of clamped.
    """

    muted = track.get("muted", False)
    if not isinstance(muted, bool):
        raise ValueError(f"Track {track.get('id')!r} muted must be a boolean")
    track_gain = _gain(
        track.get("volume"),
        f"Track {track.get('id')!r} volume",
    )
    clip_gain = _gain(
        clip.get("volume"),
        f"Clip {clip.get('id')!r} volume",
    )
    return 0.0 if muted else track_gain * clip_gain


def _clip_range(clip: Mapping[str, Any]) -> _ClipRange:
    clip_id = clip.get("id")
    at = _number(clip.get("at", 0), f"Clip {clip_id!r} at")
    source_from = _number(
        clip.get("from", 0),
        f"Clip {clip_id!r} from",
    )
    if "to" not in clip:
        raise ValueError(f"Clip {clip_id!r} must declare a source to bound")
    source_to = _number(clip.get("to"), f"Clip {clip_id!r} to")
    if at < 0:
        raise ValueError(f"Clip {clip_id!r} has a negative timeline frame bound")
    if source_from < 0 or source_to <= source_from:
        raise ValueError(
            f"Clip {clip_id!r} must have positive source bounds with to > from"
        )
    return _ClipRange(
        clip=clip,
        at=at,
        source_from=source_from,
        source_to=source_to,
    )


def _hold_range(clip: Mapping[str, Any]) -> _TimelineRange:
    clip_id = clip.get("id")
    at = _number(clip.get("at", 0), f"Clip {clip_id!r} at")
    hold = _number(clip.get("hold"), f"Clip {clip_id!r} hold")
    if at < 0:
        raise ValueError(f"Clip {clip_id!r} has a negative timeline frame bound")
    if hold <= 0:
        raise ValueError(f"Clip {clip_id!r} hold must be positive")
    if "from" in clip or "to" in clip:
        raise ValueError(f"Clip {clip_id!r} static hold cannot also declare source bounds")
    return _TimelineRange(clip=clip, at=at, duration=hold)


def _is_static_hold_clip(clip: Mapping[str, Any]) -> bool:
    return clip.get("clipType") == "media" and "hold" in clip


def _is_default(value: Any, default: Any) -> bool:
    return value is None or value == default


def _nonempty(value: Any) -> bool:
    return value not in (None, False, "", (), [], {})


def _validate_track_semantics(track: Mapping[str, Any]) -> list[str]:
    reasons: list[str] = []
    track_id = track.get("id")
    try:
        effective_gain(track, {})
    except ValueError as exc:
        reasons.append(str(exc))
    if not _is_default(track.get("scale"), 1) or not _is_default(
        track.get("fit"), "contain"
    ) or not _is_default(track.get("blendMode"), "normal"):
        reasons.append(
            f"Track {track_id!r} uses unsupported visual transform semantics"
        )
    opacity = track.get("opacity")
    if opacity is not None:
        try:
            if _number(opacity, f"Track {track_id!r} opacity") != 1.0:
                reasons.append(
                    f"Track {track_id!r} uses unsupported non-default opacity"
                )
        except ValueError as exc:
            reasons.append(str(exc))
    return reasons


def _validate_clip_semantics(
    clip: Mapping[str, Any],
    track: Mapping[str, Any],
    fps: float = 30,
) -> list[str]:
    reasons: list[str] = []
    clip_id = clip.get("id")
    is_text = clip.get("clipType") == "text"
    if "muted" in clip:
        reasons.append(
            f"Clip {clip_id!r} uses unsupported clip-level muted; use volume: 0"
        )
    try:
        speed = _number(clip.get("speed", 1), f"Clip {clip_id!r} speed")
        if speed != 1.0:
            reasons.append(
                f"Clip {clip_id!r} uses unsupported speed {speed}; rendering.ffmpeg requires 1.0"
            )
    except ValueError as exc:
        reasons.append(str(exc))

    positioned = sorted(key for key in _POSITION_KEYS if key in clip)
    if positioned:
        reasons.append(
            f"Clip {clip_id!r} uses unsupported transforms: {', '.join(positioned)}"
        )
    cropped = sorted(
        key
        for key in _CROP_KEYS
        if key in clip and not _is_default(clip.get(key), 0)
    )
    if cropped:
        reasons.append(
            f"Clip {clip_id!r} uses unsupported crop: {', '.join(cropped)}"
        )
    effect_keys = _EFFECT_KEYS - {"effects"} if is_text else _EFFECT_KEYS
    effects = sorted(
        key for key in effect_keys if key in clip and _nonempty(clip.get(key))
    )
    if effects:
        reasons.append(
            f"Clip {clip_id!r} uses unsupported effects: {', '.join(effects)}"
        )
    if _nonempty(clip.get("transition")):
        reasons.append(f"Clip {clip_id!r} uses an unsupported transition")
    opacity = clip.get("opacity")
    if opacity is not None:
        try:
            if _number(opacity, f"Clip {clip_id!r} opacity") != 1.0:
                reasons.append(
                    f"Clip {clip_id!r} uses unsupported non-default opacity"
                )
        except ValueError as exc:
            reasons.append(str(exc))

    if "audio" in clip:
        try:
            compile_legacy(clip, fps)
            if track.get("kind") != "audio":
                reasons.append(f"ENVELOPE_UNRENDERED: Clip {clip_id!r} needs an audio track")
        except (ValueError, TypeError) as exc:
            reasons.append(str(exc))

    app = clip.get("app")
    app = app if isinstance(app, Mapping) else {}
    namespace = app.get("astrid")
    namespace = namespace if isinstance(namespace, Mapping) else {}
    authored_duck = namespace.get("duck", app.get("duck"))
    audio = clip.get("audio")
    envelope = audio.get("envelope") if isinstance(audio, Mapping) else None
    if authored_duck is not None and not (isinstance(envelope, Mapping) and envelope.get("duck")):
        reasons.append(f"DUCK_UNRENDERED: Clip {clip_id!r} requires compiled narration projection")

    params = clip.get("params")
    if params is not None and not isinstance(params, Mapping):
        reasons.append(f"Clip {clip_id!r} params must be an object")
    elif isinstance(params, Mapping):
        audio_keys = {"fadeIn", "fadeOut", "fadeInCurve", "fadeOutCurve"}
        if track.get("kind") == "audio":
            try:
                compile_legacy(clip, fps)  # curve/plan validation; actual fps belongs to command
            except ValueError as exc:
                reasons.append(str(exc))
        elif any(params.get(name) for name in audio_keys):
            reasons.append(f"ENVELOPE_UNRENDERED: Clip {clip_id!r} audio envelope needs an audio track")
        other_params = sorted(set(params) - audio_keys)
        if other_params and clip.get("clipType") == "media":
            reasons.append(
                f"Clip {clip_id!r} uses unsupported media params: {', '.join(other_params)}"
            )
        if is_text:
            unknown_params = sorted(set(params) - _TEXT_PARAM_KEYS)
            if unknown_params:
                reasons.append(
                    f"Clip {clip_id!r} uses unsupported text params: "
                    f"{', '.join(unknown_params)}"
                )

    if clip.get("clipType") == "media":
        if _is_static_hold_clip(clip):
            try:
                _hold_range(clip)
            except ValueError as exc:
                reasons.append(str(exc))
            if track.get("kind") != "visual":
                reasons.append(f"Clip {clip_id!r} uses static hold outside a visual track")
        else:
            try:
                _clip_range(clip)
            except ValueError as exc:
                reasons.append(str(exc))
        try:
            effective_gain(track, clip)
        except ValueError as exc:
            reasons.append(str(exc))
    if is_text:
        reasons.extend(_validate_text_semantics(clip, track))
    return reasons


def _validate_text_semantics(
    clip: Mapping[str, Any],
    track: Mapping[str, Any],
    fps: float = 30,
) -> list[str]:
    """Text-only semantics: content, window, no source, color/shadow parity."""
    reasons: list[str] = []
    clip_id = clip.get("id")
    if track.get("kind") != "visual":
        reasons.append(f"Text clip {clip_id!r} must sit on a visual track")
    if "from" in clip:
        reasons.append(
            f"Text clip {clip_id!r} must not declare from; use at with hold or to"
        )
    if clip.get("asset") is not None:
        reasons.append(f"Text clip {clip_id!r} must not reference an asset")
    text_field = clip.get("text")
    text_field = text_field if isinstance(text_field, Mapping) else {}
    content = text_field.get("content")
    if not isinstance(content, str) or not content:
        reasons.append(f"Text clip {clip_id!r} requires non-empty text.content")
    fades: tuple[float, float] = (0.0, 0.0)
    try:
        fades = _parse_fades(clip.get("effects"))
    except ValueError as exc:
        reasons.append(str(exc))
    window: tuple[float, float] | None = None
    try:
        window = _text_window(clip)
    except ValueError as exc:
        reasons.append(str(exc))
    if window is not None:
        fade_total = fades[0] + fades[1]
        duration = window[1] - window[0]
        if fade_total > duration + _TIMELINE_EPSILON_SECONDS:
            reasons.append(
                f"Text clip {clip_id!r} fade envelope {fade_total:.6f}s exceeds "
                f"its window {duration:.6f}s"
            )
    color_text = text_field.get("color")
    if isinstance(color_text, str) and color_text.strip():
        try:
            _parse_color(color_text)
        except ValueError as exc:
            reasons.append(str(exc))
    params = clip.get("params")
    params = params if isinstance(params, Mapping) else {}
    try:
        _parse_text_shadow(params.get("textShadow"))
    except ValueError as exc:
        reasons.append(str(exc))
    return reasons


def structural_reasons(
    timeline_data: Mapping[str, Any],
    *,
    allow_audio_reactive: bool = True,
) -> list[str]:
    """Return semantic rejections that do not require filesystem probing."""

    reasons: list[str] = []
    raw_tracks = timeline_data.get("tracks")
    raw_clips = timeline_data.get("clips")
    if not isinstance(raw_tracks, list):
        reasons.append("timeline tracks must be an array")
        raw_tracks = []
    if not isinstance(raw_clips, list):
        reasons.append("timeline clips must be an array")
        raw_clips = []

    tracks: dict[str, Mapping[str, Any]] = {}
    visual_track_ids: list[str] = []
    for index, raw_track in enumerate(raw_tracks):
        if not isinstance(raw_track, Mapping):
            reasons.append(f"Track at index {index} must be an object")
            continue
        track_id = raw_track.get("id")
        if not isinstance(track_id, str) or not track_id:
            reasons.append(f"Track at index {index} must have a non-empty id")
            continue
        if track_id in tracks:
            reasons.append(f"Timeline contains duplicate track id {track_id!r}")
            continue
        tracks[track_id] = raw_track
        kind = raw_track.get("kind")
        if kind not in _TRACK_KINDS:
            reasons.append(f"Track {track_id!r} has unsupported kind {kind!r}")
        elif kind == "visual":
            visual_track_ids.append(track_id)
        reasons.extend(_validate_track_semantics(raw_track))


    clips: list[Mapping[str, Any]] = []
    seen_clip_ids: set[str] = set()
    reactive_count = 0
    for index, raw_clip in enumerate(raw_clips):
        if not isinstance(raw_clip, Mapping):
            reasons.append(f"Clip at index {index} must be an object")
            continue
        clips.append(raw_clip)
        clip_id = raw_clip.get("id")
        if not isinstance(clip_id, str) or not clip_id:
            reasons.append(f"Clip at index {index} must have a non-empty id")
        elif clip_id in seen_clip_ids:
            reasons.append(f"Timeline contains duplicate clip id {clip_id!r}")
        else:
            seen_clip_ids.add(clip_id)
        track = tracks.get(str(raw_clip.get("track")))
        if track is None:
            reasons.append(
                f"Clip {clip_id!r} references unknown track {raw_clip.get('track')!r}"
            )
            track = {}
        clip_type = raw_clip.get("clipType")
        if clip_type == audio_reactive_colour.EFFECT_ID:
            reactive_count += 1
            if not allow_audio_reactive:
                reasons.append(
                    f"rendering.ffmpeg media path does not support clip kind {clip_type!r}"
                )
        elif clip_type not in ("media", "text"):
            reasons.append(
                f"Clip {clip_id!r} has unsupported clip kind {clip_type!r}"
            )
        try:
            fps = _canvas(timeline_data)[2]
        except ValueError:
            fps = 30  # the canvas validator separately reports malformed clocks
        reasons.extend(_validate_clip_semantics(raw_clip, track, fps))

    if reactive_count:
        if reactive_count != 1:
            reasons.append(
                "audio-reactive-colour specialization requires exactly one effect clip"
            )
        return _dedupe(reasons)

    visual_clips_by_track: dict[str, list[Mapping[str, Any]]] = {
        track_id: [] for track_id in visual_track_ids
    }
    audio_ranges: list[_ClipRange] = []
    for clip in clips:
        if clip.get("clipType") != "media":
            continue
        track = tracks.get(str(clip.get("track")), {})
        if track.get("kind") == "visual":
            visual_clips_by_track.setdefault(str(clip.get("track")), []).append(clip)
        elif track.get("kind") == "audio":
            try:
                audio_ranges.append(_clip_range(clip))
            except ValueError:
                continue

    static_holds = [
        clip for track_clips in visual_clips_by_track.values()
        for clip in track_clips if _is_static_hold_clip(clip)
    ]
    base_ranges: list[_ClipRange] = []
    overlay_range: _TimelineRange | None = None
    if static_holds:
        if len(visual_track_ids) != 2:
            reasons.append("rendering.ffmpeg static overlay requires exactly two visual tracks")
        if len(static_holds) != 1:
            reasons.append("rendering.ffmpeg static overlay requires exactly one held image clip")
        if len(static_holds) == 1:
            overlay = static_holds[0]
            overlay_track_id = str(overlay.get("track"))
            if list(visual_track_ids)[0] != overlay_track_id:
                reasons.append("rendering.ffmpeg static overlay track must be the first visual track (top layer)")
            if len(visual_clips_by_track.get(overlay_track_id, [])) != 1:
                reasons.append("rendering.ffmpeg static overlay track must contain only the held image clip")
            try:
                overlay_range = _hold_range(overlay)
            except ValueError:
                pass
        base_tracks = [track_id for track_id in visual_track_ids if track_id != str(static_holds[0].get("track"))]
        if len(base_tracks) != 1:
            reasons.append("rendering.ffmpeg static overlay requires one ordinary picture track")
        else:
            for clip in visual_clips_by_track.get(base_tracks[0], []):
                try:
                    base_ranges.append(_clip_range(clip))
                except ValueError:
                    continue
    else:
        media_visual_track_ids = {
            track_id for track_id, track_clips in visual_clips_by_track.items()
            if track_clips
        }
        if len(media_visual_track_ids) != 1:
            reasons.append(
                "rendering.ffmpeg requires exactly one visual track carrying media clips"
            )
        for track_id in visual_track_ids:
            if not any(str(clip.get("track")) == track_id for clip in clips):
                reasons.append(f"Visual track {track_id!r} has no clips")
        for track_clips in visual_clips_by_track.values():
            for clip in track_clips:
                try:
                    base_ranges.append(_clip_range(clip))
                except ValueError:
                    continue

    base_ranges.sort(key=lambda item: item.at)
    if not base_ranges:
        reasons.append("rendering.ffmpeg needs at least one visual media clip")
    else:
        cursor = 0.0
        for bounds in base_ranges:
            clip_id = bounds.clip.get("id")
            if bounds.at > cursor + _TIMELINE_EPSILON_SECONDS:
                reasons.append(
                    f"Visual gap before clip {clip_id!r}: starts at {bounds.at:.6f}, expected {cursor:.6f}"
                )
            elif bounds.at < cursor - _TIMELINE_EPSILON_SECONDS:
                reasons.append(
                    f"Visual overlap at clip {clip_id!r}: starts at {bounds.at:.6f}, previous visual ends at {cursor:.6f}"
                )
            cursor = max(cursor, bounds.end)

        if overlay_range is not None:
            if overlay_range.at != 0:
                reasons.append("rendering.ffmpeg static overlay must start at timeline zero")
            if abs(overlay_range.duration - cursor) > _TIMELINE_EPSILON_SECONDS:
                reasons.append("rendering.ffmpeg static overlay hold must equal the ordinary picture duration")

        audio_ranges.sort(key=lambda item: item.at)
        audio_cursor = 0.0
        for bounds in audio_ranges:
            clip_id = bounds.clip.get("id")
            if bounds.at < audio_cursor - _TIMELINE_EPSILON_SECONDS and not any(
                "audio" in item.clip or any(
                    (item.clip.get("params") or {}).get(name) for name in ("fadeIn", "fadeOut")
                ) for item in audio_ranges
            ):
                reasons.append(
                    f"Overlapping audio at clip {clip_id!r}: starts at {bounds.at:.6f}, previous audio ends at {audio_cursor:.6f}"
                )
            if bounds.end > cursor + _TIMELINE_EPSILON_SECONDS:
                reasons.append(
                    f"Audio clip {clip_id!r} ends outside the visual frame bounds"
                )
            audio_cursor = max(audio_cursor, bounds.end)
        media_coverage_end = max(bounds.end for bounds in base_ranges)
        for clip in clips:
            if clip.get("clipType") != "text":
                continue
            try:
                _, text_end = _text_window(clip)
            except ValueError:
                continue
            if text_end > media_coverage_end + _TIMELINE_EPSILON_SECONDS:
                reasons.append(
                    f"Text clip {clip.get('id')!r} ends at {text_end:.6f}, beyond "
                    f"the visual media coverage end {media_coverage_end:.6f}"
                )
    return _dedupe(reasons)


def _dedupe(reasons: list[str]) -> list[str]:
    return list(dict.fromkeys(reason for reason in reasons if reason))


def _assets_table(assets: Mapping[str, Any]) -> Mapping[str, Any] | None:
    value = assets.get("assets")
    return value if isinstance(value, Mapping) else None


def _registry_path(request: RenderRequest) -> Path:
    if request.assets_registry_path is None:
        return Path.cwd() / "assets.json"
    return Path(request.assets_registry_path).expanduser().resolve()


def _asset_path(
    entry: Mapping[str, Any],
    *,
    asset_id: str,
    assets_path: Path,
) -> Path:
    if _nonempty(entry.get("url")):
        raise ValueError(
            f"Asset {asset_id!r} is remote; rendering.ffmpeg requires a local source file"
        )
    file_value = entry.get("file")
    if not isinstance(file_value, str) or not file_value:
        raise ValueError(f"Asset {asset_id!r} has no local source file")
    path = Path(file_value).expanduser()
    if not path.is_absolute():
        path = (assets_path.parent / path).resolve()
    else:
        path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Asset {asset_id!r} source is missing: {path}")
    return path


def _png_has_transparency(path: Path) -> bool:
    """Inspect PNG headers for alpha, including palette transparency."""
    try:
        with path.open("rb") as stream:
            if stream.read(8) != b"\x89PNG\r\n\x1a\n":
                return False
            colour_type: int | None = None
            while True:
                raw_length = stream.read(4)
                if len(raw_length) != 4:
                    return False
                length = struct.unpack(">I", raw_length)[0]
                chunk_type = stream.read(4)
                if len(chunk_type) != 4 or length > 16 * 1024 * 1024:
                    return False
                payload = stream.read(length)
                if len(payload) != length or len(stream.read(4)) != 4:
                    return False
                if chunk_type == b"IHDR":
                    if length != 13:
                        return False
                    colour_type = payload[9]
                    if colour_type in {4, 6}:
                        return True
                elif chunk_type == b"tRNS":
                    if colour_type in {0, 2}:
                        return True
                    if colour_type == 3 and any(alpha < 255 for alpha in payload):
                        return True
                elif chunk_type in {b"IDAT", b"IEND"}:
                    return False
    except OSError:
        return False


def _probe_duration(probe: MediaProbe) -> float | None:
    if probe.duration_seconds is not None:
        return float(probe.duration_seconds)
    if probe.duration_rational is not None:
        numerator, denominator = probe.duration_rational
        return numerator / denominator
    return None


def _is_still_asset(entry: Mapping[str, Any]) -> bool:
    """Return whether an asset is a source image with looped render semantics."""

    kind = str(entry.get("type", "")).lower()
    if kind in {"image", "still", "image/png", "image/jpeg", "image/webp"}:
        return True
    file_value = entry.get("file")
    return isinstance(file_value, str) and Path(file_value).suffix.lower() in {
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".gif",
        ".bmp",
        ".tif",
        ".tiff",
    }


def _source_duration(entry: Mapping[str, Any], probe: MediaProbe) -> float | None:
    """Resolve a finite source bound, including compiled still-image windows.

    Still images intentionally have no intrinsic duration in ffprobe.  A
    compiler-provided positive ``duration`` is valid only for a typed image;
    all ordinary media continues to require probe-derived duration evidence.
    """

    probed = _probe_duration(probe)
    if probed is not None:
        return probed
    if not _is_still_asset(entry):
        return None
    declared = entry.get("duration")
    if isinstance(declared, bool) or not isinstance(declared, (int, float)):
        return None
    declared_float = float(declared)
    return declared_float if math.isfinite(declared_float) and declared_float > 0 else None


def _requested_ownership(
    request: RenderRequest,
    *,
    has_audio_clips: bool,
) -> tuple[AudioOwnership, list[str]]:
    natural = AudioOwnership.RENDERED if has_audio_clips else AudioOwnership.NONE
    ownership = natural
    reasons: list[str] = []
    if request.audio is AudioOwnership.PASSTHROUGH and not has_audio_clips:
        ownership = AudioOwnership.PASSTHROUGH
    elif request.audio is not None and request.audio is not natural:
        reasons.append(
            f"audio={request.audio.value!r} is incompatible with timeline audio ownership {natural.value!r}"
        )
    if request.profile is not None and request.profile.has_audio != (
        ownership is AudioOwnership.RENDERED
    ):
        reasons.append(
            "requested profile audio fields do not match rendering.ffmpeg audio ownership"
        )
    return ownership, reasons


def _whole_media_optimization(
    timeline_data: Mapping[str, Any],
    assets: Mapping[str, Any],
    probes: Mapping[str, MediaProbe],
) -> bool:
    tracks = {
        track.get("id"): track
        for track in timeline_data.get("tracks", [])
        if isinstance(track, Mapping)
    }
    visual = [
        clip
        for clip in timeline_data.get("clips", [])
        if isinstance(clip, Mapping)
        and clip.get("clipType") == "media"
        and tracks.get(clip.get("track"), {}).get("kind") == "visual"
    ]
    table = _assets_table(assets)
    if len(visual) != 1 or table is None:
        return False
    clip = visual[0]
    entry = table.get(clip.get("asset"))
    if not isinstance(entry, Mapping):
        return False
    media_probe = probes.get(str(clip.get("asset")))
    if media_probe is None or not media_probe.has_video_stream:
        return False
    try:
        bounds = _clip_range(clip)
        width, height, fps = _canvas(timeline_data)
        duration = _number(entry.get("duration"), "asset duration")
        source_fps = _number(entry.get("fps"), "asset fps")
    except ValueError:
        return False
    probed_duration = _probe_duration(media_probe)
    probed_fps = media_probe.fps
    if probed_fps is None and media_probe.fps_rational is not None:
        numerator, denominator = media_probe.fps_rational
        probed_fps = numerator / denominator
    # Frame-accurate tolerance: at most ONE frame of drift is acceptable
    # (half a frame each way), so extra trailing frames at high FPS cannot
    # slip through a coarse 50 ms window.
    frame_tolerance = 0.5 / fps if fps > 0 else 0.0
    return (
        bounds.at == 0
        and bounds.source_from == 0
        and abs(bounds.duration - duration) < frame_tolerance
        and entry.get("resolution") == f"{width}x{height}"
        and abs(source_fps - fps) < 1e-6
        and probed_duration is not None
        and abs(bounds.duration - probed_duration) < frame_tolerance
        and media_probe.width == width
        and media_probe.height == height
        and probed_fps is not None
        and abs(probed_fps - fps) < 1e-6
        and (media_probe.video_codec or "") == "h264"
        and (media_probe.pixel_format or "") == "yuv420p"
        and _probe_time_base_matches(media_probe, (1, _mp4_time_base(Fraction(fps))[1]))
    )


def _probe_time_base_matches(
    probe: MediaProbe, expected: tuple[int, int]
) -> bool:
    """The probed stream time base must equal the canonical MP4 timescale."""
    if probe.time_base is None:
        return False
    return Fraction(*probe.time_base) == Fraction(*expected)


def _profile_support_reasons(
    request: RenderRequest, timeline_data: Mapping[str, Any]
) -> list[str]:
    """Fail closed when the requested profile deviates from what the FFmpeg
    backend actually produces (canvas dims/fps, codecs, pixel format, and
    canonical audio rate/layout)."""
    profile = request.profile
    if profile is None:
        return []
    reasons: list[str] = []
    try:
        width, height, fps = _canvas(timeline_data)
    except ValueError:
        return reasons  # canvas failure already reported elsewhere
    checks = (
        ("width", profile.width, width),
        ("height", profile.height, height),
        ("fps", profile.fps_rational, (fps, 1)),
        ("time_base", profile.time_base, _mp4_time_base(Fraction(fps))),
        ("container", profile.container, "mp4"),
        ("video_codec", profile.video_codec, "h264"),
        ("pixel_format", profile.pixel_format, "yuv420p"),
    )
    for field, requested, produced in checks:
        if requested is None:
            continue
        if field in ("fps", "time_base"):
            equal = _rational_equal(requested, produced)
        else:
            equal = requested == produced
        if not equal:
            reasons.append(
                f"requested profile {field}={requested!r} is not produced by "
                f"rendering.ffmpeg (produces {produced!r})"
            )
    # The command does NOT pin video_profile/video_level (libx264 picks the
    # encoder default; stream-copy preserves whatever the source has). A
    # request pinning them cannot be guaranteed at support time, so fail
    # closed rather than report success and fail strict post-render
    # validation.
    for field, requested in (
        ("video_profile", profile.video_profile),
        ("video_level", profile.video_level),
    ):
        if requested is not None:
            reasons.append(
                f"requested profile {field}={requested!r} cannot be guaranteed "
                f"by rendering.ffmpeg (encoder default or stream-copy preserves "
                f"source values; omit {field} to use defaults)"
            )
    if profile.has_audio:
        for field, requested, produced in (
            ("audio_sample_rate", profile.audio_sample_rate, 48000),
            ("audio_channel_layout", profile.audio_channel_layout, "stereo"),
            ("audio_codec", profile.audio_codec, "aac"),
        ):
            if requested is not None and requested != produced:
                reasons.append(
                    f"requested profile {field}={requested!r} is not produced by "
                    f"rendering.ffmpeg (produces {produced!r})"
                )
    return reasons


def _fps_int(fps_rational: tuple[int, int] | None) -> int | None:
    if fps_rational is None:
        return None
    num, den = fps_rational
    return num // den if den and num % den == 0 else None


def _rational_equal(a: Any, b: Any) -> bool:
    try:
        return Fraction(*a) == Fraction(*b)
    except (TypeError, ValueError, ZeroDivisionError):
        return False


def _canvas(timeline_data: Mapping[str, Any]) -> tuple[int, int, int]:
    overrides = timeline_data.get("theme_overrides")
    visual = overrides.get("visual") if isinstance(overrides, Mapping) else None
    canvas = visual.get("canvas") if isinstance(visual, Mapping) else None
    canvas = canvas if isinstance(canvas, Mapping) else {}
    values: list[int] = []
    for key, default in (("width", 1920), ("height", 1080), ("fps", 30)):
        value = canvas.get(key, default)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError(f"Canvas {key} must be a positive integer")
        values.append(value)
    return values[0], values[1], values[2]


def support(
    request: RenderRequest,
    timeline_data: Mapping[str, Any],
    assets: Mapping[str, Any],
    *,
    probe: Probe | None = None,
    which: BinaryResolver | None = None,
) -> SupportReport:
    """Return strict support evidence for one already-decoded request."""

    reasons: list[str] = []
    probe_media = probe or ffprobe_metadata_strict
    resolve_binary = which or shutil.which
    binary_available: dict[str, bool] = {}
    for binary in ("ffmpeg", "ffprobe"):
        available = resolve_binary(binary) is not None
        binary_available[binary] = available
        if not available:
            reasons.append(f"required binary is unavailable: {binary}")

    if request.window is not None:
        reasons.append(
            "rendering.ffmpeg accepts complete timelines, not native frame windows"
        )
    config = request.backend_config.get(BACKEND_ID, {})
    if config:
        reasons.append(
            "rendering.ffmpeg does not accept backend-specific configuration"
        )
    if request.assets_registry_path is None:
        reasons.append("rendering.ffmpeg requires an assets registry")
    try:
        _canvas(timeline_data)
    except ValueError as exc:
        reasons.append(str(exc))
    reasons.extend(structural_reasons(timeline_data))

    table = _assets_table(assets)
    if table is None:
        reasons.append("assets registry must contain an assets object")
        table = {}
    assets_path = _registry_path(request)
    if request.materialized_root is not None and assets_path.is_file():
        try:
            # The host has already staged and verified these objects. This
            # defense-in-depth check proves the derived registry still points
            # only inside that attempt-local root and that its digests match.
            with AssetMaterializer(
                assets_path,
                materialized_objects=request.materialized_objects,
                materialized_root=request.materialized_root,
                allow_derived_files=True,
            ):
                pass
        except Exception as exc:  # noqa: BLE001 - folded into support evidence
            reasons.append(f"attempt-local managed assets are not renderable: {exc}")
    tracks = {
        track.get("id"): track
        for track in timeline_data.get("tracks", [])
        if isinstance(track, Mapping)
    }
    media_clips = [
        clip
        for clip in timeline_data.get("clips", [])
        if isinstance(clip, Mapping) and clip.get("clipType") == "media"
    ]
    audio_clips = [
        clip
        for clip in media_clips
        if tracks.get(clip.get("track"), {}).get("kind") == "audio"
    ]
    text_clips = [
        clip
        for clip in timeline_data.get("clips", [])
        if isinstance(clip, Mapping) and clip.get("clipType") == "text"
    ]
    static_overlay_clips = [
        clip for clip in media_clips if _is_static_hold_clip(clip)
    ]
    if text_clips:
        for bold in sorted({text_wants_bold(clip) for clip in text_clips}):
            if _resolve_font_path(bold=bold) is None:
                reasons.append(
                    "no TTF font found for text rendering (searched "
                    "Supplemental Arial, /Library/Fonts Arial, DejaVu)"
                )
    ownership, ownership_reasons = _requested_ownership(
        request,
        has_audio_clips=bool(audio_clips),
    )
    reasons.extend(ownership_reasons)

    probes: dict[str, MediaProbe] = {}
    for clip in media_clips:
        clip_id = clip.get("id")
        asset_id = clip.get("asset")
        if not isinstance(asset_id, str) or not asset_id:
            reasons.append(f"Clip {clip_id!r} has no asset")
            continue
        entry = table.get(asset_id)
        if not isinstance(entry, Mapping):
            reasons.append(f"Clip {clip_id!r} references missing asset {asset_id!r}")
            continue
        try:
            path = _asset_path(entry, asset_id=asset_id, assets_path=assets_path)
        except (ValueError, FileNotFoundError) as exc:
            reasons.append(str(exc))
            continue
        if asset_id not in probes and binary_available["ffprobe"]:
            try:
                probed = probe_media(path)
                if not isinstance(probed, MediaProbe):
                    raise TypeError("probe did not return MediaProbe")
                probes[asset_id] = probed
            except Exception as exc:
                reasons.append(f"Asset {asset_id!r} cannot be probed: {exc}")

        media_probe = probes.get(asset_id)
        if media_probe is None:
            continue
        track = tracks.get(clip.get("track"), {})
        kind = track.get("kind")
        if kind == "visual" and not media_probe.has_video_stream:
            reasons.append(
                f"Visual clip {clip_id!r} source {asset_id!r} has no video stream"
            )
        if kind == "audio" and not media_probe.has_audio_stream:
            reasons.append(
                f"Audio clip {clip_id!r} source {asset_id!r} has no audio stream"
            )
        if kind == "visual" and media_probe.has_audio_stream:
            try:
                gain = effective_gain(track, clip)
            except ValueError:
                gain = 0.0
            if gain != 0.0:
                reasons.append(
                    f"Visual clip {clip_id!r} requests embedded audio that rendering.ffmpeg would discard"
                )
        if kind == "visual" and _is_static_hold_clip(clip):
            media_type = str(entry.get("type", "")).lower()
            if media_type not in {"image", "still", "image/png"}:
                reasons.append(f"Static overlay clip {clip_id!r} requires an image asset")
            if media_probe.video_codec != "png":
                reasons.append(f"Static overlay clip {clip_id!r} requires a probed PNG image")
            try:
                canvas_width, canvas_height, _fps = _canvas(timeline_data)
            except ValueError:
                canvas_width = canvas_height = None
            if (canvas_width is not None and canvas_height is not None and
                    (media_probe.width, media_probe.height) != (canvas_width, canvas_height)):
                reasons.append(
                    f"Static overlay clip {clip_id!r} must match the {canvas_width}x{canvas_height} canvas"
                )
            if not _png_has_transparency(path):
                reasons.append(f"Static overlay clip {clip_id!r} PNG has no transparency")
        try:
            bounds = _hold_range(clip) if _is_static_hold_clip(clip) else _clip_range(clip)
        except ValueError:
            continue
        if _is_static_hold_clip(clip):
            continue
        source_duration = _source_duration(entry, media_probe)
        if source_duration is None:
            reasons.append(
                f"Asset {asset_id!r} has no probed duration for source-bound validation"
            )
        elif bounds.source_to > source_duration + _SOURCE_BOUND_TOLERANCE_SECONDS:
            reasons.append(
                f"Clip {clip_id!r} source bound {bounds.source_to:.6f} exceeds "
                f"asset {asset_id!r} duration {source_duration:.6f}"
            )

    reactive = any(
        isinstance(clip, Mapping)
        and clip.get("clipType") == audio_reactive_colour.EFFECT_ID
        for clip in timeline_data.get("clips", [])
    )
    specialization = False
    if reactive:
        try:
            spec = audio_reactive_colour.match_and_validate(
                dict(timeline_data),
                dict(assets),
                assets_path,
            )
        except Exception as exc:
            reasons.append(f"audio-reactive-colour specialization is unsupported: {exc}")
        else:
            specialization = spec is not None

    has_text_overlay = bool(text_clips)
    fade_envelope = False
    for clip in text_clips:
        try:
            fade_in, fade_out = _parse_fades(clip.get("effects"))
        except ValueError:
            continue
        if fade_in > 0 or fade_out > 0:
            fade_envelope = True
            break
    whole_media = (
        not reactive
        and not has_text_overlay
        and _whole_media_optimization(timeline_data, assets, probes)
    )
    features: dict[str, bool | str] = {
        "media_only": not specialization and not has_text_overlay,
        "full_timeline": True,
        "windows": False,
        "sequential_audio": True,
        "audio_reactive_colour": specialization,
        "text_overlay": has_text_overlay,
        "fade_envelope": fade_envelope,
        "whole_media": whole_media,
        "whole_media_optimization": whole_media,
        "stream_copy": whole_media,
        "static_image_overlay": bool(static_overlay_clips),
        "audio_ownership": ownership.value,
    }
    if specialization:
        features["specialization"] = audio_reactive_colour.ADAPTER_ID

    reasons.extend(_profile_support_reasons(request, timeline_data))

    reasons = _dedupe(reasons)
    return SupportReport(
        schema_version=SCHEMA_VERSION,
        supported=not reasons,
        reasons=reasons,
        features=features,
        alternatives=list(ALTERNATIVE_BACKENDS) if reasons else [],
        backend=BACKEND_ID,
        backend_version=BACKEND_VERSION,
    )


__all__ = [
    "ALTERNATIVE_BACKENDS",
    "BACKEND_ID",
    "BACKEND_VERSION",
    "effective_gain",
    "structural_reasons",
    "support",
]
