"""Deterministic storage estimates for canonical managed timeline renders.

The estimate is both a transparent, JSON-safe audit record and the source of
the runtime's small top-level per-task storage request.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping
from dataclasses import replace
from fractions import Fraction
from typing import Any

from astrid.core.rendering.contracts import FrameWindow, RenderProfile
from astrid.core.rendering.profile import resolve_render_profile
from astrid.core.timeline.duration import (
    clip_end_frame,
    clip_start_frame,
    clip_timeline_duration,
    timeline_duration_frames,
    timeline_render_duration_frames,
)

_MIB = 1024**2

# These are visible policy inputs, not hidden blanket reservations. H.264 is
# modelled at a generous quarter bit per pixel per frame, while ProRes 4444 is
# modelled near its high-quality mezzanine data rate. The result remains an
# bound is enforced by the matching max-rate/buffer flags in the renderer.
_H264_BITS_PER_PIXEL_FRAME = Fraction(1, 4)
_H264_MIN_VIDEO_BITRATE = 4_000_000
_H264_MAX_VIDEO_BITRATE = 80_000_000
_PRORES_4444_BITS_PER_PIXEL_FRAME = Fraction(6, 1)
_AAC_BITRATE = 320_000
_PCM_S16LE_STEREO_BITRATE = 48_000 * 2 * 16
_INLINE_AUDIO_WAV_BYTES_PER_SECOND = 48_000 * 2 * 2
_INLINE_AUDIO_WAV_HEADER_BYTES = 44
_MUX_OVERHEAD_PERCENT = 3
_MUX_FIXED_OVERHEAD_BYTES = _MIB
_OPERATIONAL_GUARD_PERCENT = 10
# Remotion creates a webpack bundle, Chromium profile/cache, and renderer
# bookkeeping outside the media-size model below.
_REMOTION_RUNTIME_WORKSPACE_BYTES = 128 * _MIB


REVIEW_MAX_WIDTH = 640
REVIEW_MAX_HEIGHT = 360


class StorageEstimateError(ValueError):
    """Raised when an exact estimate input is absent or malformed."""


def remotion_frame_format(
    *,
    alpha: bool = False,
) -> str:
    """Return the fixed Remotion frame format for opaque and alpha renders."""

    return "png" if alpha else "jpeg"


def remotion_frame_sequence_bytes(
    *,
    frames: int,
    width: int,
    height: int,
    image_format: str,
) -> int:
    """Expected scratch bytes for one selected-format frame sequence."""

    if image_format not in {"png", "jpeg"}:
        raise StorageEstimateError(f"unknown Remotion frame format: {image_format!r}")
    if image_format == "png":
        per_frame = math.ceil(
            Fraction((int(width) * 4 + 1) * int(height), 1) * Fraction(101, 100)
        )
    else:
        # Nine representative 1080p film frames encoded at JPEG95 averaged
        # 0.089 bytes/pixel (range 0.040-0.162). Use 0.25 for planning;
        # actual free volume space guards content that exceeds the estimate.
        per_frame = math.ceil(Fraction(int(width) * int(height), 4))
    return per_frame * int(frames)


def review_render_profile(profile: RenderProfile, *, alpha: bool = False) -> RenderProfile:
    """Return the size Remotion emits for a review render (``--scale``).

    Mirrors the render backend: the canvas is scaled to fit inside 640x360.
    Opaque H.264 dimensions are rounded down to even values; alpha preserves
    the native rounded dimensions.
    """

    scale = min(1.0, REVIEW_MAX_WIDTH / profile.width, REVIEW_MAX_HEIGHT / profile.height)

    def scaled(source: int) -> int:
        candidate = source
        while not alpha and candidate > 1 and int(candidate * scale + 0.5) % 2:
            candidate -= 1
        return max(1, int(candidate * scale + 0.5))

    return replace(profile, width=scaled(profile.width), height=scaled(profile.height))


def _canonical_json_size(value: Any) -> int:
    return len(
        json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    )


def _normalized_digest(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.removeprefix("sha256:")
    if len(normalized) != 64 or any(char not in "0123456789abcdef" for char in normalized):
        return None
    return normalized


def managed_object_sizes(
    registry: Mapping[str, Any],
    media_rows: Iterable[Mapping[str, Any]],
) -> dict[str, int]:
    """Return exact, digest-deduplicated sizes for registry-managed objects.

    Runtime media rows are the size authority. Registry hints are intentionally
    ignored: an authored document may identify an object, but it cannot assert
    how many bytes the runtime will materialize.
    """

    indexed: dict[str, tuple[str, int]] = {}
    for row in media_rows:
        if not isinstance(row, Mapping):
            continue
        digest = _normalized_digest(
            row.get("digest")
            or row.get("content_hash")
            or row.get("content_sha256")
            or row.get("object_id")
        )
        size = row.get("size")
        if digest is None or isinstance(size, bool) or not isinstance(size, int) or size < 0:
            continue
        identities = {
            value
            for value in (row.get("media_id"), row.get("id"), row.get("object_id"), digest, f"sha256:{digest}")
            if isinstance(value, str) and value
        }
        for identity in identities:
            previous = indexed.get(identity)
            current = (digest, size)
            if previous is not None and previous != current:
                raise StorageEstimateError(
                    f"runtime media identity {identity!r} has contradictory digest/size rows"
                )
            indexed[identity] = current

    raw_assets = registry.get("assets", {})
    if not isinstance(raw_assets, Mapping):
        raise StorageEstimateError("managed registry assets must be an object")
    result: dict[str, int] = {}
    for asset_id, raw in raw_assets.items():
        if not isinstance(raw, Mapping):
            continue
        identity = raw.get("media_id") or raw.get("object_id")
        digest = _normalized_digest(
            raw.get("content_sha256")
            or raw.get("digest")
            or raw.get("sha256")
            or raw.get("hash")
        )
        record = indexed.get(identity) if isinstance(identity, str) else None
        if record is None and digest is not None:
            record = indexed.get(digest) or indexed.get(f"sha256:{digest}")
        if record is None:
            raise StorageEstimateError(
                f"managed asset {asset_id!r} has no exact runtime object size"
            )
        actual_digest, size = record
        if digest is not None and actual_digest != digest:
            raise StorageEstimateError(
                f"managed asset {asset_id!r} size row does not match its admitted digest"
            )
        previous_size = result.get(actual_digest)
        if previous_size is not None and previous_size != size:
            raise StorageEstimateError(
                f"managed object {actual_digest!r} has contradictory sizes"
            )
        result[actual_digest] = size
    return result


def used_effect_asset_sizes(
    timeline: Mapping[str, Any],
    *,
    element_registry: Any | None = None,
) -> dict[str, int]:
    """Return exact sizes of declared effect assets used by the timeline.

    Discovery is kept outside :func:`estimate_managed_render_storage`, leaving
    the estimator itself pure. Paths are digest-independent source inputs and
    are deduplicated because the renderer stages each used effect asset once.
    """

    if element_registry is None:
        from astrid.core.element.registry import load_default_registry
        from astrid.core.foundation.paths import REPO_ROOT

        element_registry = load_default_registry(project_root=REPO_ROOT)
    effects = {element.id: element for element in element_registry.list(kind="effects")}
    aliases: dict[str, str] = {}
    for effect_id, element in effects.items():
        raw_aliases = element.metadata.get("clipTypeAliases")
        if isinstance(raw_aliases, list):
            for alias in raw_aliases:
                if isinstance(alias, str) and alias:
                    aliases[alias] = effect_id
    if "text-card" in effects:
        aliases.setdefault("text", "text-card")

    used: set[str] = set()
    for clip in timeline.get("clips", []):
        if not isinstance(clip, Mapping):
            continue
        clip_type = clip.get("clipType")
        if not isinstance(clip_type, str):
            continue
        effect_id = clip_type if clip_type in effects else aliases.get(clip_type)
        if effect_id is not None:
            used.add(effect_id)

    sizes: dict[str, int] = {}
    for effect_id in sorted(used):
        element = effects[effect_id]
        for asset in element.assets:
            path = (element.root / asset.path).resolve()
            if not path.is_file():
                raise StorageEstimateError(
                    f"effect asset is unavailable for storage estimation: {path}"
                )
            sizes[str(path)] = path.stat().st_size
    return sizes


def _is_alpha_timeline(timeline: Mapping[str, Any]) -> bool:
    metadata = timeline.get("metadata")
    layer = metadata.get("astrid_layer") if isinstance(metadata, Mapping) else None
    return isinstance(layer, Mapping) and layer.get("alpha") is True


def h264_encoder_bitrates(
    *, width: int, height: int, fps_rational: tuple[int, int]
) -> tuple[int, int]:
    """Return the opaque render max-rate and 2x VBV buffer in whole Kbit/s."""

    fps = Fraction(*fps_rational)
    modelled = math.ceil(
        Fraction(width * height, 1) * fps * _H264_BITS_PER_PIXEL_FRAME
    )
    clamped = min(_H264_MAX_VIDEO_BITRATE, max(_H264_MIN_VIDEO_BITRATE, modelled))
    max_rate = math.ceil(clamped / 1000) * 1000
    return max_rate, max_rate * 2


def _render_profile(
    timeline: Mapping[str, Any],
    registry: Mapping[str, Any],
    requested_profile: Mapping[str, Any] | RenderProfile | None,
) -> RenderProfile:
    if isinstance(requested_profile, RenderProfile):
        return requested_profile
    if isinstance(requested_profile, Mapping):
        return RenderProfile.from_dict(requested_profile)
    return resolve_render_profile(timeline, registry)


def managed_render_window(
    timeline: Mapping[str, Any],
    registry: Mapping[str, Any],
    time_range: Any,
    requested_profile: Mapping[str, Any] | RenderProfile | None = None,
) -> FrameWindow:
    """Resolve a half-open seconds range against the exact managed render clock."""
    if not isinstance(time_range, (list, tuple)) or len(time_range) != 2:
        raise StorageEstimateError("range must contain START and END seconds")
    try:
        start, end = (Fraction(str(value)) for value in time_range)
    except (ValueError, ZeroDivisionError, OverflowError) as exc:
        raise StorageEstimateError("range START and END must be finite seconds") from exc
    if start < 0 or end <= start:
        raise StorageEstimateError("range must satisfy 0 <= START < END")
    profile = _render_profile(timeline, registry, requested_profile)
    fps = Fraction(*profile.fps_rational)
    total_frames = timeline_render_duration_frames(timeline, float(fps))
    duration = Fraction(total_frames, 1) / fps
    if end > duration:
        raise StorageEstimateError(
            f"range ends at {float(end):g}s, beyond timeline duration {float(duration):g}s"
        )
    # Match the timeline compositor's Math.round (ties toward +infinity).
    start_frame = (2 * start * fps + 1) // 2
    end_frame = (2 * end * fps + 1) // 2
    if end_frame > total_frames:
        raise StorageEstimateError(
            f"range resolves beyond the timeline's {total_frames}-frame render clock"
        )
    try:
        return FrameWindow(
            start_frame=int(start_frame), end_frame=int(end_frame),
            fps_rational=profile.fps_rational,
        )
    except (TypeError, ValueError) as exc:
        raise StorageEstimateError(str(exc)) from exc


def estimate_managed_render_storage(
    *,
    timeline: Mapping[str, Any],
    registry: Mapping[str, Any],
    object_sizes: Mapping[str, int],
    effect_asset_sizes: Mapping[str, int] | None = None,
    requested_profile: Mapping[str, Any] | RenderProfile | None = None,
    review_render: bool = False,
    window: FrameWindow | Mapping[str, Any] | None = None,
    streaming_memory_budget_bytes: int = 0,
) -> dict[str, Any]:
    """Estimate peak task storage from one expanded canonical snapshot.

    ``object_sizes`` must contain exact runtime-owned sizes, keyed by unique
    SHA-256 digest. The estimate models the measured materialization,
    pre-encode, frame-sequence, output-staging, and settlement phases, then
    adds an explicit operational guard. ``estimated_output_bytes`` is the
    output copy published into runtime CAS at settlement.
    """

    normalized_sizes: dict[str, int] = {}
    managed_input_bytes = 0
    for digest, size in object_sizes.items():
        normalized = _normalized_digest(digest)
        if normalized is None:
            raise StorageEstimateError(f"invalid managed object digest: {digest!r}")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise StorageEstimateError(f"invalid managed object size for {digest!r}")
        previous = normalized_sizes.get(normalized)
        if previous is not None and previous != size:
            raise StorageEstimateError(f"contradictory managed object size for {digest!r}")
        normalized_sizes[normalized] = size
    managed_input_bytes = sum(normalized_sizes.values())

    raw_assets = registry.get("assets", {})
    if not isinstance(raw_assets, Mapping):
        raise StorageEstimateError("managed registry assets must be an object")
    managed_entry_bytes = 0
    for asset_id, raw in raw_assets.items():
        if not isinstance(raw, Mapping):
            continue
        digest = _normalized_digest(
            raw.get("content_sha256")
            or raw.get("digest")
            or raw.get("sha256")
            or raw.get("hash")
        )
        if digest is None or digest not in normalized_sizes:
            raise StorageEstimateError(
                f"managed asset {asset_id!r} has no exact entry size"
            )
        managed_entry_bytes += normalized_sizes[digest]

    effect_asset_bytes = 0
    for path, size in (effect_asset_sizes or {}).items():
        if not isinstance(path, str) or not path:
            raise StorageEstimateError("effect asset size keys must be non-empty paths")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise StorageEstimateError(f"invalid effect asset size for {path!r}")
        effect_asset_bytes += size

    profile = _render_profile(timeline, registry, requested_profile)
    alpha = _is_alpha_timeline(timeline)
    implicit_review_scale = bool(review_render) and requested_profile is None
    if implicit_review_scale:
        profile = review_render_profile(profile, alpha=alpha)
    fps = Fraction(*profile.fps_rational)
    authored_frames = timeline_duration_frames(timeline, float(fps))
    authored_render_frames = timeline_render_duration_frames(timeline, float(fps))
    if window is not None and not isinstance(window, FrameWindow):
        try:
            window = FrameWindow.from_dict(window)
        except (TypeError, ValueError) as exc:
            raise StorageEstimateError(f"invalid render window: {exc}") from exc
    if window is not None:
        if window.fps_rational != profile.fps_rational or window.end_frame > authored_render_frames:
            raise StorageEstimateError("render window does not fit the resolved timeline clock")
        frames = window.duration_frames
    else:
        frames = authored_render_frames
    duration = Fraction(frames, 1) / fps
    pixel_rate = Fraction(profile.width * profile.height, 1) * fps
    encoder_buffer_bps = 0
    if alpha:
        codec = "prores-4444"
        video_bitrate = math.ceil(pixel_rate * _PRORES_4444_BITS_PER_PIXEL_FRAME)
        audio_bitrate = _PCM_S16LE_STEREO_BITRATE
        bitrate_basis = "6 bits_per_pixel_frame plus 48kHz stereo PCM"
    else:
        codec = "h264"
        video_bitrate, encoder_buffer_bps = h264_encoder_bitrates(
            width=profile.width,
            height=profile.height,
            fps_rational=profile.fps_rational,
        )
        # Remotion's opaque path always muxes AAC, including a silent track.
        audio_bitrate = _AAC_BITRATE
        bitrate_basis = "0.25 bits_per_pixel_frame clamped to 4-80Mbps plus 320kbps AAC"

    encoded_payload = (
        (duration * video_bitrate + encoder_buffer_bps) / 8
        + duration * audio_bitrate / 8
    )
    encoded_payload_bytes = math.ceil(encoded_payload)
    estimated_output_bytes = (
        math.ceil(
            encoded_payload
            * Fraction(100 + _MUX_OVERHEAD_PERCENT, 100)
        )
        + _MUX_FIXED_OVERHEAD_BYTES
    )
    container_overhead_bytes = estimated_output_bytes - encoded_payload_bytes
    snapshot_bytes = _canonical_json_size(timeline) + _canonical_json_size(registry)
    audio_tracks = {
        track.get("id")
        for track in timeline.get("tracks", [])
        if isinstance(track, Mapping) and track.get("kind") == "audio"
    }
    effective_audio_duration = Fraction(0)
    audio_asset_count = 0
    for clip in timeline.get("clips", []):
        if not isinstance(clip, Mapping) or clip.get("track") not in audio_tracks:
            continue
        if window is None:
            effective_audio_duration += Fraction(str(clip_timeline_duration(clip)))
            audio_asset_count += 1
            continue
        overlap = max(
            0,
            min(clip_end_frame(clip, float(fps)), window.end_frame)
            - max(clip_start_frame(clip, float(fps)), window.start_frame),
        )
        if overlap:
            effective_audio_duration += Fraction(overlap, 1) / fps
            audio_asset_count += 1
    merge_pcm_outputs = 1
    merge_level_inputs = audio_asset_count
    while merge_level_inputs >= 32:
        merge_level_inputs = math.ceil(merge_level_inputs / 10)
        merge_pcm_outputs += merge_level_inputs
    audio_pcm_working_bytes = math.ceil(
        4
        * 48_000
        * (effective_audio_duration + duration * merge_pcm_outputs)
    )
    registry_assets = registry.get("assets", {})
    visual_tracks = {
        track.get("id"): track
        for track in timeline.get("tracks", [])
        if isinstance(track, Mapping)
        and track.get("kind") == "visual"
        and track.get("id") is not None
    }
    # ``@remotion/media`` gives each <Video> a render-asset ID made from its
    # source and Sequence context (source, rounded start, rounded duration),
    # rather than from the registry asset name alone.  The inline-audio
    # mixer keeps one sparse WAV per such ID.  Each frame writes at its global
    # output position, so a clip at 90s leaves a WAV whose logical extent is
    # 90s + its duration.  These files coexist until createAudio() finishes.
    inline_audio_assets: set[tuple[str, int, int]] = set()
    for clip in timeline.get("clips", []):
        if not isinstance(clip, Mapping):
            continue
        track = visual_tracks.get(clip.get("track"))
        asset_name = clip.get("asset")
        if not isinstance(track, Mapping) or not isinstance(asset_name, str):
            continue
        if clip.get("clipType") not in {None, "media", "video"}:
            continue
        asset_entry = registry_assets.get(asset_name)
        if not isinstance(asset_entry, Mapping):
            continue
        asset_type = str(asset_entry.get("type") or asset_entry.get("media_type") or "")
        if asset_type.startswith("image"):
            continue
        if track.get("muted") is True:
            continue
        track_volume = track.get("volume", 1)
        clip_volume = clip.get("volume", 1)
        if (
            isinstance(track_volume, (int, float))
            and track_volume <= 0
        ) or (
            isinstance(clip_volume, (int, float))
            and clip_volume <= 0
        ):
            continue
        source = asset_entry.get("file")
        if not isinstance(source, str) or not source:
            source = asset_name
        start_frame = clip_start_frame(clip, float(fps))
        end_frame = clip_end_frame(clip, float(fps))
        if window is not None:
            start_frame = max(start_frame, window.start_frame)
            end_frame = min(end_frame, window.end_frame)
            if end_frame <= start_frame:
                continue
        duration_frames = max(1, end_frame - start_frame)
        inline_audio_assets.add((source, start_frame, duration_frames))
    inline_audio_mix_working_bytes = sum(
        math.ceil(
            Fraction(start_frame + duration_frames, 1)
            / fps
            * _INLINE_AUDIO_WAV_BYTES_PER_SECOND
        )
        + _INLINE_AUDIO_WAV_HEADER_BYTES
        for _, start_frame, duration_frames in inline_audio_assets
    )
    encoded_working_copy_bytes = estimated_output_bytes
    alpha_frame_bytes_per_frame = (
        math.ceil(
            Fraction(profile.width * profile.height * 4 + profile.height, 1)
            * Fraction(101, 100)
        )
        if alpha
        else 0
    )
    alpha_frame_working_bytes = alpha_frame_bytes_per_frame * frames
    # Paint coordinates remain authored; frame storage follows the selected
    # format at rendered device pixels, before any frame-provider resize.
    # Explicit profiles do not enable the renderer's implicit review scale.
    capture_profile = _render_profile(timeline, registry, None)
    frame_profile = profile if implicit_review_scale else max(
        (profile, capture_profile), key=lambda value: value.width * value.height
    )
    frame_image_format = remotion_frame_format(alpha=alpha)
    frame_sequence_bytes_per_frame = remotion_frame_sequence_bytes(
        frames=1,
        width=frame_profile.width,
        height=frame_profile.height,
        image_format=frame_image_format,
    )
    frame_sequence_bytes = frame_sequence_bytes_per_frame * frames
    if (type(streaming_memory_budget_bytes) is not int or streaming_memory_budget_bytes < 0):
        raise StorageEstimateError("streaming memory budget must be a non-negative owner allocation")
    streaming_memory_required_bytes = profile.width * profile.height * 1000 + 2 * 1024**3
    streams_frames = (not alpha and streaming_memory_budget_bytes >= streaming_memory_required_bytes)
    # Streaming pipes frames directly to ffmpeg. If the renderer falls back
    # to disk, the live volume guard still protects available space.
    frame_working_bytes = 0 if streams_frames else max(alpha_frame_working_bytes, frame_sequence_bytes)
    base_bytes = (
        managed_input_bytes
        + effect_asset_bytes
        + snapshot_bytes
    )
    # A support probe stages one asset copy and removes it before rendering.
    # The normal renderer reuses the host's materialized assets. Disk frames
    # and the parallel encoder's working file are alternative paths, and CAS
    # publication can need an additional output copy after frame cleanup.
    phase_working_bytes = max(
        managed_entry_bytes,
        audio_pcm_working_bytes
        + inline_audio_mix_working_bytes
        + max(frame_working_bytes, encoded_working_copy_bytes)
        + estimated_output_bytes,
    )
    peak_before_guard_bytes = base_bytes + phase_working_bytes
    operational_guard_bytes = (
        math.ceil((peak_before_guard_bytes + _REMOTION_RUNTIME_WORKSPACE_BYTES)
                  * _OPERATIONAL_GUARD_PERCENT / 100)
        + _REMOTION_RUNTIME_WORKSPACE_BYTES
    )
    estimated_total_bytes = peak_before_guard_bytes + operational_guard_bytes
    estimated_scratch_bytes = estimated_total_bytes - estimated_output_bytes

    return {
        "schema_version": 1,
        "kind": "rendering.timeline_storage_estimate",
        "status": "expected_usage",
        "basis": "expanded_canonical_snapshot",
        "codec": codec,
        "bitrate_basis": bitrate_basis,
        "width": profile.width,
        "height": profile.height,
        "fps_rational": list(profile.fps_rational),
        "authored_duration_frames": authored_frames,
        "duration_frames": frames,
        "range_start_frame": window.start_frame if window is not None else 0,
        "range_end_frame": window.end_frame if window is not None else authored_render_frames,
        "duration_seconds_rational": [duration.numerator, duration.denominator],
        "managed_object_count": len(normalized_sizes),
        "managed_input_bytes": managed_input_bytes,
        "managed_entry_bytes": managed_entry_bytes,
        "effect_asset_bytes": effect_asset_bytes,
        "snapshot_bytes": snapshot_bytes,
        "video_bitrate_bps": video_bitrate,
        "audio_bitrate_bps": audio_bitrate,
        "encoder_buffer_bps": encoder_buffer_bps,
        "encoded_payload_bytes": encoded_payload_bytes,
        "container_overhead_bytes": container_overhead_bytes,
        "effective_audio_seconds_rational": [
            effective_audio_duration.numerator,
            effective_audio_duration.denominator,
        ],
        "audio_asset_count": audio_asset_count,
        "merge_pcm_outputs": merge_pcm_outputs,
        "audio_pcm_working_bytes": audio_pcm_working_bytes,
        "inline_audio_asset_count": len(inline_audio_assets),
        "inline_audio_mix_working_bytes": inline_audio_mix_working_bytes,
        "renderer": "rendering.remotion",
        "review_render": bool(review_render),
        "frame_image_format": frame_image_format,
        "capture_canvas_width": capture_profile.width,
        "capture_canvas_height": capture_profile.height,
        "frame_image_width": frame_profile.width,
        "frame_image_height": frame_profile.height,
        "frame_sequence_bytes_per_frame": frame_sequence_bytes_per_frame,
        "frame_sequence_bytes": frame_sequence_bytes,
        "streams_frames": streams_frames,
        "streaming_memory_budget_bytes": streaming_memory_budget_bytes,
        "streaming_memory_required_bytes": streaming_memory_required_bytes,
        "frame_scratch_mode": "streaming" if streams_frames else "disk_fallback",
        "alpha_frame_bytes_per_frame": alpha_frame_bytes_per_frame,
        "alpha_frame_working_bytes": alpha_frame_working_bytes,
        "encoded_working_copy_bytes": encoded_working_copy_bytes,
        "staged_output_bytes": estimated_output_bytes,
        "base_bytes": base_bytes,
        "phase_working_bytes": phase_working_bytes,
        "peak_before_guard_bytes": peak_before_guard_bytes,
        "operational_guard_bytes": operational_guard_bytes,
        "remotion_runtime_workspace_bytes": _REMOTION_RUNTIME_WORKSPACE_BYTES,
        "estimated_scratch_bytes": estimated_scratch_bytes,
        "estimated_output_bytes": estimated_output_bytes,
        "estimated_total_bytes": estimated_total_bytes,
    }


__all__ = [
    "StorageEstimateError",
    "estimate_managed_render_storage",
    "managed_render_window",
    "h264_encoder_bitrates",
    "managed_object_sizes",
    "remotion_frame_format",
    "remotion_frame_sequence_bytes",
    "review_render_profile",
    "used_effect_asset_sizes",
]
