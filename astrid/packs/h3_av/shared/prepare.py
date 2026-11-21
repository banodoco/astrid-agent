"""Attempt-local preparation for an H3 audiovisual request."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping

from .errors import PreparationError
from .generation import _BUILDER_SLOT_VALUES, LIMITS
from .masks import (
    MaskScheduleError,
    build_mask_schedule,
    interval_difference,
    merge_intervals,
)
from .request import H3Request, H3RequestError, _asset_ids
from .generation import DEFAULT_SIZE, LIMITS
from .request_v2 import branch_for, timeline_image_limit_error


def _probe_conditioning(path: Path) -> dict[str, Any]:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise PreparationError("conditioning limits require ffprobe")
    try:
        result = subprocess.run([ffprobe, "-v", "error", "-show_entries",
            "stream=codec_type,duration,start_time,nb_frames,width,height,avg_frame_rate,sample_rate,channels:format=duration", "-of", "json", str(path)],
            check=False, capture_output=True, text=True, timeout=30)
        payload = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        raise PreparationError(f"cannot measure conditioning media {path.name!r}: {exc}") from exc
    if not isinstance(payload, Mapping) or not isinstance(payload.get("streams"), list):
        raise PreparationError(f"conditioning media {path.name!r} is unreadable (conditioning clip 2–15 seconds)")
    return dict(payload)



def _retained_video_frames(loaded: int, target: int) -> int | None:
    """Apply the pinned H3 node's target cap and 5+17k alignment."""
    frames = min(loaded, target)
    return frames - ((frames - 5) % 17) if frames >= 5 else None


def _reference_size(
    modality: str, width: int, height: int, *, canvas: tuple[int, int] = DEFAULT_SIZE,
) -> dict[str, int]:
    # MiniMaxH3ReferenceToVideo uses the target canvas area for image size "match".
    if modality == "image":
        scale = min(1.0, math.sqrt((canvas[0] * canvas[1]) / (width * height)))
        w, h = width * scale, height * scale
    else:
        ratio = width / height
        w, h = (768 * ratio, 768) if ratio >= 1 else (768, 768 / ratio)
        if w * h > 768 * 1344:
            scale = math.sqrt((768 * 1344) / (w * h))
            w, h = w * scale, h * scale
        cw, ch = max(32, round(w / 32) * 32), max(32, round(h / 32) * 32)
        if width * height >= cw * ch:
            return {"width": cw, "height": ch}
        w, h = width, height
    return {"width": max(32, round(w / 32) * 32), "height": max(32, round(h / 32) * 32)}

