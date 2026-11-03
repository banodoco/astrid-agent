"""Production structural and timing validation for timeline visualization."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from astrid.core.timeline.duration import validate_clip_timing


def _clip_label(clip: Mapping[str, Any], index: int) -> str:
    clip_id = clip.get("id")
    return f"clip {clip_id!r}" if isinstance(clip_id, str) else f"clips[{index}]"


def validate_structural(timeline: dict[str, Any]) -> list[str]:
    """Return structural and compositor-timing errors without mutating input.

    The canonical timeline schema remains responsible for field shape.  This
    production boundary adds cross-record integrity (unique IDs and resolvable
    track references) and delegates clip arithmetic rules to
    :func:`astrid.core.timeline.duration.validate_clip_timing`.
    """

    if not isinstance(timeline, dict):
        return ["timeline must be an object"]

    errors: list[str] = []
    tracks = timeline.get("tracks", [])
    clips = timeline.get("clips", [])

    if not isinstance(tracks, list):
        errors.append("timeline.tracks must be a list")
        tracks = []
    if not isinstance(clips, list):
        errors.append("timeline.clips must be a list")
        clips = []

    track_ids: set[str] = set()
    for index, track in enumerate(tracks):
        if not isinstance(track, Mapping):
            errors.append(f"tracks[{index}] must be an object")
            continue
        track_id = track.get("id")
        if not isinstance(track_id, str):
            continue
        if track_id in track_ids:
            errors.append(f"duplicate track id {track_id!r}")
        track_ids.add(track_id)

    clip_ids: set[str] = set()
    for index, clip in enumerate(clips):
        if not isinstance(clip, Mapping):
            errors.append(f"clips[{index}] must be an object")
            continue

        label = _clip_label(clip, index)
        clip_id = clip.get("id")
        if isinstance(clip_id, str):
            if clip_id in clip_ids:
                errors.append(f"duplicate clip id {clip_id!r}")
            clip_ids.add(clip_id)

        track_id = clip.get("track")
        if isinstance(track_id, str) and track_id not in track_ids:
            errors.append(f"{label} references nonexistent track {track_id!r}")

        try:
            validate_clip_timing(clip)
        except ValueError as exc:
            errors.append(f"{label}: {exc}")

    return errors


__all__ = ["validate_structural"]
