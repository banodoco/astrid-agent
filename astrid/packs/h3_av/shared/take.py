"""A continuation take: which part of its stored file is the new performance.

The H3 continuation file always starts with the source frames it continued
from (the lead-in), then the generated performance. The file is stored whole so
the lead-in can be verified against the source. ``output.lead_in`` says what the
take *uses*:

- ``"drop"`` (default): the take's range is the generated span only, taken from
  the composition manifest's ``changed_permissions.video`` (seconds into the file).
- ``"keep"``: the take's range is the whole file.

Nothing here re-encodes the stored file. ``clip_take`` writes a separate
frame-exact copy of the range when ``output.clip_lead_in`` is set; the clip uses
the same code as ``astrid media trim``.
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


def clip_requested(request_value: Mapping[str, Any]) -> bool:
    """True when the request asks for a clipped copy of the take's range."""
    return bool((request_value.get("output") or {}).get("clip_lead_in", False))


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


def clip_take(facts: Any, src: Path, dst: Path, *, start: float, end: float, probe: Any) -> dict[str, Any]:
    """Write the frame-exact clip of ``[start, end)`` via the engine facts (same code as ``media trim``)."""
    return dict(facts.clip_media(src, dst, start=start, end=end, probe=probe))


def write_take_report(out_dir: Path, report: Mapping[str, Any]) -> Path:
    path = Path(out_dir) / "take.json"
    path.write_text(json.dumps(dict(report), indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


__all__ = ["TakeError", "clip_requested", "clip_take", "is_continuation", "lead_in_of", "take_range",
           "write_take_report"]
