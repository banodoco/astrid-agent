"""Deterministic video/audio permission schedules for H3 requests.

The H3 graph may need a larger latent context than the user asked to change.
This module deliberately keeps the user-facing delivery permissions separate
from that implementation detail.  Nothing here guesses a track, region, or
duration: ambiguous requests are rejected or marked for explicit resolution.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Iterable, Mapping

from .request import H3Request


class MaskScheduleError(ValueError):
    """A request cannot be represented as a safe permission schedule."""


def _interval(value: Iterable[float], *, path: str) -> tuple[float, float]:
    values = tuple(float(item) for item in value)
    if len(values) != 2 or values[0] < 0 or values[1] <= values[0]:
        raise MaskScheduleError(f"{path} must be a positive half-open interval")
    return values


def merge_intervals(intervals: Iterable[Iterable[float]]) -> list[list[float]]:
    """Merge touching/overlapping intervals without changing their meaning."""

    ordered = sorted((_interval(item, path="interval") for item in intervals), key=lambda item: item[0])
    merged: list[list[float]] = []
    for start, end in ordered:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def interval_difference(interval: Iterable[float], covered: Iterable[Iterable[float]]) -> list[list[float]]:
    """Return portions of *interval* not covered by *covered* intervals."""

    start, end = _interval(interval, path="interval")
    cursor = start
    result: list[list[float]] = []
    for other_start, other_end in merge_intervals(covered):
        if other_end <= cursor:
            continue
        if other_start >= end:
            break
        if other_start > cursor:
            result.append([cursor, min(other_start, end)])
        cursor = max(cursor, other_end)
        if cursor >= end:
            break
    if cursor < end:
        result.append([cursor, end])
    return result


def _area_key(area: Mapping[str, Any]) -> str:
    if area.get("full_frame") is True:
        return "full_frame"
    for key in ("mask_asset", "subjects", "rectangle", "polygon"):
        if key in area:
            return key + ":" + json.dumps(area[key], sort_keys=True, separators=(",", ":"))
    raise MaskScheduleError("area has no supported selector")


def _area_record(area: Mapping[str, Any], *, path: str) -> dict[str, Any]:
    key = _area_key(area)
    record: dict[str, Any] = {"key": key, "selector": dict(area)}
    if key == "full_frame":
        record["resolution"] = "exact"
    elif key.startswith("mask_asset:"):
        record["resolution"] = "supplied_mask"
    elif key.startswith("subjects:"):
        # Subject tracking is a separate, runtime-specific operation.  Never
        # claim pixel accuracy before it has produced a mask artifact.
        record["resolution"] = "requires_resolution"
        record["requires_resolution"] = True
    else:
        record["resolution"] = "geometry"
    record["path"] = path
    return record


def _bounded_duration(request: H3Request) -> float:
    value = request.value
    if "duration" in value.get("output", {}):
        return float(value["output"]["duration"])
    candidates: list[float] = []
    source = value.get("source")
    if isinstance(source, Mapping) and source.get("range"):
        candidates.append(float(source["range"][1]))
    for domain in ("video", "audio"):
        candidates.extend(float(change["during"][1]) for change in value["changes"][domain])
    if candidates:
        return max(candidates)
    raise MaskScheduleError(
        "output.duration is required when the request has no source range or bounded change"
    )


def build_mask_schedule(request: H3Request) -> dict[str, Any]:
    """Compile normalized request changes into explicit delivery permissions."""

    value = request.value
    duration = _bounded_duration(request)
    video_changes: list[dict[str, Any]] = []
    audio_changes: list[dict[str, Any]] = []
    requires_resolution: list[str] = []

    for index, change in enumerate(value["changes"]["video"]):
        interval = _interval(change["during"], path=f"changes.video[{index}].during")
        if interval[1] > duration:
            raise MaskScheduleError(f"changes.video[{index}].during ends after output.duration")
        area = _area_record(change["area"], path=f"changes.video[{index}].area")
        if area.get("requires_resolution"):
            requires_resolution.append(area["path"])
        video_changes.append(
            {
                "index": index,
                "during": [interval[0], interval[1]],
                "area": area,
                "action": change["action"],
                **({"mask_asset": change["mask_asset"]} if "mask_asset" in change else {}),
            }
        )

    for index, change in enumerate(value["changes"]["audio"]):
        interval = _interval(change["during"], path=f"changes.audio[{index}].during")
        if interval[1] > duration:
            raise MaskScheduleError(f"changes.audio[{index}].during ends after output.duration")
        audio_changes.append(
            {
                "index": index,
                "during": [interval[0], interval[1]],
                "action": change["action"],
                **{key: change[key] for key in ("dialogue", "mask_asset", "stem") if key in change},
            }
        )

    # For source-backed edits and continuations, an empty schedule means
    # preserve the whole source.
    source_protected = value["operation"] in {"edit", "continue"} and not video_changes and not audio_changes
    generated_video = merge_intervals(
        change["during"] for change in video_changes if change["action"] == "generate"
    )
    generated_audio = merge_intervals(
        change["during"] for change in audio_changes if change["action"] == "generate"
    )
    protected_video = merge_intervals(
        change["during"] for change in video_changes if change["action"] == "preserve"
    )
    protected_audio = merge_intervals(
        change["during"] for change in audio_changes if change["action"] == "preserve"
    )
    if value["operation"] == "generate":
        generated_video = [[0.0, duration]]
        generated_audio = [[0.0, duration]]
    # A source-backed request protects every unmentioned part of each domain.
    # This is the key fail-closed rule: a caller must opt a region/time range
    # into generation; it is never implicitly allowed to drift.
    if value["operation"] in {"edit", "continue"}:
        protected_video = merge_intervals(
            [*protected_video, *interval_difference([0.0, duration], generated_video)]
        )
        protected_audio = merge_intervals(
            [*protected_audio, *interval_difference([0.0, duration], generated_audio)]
        )
    if source_protected:
        protected_video = [[0.0, duration]]
        protected_audio = [[0.0, duration]]

    schedule = {
        "schema_version": 1,
        "request_digest": request.digest,
        "operation": value["operation"],
        "duration": duration,
        "video": {
            "changes": video_changes,
            "generated_intervals": generated_video,
            "protected_intervals": protected_video,
        },
        "audio": {
            "changes": audio_changes,
            "generated_intervals": generated_audio,
            "protected_intervals": protected_audio,
        },
        "source_protected": source_protected,
        "requires_resolution": sorted(requires_resolution),
        "status": "requires_resolution" if requires_resolution else "ready",
    }
    encoded = json.dumps(schedule, sort_keys=True, separators=(",", ":")).encode("utf-8")
    schedule["digest"] = hashlib.sha256(encoded).hexdigest()
    return schedule


__all__ = [
    "MaskScheduleError",
    "build_mask_schedule",
    "interval_difference",
    "merge_intervals",
]
