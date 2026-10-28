"""Attempt-local preparation for an H3 audiovisual request."""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping

from astrid.packs.h3_av.actions.prepare.masks import (
    MaskScheduleError,
    build_mask_schedule,
    interval_difference,
    merge_intervals,
)
from astrid.packs.h3_av.shared.errors import PreparationError
from astrid.packs.h3_av.shared.request import H3Request, _asset_ids
from astrid.packs.h3_av.shared.request_v2 import branch_for



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
        raise PreparationError("Character Swap source inspection requires ffprobe")
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

    from astrid.packs.h3_av.shared.compose import CompositionError, iter_mask_frames, probe_mask

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
            threshold = mask.get("threshold")
            start = round(float(edit["during"][0]) * 24)
            end = round(float(edit["during"][1]) * 24)
            try:
                info = probe_mask(path, mask_id=mask_id, source=source, duration=duration)
                if info["kind"] != "video":
                    continue
                width, height = _mask_dimensions(path, mask_id)
                for index, frame in enumerate(
                    iter_mask_frames(path, start_frame=0, count=total, width=width, height=height)
                ):
                    values = set(frame)
                    if threshold is not None:
                        values = {255 if value >= threshold else 0 for value in values}
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
SOURCE_AV_CONFORM_HINT = "conform with: ffmpeg -i IN -vf fps=24 -c:v libx264 -crf 12 -c:a aac OUT"
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


def _check_source_av_rate(asset_id: str, path: Path) -> None:
    """Reject a source-backed video that is not exactly 24/1 CFR or whose audio and video diverge."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        raise PreparationError(f"{SOURCE_AV_RATE_CODE}: source {asset_id!r} rate check requires ffprobe")
    try:
        result = subprocess.run(
            [
                ffprobe, "-v", "error", "-show_entries",
                "stream=codec_type,codec_name,r_frame_rate,avg_frame_rate,duration:format=duration",
                "-of", "json", str(path),
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=30,
        )
        payload = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PreparationError(f"{SOURCE_AV_RATE_CODE}: cannot probe source {asset_id!r}: {exc}") from exc
    except json.JSONDecodeError:
        payload = None
    streams = payload.get("streams") if isinstance(payload, Mapping) else None
    if not isinstance(streams, list):
        # Not a readable media container, such as a non-media fixture. There is no
        # measurable frame rate to certify; composition still requires real media.
        return
    video = next((row for row in streams if isinstance(row, Mapping) and row.get("codec_type") == "video"), None)
    audio = next((row for row in streams if isinstance(row, Mapping) and row.get("codec_type") == "audio"), None)
    if video is None:
        raise PreparationError(
            f"{SOURCE_AV_RATE_CODE}: source {asset_id!r} has no video stream; {SOURCE_AV_CONFORM_HINT}"
        )
    container = payload.get("format")
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
    if video_duration is None or (audio is not None and audio_duration is None):
        problems.append("durations could not be measured")
    elif audio is not None and abs(audio_duration - video_duration) > _SOURCE_AV_TOLERANCE_SECONDS:
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
        f"video duration {video_text}, audio duration {audio_text}"
    )
    hint = SOURCE_AV_CONFORM_HINT
    codec = audio.get("codec_name") if audio is not None else None
    if audio is not None and codec != "aac":
        hint += f" (audio codec is {codec}: the audio must be re-encoded to aac, as the command does)"
    raise PreparationError(
        f"{SOURCE_AV_RATE_CODE}: source {asset_id!r} rejected before any pod is held: "
        f"{'; '.join(problems)}. Measured {measured}. {hint}"
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
    for asset_id in dict.fromkeys(targets):
        path = resolved.get(asset_id)
        # An unresolved source is blocked_until_resolution, so no pod can be held for it.
        if path is not None:
            _check_source_av_rate(asset_id, path)


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
                    # The selected native workflow preserves source audio from
                    # its video container; Character Swap inspects whether that
                    # container actually carries an audio stream.
                    has_audio = (timeline_audio_presence or {}).get(str(item["asset"]), True)
                    if has_audio:
                        baselines["audio"].append([start, end])
                elif modality == "audio":
                    baselines["audio"].append([start, end])

        for edit_index, edit in enumerate(item.get("edit", [])):
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
            for field in ("text", "guides"):
                if field in edit:
                    record[field] = edit[field]
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


def prepare_request(
    request: H3Request,
    *,
    asset_map: Mapping[str, str] | None = None,
    fps: float = 24.0,
    width: int = 1024,
    height: int = 576,
    sample_rate: int = 48000,
) -> dict[str, Any]:
    """Create a deterministic preparation manifest without touching a runtime."""

    if fps <= 0 or width <= 0 or height <= 0 or sample_rate <= 0:
        raise PreparationError("fps, width, height, and sample_rate must be positive")
    if request.value.get("version") == 2:
        assets = _resolve_assets(request, asset_map)
        unresolved = [record["asset"] for record in assets if record["status"] != "resolved"]
        timeline_audio_presence: dict[str, bool] = {}
        adapter = request.value.get("settings", {}).get("adapter")
        if isinstance(adapter, Mapping) and adapter.get("id") == "character_swap_v1":
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
        _check_source_av_rates(request, assets)
        try:
            schedule = _v2_mask_schedule(request, timeline_audio_presence=timeline_audio_presence)
        except MaskScheduleError as exc:
            raise PreparationError(str(exc)) from exc
        return {
            "schema_version": 2,
            "kind": "h3_av_preparation",
            "request_digest": request.digest,
            "request": request.value,
            "media": {"fps": float(fps), "width": int(width), "height": int(height), "sample_rate": int(sample_rate)},
            "assets": assets,
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
    _check_source_av_rates(request, assets)
    unresolved = [record["asset"] for record in assets if record["status"] != "resolved"]
    # Managed identifiers are intentionally retained for the runtime, but a
    # local asset map must never silently contain a missing path.
    return {
        "schema_version": 1,
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
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(manifest, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8")
    return destination


__all__ = ["PreparationError", "prepare_request", "write_preparation"]
