"""A continuation take: which part of its stored file is the new performance.

The H3 continuation file always starts with the source frames it continued
from (the lead-in), then the generated performance. The file is stored whole so
the lead-in can be verified against the source. ``output.lead_in`` says what the
take *uses*:

- ``"drop"`` (default): the take's range is the generated span only, taken from
  the composition manifest's ``changed_permissions.video`` (seconds into the file).
- ``"keep"``: the take's range is the whole file.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from .request import DEFAULT_LEAD_IN, LEAD_IN_MODES, H3RequestError


class TakeError(ValueError):
    """The take's evidence cannot give a single, honest range."""


def is_continuation(request_value: Mapping[str, Any]) -> bool:
    """A v1 ``operation: continue`` or a v2 request with ``continuation: true``."""
    if request_value.get("version") == 2:
        return request_value.get("continuation") is True
    return request_value.get("operation") == "continue"


def lead_in_of(request_value: Mapping[str, Any]) -> str:
    """The take's lead-in mode from a normalized request (default ``drop``)."""
    mode = (request_value.get("output") or {}).get("lead_in", DEFAULT_LEAD_IN)
    if mode not in LEAD_IN_MODES:
        raise H3RequestError(f"output.lead_in must be one of {list(LEAD_IN_MODES)}")
    return mode


def video_duration(probe: Any) -> float | None:
    """Seconds of the video stream (frames / fps) when known, else the container duration.

    Audio can run past the last video frame, which would put ``keep``'s end off
    the frame grid; the video stream's own length is the honest end.
    """
    frames, rate = getattr(probe, "frames", None), getattr(probe, "fps_rational", None)
    if frames and rate and rate[0] and rate[1]:
        return frames * rate[1] / rate[0]
    return getattr(probe, "duration_seconds", None)


def take_range(composition: Mapping[str, Any], *, lead_in: str, media_duration: float | None) -> list[float]:
    """``[start, end]`` seconds into the take's media.

    ``drop`` uses the one generated video span from the composition manifest;
    ``keep`` uses ``[0, duration]`` of the media actually stored.
    """
    if composition.get("kind") != "h3_av_composition":
        raise TakeError("composition evidence is not an H3 composition manifest")
    if lead_in == "keep":
        if media_duration is None or media_duration <= 0:
            raise TakeError("keep needs the stored media's duration")
        return [0.0, round(float(media_duration), 6)]
    if lead_in != "drop":
        raise TakeError(f"unknown lead_in {lead_in!r}")
    spans = (composition.get("changed_permissions") or {}).get("video") or []
    if len(spans) != 1:
        raise TakeError(f"drop needs exactly one generated video span, found {len(spans)}")
    start, end = float(spans[0][0]), float(spans[0][1])
    if not 0 <= start < end:
        raise TakeError(f"generated span {start}..{end} is not a forward range")
    return [start, end]


def write_take_report(out_dir: Path, report: Mapping[str, Any]) -> Path:
    path = Path(out_dir) / "take.json"
    path.write_text(json.dumps(dict(report), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


__all__ = ["TakeError", "is_continuation", "lead_in_of", "take_range",
           "video_duration", "write_take_report"]
