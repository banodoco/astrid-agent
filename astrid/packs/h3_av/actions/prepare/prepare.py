"""Attempt-local preparation for an H3 audiovisual request."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
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
