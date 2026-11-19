"""Deterministic H3 composition and preservation-evidence boundary."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
import sys
import tempfile
import wave
import zipfile
from array import array
from contextlib import contextmanager
from fractions import Fraction
from pathlib import Path, PurePosixPath
from typing import Any, Mapping, Sequence

from .timing import (
    ContinuationTiming,
    ContinuationTimingError,
    plan_continuation,
    plan_from_preparation,
)


class CompositionError(ValueError):
    """A generated candidate cannot be safely composed."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _probe(path: Path) -> dict[str, Any] | None:
    """Return ffprobe data, or ``None`` for a non-media fixture."""

    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return None
    try:
        completed = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-count_frames",
                "-show_streams",
                "-show_format",
                "-of",
                "json",
                str(path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        value = json.loads(completed.stdout)
    except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _stream_types(probe: Mapping[str, Any] | None) -> set[str]:
    streams = probe.get("streams") if isinstance(probe, Mapping) else None
    if not isinstance(streams, list):
        return set()
    return {
        str(stream.get("codec_type"))
        for stream in streams
        if isinstance(stream, Mapping) and isinstance(stream.get("codec_type"), str)
    }


def _video_dimensions(probe: Mapping[str, Any] | None, *, role: str) -> tuple[int, int]:
    videos = _streams(probe, "video")
    if len(videos) != 1:
        raise CompositionError(f"{role} media must contain exactly one video stream")
    try:
        width = int(videos[0]["width"])
        height = int(videos[0]["height"])
    except (KeyError, TypeError, ValueError):
        raise CompositionError(f"{role} video stream has no usable dimensions") from None
    if width <= 0 or height <= 0:
        raise CompositionError(f"{role} video stream has no usable dimensions")
    return width, height


def _streams(probe: Mapping[str, Any] | None, stream_type: str) -> list[Mapping[str, Any]]:
    values = probe.get("streams") if isinstance(probe, Mapping) else None
    if not isinstance(values, list):
        return []
    return [
        item
        for item in values
        if isinstance(item, Mapping) and item.get("codec_type") == stream_type
    ]


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _duration(probe: Mapping[str, Any], stream: Mapping[str, Any]) -> float | None:
    direct = _number(stream.get("duration"))
    if direct is not None:
        return direct
    fmt = probe.get("format")
    return _number(fmt.get("duration")) if isinstance(fmt, Mapping) else None


def _video_coverage(path: Path) -> dict[str, Any]:
    probe = _probe(path)
    video = _streams(probe, "video")
    if probe is None or len(video) != 1:
        raise CompositionError("generated/candidate media must contain exactly one video stream")
    stream = video[0]
    rate_text = stream.get("avg_frame_rate") or stream.get("r_frame_rate")
    try:
        rate = Fraction(str(rate_text))
    except (ValueError, ZeroDivisionError):
        raise CompositionError("video stream has no usable frame rate") from None
    if rate <= 0:
        raise CompositionError("video stream has no usable frame rate")
    raw_frames = stream.get("nb_read_frames") or stream.get("nb_frames")
    try:
        if raw_frames is None:
            raise TypeError("frame count is unavailable")
        frames = int(raw_frames)
    except (TypeError, ValueError):
        observed_duration = _duration(probe, stream)
        if observed_duration is None:
            raise CompositionError("video stream has no measurable frame coverage") from None
        frames = round(observed_duration * float(rate))
    if frames <= 0:
        raise CompositionError("video stream contains no frames")
    coverage = {
        "path": str(path),
        "frames": frames,
        "fps": float(rate),
        "duration": frames / float(rate),
        "stream_count": 1,
    }
    # Synthetic coverage callers may provide only timing fields. Public
    # composition and verification require usable dimensions separately.
    try:
        width, height = _video_dimensions(probe, role="generated/candidate")
    except CompositionError:
        pass
    else:
        coverage.update(width=width, height=height)
    return coverage


def _audio_coverage(path: Path) -> dict[str, Any]:
    probe = _probe(path)
    audio = _streams(probe, "audio")
    if probe is None or len(audio) != 1:
        raise CompositionError("generated/candidate media must contain exactly one audio stream")
    observed_duration = _duration(probe, audio[0])
    if observed_duration is None or observed_duration <= 0:
        raise CompositionError("audio stream has no measurable duration")
    return {
        "path": str(path),
        "duration": observed_duration,
        "sample_rate": int(audio[0]["sample_rate"]) if str(audio[0].get("sample_rate", "")).isdigit() else None,
        "stream_count": 1,
    }


def _continuation_timing(preparation: Mapping[str, Any]) -> ContinuationTiming | None:
    try:
        timing = plan_from_preparation(preparation)
        if timing is not None:
            return timing
        request = preparation.get("request")
        schedule = preparation.get("mask_schedule")
        if (
            not isinstance(request, Mapping)
            or request.get("version") != 2
            or not isinstance(schedule, Mapping)
            or schedule.get("branch") != "extension_context"
        ):
            return None
        videos = [
            item
            for item in request.get("media", [])
            if isinstance(item, Mapping)
            and item.get("role") == "timeline"
            and item.get("modality") == "video"
        ]
        if len(videos) != 1:
            raise ContinuationTimingError(
                "native-v2 continuation preparation requires one timeline video"
            )
        source_range = videos[0].get("range")
        if not isinstance(source_range, list) or len(source_range) != 2:
            raise ContinuationTimingError(
                "native-v2 continuation preparation requires a bounded timeline video range"
            )
        return plan_continuation(
            source_end=float(source_range[1]) - float(source_range[0]),
            output_duration=float(request["duration"]),
        )
    except ContinuationTimingError as exc:
        raise CompositionError(str(exc)) from exc


def _is_source_free_generation(preparation: Mapping[str, Any]) -> bool:
    request = preparation.get("request")
    if not isinstance(request, Mapping):
        return False
    if request.get("operation") == "generate":
        return True
    schedule = preparation.get("mask_schedule")
    return (
        request.get("version") == 2
        and isinstance(schedule, Mapping)
        and schedule.get("branch") == "source_free"
    )


def _generation_duration(preparation: Mapping[str, Any]) -> float:
    request = preparation["request"]
    if request.get("version") == 2:
        return float(request["duration"])
    return float(request["output"]["duration"])


def _require_decoded_video_size(video: Mapping[str, Any], *, role: str) -> None:
    if any(type(video.get(key)) is not int or video[key] <= 0 for key in ("width", "height")):
        raise CompositionError(f"{role} video has no usable decoded dimensions")


def _validate_video_size(preparation: Mapping[str, Any], video: Mapping[str, Any], *, role: str) -> None:
    media = preparation.get("media")
    if not isinstance(media, Mapping) or any(
        type(media.get(key)) is not int or media[key] <= 0 for key in ("width", "height")
    ):
        raise CompositionError("preparation has no usable requested video dimensions")
    _require_decoded_video_size(video, role=role)
    actual = (video["width"], video["height"])
    requested = (media["width"], media["height"])
    if actual != requested:
        raise CompositionError(
            f"{role} video size is {actual[0]}x{actual[1]}, requested {requested[0]}x{requested[1]}"
        )


def _validate_generated_coverage(
    *,
    preparation: Mapping[str, Any],
    generated_video: Path,
    generated_audio: Path | None,
) -> dict[str, Any]:
    """Reject a no-op/short H3 result before attempting final composition."""

    if _is_source_free_generation(preparation):
        from .generation import generation_timing
        plan = generation_timing(_generation_duration(preparation))
        video = _video_coverage(generated_video)
        _validate_video_size(preparation, video, role="generated")
        audio = _audio_coverage(generated_audio or generated_video)
        if video["fps"] != plan["fps"] or video["frames"] != plan["raw_frames"]:
            raise CompositionError("H3 generation frame coverage does not match the compiled plan")
        if abs(audio["duration"] - plan["raw_frames"] / plan["fps"]) > 1 / plan["fps"]:
            raise CompositionError("H3 generation audio does not cover the compiled timeline")
        return {"video": video, "audio": audio, "timing": plan}
    timing = _continuation_timing(preparation)
    if timing is None:
        schedule = preparation.get("mask_schedule")
        if isinstance(schedule, Mapping) and schedule.get("branch") == "source_backed_v2v":
            video = _video_coverage(generated_video)
            _validate_video_size(preparation, video, role="generated")
            return {"video": video}
        return {}
    video = _video_coverage(generated_video)
    _require_decoded_video_size(video, role="generated")
    audio = _audio_coverage(generated_audio or generated_video)
    if abs(video["fps"] - timing.fps) > 1e-6:
        raise CompositionError(f"H3 continuation output must be {timing.fps} fps")
    if video["frames"] <= timing.source_frames:
        raise CompositionError(
            "H3 continuation produced no net new frames "
            f"({video['frames']} observed, {timing.source_frames} source)"
        )
    if video["frames"] != timing.expected_graph_output_frames:
        raise CompositionError(
            "H3 continuation frame coverage does not match the compiled raw/context plan: "
            f"observed {video['frames']}, expected {timing.expected_graph_output_frames}"
        )
    tolerance = 1.0 / timing.fps
    if abs(audio["duration"] - timing.expected_graph_output_duration) > tolerance:
        raise CompositionError(
            "H3 continuation audio does not cover the compiled raw/context timeline: "
            f"observed {audio['duration']:.6f}s, expected {timing.expected_graph_output_duration:.6f}s"
        )
    return {"video": video, "audio": audio, "timing": timing.to_dict()}


def _validate_candidate_coverage(
    *, preparation: Mapping[str, Any], candidate: Path
) -> dict[str, Any]:
    """Prove final duration and AV stream coverage from delivered bytes."""

    video = _video_coverage(candidate)
    audio = _audio_coverage(candidate)
    timing = _continuation_timing(preparation)
    if _is_source_free_generation(preparation):
        from .generation import generation_timing
        plan = generation_timing(_generation_duration(preparation))
        if video["fps"] != plan["fps"] or video["frames"] != plan["requested_frames"]:
            raise CompositionError("H3 generation candidate does not match requested frame coverage")
        requested_duration = plan["duration"]
    elif timing is not None:
        if abs(video["fps"] - timing.fps) > 1e-6:
            raise CompositionError(f"composed H3 candidate must be {timing.fps} fps")
        if video["frames"] != timing.requested_output_frames:
            raise CompositionError(
                "composed H3 candidate does not match requested frame coverage: "
                f"observed {video['frames']}, expected {timing.requested_output_frames}"
            )
        requested_duration = timing.requested_output_duration
    else:
        schedule = preparation.get("mask_schedule")
        if not isinstance(schedule, Mapping):
            raise CompositionError("preparation is missing mask_schedule")
        requested_duration = float(schedule["duration"])
        if schedule.get("branch") == "source_backed_v2v":
            native_fps = 24.0
            if abs(video["fps"] - native_fps) > 1e-6:
                raise CompositionError(
                    f"source-backed native-v2 candidate must be {native_fps:g} fps"
                )
            requested_frames = round(requested_duration * native_fps)
            if abs(requested_frames / native_fps - requested_duration) > 1e-6:
                raise CompositionError(
                    "source-backed native-v2 duration is not representable on the 24 fps frame grid"
                )
            if video["frames"] != requested_frames:
                raise CompositionError(
                    "source-backed native-v2 candidate does not match requested frame coverage: "
                    f"observed {video['frames']}, expected {requested_frames}"
                )
            requested_duration = requested_frames / native_fps
        else:
            tolerance = max(1.0 / video["fps"], 0.05)
            if abs(video["duration"] - requested_duration) > tolerance:
                raise CompositionError(
                    "composed candidate video does not cover output.duration: "
                    f"observed {video['duration']:.6f}s, expected {requested_duration:.6f}s"
                )
    if abs(audio["duration"] - requested_duration) > max(1.0 / video["fps"], 0.05):
        raise CompositionError(
            "composed candidate audio does not cover output.duration: "
            f"observed {audio['duration']:.6f}s, expected {requested_duration:.6f}s"
        )
    return {
        "requested_duration": requested_duration,
        "video": video,
        "audio": audio,
        "status": "covered",
    }


def _source_offset(preparation: Mapping[str, Any]) -> float:
    request = preparation.get("request")
    source = request.get("source") if isinstance(request, Mapping) else None
    value = source.get("range") if isinstance(source, Mapping) else None
    if isinstance(value, (list, tuple)) and len(value) == 2:
        return float(value[0])
    if isinstance(request, Mapping) and request.get("version") == 2:
        for item in request.get("media", []):
            if (
                isinstance(item, Mapping)
                and item.get("role") == "timeline"
                and item.get("modality") == "video"
            ):
                value = item.get("range")
                if isinstance(value, (list, tuple)) and len(value) == 2:
                    return float(value[0])
    return 0.0


def _generated_at(intervals: list[list[float]], start: float, end: float) -> bool:
    midpoint = (start + end) / 2.0
    return any(float(item[0]) <= midpoint < float(item[1]) for item in intervals)


def _segments(duration: float, generated: list[list[float]]) -> list[tuple[float, float, bool]]:
    boundaries = {0.0, float(duration)}
    for interval in generated:
        boundaries.add(max(0.0, min(float(duration), float(interval[0]))))
        boundaries.add(max(0.0, min(float(duration), float(interval[1]))))
    ordered = sorted(boundaries)
    return [
        (start, end, _generated_at(generated, start, end))
        for start, end in zip(ordered, ordered[1:])
        if end > start
    ]


_IMAGE_MASK_SUFFIXES = {".bmp", ".jpeg", ".jpg", ".png", ".tif", ".tiff", ".webp"}
_LOSSLESS_MASK_CODECS = {"ffv1", "ffvhuff", "huffyuv", "png", "rawvideo", "utvideo"}
_MASK_FPS = 24


def probe_mask(
    path: Path,
    *,
    mask_id: str = "mask",
    source: Path | None = None,
    duration: float | None = None,
) -> dict[str, Any]:
    """Classify a mask as a still image or a lossless per-frame mask video."""

    if path.suffix.lower() in _IMAGE_MASK_SUFFIXES:
        try:
            from PIL import Image

            with Image.open(path) as image:
                image.verify()
        except Exception as exc:  # noqa: BLE001 - fail closed on untrusted mask bytes
            raise CompositionError(f"regional video mask {mask_id!r} is not a decodable image") from exc
        return {"kind": "image", "frames": 1}
    probe = _probe(path)
    if probe is None:
        raise CompositionError(f"regional video mask {mask_id!r} is not decodable image or video media")
    width, height = _video_dimensions(probe, role=f"mask {mask_id!r}")
    stream = _streams(probe, "video")[0]
    if str(stream.get("codec_name")) not in _LOSSLESS_MASK_CODECS:
        raise CompositionError(
            f"regional video mask {mask_id!r} must use a lossless codec "
            f"({', '.join(sorted(_LOSSLESS_MASK_CODECS))}), not {stream.get('codec_name')!r}"
        )
    if source is not None:
        source_dimensions = _video_dimensions(_probe(source), role="source")
        if (width, height) != source_dimensions:
            raise CompositionError(
                f"regional video mask {mask_id!r} is {width}x{height} but the source is "
                f"{source_dimensions[0]}x{source_dimensions[1]}"
            )
    try:
        raw_frames = stream.get("nb_read_frames") or stream.get("nb_frames")
        if raw_frames is None:
            raise CompositionError(f"regional video mask {mask_id!r} has no countable frames")
        frames = int(raw_frames)
    except (TypeError, ValueError):
        raise CompositionError(f"regional video mask {mask_id!r} has no countable frames") from None
    if duration is not None and frames != round(float(duration) * _MASK_FPS):
        raise CompositionError(
            f"regional video mask {mask_id!r} has {frames} frames; "
            f"expected {round(float(duration) * _MASK_FPS)}"
        )
    return {"kind": "video", "frames": frames}



def _guard_unprepared_static_permissions(preparation: Mapping[str, Any]) -> None:
    """Reject legacy static fallbacks when they would change authored pixels."""
    from .generation import DEFAULT_SIZE

    media = preparation.get("media")
    request = preparation.get("request")
    canvas = request.get("canvas") if isinstance(request, Mapping) else None
    dimensions = media if isinstance(media, Mapping) else canvas
    if isinstance(dimensions, Mapping):
        width, height = dimensions.get("width"), dimensions.get("height")
        if type(width) is not int or type(height) is not int:
            if isinstance(media, Mapping):
                raise CompositionError("preparation has no usable requested video dimensions")
            raise CompositionError("preparation has invalid canvas dimensions")
        target = (width, height)
    else:
        target = DEFAULT_SIZE  # Historical preparations without dimensions use the default.
    schedule = preparation.get("mask_schedule")
    video = schedule.get("video") if isinstance(schedule, Mapping) else None
    changes = video.get("changes") if isinstance(video, Mapping) else None
    assets = {row.get("asset"): row.get("path") for row in preparation.get("assets", [])
              if isinstance(row, Mapping) and row.get("status") == "resolved"}
    for change in changes if isinstance(changes, list) else []:
        if not isinstance(change, Mapping) or change.get("action") != "generate" or change.get("prepared_mask_asset"):
            continue
        area = change.get("area")
        if not isinstance(area, Mapping):
            continue
        selector = area.get("selector") if isinstance(area.get("selector"), Mapping) else area
        if "rectangle" in selector:
            authored = selector.get("canvas", DEFAULT_SIZE)
            if not isinstance(authored, (list, tuple)) or tuple(authored) != target:
                raise CompositionError("static permission requires a compiled mask for its authored canvas")
        mask_id = change.get("mask_asset") or selector.get("mask_asset") or selector.get("asset")
        if mask_id:
            path = assets.get(mask_id)
            if not isinstance(path, str) or not Path(path).is_file():
                raise CompositionError(f"regional video mask {mask_id!r} is not resolved")
            if probe_mask(Path(path), mask_id=str(mask_id))["kind"] == "image":
                if selector.get("threshold") is not None:
                    raise CompositionError("static permission requires a compiled mask for its threshold")
                from .request import H3RequestError
                from .static_regions import _image_pixels
                try:
                    _image_pixels(Path(path), str(mask_id), width=target[0], height=target[1], threshold=None)
                except H3RequestError as exc:
                    raise CompositionError(f"static permission requires a compiled mask: {exc}") from exc

def _spatial_video_masks(
    preparation: Mapping[str, Any], source: Path | None = None
) -> list[dict[str, Any]]:
    """Resolve generated-region masks needed to preserve unedited pixels."""

    schedule = preparation.get("mask_schedule")
    video = schedule.get("video") if isinstance(schedule, Mapping) else None
    changes = video.get("changes") if isinstance(video, Mapping) else None
    if not isinstance(changes, list):
        return []
    assets = preparation.get("assets")
    resolved = {
        str(record.get("asset")): Path(str(record["path"]))
        for record in assets or []
        if isinstance(record, Mapping)
        and record.get("status") == "resolved"
        and isinstance(record.get("asset"), str)
        and isinstance(record.get("path"), str)
    }
    result: list[dict[str, Any]] = []
    schedule_duration = schedule.get("duration") if isinstance(schedule, Mapping) else None
    has_regional = any(
        isinstance(change, Mapping) and change.get("action") == "generate"
        and isinstance(change.get("area"), Mapping)
        and change["area"].get("selector", change["area"]).get("full_frame") is not True
        for change in changes
    )
    for change in changes:
        if not isinstance(change, Mapping) or change.get("action") != "generate":
            continue
        area = change.get("area")
        if not isinstance(area, Mapping):
            raise CompositionError("generated video change is missing its spatial selector")
        selector = area.get("selector")
        selector = selector if isinstance(selector, Mapping) else area
        mask_id = change.get("mask_asset") or selector.get("mask_asset") or selector.get("asset")
        derived = False
        prepared_id = change.get("prepared_mask_asset")
        prepared_path = resolved.get(prepared_id) if isinstance(prepared_id, str) else None
        if prepared_id is not None:
            if prepared_path is None:
                raise CompositionError("prepared static permission mask is missing")
            mask_id = prepared_id
        if not mask_id and selector.get("full_frame") is True and not has_regional:
            continue
        if not mask_id and ("rectangle" in selector or selector.get("full_frame") is True):
            if source is None:
                raise CompositionError("regional video generation requires an authoritative source")
            from .request import H3RequestError
            from .spec import Region
            from .static_regions import resolve_static_region

            width, height = _video_dimensions(_probe(source), role="source")
            box = tuple(selector.get("rectangle", (0, 0, width, height)))
            try:
                region = resolve_static_region(Region("composition", box=box), width=width, height=height)
            except H3RequestError as exc:
                raise CompositionError(str(exc)) from exc
            digest = hashlib.sha256(region.mask_png).hexdigest()
            mask_id = "resolved-box:" + digest
            mask_path = source.parent / (digest + ".mask.png")
            mask_path.write_bytes(region.mask_png)
            derived = True
        else:
            if not isinstance(mask_id, str) or not mask_id:
                raise CompositionError(
                    "regional video generation requires a resolved image mask for preservation"
                )
            resolved_path = resolved.get(mask_id)
            if resolved_path is None:
                raise CompositionError(f"regional video mask {mask_id!r} is not resolved")
            mask_path = resolved_path
            if mask_path is None or not mask_path.is_file():
                raise CompositionError(f"regional video mask {mask_id!r} is not resolved")
        mask_info = probe_mask(
            mask_path,
            mask_id=mask_id,
            source=source,
            duration=schedule_duration,
        )
        if prepared_path is not None and source is not None:
            from PIL import Image
            with Image.open(mask_path) as prepared_image:
                if prepared_image.size != _video_dimensions(_probe(source), role="source"):
                    raise CompositionError("prepared static permission dimensions do not match the source")
        during = change.get("during")
        if not isinstance(during, (list, tuple)) or len(during) != 2:
            raise CompositionError("regional video mask has a malformed edit interval")
        start, end = float(during[0]), float(during[1])
        if end <= start:
            raise CompositionError("regional video mask interval must be positive")
        result.append({
            "during": [start, end], "asset": mask_id, "path": mask_path,
            "kind": mask_info["kind"], "frames": mask_info["frames"],
            **({"prepared": True} if prepared_path is not None else {}),
            **({"derived_box": list(box)} if derived else {}),
        })
    return result


# Canonical decoded-pixel conversion shared by composition and verification.
# Composition converts source segments to planar gbrp with a lanczos scaler;
# swscale upsamples subsampled chroma differently for planar RGB than for the
# packed rgb24 it picks by default (duplicated chroma samples), so evidence
# must be decoded through the same path or yuv420p sources never compare equal.
_CANONICAL_RGB_FILTER = "scale={size}:flags=lanczos,format=gbrp,format=rgb24"


def iter_mask_frames(path: Path, *, start_frame: int, count: int, width: int, height: int):
    """Yield ``count`` decoded rgb24 mask frames starting at ``start_frame``."""

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise CompositionError("ffmpeg is required to verify regional video preservation")
    video_filter = (
        f"trim=start_frame={start_frame}:end_frame={start_frame + count},setpts=PTS-STARTPTS,"
        "format=gbrp,format=rgb24"
    )
    frame_bytes = width * height * 3
    process = subprocess.Popen(
        [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(path), "-vf", video_filter,
         "-an", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert process.stdout is not None
        for _ in range(count):
            frame = process.stdout.read(frame_bytes)
            if len(frame) != frame_bytes:
                raise CompositionError("regional mask video ended before the requested frames")
            yield frame
    finally:
        if process.stdout is not None:
            process.stdout.close()
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        if process.stderr is not None:
            process.stderr.close()


def _protected_from_rgb(frame: bytes) -> list[int]:
    """Pixel indexes whose red permission channel is zero."""

    return [index for index in range(len(frame) // 3) if not frame[index * 3]]


def _verify_spatial_mask_complement(
    *,
    source: Path,
    candidate: Path,
    masks: list[Mapping[str, Any]],
    source_offset: float,
    fps: float = 24.0,
    tolerance: int = 0,
) -> list[dict[str, Any]]:
    """Compare every fully protected pixel in masked generated intervals."""

    if not masks:
        return []
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise CompositionError("ffmpeg is required to verify regional video preservation")
    source_probe = _probe(source)
    candidate_probe = _probe(candidate)
    width, height = _video_dimensions(source_probe, role="source")
    if _video_dimensions(candidate_probe, role="candidate") != (width, height):
        raise CompositionError("regional preservation candidate dimensions do not match the source")
    try:
        from PIL import Image

        resampling = getattr(Image, "Resampling", Image).NEAREST
    except ImportError as exc:
        raise CompositionError("Pillow is required to verify regional video preservation") from exc

    results: list[dict[str, Any]] = []
    duration = max(float(mask["during"][1]) for mask in masks)
    segments = _segments_with_spatial_masks(duration, [list(mask["during"]) for mask in masks], masks)
    for start, end, _generated, original_mask in segments:
        if original_mask is None:
            continue
        mask = {**original_mask, "during": [start, end]}
        members = _mask_members(mask)
        duration = end - start
        exact_frames = duration * fps
        frame_count = round(exact_frames)
        if frame_count <= 0 or abs(exact_frames - frame_count) > 1e-6:
            raise CompositionError("regional mask interval is not aligned to the native frame grid")
        is_video = any(member.get("kind") == "video" for member in members)
        mask_frame_iters = []
        static_pixels = []
        for member in members:
            mask_path = Path(str(member["path"]))
            if member.get("kind") == "video":
                if _video_dimensions(_probe(mask_path), role="mask") != (width, height):
                    raise CompositionError("regional video mask dimensions do not match the source")
                mask_frame_iters.append(iter_mask_frames(
                    mask_path, start_frame=round(start * fps), count=frame_count,
                    width=width, height=height,
                ))
            else:
                with Image.open(mask_path) as opened:
                    channel = opened.convert("RGB").getchannel("R")
                    static_pixels.append((channel if member.get("prepared") else channel.resize((width, height), resampling)).tobytes())
        protected_pixels = [
            index for index in range(width * height)
            if all(pixels[index] == 0 for pixels in static_pixels)
        ]
        mask_evidence = _mask_segment_evidence(mask)
        frame_bytes = width * height * 3

        def start_decoder(path: Path, decode_start: float):
            video_filter = (
                f"trim=start={decode_start}:duration={duration},setpts=PTS-STARTPTS,"
                f"fps={fps},{_CANONICAL_RGB_FILTER.format(size=f'{width}:{height}')}"
            )
            return subprocess.Popen(
                [
                    ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(path),
                    "-vf", video_filter, "-an", "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

        decoders = [
            start_decoder(source, source_offset + start),
            start_decoder(candidate, start),
        ]
        max_difference = 0
        observed_frames = 0
        per_frame_counts: list[int] = []
        try:
            for frame_index in range(frame_count):
                if mask_frame_iters:
                    mask_frames = [next(iterator) for iterator in mask_frame_iters]
                    protected_pixels = [
                        pixel for pixel in range(width * height)
                        if all(pixels[pixel] == 0 for pixels in static_pixels)
                        and all(frame[pixel * 3] == 0 for frame in mask_frames)
                    ]
                    per_frame_counts.append(len(protected_pixels))
                frames: list[bytes] = []
                for decoder in decoders:
                    assert decoder.stdout is not None
                    frame = decoder.stdout.read(frame_bytes)
                    if len(frame) != frame_bytes:
                        raise CompositionError("regional preservation media ended before the requested frames")
                    frames.append(frame)
                source_frame, candidate_frame = frames
                observed_frames += 1
                for pixel_index in protected_pixels:
                    offset = pixel_index * 3
                    difference = max(
                        abs(source_frame[offset + channel] - candidate_frame[offset + channel])
                        for channel in range(3)
                    )
                    max_difference = max(max_difference, difference)
                    if difference > tolerance:
                        x, y = pixel_index % width, pixel_index // width
                        source_rgb = list(source_frame[offset:offset + 3])
                        candidate_rgb = list(candidate_frame[offset:offset + 3])
                        raise CompositionError(
                            "candidate changes source pixels outside the declared regional video mask "
                            f"at frame {frame_index}, pixel ({x}, {y}): "
                            f"source={source_rgb}, candidate={candidate_rgb}, max_delta={difference}"
                        )
            for decoder in decoders:
                assert decoder.stdout is not None
                if decoder.stdout.read(1):
                    raise CompositionError("regional preservation decoder returned extra frames")
        finally:
            for mask_frame_iter in mask_frame_iters:
                mask_frame_iter.close()
            for decoder in decoders:
                if decoder.stdout is not None:
                    decoder.stdout.close()
                try:
                    decoder.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    decoder.kill()
                    decoder.wait()
                if decoder.stderr is not None:
                    decoder.stderr.close()
        results.append({
            "during": [start, end],
            "asset": members[0].get("asset") if len(members) == 1 else None,
            "mask_sha256": mask_evidence.get("mask_sha256"),
            **({"mask_union": mask_evidence["mask_union"]} if "mask_union" in mask_evidence else {}),
            "frame_count": observed_frames,
            "protected_pixel_count_per_frame": len(protected_pixels),
            "max_abs_channel_difference": max_difference,
            "tolerance": tolerance,
            **({"mask_kind": "video", "protected_pixel_count_per_frame": per_frame_counts}
               if is_video else {}),
        })
    return results


def _mask_members(mask: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    return list(mask["union"]) if "union" in mask else [mask]


def _mask_segment_evidence(mask: Mapping[str, Any]) -> dict[str, Any]:
    members = _mask_members(mask)
    evidence = [{"mask_asset": item["asset"], "mask_sha256": _sha256(Path(item["path"])),
                 "mask_kind": item.get("kind", "image"), "mask_frames": item.get("frames", 1)}
                for item in members]
    return evidence[0] if len(evidence) == 1 else {"mask_union": evidence}


def _segments_with_spatial_masks(
    duration: float,
    generated: list[list[float]],
    masks: Sequence[Mapping[str, Any]],
) -> list[tuple[float, float, bool, Mapping[str, Any] | None]]:
    base = _segments(duration, generated)
    boundaries = {point for start, end, _ in base for point in (start, end)}
    for boundary_mask in masks:
        start, end = map(float, boundary_mask["during"])
        boundaries.update((max(0.0, min(float(duration), start)), max(0.0, min(float(duration), end))))
    ordered = sorted(boundaries)
    result: list[tuple[float, float, bool, Mapping[str, Any] | None]] = []
    for start, end in zip(ordered, ordered[1:]):
        if end <= start:
            continue
        use_generated = _generated_at(generated, start, end)
        active = [
            active_mask for active_mask in masks
            if float(active_mask["during"][0]) <= (start + end) / 2 < float(active_mask["during"][1])
        ] if use_generated else []
        mask: Mapping[str, Any] | None = (
            {"during": [start, end], "union": active}
            if len(active) > 1
            else (active[0] if active else None)
        )
        result.append((start, end, use_generated, mask))
    return result


@contextmanager
def _generated_inputs(path: Path):
    """Yield verified generated video/audio paths and their role manifest."""

    if path.suffix.lower() != ".zip":
        yield path, None, {}
        return
    try:
        archive = zipfile.ZipFile(path, "r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise CompositionError("generated audiovisual bundle is not a readable ZIP") from exc
    with archive:
        names = archive.namelist()
        if names.count("manifest.json") != 1 or len(names) != len(set(names)):
            raise CompositionError("generated audiovisual bundle has an invalid member inventory")
        try:
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
        except (KeyError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CompositionError("generated audiovisual bundle manifest is unreadable") from exc
        if not isinstance(manifest, Mapping) or manifest.get("schema_version") != 1 or manifest.get("kind") != "h3_av_generated_av":
            raise CompositionError("generated audiovisual bundle has an unsupported contract")
        records = manifest.get("outputs")
        if not isinstance(records, list) or {item.get("role") for item in records if isinstance(item, Mapping)} != {"video", "audio"} or len(records) != 2:
            raise CompositionError("generated audiovisual bundle must declare one video and one audio output")
        parsed: dict[str, Mapping[str, Any]] = {}
        for record in records:
            if not isinstance(record, Mapping):
                raise CompositionError("generated audiovisual output record is malformed")
            role = record.get("role")
            member = record.get("member")
            digest = record.get("sha256")
            size = record.get("size")
            member_path = PurePosixPath(str(member))
            if (
                role not in {"video", "audio"}
                or role in parsed
                or not isinstance(member, str)
                or member_path.is_absolute()
                or ".." in member_path.parts
                or not member_path.name
                or member_path.as_posix() != member
                or not isinstance(digest, str)
                or len(digest) != 64
                or not isinstance(size, int)
                or size < 0
            ):
                raise CompositionError("generated audiovisual output record is invalid")
            if member not in names or member == "manifest.json":
                raise CompositionError(f"generated audiovisual member {member!r} is missing")
            data = archive.read(member)
            if len(data) != size or hashlib.sha256(data).hexdigest() != digest:
                raise CompositionError(f"generated audiovisual member {member!r} failed integrity validation")
            parsed[str(role)] = record
        declared_members = {"manifest.json", *(str(record["member"]) for record in records)}
        if set(names) != declared_members:
            raise CompositionError("generated audiovisual bundle contains undeclared members")
        with tempfile.TemporaryDirectory(prefix="h3-av-generated-") as raw_root:
            root = Path(raw_root)
            paths: dict[str, Path] = {}
            for role in ("video", "audio"):
                record = parsed[role]
                member = str(record["member"])
                target = root / PurePosixPath(member).name
                target.write_bytes(archive.read(member))
                paths[role] = target
            yield paths["video"], paths["audio"], {"outputs": [dict(record) for record in records]}


def _compose_media(
    *,
    source: Path,
    generated: Path,
    generated_audio_path: Path | None = None,
    destination: Path,
    duration: float,
    generated_video: list[list[float]],
    generated_audio: list[list[float]],
    source_offset: float,
    video_fps: float | None = None,
    spatial_video_masks: Sequence[Mapping[str, Any]] | None = None,
    exact_audio_path: Path | None = None,
    preserve_rgb: bool = False,
) -> dict[str, Any]:
    """Compose source and generated streams into a lossless MKV candidate."""

    ffmpeg = shutil.which("ffmpeg")
    source_probe = _probe(source)
    generated_probe = _probe(generated)
    generated_audio_probe = _probe(generated_audio_path) if generated_audio_path is not None else generated_probe
    if ffmpeg is None or source_probe is None or generated_probe is None or generated_audio_probe is None:
        raise CompositionError("media composition requires ffmpeg and decodable source/video/audio files")
    source_types = _stream_types(source_probe)
    generated_types = _stream_types(generated_probe)
    generated_audio_types = _stream_types(generated_audio_probe)
    output_width, output_height = _video_dimensions(source_probe, role="source")
    source_video_stream = _streams(source_probe, "video")[0]
    source_pixel_format = source_video_stream.get("pix_fmt")
    video_segments = _segments_with_spatial_masks(
        duration, generated_video, spatial_video_masks or []
    )
    audio_segments = [
        (start, end, use_generated, None)
        for start, end, use_generated in _segments(duration, generated_audio)
    ]
    for stream_type, segments in (("video", video_segments), ("audio", audio_segments)):
        for segment in segments:
            _start, _end, use_generated = segment[0], segment[1], segment[2]
            available = (
                (generated_types if stream_type == "video" else generated_audio_types)
                if use_generated
                else source_types
            )
            if stream_type not in available:
                raise CompositionError(
                    f"{stream_type} stream is missing from the {'generated candidate' if use_generated else 'source baseline'}"
                )

    filters: list[str] = []
    maps: list[str] = []
    input_args = ["-i", str(source), "-i", str(generated)]
    if generated_audio_path is not None:
        input_args.extend(["-i", str(generated_audio_path)])
    exact_audio_index = 2 + int(generated_audio_path is not None)
    if exact_audio_path is not None:
        input_args.extend(["-i", str(exact_audio_path)])
    mask_fps = video_fps or 24.0
    mask_input_indices: dict[str, int] = {}
    next_input_index = exact_audio_index + int(exact_audio_path is not None)
    for mask in spatial_video_masks or []:
        path = str(mask["path"])
        if path not in mask_input_indices:
            mask_input_indices[path] = next_input_index
            if mask.get("kind") == "video":
                input_args.extend(["-i", path])
            else:
                input_args.extend(["-loop", "1", "-framerate", str(mask_fps), "-i", path])
            next_input_index += 1
    if mask_input_indices or preserve_rgb:
        # Preservation is verified on decoded RGB pixels. Keep every segment
        # and the lossless candidate in full-resolution planar RGB so a mask
        # boundary cannot alter shared chroma samples when converted back to
        # a subsampled YUV source format.
        video_format = "gbrp"
    else:
        video_format = None
    for stream_type, segments, label in (("video", video_segments, "v"), ("audio", audio_segments, "a")):
        if stream_type == "audio" and exact_audio_path is not None:
            filters.append(f"[{exact_audio_index}:a:0]asetpts=PTS-STARTPTS[aout]")
            maps.append("[aout]")
            continue
        pieces: list[str] = []
        for index, segment in enumerate(segments):
            start, end, use_generated = segment[0], segment[1], segment[2]
            spatial_mask = segment[3] if stream_type == "video" else None
            input_index = (
                0
                if not use_generated
                else (1 if stream_type == "video" or generated_audio_path is None else 2)
            )
            trim_start = start if use_generated else source_offset + start
            trim_end = end if use_generated else source_offset + end
            piece = f"{label}{index}"
            if stream_type == "video":
                # H3 intentionally runs at its working resolution.  The source
                # timeline remains authoritative, so normalize every segment to
                # its dimensions before concat; otherwise ffmpeg rejects a
                # perfectly valid low-resolution generated continuation.
                if spatial_mask is not None:
                    base = f"vbase{index}"
                    overlay = f"voverlay{index}"
                    mask_label = f"vmask{index}"
                    mask_filters = []
                    member_labels = []
                    for ordinal, member in enumerate(_mask_members(spatial_mask)):
                        member_label = f"vmask{index}_{ordinal}"
                        member_labels.append(member_label)
                        mask_index = mask_input_indices[str(member["path"])]
                        if member.get("kind") == "video":
                            mask_filter = (
                                f"[{mask_index}:v:0]"
                                f"trim=start_frame={round(start * 24)}:end_frame={round(end * 24)},"
                                f"setpts=PTS-STARTPTS,fps={mask_fps},format=gbrp,extractplanes=r,format=gray,format=gbrp[{member_label}]"
                            )
                        else:
                            mask_filter = (
                                f"[{mask_index}:v:0]"
                                f"{'' if member.get('prepared') else f'scale={output_width}:{output_height}:flags=neighbor,'}fps={mask_fps},format=gbrp,"
                                f"extractplanes=r,format=gray,format=gbrp,trim=duration={end - start},setpts=PTS-STARTPTS[{member_label}]"
                            )
                        mask_filters.append(mask_filter)
                    current_label = member_labels[0]
                    for ordinal, member_label in enumerate(member_labels[1:], 1):
                        merged_label = f"vmask{index}_union{ordinal}"
                        mask_filters.append(
                            f"[{current_label}][{member_label}]blend=all_mode=lighten[{merged_label}]"
                        )
                        current_label = merged_label
                    mask_filters.append(f"[{current_label}]null[{mask_label}]")
                    filters.extend([
                        f"[0:v:0]trim=start={source_offset + start}:end={source_offset + end},"
                        f"scale={output_width}:{output_height}:flags=lanczos,setsar=1,fps={mask_fps},"
                        f"format=gbrp,setpts=PTS-STARTPTS[{base}]",
                        f"[1:v:0]trim=start={start}:end={end},"
                        f"scale={output_width}:{output_height}:flags=lanczos,setsar=1,fps={mask_fps},"
                        f"format=gbrp,setpts=PTS-STARTPTS[{overlay}]",
                        *mask_filters,
                        f"[{base}][{overlay}][{mask_label}]maskedmerge[{piece}_rgb]",
                        f"[{piece}_rgb]format={video_format}[{piece}]",
                    ])
                else:
                    filters.append(
                        f"[{input_index}:v:0]trim=start={trim_start}:end={trim_end},"
                        f"scale={output_width}:{output_height}:flags=lanczos,setsar=1,"
                        f"{f'format={video_format},' if video_format else ''}setpts=PTS-STARTPTS[{piece}]"
                    )
            else:
                filters.append(
                    f"[{input_index}:a:0]atrim=start={trim_start}:end={trim_end},asetpts=PTS-STARTPTS[{piece}]"
                )
            pieces.append(f"[{piece}]")
        output_label = f"{label}out"
        if len(pieces) == 1:
            passthrough_filter = "null" if stream_type == "video" else "anull"
            filters.append(f"{pieces[0]}{passthrough_filter}[{output_label}]")
        else:
            filters.append(
                "".join(pieces)
                + f"concat=n={len(pieces)}:v={'1' if stream_type == 'video' else '0'}:a={'1' if stream_type == 'audio' else '0'}[{output_label}]"
            )
        if stream_type == "video" and video_fps is not None:
            # Interior native-v2 edit cuts must stay on the native frame grid.
            # Concatenated container timestamps can drift by a millisecond and
            # put a protected boundary frame before its declared interval.
            filters.append(f"[{output_label}]setpts=N/({video_fps}*TB)[vclock]")
            output_label = "vclock"
        maps.append(f"[{output_label}]")

    destination.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        *input_args,
        "-filter_complex", ";".join(filters),
        "-map", maps[0], "-map", maps[1], "-map_metadata", "-1",
        *( ["-c:v", "ffv1", "-level", "3", "-pix_fmt", video_format]
           if video_format else ["-c:v", "libx264", "-qp", "0", "-preset", "medium"] ),
        "-c:a", "pcm_s16le", "-t", str(duration), str(destination),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise CompositionError(f"ffmpeg composition failed: {detail[-500:]}") from exc
    return {
        "method": "ffmpeg-stream-compose-v1",
        "container": "matroska",
        "video_segments": [
            {
                "during": [start, end],
                "source": "generated" if use_generated else "source",
                **(_mask_segment_evidence(spatial_mask) if spatial_mask is not None else {}),
            }
            for start, end, use_generated, spatial_mask in video_segments
        ],
        "audio_segments": [
            {"during": [start, end], "source": "generated" if use_generated else "source"}
            for start, end, use_generated, _mask in audio_segments
        ],
    }


def _mux_generated(video: Path, audio: Path, destination: Path) -> None:
    """Create one candidate container without dropping LanPaint's audio role."""

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None or _probe(video) is None or _probe(audio) is None:
        raise CompositionError("paired audiovisual composition requires ffmpeg and decodable video/audio files")
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(video), "-i", str(audio),
        "-map", "0:v:0", "-map", "1:a:0", "-map_metadata", "-1",
        "-c:v", "libx264", "-qp", "0", "-preset", "medium",
        "-c:a", "pcm_s16le", str(destination),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise CompositionError(f"ffmpeg audiovisual mux failed: {detail[-500:]}") from exc


def _normalize_full_replacement(
    *, generated_video: Path, generated_audio: Path | None,
    destination: Path, duration: float,
) -> dict[str, Any]:
    """Trim and remux a full replacement to the requested AV timeline."""

    ffmpeg = shutil.which("ffmpeg")
    video_probe = _probe(generated_video)
    videos = _streams(video_probe, "video")
    if ffmpeg is None or video_probe is None or len(videos) != 1:
        raise CompositionError("full-replacement composition requires one decodable generated video")
    rate_text = videos[0].get("avg_frame_rate") or videos[0].get("r_frame_rate")
    try:
        rate = Fraction(str(rate_text))
    except (ValueError, ZeroDivisionError):
        raise CompositionError("full-replacement video has no usable frame rate") from None
    if rate <= 0:
        raise CompositionError("full-replacement video has no usable frame rate")
    requested_frames = round(duration * float(rate))
    if requested_frames <= 0 or abs(requested_frames / float(rate) - duration) > 1 / float(rate):
        raise CompositionError("requested full-replacement duration is not representable at the generated frame rate")

    if generated_audio is None:
        if len(_streams(video_probe, "audio")) != 1:
            raise CompositionError("full-replacement candidate has no generated audio stream")
        inputs = ["-i", str(generated_video)]
        audio_map = "0:a:0"
    else:
        audio_probe = _probe(generated_audio)
        if audio_probe is None or len(_streams(audio_probe, "audio")) != 1:
            raise CompositionError("full-replacement generated audio is not decodable")
        inputs = ["-i", str(generated_video), "-i", str(generated_audio)]
        audio_map = "1:a:0"

    fps = f"{rate.numerator}/{rate.denominator}"
    duration_text = f"{duration:.12g}"
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *inputs,
        "-map", "0:v:0", "-map", audio_map,
        "-vf", f"trim=end_frame={requested_frames},setpts=N/({fps}*TB)",
        "-af", f"atrim=end={duration_text},asetpts=PTS-STARTPTS",
        "-r", fps, "-fps_mode", "cfr", "-map_metadata", "-1",
        "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
        "-c:a", "aac", str(destination),
    ]
    try:
        subprocess.run(command, check=True, capture_output=True, text=True)
    except (OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "stderr", "") or str(exc)
        raise CompositionError(f"full-replacement AV normalization failed: {detail[-500:]}") from exc
    return {
        "method": "h3-full-replacement-trim-v1",
        "requested_duration": duration,
        "fps": float(rate),
        "requested_frames": requested_frames,
    }



def _decode_exact_pcm(path: Path, *, expected_channels: int | None = None, generated: bool = False,
                      model_context: bool = False) -> tuple[bytes, int]:
    """Decode the named baseline as 48 kHz interleaved signed 16-bit PCM.

    Kept and supplied inputs must already have a 48 kHz mono or stereo layout;
    the generated input alone may be conformed to the baseline sample rate.
    Stereo generated audio is mixed to mono as (left + right) / 2 when the
    prepared source is mono. Optional model context may also duplicate mono
    to stereo; no kept or supplied delivery channel is transformed.
    """
    probe = _probe(path)
    streams = _streams(probe, "audio") if probe else []
    if len(streams) != 1:
        raise CompositionError(f"{path.name}: exactly one audio stream is required")
    stream = streams[0]
    try:
        rate, channels = int(stream["sample_rate"]), int(stream["channels"])
    except (KeyError, TypeError, ValueError):
        raise CompositionError(f"{path.name}: audio sample rate and channel layout must be measurable") from None
    layout = stream.get("channel_layout")
    allowed_layout = "mono" if channels == 1 else "stereo" if channels == 2 else None
    downmix = (generated or model_context) and channels == 2 and expected_channels == 1
    duplicate_context = model_context and channels == 1 and expected_channels == 2
    if (allowed_layout is None or layout not in (None, "unknown", allowed_layout)
            or (expected_channels is not None and channels != expected_channels and not (downmix or duplicate_context))):
        raise CompositionError(f"{path.name}: audio layout must match the source mono or stereo layout")
    if rate != 48000 and not (generated or model_context):
        raise CompositionError(f"{path.name}: kept and supplied audio must be 48000 Hz; prepare a 48000 Hz source")
    channel_filter = ("pan=mono|c0=0.5*c0+0.5*c1" if downmix else
                      "pan=stereo|c0=c0|c1=c0" if duplicate_context else None)
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
               "-map", "0:a:0", "-vn", *( ["-ar", "48000"] if generated or model_context else [] ),
               *( ["-af", channel_filter] if channel_filter else [] ),
               "-c:a", "pcm_s16le", "-f", "s16le", "-"]
    try:
        pcm = subprocess.run(command, check=True, capture_output=True).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise CompositionError(f"{path.name}: cannot decode canonical PCM: {exc}") from exc
    if downmix:
        channels = 1
    elif duplicate_context:
        channels = 2
    if len(pcm) % (channels * 2):
        raise CompositionError(f"{path.name}: decoded PCM has an incomplete sample")
    return pcm, channels


def _splice_exact_pcm(source: bytes, *, channels: int, total_samples: int,
                      source_start: int = 0, generated: bytes | None = None,
                      generated_spans: list[tuple[int, int]] | None = None,
                      fixed_spans: list[tuple[int, int, bytes, int]] | None = None,
                      unfilled_generated_spans: list[tuple[int, int]] | None = None) -> bytes:
    """Resolve each sample owner, then copy only the owner's available PCM.

    Unfilled generated spans reserve ownership during pre-splice and retain
    compatible source context when available. Fixed audio wins over both.
    """
    generated_spans = generated_spans or []
    fixed_spans = fixed_spans or []
    unfilled_generated_spans = unfilled_generated_spans or []
    stride = channels * 2
    if channels not in (1, 2) or source_start < 0 or total_samples < 0:
        raise CompositionError("invalid canonical PCM source or channel count")
    if len(source) % stride:
        raise CompositionError("source audio has an incomplete sample")
    for first, last in (*generated_spans, *unfilled_generated_spans):
        if not 0 <= first < last <= total_samples:
            raise CompositionError("invalid generated audio sample span")
    if generated_spans and generated is None:
        raise CompositionError("invalid generated audio sample span")
    seen: list[tuple[int, int]] = []
    for first, last, pcm, offset in fixed_spans:
        if not 0 <= first < last <= total_samples or offset < 0 or len(pcm) < (offset + last - first) * stride:
            raise CompositionError("supplied audio does not cover its requested sample span")
        if any(first < other_end and other_start < last for other_start, other_end in seen):
            raise CompositionError("supplied audio sample spans overlap")
        seen.append((first, last))
    boundaries = sorted({0, total_samples,
        *(point for first, last in (*generated_spans, *unfilled_generated_spans) for point in (first, last)),
        *(point for first, last, _, _ in fixed_spans for point in (first, last))})
    segments: list[tuple[int, int, str, tuple[int, int, bytes, int] | None]] = []
    for first, last in zip(boundaries, boundaries[1:]):
        fixed = next((item for item in fixed_spans if item[0] <= first and last <= item[1]), None)
        if fixed is not None:
            segments.append((first, last, "fixed", fixed))
        elif any(a <= first and last <= b for a, b in generated_spans):
            segments.append((first, last, "generated", None))
        elif any(a <= first and last <= b for a, b in unfilled_generated_spans):
            segments.append((first, last, "unfilled", None))
        else:
            segments.append((first, last, "source", None))
    if generated is not None:
        if len(generated) % stride:
            raise CompositionError("generated audio has an incomplete sample")
        available = len(generated) // stride
        needed = max((last for _, last, owner, _ in segments if owner == "generated"), default=0)
        if available < needed:
            # A 40 Hz H3 tick can be at most 600 samples short of the video end.
            nearest_tick = round(total_samples / 1200) * 1200
            if available != nearest_tick or needed - available > 600:
                raise CompositionError("generated audio does not cover its requested sample spans")
            generated += bytes((needed - available) * stride)
    result = bytearray(total_samples * stride)
    for first, last, owner, fixed in segments:
        if owner == "fixed":
            assert fixed is not None
            fixed_start, _, pcm, offset = fixed
            begin = (offset + first - fixed_start) * stride
            result[first * stride:last * stride] = pcm[begin:begin + (last - first) * stride]
        elif owner == "generated":
            assert generated is not None
            result[first * stride:last * stride] = generated[first * stride:last * stride]
        elif owner == "unfilled":
            # Retain compatible source context for fractional denoising, but
            # do not require source samples in generated-owned intervals.
            begin = (source_start + first) * stride
            count = min((last - first) * stride, max(0, len(source) - begin))
            result[first * stride:first * stride + count] = source[begin:begin + count]
        elif owner == "source":
            begin, end = (source_start + first) * stride, (source_start + last) * stride
            if len(source) < end:
                raise CompositionError("source audio does not cover the requested sample window")
            result[first * stride:last * stride] = source[begin:end]
    return bytes(result)



def _exact_audio_for_request(preparation: Mapping[str, Any], *, source: Path, generated: Path,
                             destination: Path, duration: float, source_offset: float,
                             pre_splice_only: bool = False) -> tuple[Path, bytes, int, list[dict[str, Any]]] | None:
    """Prepare the v2 delivered PCM from occurrence and sample-grid intent."""
    request = preparation.get("request")
    if not isinstance(request, Mapping) or request.get("version") != 2:
        return None
    media = request["media"]
    refs = {item.get("id", item["occurrence_id"]): item for item in media if item.get("role") == "reference"}
    edits = [edit for item in media if item.get("role") == "timeline" for edit in item.get("edit", [])
             if edit.get("stream") == "audio" and edit.get("action") == "generate" and edit.get("strength", 1) > 0]
    mask_schedule = preparation.get("mask_schedule", {})
    # Continuations already have an established suffix composition path; their
    # source is intentionally shorter than the output timeline.
    if mask_schedule.get("branch") == "extension_context":
        return None
    schedule = mask_schedule.get("audio", {})
    scheduled_generated = schedule.get("generated_intervals") if isinstance(schedule, Mapping) else None
    if not edits and not scheduled_generated:
        return None
    assets = {row["asset"]: Path(row["path"]) for row in preparation.get("assets", [])
              if row.get("status") == "resolved" and row.get("path")}
    total = round(duration * 48000)
    fixed_items = []
    explicit_generated = []
    for edit in edits:
        first, last = edit["resolved"]["range"]
        guides = edit.get("guides", [])
        fixed = [refs[name] for name in guides if name in refs and refs[name].get("use") == "replacement"]
        if fixed:
            if len(fixed) != 1 or len(guides) != 1:
                raise CompositionError("a fixed audio span requires exactly one replacement audio occurrence")
            item = fixed[0]
            if item["asset"] not in assets:
                raise CompositionError(f"fixed audio asset {item['asset']!r} is missing")
            selected = item.get("resolved_range")
            offset = selected[0] if selected is not None else 0
            if selected is not None and selected[1] - offset < last - first:
                raise CompositionError("fixed audio trim does not cover its delivery span")
            fixed_items.append((first, last, item, offset))
        else:
            explicit_generated.append((first, last))
    # A derived pass retains all classified references, including fixed clips
    # it does not select. Such a pass has nothing to pre-splice.
    if pre_splice_only and not fixed_items:
        return None
    owned_generated_spans = (
        [(round(float(first) * 48000), round(float(last) * 48000)) for first, last in scheduled_generated]
        if scheduled_generated is not None else explicit_generated)
    # Fixed owners take priority; don't require generated samples underneath them.
    for fixed_start, fixed_end, _, _ in fixed_items:
        remaining = []
        for first, last in owned_generated_spans:
            if first < fixed_start:
                remaining.append((first, min(last, fixed_start)))
            if last > fixed_end:
                remaining.append((max(first, fixed_end), last))
        owned_generated_spans = [(first, last) for first, last in remaining if first < last]
    generated_spans = [] if pre_splice_only else owned_generated_spans
    owned_spans = sorted([*owned_generated_spans, *((first, last) for first, last, _, _ in fixed_items)])
    covered = 0
    source_owned = False
    for first, last in owned_spans:
        if first > covered:
            source_owned = True
        covered = max(covered, last)
    source_owned = source_owned or covered < total
    source_probe = _probe(source)
    has_source_audio = bool(_streams(source_probe, "audio")) if source_probe else False
    source_pcm = None
    candidate_pcm = None
    if source_owned:
        if not has_source_audio:
            raise CompositionError("source has no audio and generation or supplied audio does not cover the timeline")
        source_pcm, channels = _decode_exact_pcm(source)
        if generated_spans:
            candidate_pcm, _ = _decode_exact_pcm(generated, expected_channels=channels, generated=True)
    elif fixed_items:
        _, channels = _decode_exact_pcm(assets[fixed_items[0][2]["asset"]])
        if generated_spans:
            candidate_pcm, _ = _decode_exact_pcm(generated, expected_channels=channels, generated=True)
    elif generated_spans:
        candidate_pcm, channels = _decode_exact_pcm(generated, generated=True)
    else:
        raise CompositionError("source has no audio and no generated or supplied audio covers the timeline")
    fixed_spans = []
    for first, last, item, offset in fixed_items:
        pcm, _ = _decode_exact_pcm(assets[item["asset"]], expected_channels=channels)
        fixed_spans.append((first, last, pcm, offset))
    if source_pcm is None and pre_splice_only and has_source_audio:
        try:
            source_pcm, _ = _decode_exact_pcm(source, expected_channels=channels)
        except CompositionError:
            # Resampling is permitted only for optional model context in
            # fractional generated grants, never for source-owned delivery.
            fractional = [edit["resolved"]["range"] for edit in edits
                          if not edit.get("guides") and 0 < edit.get("strength", 1) < 1]
            if fractional:
                try:
                    context, _ = _decode_exact_pcm(source, expected_channels=channels, model_context=True)
                except CompositionError:
                    pass
                else:
                    sparse = bytearray(len(context))
                    stride = channels * 2
                    source_base = round(source_offset * 48000)
                    for first, last in fractional:
                        begin = (source_base + first) * stride
                        end = min((source_base + last) * stride, len(context))
                        if begin < end:
                            sparse[begin:end] = context[begin:end]
                    source_pcm = bytes(sparse)
    if source_pcm is None:
        source_pcm = bytes(total * channels * 2)
        source_start = 0
    else:
        source_start = round(source_offset * 48000)
    pcm = _splice_exact_pcm(source_pcm, channels=channels, total_samples=total,
                            source_start=source_start, generated=candidate_pcm,
                            generated_spans=generated_spans, fixed_spans=fixed_spans,
                            unfilled_generated_spans=owned_generated_spans if pre_splice_only else [])
    destination.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(destination), "wb") as wav:
        wav.setnchannels(channels)
        wav.setsampwidth(2)
        wav.setframerate(48000)
        wav.writeframes(pcm)
    boundaries = sorted({0, total, *(point for first, last in owned_generated_spans for point in (first, last)),
                         *(point for first, last, _, _ in fixed_spans for point in (first, last))})
    spans = []
    for first, last in zip(boundaries, boundaries[1:]):
        owner = "fixed" if any(a <= first and last <= b for a, b, _, _ in fixed_spans) else (
            "generated" if any(a <= first and last <= b for a, b in owned_generated_spans) else "source")
        spans.append({"samples": [first, last], "source": owner})
    return destination, pcm, channels, spans


def _decode_audio_samples(path: Path, start: float, duration: float) -> tuple[list[int], int]:
    """Decode one interval to a stable comparison format."""

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise CompositionError("ffmpeg is required for protected audio evidence")
    if duration <= 0:
        raise CompositionError("protected audio interval must be positive")
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(path),
        "-ss", str(start), "-t", str(duration), "-map", "0:a:0", "-vn",
        "-ar", "48000", "-ac", "2", "-f", "s16le", "-",
    ]
    try:
        completed = subprocess.run(command, check=True, capture_output=True)
    except (OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "stderr", b"") or str(exc).encode()
        raise CompositionError(f"could not decode protected audio samples: {detail[-500:]!r}") from exc
    if not completed.stdout or len(completed.stdout) % 2:
        raise CompositionError("protected audio interval produced no complete PCM samples")
    values = array("h")
    values.frombytes(completed.stdout)
    if sys.byteorder != "little":
        values.byteswap()
    decoded = list(values)
    return decoded, len(decoded)


def _audio_witness(
    *, source: Path, candidate: Path, source_start: float, candidate_start: float, duration: float
) -> dict[str, float | int]:
    """Compare decoded protected audio without requiring codec-identical PCM."""

    source_values, source_count = _decode_audio_samples(source, source_start, duration)
    candidate_values, candidate_count = _decode_audio_samples(candidate, candidate_start, duration)
    source_frames = len(source_values) // 2
    candidate_frames = len(candidate_values) // 2
    frame_count = min(source_frames, candidate_frames)
    if frame_count <= 0:
        raise CompositionError("protected audio interval produced no complete stereo frames")
    if frame_count < max(source_frames, candidate_frames) * 0.98:
        raise CompositionError("protected audio interval has insufficient sample overlap")

    source_mono = [
        (source_values[offset] + source_values[offset + 1]) / 2.0
        for offset in range(0, frame_count * 2, 2)
    ]
    candidate_mono = [
        (candidate_values[offset] + candidate_values[offset + 1]) / 2.0
        for offset in range(0, frame_count * 2, 2)
    ]
    source_mean = sum(source_mono) / frame_count
    candidate_mean = sum(candidate_mono) / frame_count
    source_energy = sum((value - source_mean) ** 2 for value in source_mono)
    candidate_energy = sum((value - candidate_mean) ** 2 for value in candidate_mono)
    if source_energy <= 1.0 or candidate_energy <= 1.0:
        similarity = 1.0 if max(abs(value) for value in source_mono) < 8 and max(abs(value) for value in candidate_mono) < 8 else 0.0
    else:
        covariance = sum(
            (source_mono[index] - source_mean) * (candidate_mono[index] - candidate_mean)
            for index in range(frame_count)
        )
        similarity = covariance / math.sqrt(source_energy * candidate_energy)
    return {
        "similarity": float(similarity),
        "source_sample_count": source_count,
        "candidate_sample_count": candidate_count,
        "sample_count": min(source_count, candidate_count),
    }


def _sample_digest(path: Path, stream_type: str, start: float, duration: float) -> tuple[str, int]:
    """Hash decoded video frames or PCM samples for one protected interval."""

    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise CompositionError("ffmpeg is required for protected-sample evidence")
    if duration <= 0:
        raise CompositionError("protected sample interval must be positive")
    if stream_type == "video":
        # Compare decoded pixels in one canonical representation. Regional
        # compositions may use full-resolution gbrp to preserve protected
        # RGB values across subsampled source chroma boundaries.
        command = [
            ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(path),
            "-ss", str(start), "-t", str(duration), "-map", "0:v:0", "-an",
            "-vf", _CANONICAL_RGB_FILTER.format(size="iw:ih"), "-pix_fmt", "rgb24",
            "-f", "framemd5", "-",
        ]
    elif stream_type == "audio":
        values, sample_count = _decode_audio_samples(path, start, duration)
        if not values:
            raise CompositionError("protected audio interval produced no samples")
        # This digest remains useful diagnostic evidence, but is not the
        # acceptance criterion because codec/container plumbing can change a
        # few decoded PCM values without changing the protected content.
        payload = json.dumps(values, separators=(",", ":")).encode("ascii")
        return hashlib.sha256(payload).hexdigest(), sample_count
    else:
        raise CompositionError(f"unsupported protected sample stream: {stream_type}")
    try:
        completed = subprocess.run(command, check=True, capture_output=True)
    except (OSError, subprocess.SubprocessError) as exc:
        detail = getattr(exc, "stderr", b"") or str(exc).encode()
        raise CompositionError(f"could not decode protected {stream_type} samples: {detail[-500:]!r}") from exc
    if stream_type == "video":
        hashes = [
            line.rsplit(b",", 1)[-1].strip()
            for line in completed.stdout.splitlines()
            if line and not line.startswith(b"#") and b"," in line
        ]
        if not hashes:
            raise CompositionError(f"protected {stream_type} interval produced no samples")
        return hashlib.sha256(b"\n".join(hashes)).hexdigest(), len(hashes)
    raise CompositionError(f"unsupported protected sample stream: {stream_type}")


def _auto_sample_evidence(
    *, source: Path, candidate: Path, schedule: Mapping[str, Any], source_offset: float
) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = {"video": [], "audio": []}
    for stream_type in ("video", "audio"):
        for interval in schedule[stream_type]["protected_intervals"]:
            start, end = float(interval[0]), float(interval[1])
            source_hash, sample_count = _sample_digest(source, stream_type, source_offset + start, end - start)
            candidate_hash, candidate_count = _sample_digest(candidate, stream_type, start, end - start)
            entry: dict[str, Any] = {
                "interval": [start, end],
                "source_sha256": source_hash,
                "candidate_sha256": candidate_hash,
                "sample_count": min(sample_count, candidate_count),
                "method": "ffmpeg-decoded-samples-v1",
            }
            if stream_type == "audio":
                entry.update(
                    _audio_witness(
                        source=source,
                        candidate=candidate,
                        source_start=source_offset + start,
                        candidate_start=start,
                        duration=end - start,
                    )
                )
                entry["method"] = "ffmpeg-decoded-audio-similarity-v1"
            result[stream_type].append(entry)
    return result


def compose_candidate(
    *,
    preparation: Mapping[str, Any],
    generated: str | Path,
    source: str | Path | None = None,
    baseline: str | Path | None = None,
    out_dir: str | Path,
    preservation_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compose one candidate and publish custody plus preservation evidence."""

    if preparation.get("status") == "orchestration_only":
        raise CompositionError("orchestration-only preparation cannot be composed; use a compiled pass preparation")
    _guard_unprepared_static_permissions(preparation)
    generated_path = Path(generated).expanduser().resolve()
    if not generated_path.is_file():
        raise CompositionError(f"generated candidate does not exist: {generated_path}")
    schedule = preparation.get("mask_schedule")
    if not isinstance(schedule, Mapping):
        raise CompositionError("preparation is missing mask_schedule")
    if preparation.get("status") == "requires_resolution":
        raise CompositionError("cannot compose a request whose mask schedule requires resolution")
    if source is not None and baseline is not None:
        raise CompositionError("source and baseline are mutually exclusive")
    if source is None:
        source = baseline
    source_path: Path | None = None
    if source is not None:
        source_path = Path(source).expanduser().resolve()
        if not source_path.is_file():
            raise CompositionError(f"source does not exist: {source_path}")
    spatial_video_masks = _spatial_video_masks(preparation, source_path)
    protected = (
        any(schedule[domain]["protected_intervals"] for domain in ("video", "audio"))
        or bool(spatial_video_masks)
    )
    request_value = preparation.get("request", {})
    exact_audio_requested = request_value.get("version") == 2 and any(
        edit.get("stream") == "audio" and edit.get("action") == "generate" and edit.get("strength", 1) > 0
        for item in request_value.get("media", []) for edit in item.get("edit", [])
    )
    if protected and source_path is None:
        raise CompositionError("protected composition requires an authoritative source baseline")

    destination = Path(out_dir).expanduser().resolve()
    destination.mkdir(parents=True, exist_ok=True)
    with _generated_inputs(generated_path) as (generated_video, generated_audio, generated_manifest):
        generated_coverage = _validate_generated_coverage(
            preparation=preparation,
            generated_video=generated_video,
            generated_audio=generated_audio,
        )
        composition_method: dict[str, Any] = {"method": "candidate-custody-v1"}
        candidate = destination / generated_video.name
        if _is_source_free_generation(preparation):
            plan = generated_coverage["timing"]
            candidate = destination / "candidate.mp4"
            command = ["ffmpeg", "-v", "error", "-y", "-i", str(generated_video),
                       "-map", "0:v:0", "-map", "0:a:0",
                       "-vf", f"trim=end_frame={plan['requested_frames']},setpts=N/(24*TB)",
                       "-af", f"atrim=end={plan['duration']},asetpts=PTS-STARTPTS",
                       "-r", "24", "-fps_mode", "cfr",
                       "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
                       "-c:a", "aac", str(candidate)]
            subprocess.run(command, check=True, capture_output=True)
            composition_method = {"method": "h3-generation-tail-trim-v1", **plan}
        elif source_path is not None and protected and not any(
            schedule[domain]["generated_intervals"] for domain in ("video", "audio")
        ):
            # An admitted preserve-only request has no generated permissions.
            # Its candidate is the authoritative input, never the model output.
            candidate = destination / f"{source_path.stem}.preserved{source_path.suffix}"
            shutil.copy2(source_path, candidate)
            composition_method = {"method": "h3-source-preservation-v1"}
        elif source_path is not None and (protected or exact_audio_requested) and any(
            schedule[domain]["generated_intervals"] for domain in ("video", "audio")
        ):
            if _probe(source_path) is not None and _probe(generated_video) is not None:
                candidate = destination / f"{generated_video.stem}.composed.mkv"
                exact_audio = _exact_audio_for_request(
                    preparation, source=source_path, generated=generated_audio or generated_video,
                    destination=destination / "prepared-delivery-audio.wav", duration=float(schedule["duration"]),
                    source_offset=_source_offset(preparation),
                )
                composition_method = _compose_media(
                    source=source_path,
                    generated=generated_video,
                    generated_audio_path=generated_audio,
                    destination=candidate,
                    duration=float(schedule["duration"]),
                    generated_video=list(schedule["video"]["generated_intervals"]),
                    generated_audio=list(schedule["audio"]["generated_intervals"]),
                    source_offset=_source_offset(preparation),
                    video_fps=24.0 if schedule.get("branch") == "source_backed_v2v" else None,
                    spatial_video_masks=spatial_video_masks,
                    exact_audio_path=exact_audio[0] if exact_audio is not None else None,
                    preserve_rgb=any(item.get("role") == "timeline" and item.get("modality") == "image"
                                     for item in request_value.get("media", [])),
                )
                if exact_audio is not None:
                    decoded, channels = _decode_exact_pcm(candidate, expected_channels=exact_audio[2])
                    if channels != exact_audio[2] or decoded != exact_audio[1]:
                        raise CompositionError("published PCM differs from the prepared exact audio baseline")
                    composition_method["audio_segments"] = [
                        {"during": [span["samples"][0] / 48000, span["samples"][1] / 48000], "source": span["source"]}
                        for span in exact_audio[3]
                    ]
                    composition_method["exact_audio"] = {"rate": 48000, "channels": channels,
                                                          "representation": "interleaved signed 16-bit little endian",
                                                          "samples": len(decoded) // (channels * 2),
                                                          "delivered_spans": exact_audio[3]}
            else:
                if spatial_video_masks:
                    raise CompositionError(
                        "regional video preservation requires decodable source and generated media"
                    )
                # Non-media fixtures remain composable for legacy unit coverage,
                # but verification will not accept interval-only preservation.
                shutil.copy2(generated_video, candidate)
        elif source_path is not None and not protected:
            candidate = destination / f"{generated_video.stem}.full-replacement.mp4"
            composition_method = _normalize_full_replacement(
                generated_video=generated_video,
                generated_audio=generated_audio,
                destination=candidate,
                duration=float(schedule["duration"]),
            )
        elif generated_audio is not None:
            # A paired LanPaint result must never silently discard its audio
            # stream, even when no protected source interval needs splicing.
            candidate = destination / f"{generated_video.stem}.muxed.mkv"
            _mux_generated(generated_video, generated_audio, candidate)
        elif candidate.resolve() != generated_video:
            shutil.copy2(generated_video, candidate)

        candidate_coverage: dict[str, Any] = {}
        if _probe(candidate) is not None:
            candidate_coverage = _validate_candidate_coverage(
                preparation=preparation,
                candidate=candidate,
            )
            _validate_video_size(preparation, candidate_coverage["video"], role="candidate")
        elif _continuation_timing(preparation) is not None:
            raise CompositionError("H3 continuation result is not decodable audiovisual media")

        evidence = dict(preservation_evidence or {})
        if source_path is not None and protected and _probe(source_path) is not None and _probe(candidate) is not None:
            try:
                auto = _auto_sample_evidence(
                    source=source_path,
                    candidate=candidate,
                    schedule=schedule,
                    source_offset=_source_offset(preparation),
                )
            except CompositionError:
                auto = {"video": [], "audio": []}
            if any(auto.values()):
                evidence["protected_samples"] = auto

        provenance = preparation.get("provenance")
        generated_types = _stream_types(_probe(generated_video))
        output_roles = (
            ["video", "audio"]
            if generated_audio is not None
            else (["muxed_av"] if {"video", "audio"}.issubset(generated_types) else ["video"])
        )
        manifest: dict[str, Any] = {
            "schema_version": 1,
            "kind": "h3_av_composition",
            "request_digest": preparation.get("request_digest"),
            "schedule_digest": schedule.get("digest"),
            "candidate": {"path": str(candidate), "sha256": _sha256(candidate), "size": candidate.stat().st_size},
            "source": None if source_path is None else {"path": str(source_path), "sha256": _sha256(source_path), "size": source_path.stat().st_size},
            "generated_outputs": generated_manifest.get("outputs", []),
            "changed_permissions": {"video": schedule["video"]["generated_intervals"], "audio": schedule["audio"]["generated_intervals"]},
            "protected_permissions": {"video": schedule["video"]["protected_intervals"], "audio": schedule["audio"]["protected_intervals"]},
            "preservation_evidence": evidence,
            "composition": {**composition_method, "output_roles": output_roles},
            "coverage": {
                "generated": generated_coverage,
                "candidate": candidate_coverage,
            },
            "provenance": dict(provenance) if isinstance(provenance, Mapping) else {},
            "status": "composed",
        }
        manifest_path = destination / "composition-manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
        manifest["manifest_path"] = str(manifest_path)
        return manifest


__all__ = ["CompositionError", "compose_candidate", "_probe", "_sample_digest"]