def _admit_conditioning(request: H3Request, assets: list[dict[str, Any]], warnings: list[dict[str, str]] | None = None) -> list[dict[str, Any]]:
    """Apply the shared reference table before scheduling any paid/runtime work."""
    from .compose import _number, _streams
    warnings = warnings if warnings is not None else []
    resolved = {str(row["asset"]): Path(str(row["path"])) for row in assets
                if row.get("status") == "resolved" and isinstance(row.get("path"), str)}
    refs = [row for row in request.value["media"] if row.get("role") == "reference" and row.get("use") != "replacement"]
    counts = {"image": 0, "video": 0, "audio": 0}
    totals = {"image": Fraction(0), "video": Fraction(0), "audio": Fraction(0)}
    total_uncertainty = {"video": Fraction(0), "audio": Fraction(0)}
    files = 0
    entry: dict[str, int] = {
        "image": _BUILDER_SLOT_VALUES["image"],
        "video": _BUILDER_SLOT_VALUES["video"],
        "audio": _BUILDER_SLOT_VALUES["audio"],
    }
    duration_clock = {"video": 24, "audio": 48000}
    canvas = request.value.get("canvas")
    target_size = (canvas["width"], canvas["height"]) if isinstance(canvas, Mapping) else DEFAULT_SIZE
    measurements: list[dict[str, Any]] = []
    for item in refs:
        modality = item.get("modality")
        if modality not in counts:
            raise PreparationError(f"conditioning occurrence {item['occurrence_id']!r} has unresolved modality; declare image, video, or audio")
        counts[modality] += 1
        files += 1
        ordinal = counts[modality]
        slot = {"image": f"ref_images.ref_image_{ordinal - 1}", "video": f"ref_videos.ref_video_{ordinal - 1}", "audio": f"ref_audios.ref_audio_{ordinal - 1}"}[modality]
        tag = {"image": f"<Picture {ordinal}>", "video": f"<Video {ordinal}>", "audio": None}[modality]
        measurement: dict[str, Any] = {"occurrence": item["occurrence_id"], "modality": modality,
            "trim": item.get("range"), "bound_file": None, "tracks": [], "retained_picture_frames": None,
            "audio_duration_seconds": None, "slot": slot, "tag": tag, "effective_size": None,
            "resource_estimate": {"schema_valid": False, "qualified": False, "observed": False, "raw_bytes": None}}
        if modality == "video":
            measurement["audio_enabled"] = item.get("audio", True)
        measurements.append(measurement)
        path = resolved.get(str(item["asset"]))
        if path is None:
            continue
        measurement["bound_file"] = str(path)
        if modality == "image":
            try:
                from PIL import Image
                with Image.open(path) as image:
                    width, height = image.size
                    image.verify()
            except Exception as exc:
                raise PreparationError(f"conditioning image metadata is unreadable: {item['occurrence_id']}") from exc
            measurement["tracks"] = ["video"]
            measurement["effective_size"] = _reference_size(modality, width, height, canvas=target_size)
            measurement["retained_picture_frames"] = 1
            measurement["resource_estimate"].update({"schema_valid": True, "raw_bytes": measurement["effective_size"]["width"] * measurement["effective_size"]["height"] * 3})
            continue
        probe = _probe_conditioning(path)
        measurement["tracks"] = [str(row.get("codec_type")) for row in probe.get("streams", []) if isinstance(row, Mapping)]
        streams = _streams(probe, modality)
        if not streams:
            raise PreparationError(f"conditioning clip 2–15 seconds: {item['occurrence_id']} has no {modality} stream")
        stream = streams[0]
        measured = _number(stream.get("duration"))
        width, height = stream.get("width"), stream.get("height")
        if (
            modality == "video"
            and isinstance(width, int)
            and width > 0
            and isinstance(height, int)
            and height > 0
        ):
            measurement["effective_size"] = _reference_size(modality, width, height)
        if measured is None:
            raise PreparationError(f"conditioning clip 2–15 seconds: cannot measure {item['occurrence_id']} duration")
        selected = item.get("resolved_range")
        if isinstance(selected, list) and len(selected) == 2:
            start, end = (float(v) / duration_clock[modality] for v in selected)
            if end > measured + 1e-6:
                raise PreparationError(f"conditioning clip 2–15 seconds: {item['occurrence_id']} trim ends past measured stream duration")
            span_exact = Fraction(int(selected[1]) - int(selected[0]), duration_clock[modality])
            span = float(span_exact)
        else:
            span = measured
            span_exact = Fraction(str(span))
            start = 0.0
            end = measured
        clip_min, clip_max = LIMITS["clip_seconds"]["value"]
        exact_trim = isinstance(selected, list) and len(selected) == 2
        uncertainty = Fraction(0) if exact_trim else Fraction(1, duration_clock[modality])
        if span_exact < Fraction(str(clip_min)) - uncertainty or span_exact > Fraction(str(clip_max)) + uncertainty:
            raise PreparationError(f"conditioning clip 2–15 seconds: {item['occurrence_id']} selects {span:.3f}s")
        if span_exact <= Fraction(str(clip_min)) + uncertainty or span_exact >= Fraction(str(clip_max)) - uncertainty:
            warnings.append({"limit": "clip_seconds", "message": f"{item['occurrence_id']} selects {span:.3f}s near the clip duration limit."})
        if modality == "video":
            video_bound = (
                int(selected[1]) - int(selected[0])
                if isinstance(selected, list)
                else math.ceil(span * 24 - 1e-9) + 1
            )
            totals[modality] += Fraction(video_bound, 24)
        else:
            video_bound = None
            totals[modality] += span_exact
        if not exact_trim:
            total_uncertainty[modality] += Fraction(2, 24) if modality == "video" else Fraction(1, 48000)
        if modality == "video":
            # Estimate the loader's maximum 24-fps frames from metadata; the
            # actual filter output and node-retained count remain unobserved.
            assert video_bound is not None
            bound = video_bound
            duration = request.value.get("duration")
            if duration is not None:
                from .timing import frame_index, smallest_h3_run_at_least
                target = smallest_h3_run_at_least(frame_index(float(duration), "duration"))
            else:
                target = bound
            estimate = _retained_video_frames(bound, target)
            measurement["retained_picture_frames_estimate"] = {"value": estimate, "kind": "metadata_upper_bound", "observed": False}
            effective = measurement["effective_size"]
            measurement["resource_estimate"].update({"schema_valid": True, "raw_bytes_upper_bound": estimate * effective["width"] * effective["height"] * 3 if estimate is not None and effective else None})
        else:
            measurement["resource_estimate"]["schema_valid"] = True
        if modality == "video" and item.get("audio", True):
            audio_streams = _streams(probe, "audio")
            if not audio_streams:
                raise PreparationError(f"conditioning soundtrack trim: {item['occurrence_id']} requests audio but the video has no audio stream")
            audio_duration = _number(audio_streams[0].get("duration"))
            if audio_duration is None:
                raise PreparationError(f"conditioning soundtrack trim: cannot measure soundtrack for {item['occurrence_id']}")
            audio_start, audio_end = start, end
            try:
                video_origin = float(stream.get("start_time", 0))
                audio_origin = float(audio_streams[0].get("start_time", 0))
            except (TypeError, ValueError):
                raise PreparationError(f"conditioning soundtrack trim: cannot measure stream start for {item['occurrence_id']}") from None
            if not math.isfinite(video_origin) or not math.isfinite(audio_origin):
                raise PreparationError(f"conditioning soundtrack trim: cannot measure stream start for {item['occurrence_id']}")
            if video_origin + audio_start < audio_origin - 1e-6 or video_origin + audio_end > audio_origin + audio_duration + 1e-6:
                raise PreparationError(f"conditioning soundtrack trim: {item['occurrence_id']} trim ends past measured audio stream duration")
            # VHS frame_load_cap=0 extracts the whole soundtrack; capped loads
            # extract the selected span, independent of H3 picture truncation.
            bound_audio = span if exact_trim else audio_duration
            totals["audio"] += span_exact if exact_trim else Fraction(str(audio_duration))
            if not exact_trim:
                total_uncertainty["audio"] += Fraction(1, 48000)
            measurement["audio_duration_seconds"] = bound_audio
        elif modality == "audio":
            measurement["audio_duration_seconds"] = span
            channels = int(stream.get("channels") or 1)
            measurement["resource_estimate"]["raw_bytes"] = round(span * int(stream.get("sample_rate") or 48000) * channels * 4)
    # Audio tags follow node input order: video soundtracks, then standalone clips.
    audio_index = 0
    pairs = list(zip([row for row in refs if row.get("modality") in counts], measurements))
    for item, measurement in pairs:
        if item.get("modality") == "video" and item.get("audio", True):
            audio_index += 1
            measurement["audio_tag"] = f"<Audio {audio_index}>"
    for item, measurement in pairs:
        if item.get("modality") == "audio":
            audio_index += 1
            measurement["tag"] = f"<Audio {audio_index}>"
            measurement["input_type"] = "AUDIO"
    for row in measurements:
        estimate = row["resource_estimate"]["raw_bytes"]
        row["resource_estimate"].update({"qualified": estimate is not None, "unit": "bytes"})
    for modality, slot_limit in entry.items():
        if counts[modality] > slot_limit:
            raise PreparationError(f"builder slots {entry}: {modality} has {counts[modality]} references (limit {slot_limit})")
    if files > LIMITS["conditioning_files"]["value"]:
        raise PreparationError("conditioning files 12: supplied conditioning occurrences exceed 12")
    if files == LIMITS["conditioning_files"]["value"]:
        warnings.append({"limit": "conditioning_files", "message": "Conditioning references are at the 12-file limit."})
    for modality, total in totals.items():
        limit = Fraction(str(LIMITS["modality_seconds"]["value"]))
        uncertainty = total_uncertainty.get(modality, Fraction(0))
        if total > limit + uncertainty:
            raise PreparationError(f"per modality total 15 seconds: {modality} selects {float(total):.3f}s")
        if total >= limit - uncertainty:
            warnings.append({"limit": "modality_seconds", "message": f"{modality} selects {float(total):.3f}s near the modality duration limit."})
    continuation = request.value.get("continuation")
    if continuation:
        allowed = LIMITS["continuation_references"]["value"]
        if counts["image"] > allowed["image"] or counts["video"] > allowed["video"] or counts["audio"] > allowed["audio"]:
            raise PreparationError(f"continuation references {allowed}: supplied references exceed lowering slots")
    return measurements


