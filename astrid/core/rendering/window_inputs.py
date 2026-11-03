"""Invocation-private timeline selection for native and legacy frame windows."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path
from typing import Any

from astrid.core.foundation.atomic_io import write_json_atomic
from astrid.core.foundation.hash import sha256_file

from .contracts import RenderRequest, RenderSegment
from .errors import raise_protocol_error
from .registry import RenderingCandidate


def materialize_window_input(
    request: RenderRequest,
    *,
    candidate: RenderingCandidate[Any],
    segment: RenderSegment,
    index: int,
    workspace: Path,
    slice_timeline: Callable[..., dict[str, Any]],
) -> tuple[RenderRequest, dict[str, str]]:
    """Retain native clip clocks, or slice for a full-timeline-only renderer."""
    extent_key = "astrid_composition_duration_frames"
    if extent_key in request.metadata:
        request = replace(request, metadata={
            key: value for key, value in request.metadata.items() if key != extent_key
        })
    supports_windows = candidate.manifest.capabilities.get("supports_windows") is not False
    if segment.layer is None and supports_windows:
        return request, {}
    timeline_path = Path(request.timeline_path)
    try:
        timeline_data = json.loads(timeline_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise_protocol_error(
            backend=candidate.id,
            message=f"cannot materialize planned timeline window: {exc}",
            recovery_command="repair the timeline JSON and retry the planned render",
            details={"timeline_path": str(timeline_path)},
        )
    if not isinstance(timeline_data, Mapping):
        raise_protocol_error(
            backend=candidate.id,
            message="cannot materialize a window from a non-object timeline",
            recovery_command="write the timeline as a JSON object and retry",
            details={"timeline_path": str(timeline_path)},
        )
    if supports_windows and segment.layer is not None:
        from astrid.core.timeline.banodoco_composer import Timeline
        from astrid.core.timeline.duration import timeline_render_duration_frames

        # Measure the original render view before isolating tracks. Source
        # references do not contribute; an empty layer still spans this clock.
        render_view = Timeline.from_json_data(timeline_data).for_render().to_json_data()
        total = timeline_render_duration_frames(
            render_view, segment.window.fps_rational[0] / segment.window.fps_rational[1]
        )
        if segment.window.end_frame > total:
            raise ValueError("planned window extends beyond the full composition")
        request = replace(request, metadata={**request.metadata, extent_key: str(total)})
        # Isolate the layer without restarting fades, media or animation.
        # Keep all its original clips, including overlap/transition peers.
        allowed_tracks = set(segment.layer.tracks)
        materialized = dict(timeline_data)
        materialized["tracks"] = [
            track for track in timeline_data.get("tracks", [])
            if track.get("id") in allowed_tracks
        ]
        materialized["clips"] = [
            clip for clip in timeline_data.get("clips", [])
            if clip.get("track") in allowed_tracks
        ]
        materialized["metadata"] = dict(timeline_data.get("metadata") or {})
    else:
        materialized = slice_timeline(
            timeline_data,
            segment.window,
            tracks=segment.layer.tracks if segment.layer is not None else None,
        )
    if segment.layer is not None:
        # Stamp the layer hint into the materialized timeline's metadata so
        # the owning renderer knows it is rendering one z-layer: z > 0 must
        # emit transparent output (batch 4 consumes the stamp).  Merge into
        # the copied metadata dict; never clobber existing keys.
        materialized.setdefault("metadata", {})
        materialized["metadata"]["astrid_layer"] = {
            "z": segment.layer.z,
            "alpha": segment.layer.z > 0,
        }
    materialized_path = (
        workspace / "segment-inputs" / f"{index:04d}-timeline.json"
    )
    write_json_atomic(materialized_path, materialized)
    return (
        replace(
            request, timeline_path=str(materialized_path),
            window=request.window if supports_windows else None,
        ),
        {"materialized_timeline": sha256_file(materialized_path)},
    )