class _SourceAVError(H3RequestError, PreparationError):
    """A G21-shaped source error that remains catchable as PreparationError."""


def _missing_ffprobe(message: str) -> _SourceAVError:
    return _SourceAVError(
        message, code="H3_SOURCE_AV_FFPROBE", field="source.asset",
        fix="Install ffprobe and ensure it is available on PATH",
    )


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()



def _video_stream_types(path: Path) -> set[str]:
    """Inspect timeline streams before composition permissions are sealed."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise _missing_ffprobe("Character Swap source inspection requires ffprobe")
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(path)],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        payload = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        raise PreparationError(f"cannot inspect Character Swap source video streams: {exc}") from exc
    streams = payload.get("streams") if isinstance(payload, Mapping) else None
    if not isinstance(streams, list):
        raise PreparationError("ffprobe returned no stream list for the Character Swap source video")
    return {
        str(row["codec_type"])
        for row in streams
        if isinstance(row, Mapping) and isinstance(row.get("codec_type"), str)
    }


def _resolve_assets(request: H3Request, asset_map: Mapping[str, str] | None) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for asset_id in _asset_ids(request):
        raw_path = asset_map.get(asset_id) if asset_map else None
        record: dict[str, Any] = {"asset": asset_id}
        if raw_path is None:
            record.update({"kind": "managed_asset", "status": "unresolved"})
        else:
            path = Path(raw_path).expanduser().resolve()
            if not path.is_file():
                raise PreparationError(f"asset {asset_id!r} does not resolve to a file: {path}")
            record.update(
                {
                    "kind": "file",
                    "status": "resolved",
                    "path": str(path),
                    "size": path.stat().st_size,
                    "sha256": _file_digest(path),
                }
            )
        records.append(record)
    return records


def _validate_mask_assets(request: H3Request, assets: list[dict[str, Any]]) -> None:
    """Fail before scheduling when a resolved video mask breaks the contract."""

    from .compose import CompositionError, iter_mask_frames, probe_mask

    resolved = {
        str(record["asset"]): Path(str(record["path"]))
        for record in assets
        if record.get("status") == "resolved" and isinstance(record.get("path"), str)
    }
    duration = request.value["duration"]
    total = round(float(duration) * 24)
    for item in request.value["media"]:
        for edit in item.get("edit", []):
            mask = edit.get("mask")
            if edit.get("stream") != "video" or not isinstance(mask, Mapping) or "asset" not in mask:
                continue
            mask_id = str(mask["asset"])
            path = resolved.get(mask_id)
            if path is None:
                continue
            source = resolved.get(str(item["asset"])) if item.get("modality") == "video" else None
            start = round(float(edit["during"][0]) * 24)
            end = round(float(edit["during"][1]) * 24)
            try:
                info = probe_mask(path, mask_id=mask_id, source=source, duration=duration)
                if info["kind"] != "video":
                    continue
                if "threshold" in mask:
                    raise H3RequestError(f"mask {mask_id!r} has unsupported field: threshold for video mask")
                width, height = _mask_dimensions(path, mask_id)
                for index, frame in enumerate(
                    iter_mask_frames(path, start_frame=0, count=total, width=width, height=height)
                ):
                    red = frame[::3]
                    if not red == frame[1::3] == frame[2::3]:
                        raise PreparationError(f"mask video {mask_id!r} frame {index}: Color mask channels disagree")
                    values = set(red)
                    if not values <= {0, 255}:
                        raise PreparationError(
                            f"mask video {mask_id!r} frame {index} has values other than 0 and 255"
                        )
                    if not start <= index < end and values - {0}:
                        raise PreparationError(
                            f"mask video {mask_id!r} frame {index} is non-zero outside the edit's during"
                        )
            except CompositionError as exc:
                raise PreparationError(f"mask video {mask_id!r}: {exc}") from exc


def _mask_dimensions(path: Path, mask_id: str) -> tuple[int, int]:
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise PreparationError(f"mask video {mask_id!r} inspection requires ffprobe")
    result = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height", "-of", "csv=p=0", str(path)],
        check=False, capture_output=True, text=True, timeout=30,
    )
    try:
        width, height = (int(part) for part in result.stdout.strip().split(","))
    except ValueError as exc:
        raise PreparationError(f"mask video {mask_id!r} has no readable dimensions") from exc
    return width, height


SOURCE_AV_RATE_CODE = "H3_SOURCE_AV_RATE"
SOURCE_AV_CONFORM_HINT = "H3 could not prepare the requested audiovisual source"
_SOURCE_AV_RATE = Fraction(24, 1)
# One output frame at 24 fps, the same bound compose.py applies to continuation audio coverage.
_SOURCE_AV_TOLERANCE_SECONDS = 1.0 / 24.0
_SOURCE_AV_BRANCHES = frozenset({"source_backed_v2v", "extension_context"})


def _rate_fraction(text: Any) -> Fraction | None:
    try:
        rate = Fraction(str(text))
    except (ValueError, ZeroDivisionError):
        return None
    return rate if rate > 0 else None


def _seconds(text: Any) -> float | None:
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _fps_text(rate: Fraction | None) -> str:
    return f"{float(rate):.3f} fps" if rate is not None else "unknown fps"


def _check_source_av_rate(
    asset_id: str, path: Path, *, expected_size: tuple[int, int] = DEFAULT_SIZE,
) -> None:
    """Reject a source-backed video that is not exactly 24/1 CFR or whose audio and video diverge."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise _missing_ffprobe(f"{SOURCE_AV_RATE_CODE}: source {asset_id!r} rate check requires ffprobe")
    try:
        result = subprocess.run(
            [
                ffprobe, "-v", "error", "-show_entries",
                "stream=codec_type,codec_name,width,height,r_frame_rate,avg_frame_rate,duration:format=duration",
                "-of", "json", str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        payload = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise _SourceAVError(
            f"{SOURCE_AV_RATE_CODE}: cannot probe source {asset_id!r}: {exc}",
            code="H3_SOURCE_AV_PROBE", field="source.asset",
            fix="Check that ffprobe can read the source and retry",
        ) from exc
    except json.JSONDecodeError:
        payload = None
    streams = payload.get("streams") if isinstance(payload, Mapping) else None
    if not isinstance(streams, list):
        raise _SourceAVError(
            f"H3_SOURCE_AV_PROBE: cannot read media streams for source {asset_id!r}",
            code="H3_SOURCE_AV_PROBE", field="source.asset",
            fix="Provide a readable audiovisual source and retry",
        )
    video = next((row for row in streams if isinstance(row, Mapping) and row.get("codec_type") == "video"), None)
    audio = next((row for row in streams if isinstance(row, Mapping) and row.get("codec_type") == "audio"), None)
    if video is None:
        raise _SourceAVError(
            f"{SOURCE_AV_RATE_CODE}: source {asset_id!r} has no video stream; {SOURCE_AV_CONFORM_HINT}",
            code=SOURCE_AV_RATE_CODE, field="source.asset", fix="Check the source range, audio and video and retry",
        )
    container = payload.get("format") if isinstance(payload, Mapping) else None
    container_duration = _seconds(container.get("duration")) if isinstance(container, Mapping) else None
    r_rate = _rate_fraction(video.get("r_frame_rate"))
    avg_rate = _rate_fraction(video.get("avg_frame_rate"))
    video_duration = _seconds(video.get("duration"))
    if video_duration is None:
        video_duration = container_duration
    audio_duration: float | None = None
    if audio is not None:
        audio_duration = _seconds(audio.get("duration"))
        if audio_duration is None:
            audio_duration = container_duration

    problems: list[str] = []
    if r_rate != _SOURCE_AV_RATE or avg_rate != _SOURCE_AV_RATE:
        problems.append("video is not exactly 24/1 CFR")
    if (video.get("width"), video.get("height")) != expected_size:
        problems.append(f"video dimensions are not {expected_size[0]}x{expected_size[1]}")
    if video_duration is None or (audio is not None and audio_duration is None):
        problems.append("durations could not be measured")
    elif (
        audio is not None
        and audio_duration is not None
        and video_duration is not None
        and abs(audio_duration - video_duration) > _SOURCE_AV_TOLERANCE_SECONDS
    ):
        problems.append(
            f"audio and video durations differ by more than {_SOURCE_AV_TOLERANCE_SECONDS:.6f}s"
        )
    if not problems:
        return

    audio_text = f"{audio_duration:.6f}s" if audio_duration is not None else "none"
    video_text = f"{video_duration:.6f}s" if video_duration is not None else "unknown"
    measured = (
        f"video r_frame_rate={video.get('r_frame_rate')} ({_fps_text(r_rate)}), "
        f"avg_frame_rate={video.get('avg_frame_rate')} ({_fps_text(avg_rate)}); "
        f"dimensions {video.get('width')}x{video.get('height')}; "
        f"video duration {video_text}, audio duration {audio_text}"
    )
    hint = SOURCE_AV_CONFORM_HINT
    codec = audio.get("codec_name") if audio is not None else None
    if audio is not None and codec != "aac":
        hint += f" (audio codec is {codec})"
    code = SOURCE_AV_RATE_CODE if r_rate != _SOURCE_AV_RATE or avg_rate != _SOURCE_AV_RATE else (
        "H3_SOURCE_AV_SIZE" if (video.get("width"), video.get("height")) != expected_size
        else SOURCE_AV_RATE_CODE
    )
    raise _SourceAVError(
        f"{SOURCE_AV_RATE_CODE}: source {asset_id!r} rejected before any pod is held: "
        f"{'; '.join(problems)}. Measured {measured}. {hint}",
        code=code, field="source.asset", fix="Check the source range, audio and video and retry",
    )


def _check_source_av_rates(request: H3Request, assets: list[dict[str, Any]]) -> None:
    """Probe each resolved source-backed video input; source-free and audio-only requests are not probed."""
    value = request.value
    if value.get("version") == 2:
        if branch_for(request) not in _SOURCE_AV_BRANCHES:
            return
        targets = [
            str(item["asset"])
            for item in value["media"]
            if item.get("role") == "timeline" and item.get("modality") == "video"
        ]
    else:
        source = value.get("source")
        if value.get("operation") not in {"edit", "continue"} or not isinstance(source, Mapping):
            return
        targets = [str(source["asset"])]
    resolved = {
        str(record["asset"]): Path(str(record["path"]))
        for record in assets
        if record.get("status") == "resolved" and isinstance(record.get("path"), str)
    }
    expected_size = DEFAULT_SIZE
    if value.get("version") == 2:
        canvas = value.get("canvas")
        if isinstance(canvas, Mapping):
            expected_size = (int(canvas["width"]), int(canvas["height"]))
    for asset_id in dict.fromkeys(targets):
        path = resolved.get(asset_id)
        # An unresolved source is blocked_until_resolution, so no pod can be held for it.
        if path is None:
            continue
        if expected_size == DEFAULT_SIZE:
            _check_source_av_rate(asset_id, path)
        else:
            _check_source_av_rate(asset_id, path, expected_size=expected_size)


def _v2_mask_schedule(
    request: H3Request,
    *,
    timeline_audio_presence: Mapping[str, bool] | None = None,
) -> dict[str, Any]:
    """Translate native-v2 timeline media into composer permissions.

    Timeline coverage is an authoritative baseline only for the streams the
    media actually carries. Everything outside that coverage must be
    generated. In particular, references in a source-free request never
    invent protected intervals.
    """

    value = request.value
    duration = float(value["duration"])
    branch = branch_for(request)
    baselines: dict[str, list[list[float]]] = {"video": [], "audio": []}
    changes: dict[str, list[dict[str, Any]]] = {"video": [], "audio": []}

    for item in value["media"]:
        if item["role"] == "timeline":
            if item.get("modality") == "image":
                baselines["video"].append([0, 1 / 24])
                continue
            start = float(item["resolved_at"]["value"]) / 24.0
            raw_range = item.get("range")
            span = (
                float(raw_range[1]) - float(raw_range[0])
                if isinstance(raw_range, list) and len(raw_range) == 2
                else duration - start
            )
            end = min(duration, start + span)
            if end > start:
                modality = item.get("modality")
                if modality == "video":
                    baselines["video"].append([start, end])
                    # A measured silent video cannot own prepared audio.
                    has_audio = (timeline_audio_presence or {}).get(str(item["asset"]), True)
                    if has_audio:
                        baselines["audio"].append([start, end])
                elif modality == "audio":
                    baselines["audio"].append([start, end])

        for edit_index, edit in enumerate(item.get("edit", [])):
            # A zero-strength generation grant keeps the source. Other active
            # grants still contribute their own independent permissions.
            if edit.get("action") == "generate" and edit.get("strength", 1) == 0:
                continue
            stream = str(edit["stream"])
            start, end = map(float, edit["during"])
            if end > duration:
                raise MaskScheduleError(
                    f"media[{item['occurrence_id']}].edit[{edit_index}].during ends after duration"
                )
            record = {
                "occurrence_id": item["occurrence_id"],
                "index": edit_index,
                "during": [start, end],
                "action": edit["action"],
            }
            if stream == "video":
                record["area"] = dict(edit["mask"])
            else:
                record["channels"] = edit.get("channels", "all")
            if "guides" in edit:
                record["guides"] = edit["guides"]
            changes[stream].append(record)

    schedule: dict[str, Any] = {
        "schema_version": 2,
        "request_digest": request.digest,
        "profile": value.get("profile"),
        "branch": branch,
        "operation": "generate" if branch == "source_free" else "transform",
        "duration": duration,
        "requires_resolution": [],
        "status": "ready",
    }
    for stream in ("video", "audio"):
        baseline = merge_intervals(baselines[stream])
        generated_edits = merge_intervals(
            record["during"]
            for record in changes[stream]
            if record["action"] == "generate"
        )
        preserved_edits = merge_intervals(
            record["during"]
            for record in changes[stream]
            if record["action"] == "preserve"
        )
        uncovered_preservation = [
            interval
            for interval in preserved_edits
            if interval_difference(interval, baseline)
        ]
        if uncovered_preservation:
            raise MaskScheduleError(
                f"native-v2 {stream} preservation requires an authoritative {stream} baseline"
            )
        for interval in preserved_edits:
            if interval_difference(interval, generated_edits) != [interval]:
                raise MaskScheduleError(
                    f"native-v2 {stream} generate/preserve intervals overlap"
                )
        generated = merge_intervals(
            [
                *generated_edits,
                *interval_difference([0.0, duration], baseline),
            ]
        )
        protected: list[list[float]] = []
        for interval in baseline:
            protected.extend(interval_difference(interval, generated))
        schedule[stream] = {
            "changes": changes[stream],
            "baseline_intervals": baseline,
            "generated_intervals": generated,
            "protected_intervals": merge_intervals(protected),
        }

    schedule["source_protected"] = not any(
        schedule[stream]["generated_intervals"] for stream in ("video", "audio")
    )
    encoded = json.dumps(schedule, sort_keys=True, separators=(",", ":")).encode("utf-8")
    schedule["digest"] = hashlib.sha256(encoded).hexdigest()
    return schedule


def _validate_frozen_source(request: H3Request, source: Mapping[str, Any], path: Path,
                            envelope: Mapping[str, Any], *, canvas: tuple[int, int]) -> dict[str, Any]:
    """Verify the controller's sealed derivative before using local coordinates."""
    record = envelope.get("conform")
    if (envelope.get("version") != 1 or envelope.get("request_digest") != request.digest
            or envelope.get("asset") != source.get("asset") or not isinstance(record, Mapping)):
        raise PreparationError("prepared source identity does not match the original request")
    origin, output = record.get("source"), record.get("output")
    managed = envelope.get("managed_source")
    if not isinstance(managed, Mapping):
        raise PreparationError("prepared source managed object descriptor is missing")
    if not isinstance(origin, Mapping) or not isinstance(output, Mapping):
        raise PreparationError("prepared source provenance is missing")
    try:
        expected_range = [Fraction(str(value)) for value in source["range"]]
        actual_range = [Fraction(str(value)) for value in origin["range_seconds"]]
        frame_value = (expected_range[1] - expected_range[0]) * 24
        expected_frames = round(frame_value)
        valid = (expected_range[0] >= 0 and expected_frames > 0
                 and len(actual_range) == 2 and expected_range == actual_range
                 and output["fit"] == source.get("fit", "fill")
                 and output["frames"] == expected_frames
                 and output["fps"] == 24 and (output["width"], output["height"]) == canvas
                 and output["range_seconds"] == [0, expected_frames / 24])
        digest = output["sha256"]
        original_digest = origin["sha256"]
    except (KeyError, TypeError, ValueError, ZeroDivisionError, IndexError) as exc:
        raise PreparationError("prepared source provenance is malformed") from exc
    if (not valid or not isinstance(digest, str) or len(digest) != 64
            or not isinstance(original_digest, str) or len(original_digest) != 64
            or any(char not in "0123456789abcdef" for char in original_digest)):
        raise PreparationError("prepared source provenance does not match the requested range")
    if source["asset"].startswith("sha256:") and source["asset"] != "sha256:" + original_digest:
        raise PreparationError("prepared source original digest does not match its media handle")
    if (managed.get("digest") != "sha256:" + digest or managed.get("sha256") != digest
            or not path.is_file() or _file_digest(path) != digest):
        raise PreparationError("prepared source digest mismatch")
    # A sealed digest binds bytes, but the envelope can still claim the wrong
    # geometry for those bytes. Probe the managed file before accepting it.
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise _missing_ffprobe("prepared source geometry check requires ffprobe")
    try:
        probe = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height", "-of", "csv=p=0:s=x", str(path)],
            check=True, capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PreparationError("cannot probe prepared source geometry") from exc
    if probe.stdout.strip() != f"{canvas[0]}x{canvas[1]}":
        raise PreparationError(
            f"prepared source video dimensions {probe.stdout.strip() or 'unknown'} "
            f"do not match requested {canvas[0]}x{canvas[1]}"
        )
    if not isinstance(record.get("decoded_av"), Mapping):
        raise PreparationError("prepared source decoded AV measurement is missing")
    return dict(record)


def prepare_request(
    request: H3Request,
    *,
    asset_map: Mapping[str, str] | None = None,
    fps: float = 24.0,
    width: int | None = None,
    height: int | None = None,
    sample_rate: int = 48000,
    out_dir: str | Path | None = None,
    frozen_source: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a deterministic preparation manifest without touching a runtime."""

    if (width is None) != (height is None):
        raise PreparationError("width and height compatibility inputs must be supplied together")
    resolved_canvas = request.value.get("canvas")
    has_resolved_canvas = isinstance(resolved_canvas, Mapping)
    if has_resolved_canvas:
        effective_width = resolved_canvas.get("width")
        effective_height = resolved_canvas.get("height")
    else:
        effective_width, effective_height = DEFAULT_SIZE
    canvas = LIMITS["canvas_dimension"]["value"]
    # Compatibility for existing internal callers. Normalized request records
    # are authoritative: a compatibility argument may confirm, but cannot
    # replace, their canvas. Unresolved legacy records have no canvas to conflict.
    if width is not None and height is not None:
        if any(type(value) is not int or value < canvas["min"] or value > canvas["max"]
               or value % canvas["multiple"] for value in (width, height)):
            raise PreparationError(f"width and height 32–16384, multiples of 32: received {width}x{height}")
        if has_resolved_canvas and (width, height) != (effective_width, effective_height):
            raise PreparationError("width and height compatibility inputs cannot override the normalized request canvas")
        if not has_resolved_canvas:
            # Persisted normalized v2 requests predate the canvas field; their
            # identity and compiler both imply the historical default.
            if request.value.get("version") == 2 and request.value.get("profile") and (width, height) != DEFAULT_SIZE:
                raise PreparationError("width and height compatibility inputs cannot override the normalized request canvas")
            effective_width, effective_height = width, height
    width, height = effective_width, effective_height
    if any(type(value) is not int or value < canvas["min"] or value > canvas["max"] or value % canvas["multiple"] for value in (width, height)):
        raise PreparationError(f"width and height 32–16384, multiples of 32: received {width}x{height}")
    if fps <= 0 or sample_rate <= 0:
        raise PreparationError("fps and sample_rate must be positive")
    duration = request.value.get("duration") if request.value.get("version") == 2 else request.value.get("output", {}).get("duration")
    duration_limits = LIMITS["job_duration_seconds"]
    warnings = []
    if duration is not None and not duration_limits["value"][0] <= float(duration) <= duration_limits["value"][1]:
        warnings.append({"limit": "job_duration_seconds", "message": duration_limits["warning"]})
    original_request = request
    source_conform = None
    is_v2 = request.value.get("version") == 2
    if out_dir is not None:
        if is_v2:
            candidates = [(i, item) for i, item in enumerate(request.value["media"])
                          if item.get("role") == "timeline" and item.get("modality") == "video"]
            source_index, source = candidates[0] if len(candidates) == 1 else (None, None)
        else:
            source_index, source = None, request.value.get("source")
        if isinstance(source, Mapping) and source.get("range") and asset_map and source["asset"] in asset_map:
            from .conform import conform_source
            from .engine_facts import facts
            original_path = Path(asset_map[source["asset"]])
            start, end = source["range"]
            if frozen_source is None:
                source_conform = conform_source(
                    original_path, Path(out_dir) / "conformed-source", start=str(start), end=str(end),
                    fit=source.get("fit", "fill"),
                    validate=facts().measure_source_av, canvas=(width, height),
                )
                asset_map = dict(asset_map)
                asset_map[source["asset"]] = str(Path(out_dir) / "conformed-source" / "source.mp4")
            else:
                source_conform = _validate_frozen_source(request, source, original_path, frozen_source, canvas=(width, height))
            adjusted = copy.deepcopy(request.value)
            if is_v2:
                from .request_v2 import _identity_value, json_times
                adjusted_source = adjusted["media"][source_index]
                span = Fraction(str(end)) - Fraction(str(start))
                adjusted_source["range"] = [Fraction(0), span]
                adjusted_source["resolved_range"] = [0, int(span * 24)]
                encoded = json.dumps(json_times(_identity_value(adjusted)), ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode("utf-8")
                request = H3Request(value=adjusted, digest=hashlib.sha256(encoded).hexdigest())
            else:
                adjusted["source"]["range"] = [0, source_conform["output"]["range_seconds"][1]]
                from .request import normalize_request
                request = normalize_request(adjusted)
    if frozen_source is not None and source_conform is None:
        raise PreparationError("prepared source handoff has no matching ranged source")
    if request.value.get("version") == 2:
        assets = _resolve_assets(request, asset_map)
        still_error = timeline_image_limit_error(request.value)
        if still_error:
            raise PreparationError(still_error)
        conditioning_measurements = _admit_conditioning(request, assets, warnings)
        if branch_for(request) == "source_free" and not request.value["media"]:
            warnings.append({"limit": "text_only_documented_use", "message": "Text-only generation is outside the documented use of the Ref2VA checkpoint."})
        if branch_for(request) == "source_backed_v2v":
            for row in conditioning_measurements:
                row.update({"slot": None, "tag": None, "binding_status": "provisional_until_lowering"})
                row.pop("audio_tag", None)
        from .generation import generation_timing
        if branch_for(request) in {"source_free", "source_backed_v2v"}:
            try:
                target_timing = generation_timing(float(request.value["duration"]))
            except ValueError as exc:
                raise PreparationError(str(exc)) from exc
        else:
            target_timing = {"requested_frames": round(float(request.value["duration"]) * 24), "raw_frames": None}
            if branch_for(request) == "extension_context":
                from .timing import ContinuationTimingError, plan_continuation
                timeline = next((item for item in request.value["media"] if item.get("role") == "timeline" and item.get("modality") == "video"), None)
                selected = timeline.get("resolved_range") if timeline else None
                if isinstance(selected, list) and len(selected) == 2:
                    try:
                        continuation = plan_continuation(source_end=Fraction(int(selected[1]) - int(selected[0]), 24), output_duration=request.value["duration"])
                    except ContinuationTimingError:
                        pass  # The continuation lowering reports its own unsupported timing.
                    else:
                        target_timing["raw_frames"] = continuation.raw_extension_frames
        unresolved = [record["asset"] for record in assets if record["status"] != "resolved"]
        timeline_audio_presence: dict[str, bool] = {}
        adapter = request.value.get("settings", {}).get("adapter")
        if source_conform is not None:
            timeline_audio_presence[str(source["asset"])] = source_conform.get("audio") is not None
        elif isinstance(adapter, Mapping) and adapter.get("id") == "character_swap_v1":
            resolved_paths = {
                str(record["asset"]): Path(str(record["path"]))
                for record in assets
                if record.get("status") == "resolved" and isinstance(record.get("path"), str)
            }
            for item in request.value["media"]:
                if item.get("role") != "timeline" or item.get("modality") != "video":
                    continue
                path = resolved_paths.get(str(item["asset"]))
                if path is not None:
                    streams = _video_stream_types(path)
                    if "video" not in streams:
                        raise PreparationError("Character Swap timeline source contains no video stream")
                    timeline_audio_presence[str(item["asset"])] = "audio" in streams
        _validate_mask_assets(request, assets)
        # Runs after the Character Swap stream inspection so its more specific errors win.
        if source_conform is None:
            _check_source_av_rates(request, assets)
        try:
            schedule = _v2_mask_schedule(request, timeline_audio_presence=timeline_audio_presence)
        except MaskScheduleError as exc:
            raise PreparationError(str(exc)) from exc
        # Exact composition copies protected samples without resampling. Check
        # those prerequisites before runtime, but leave unowned model context
        # free to use the compositor's optional resampling path.
        audio_schedule = schedule.get("audio")
        if branch_for(request) == "source_backed_v2v" and not (
            isinstance(audio_schedule, Mapping)
            and isinstance(audio_schedule.get("protected_intervals"), list)
            and isinstance(audio_schedule.get("generated_intervals"), list)
        ):
            # Source-free, unresolved and non-media inputs do not need PCM
            # ownership. A readable source must never bypass admission because
            # its schedule is missing or malformed.
            from .compose import _probe
            paths = {str(row["asset"]): Path(str(row["path"])) for row in assets
                     if row.get("status") == "resolved" and row.get("path")}
            for item in request.value["media"]:
                if item.get("role") == "timeline" and item.get("modality") == "video":
                    path = paths.get(str(item["asset"]))
                    if path is not None and _probe(path) is not None:
                        raise PreparationError("source-backed audio ownership schedule is missing or malformed")
        active_audio_edits = any(
            edit.get("stream") == "audio" and edit.get("action") == "generate"
            and edit.get("strength", 1) > 0
            for item in request.value["media"] if item.get("role") == "timeline"
            for edit in item.get("edit", [])
        )
        if (branch_for(request) != "extension_context" and isinstance(audio_schedule, Mapping)
                and audio_schedule.get("protected_intervals")
                and (audio_schedule.get("generated_intervals") or active_audio_edits)):
            from .compose import CompositionError, _decode_exact_pcm, _probe
            paths = {str(row["asset"]): Path(str(row["path"])) for row in assets
                     if row.get("status") == "resolved" and row.get("path")}
            for item in request.value["media"]:
                if item.get("role") != "timeline" or item.get("modality") != "video":
                    continue
                path = paths.get(str(item["asset"]))
                if path is None or _probe(path) is None:
                    continue  # unresolved or non-media fixture
                try:
                    pcm, channels = _decode_exact_pcm(path)
                    offset = int(item.get("resolved_range", [0, 0])[0]) / 24
                    end = max(float(span[1]) for span in audio_schedule["protected_intervals"])
                    if len(pcm) < round((offset + end) * 48000) * channels * 2:
                        raise CompositionError(f"{path.name}: source audio does not cover kept samples")
                except CompositionError as exc:
                    raise PreparationError(f"source-owned audio rejected before submission: {exc}") from exc
        guide_occurrences = {
            guide for item in request.value["media"] if item.get("role") == "timeline"
            for edit in item.get("edit", []) for guide in edit.get("guides", [])
        }
        return {
            **({"managed_source": dict(frozen_source["managed_source"])} if frozen_source is not None else {}),
            "source_conform": source_conform,
            "original_request_digest": original_request.digest,
            "original_request": original_request.value,
            "schema_version": 2,
            "kind": "h3_av_preparation",
            "request_digest": request.digest,
            "request": request.value,
            "media": {"fps": float(fps), "width": int(width), "height": int(height), "sample_rate": int(sample_rate)},
            "assets": assets,
            "conditioning_measurements": conditioning_measurements,
            "warnings": warnings,
            "resource_estimate": {
                "schema_valid": True,
                "qualified": False,  # The target is not yet resource-qualified.
                "conditioning_qualified": all(row["resource_estimate"]["qualified"] for row in conditioning_measurements),
                "target_qualified": None,  # Target resource feasibility has not been established.
                "observed": False,
                "target_rows": target_timing["raw_frames"],
                "requested_target_rows": target_timing["requested_frames"],
                "target_raw_rgb_bytes": (target_timing["raw_frames"] * int(width) * int(height) * 3 if target_timing["raw_frames"] is not None else None),
                "target_raw_float32_rgb_bytes": (target_timing["raw_frames"] * int(width) * int(height) * 3 * 4 if target_timing["raw_frames"] is not None else None),
                "reference_rows": len(conditioning_measurements),
                "reference_raw_rgb_bytes": (sum(row["resource_estimate"]["raw_bytes"] for row in conditioning_measurements if row["modality"] in {"image", "video"}) if all(row["resource_estimate"]["raw_bytes"] is not None for row in conditioning_measurements if row["modality"] in {"image", "video"}) else None),
                "reference_raw_rgb_bytes_upper_bound": (sum((row["resource_estimate"].get("raw_bytes_upper_bound") if row["modality"] == "video" else row["resource_estimate"]["raw_bytes"]) for row in conditioning_measurements if row["modality"] in {"image", "video"}) if all((row["resource_estimate"].get("raw_bytes_upper_bound") if row["modality"] == "video" else row["resource_estimate"]["raw_bytes"]) is not None for row in conditioning_measurements if row["modality"] in {"image", "video"}) else None),
                "reference_raw_audio_bytes": (None if any(row.get("audio_enabled") for row in conditioning_measurements) or any(row["resource_estimate"]["raw_bytes"] is None for row in conditioning_measurements if row["modality"] == "audio") else sum(row["resource_estimate"]["raw_bytes"] for row in conditioning_measurements if row["modality"] == "audio")),
                "guide_rows": len(guide_occurrences),
                "unit": "bytes",
            },
            "unresolved_assets": unresolved,
            "mask_schedule": schedule,
            "status": "prepared" if not unresolved else "requires_resolution",
            "runtime_submission": "eligible" if not unresolved else "blocked_until_resolution",
        }
    try:
        schedule = build_mask_schedule(request)
    except MaskScheduleError as exc:
        raise PreparationError(str(exc)) from exc
    assets = _resolve_assets(request, asset_map)
    if source_conform is None:
        _check_source_av_rates(request, assets)
    unresolved = [record["asset"] for record in assets if record["status"] != "resolved"]
    # Managed identifiers are intentionally retained for the runtime, but a
    # local asset map must never silently contain a missing path.
    return {
        **({"managed_source": dict(frozen_source["managed_source"])} if frozen_source is not None else {}),
        "source_conform": source_conform,
        "original_request_digest": original_request.digest,
        "original_request": original_request.value,
        "schema_version": 1,
        "warnings": warnings,
        "kind": "h3_av_preparation",
        "request_digest": request.digest,
        "request": request.value,
        "media": {
            "fps": float(fps),
            "width": int(width),
            "height": int(height),
            "sample_rate": int(sample_rate),
        },
        "assets": assets,
        "unresolved_assets": unresolved,
        "mask_schedule": schedule,
        "status": "prepared" if schedule["status"] == "ready" else "requires_resolution",
        "runtime_submission": "blocked_until_resolution" if schedule["status"] != "ready" else "eligible",
    }


def write_preparation(path: str | Path, manifest: Mapping[str, Any]) -> Path:
    from .request_v2 import json_times

    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(json_times(manifest), indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return destination


__all__ = ["PreparationError", "prepare_request", "write_preparation"]
