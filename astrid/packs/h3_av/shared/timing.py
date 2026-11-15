"""Exact MiniMax H3 continuation timing.

The Seitanism continuation graph preserves a 39-frame audiovisual prefix in
each sampled extension.  The model's raw video runs are ``5 + 17*k`` frames,
so the user-visible suffix is ``raw_frames - context_frames``.  Treating the
requested suffix duration as the raw run duration can therefore produce a
fully preserved, zero-generation latent.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from fractions import Fraction
from typing import Any, Mapping


H3_FPS = 24
H3_FIRST_RUN_FRAMES = 5
H3_RUN_STRIDE_FRAMES = 17
H3_DEFAULT_CONTEXT_FRAMES = 39
H3_AV_CONTEXT_STRIDE_FRAMES = 51


class ContinuationTimingError(ValueError):
    """A continuation cannot be represented by the pinned H3 graph."""


def frame_index(seconds: Fraction | int | float | str, path: str, *, fps: int = H3_FPS) -> int:
    """Convert frame-aligned seconds to an integer frame index."""

    # Exact request times never pass through the legacy float projection used
    # by media coverage and v1 callers.
    if not isinstance(seconds, float):
        frame = Fraction(seconds) * fps
        if frame.denominator == 1:
            return frame.numerator
    else:
        frame = round(seconds * fps)
        if abs(frame / fps - seconds) <= 1e-6:
            return frame
    raise ContinuationTimingError(f"{path} must be aligned to the selected graph's {fps} fps frame grid")


def is_h3_run(frame_count: int) -> bool:
    return frame_count >= H3_FIRST_RUN_FRAMES and (frame_count - H3_FIRST_RUN_FRAMES) % H3_RUN_STRIDE_FRAMES == 0


def is_shared_av_context(frame_count: int) -> bool:
    return (
        frame_count >= H3_DEFAULT_CONTEXT_FRAMES
        and (frame_count - H3_DEFAULT_CONTEXT_FRAMES) % H3_AV_CONTEXT_STRIDE_FRAMES == 0
    )


def smallest_h3_run_at_least(frame_count: int) -> int:
    """Return the first native H3 video run at least ``frame_count`` long."""

    required = max(H3_FIRST_RUN_FRAMES, int(frame_count))
    steps = max(0, math.ceil((required - H3_FIRST_RUN_FRAMES) / H3_RUN_STRIDE_FRAMES))
    return H3_FIRST_RUN_FRAMES + steps * H3_RUN_STRIDE_FRAMES


@dataclass(frozen=True)
class ContinuationTiming:
    fps: int
    source_frames: int
    requested_output_frames: int
    requested_new_frames: int
    context_frames: int
    raw_extension_frames: int
    generated_capacity_frames: int
    trim_tail_frames: int
    expected_graph_output_frames: int

    @property
    def workflow_duration(self) -> float:
        """Raw duration passed to the workflow's H3-length expression."""

        return self.raw_extension_frames / self.fps

    @property
    def requested_output_duration(self) -> float:
        return self.requested_output_frames / self.fps

    @property
    def expected_graph_output_duration(self) -> float:
        return self.expected_graph_output_frames / self.fps

    def to_dict(self) -> dict[str, int | float]:
        return {
            **asdict(self),
            "workflow_duration": self.workflow_duration,
            "requested_output_duration": self.requested_output_duration,
            "expected_graph_output_duration": self.expected_graph_output_duration,
        }


def plan_continuation(
    *,
    source_end: Fraction | int | float | str,
    output_duration: Fraction | int | float | str,
    context_frames: int = H3_DEFAULT_CONTEXT_FRAMES,
) -> ContinuationTiming:
    """Plan one full-timeline continuation for the pinned Seitanism graph."""

    context = int(context_frames)
    if not is_shared_av_context(context):
        raise ContinuationTimingError(
            "context_frames must be a shared H3 video/audio boundary (39, 90, 141, ...)"
        )
    source_frames = frame_index(source_end, "source.range[1]")
    requested_output_frames = frame_index(output_duration, "output.duration")
    requested_new_frames = requested_output_frames - source_frames
    if requested_new_frames <= 0:
        raise ContinuationTimingError("continuation must request at least one net new frame")

    raw_extension_frames = smallest_h3_run_at_least(context + requested_new_frames)
    generated_capacity_frames = raw_extension_frames - context
    if generated_capacity_frames <= 0:
        raise ContinuationTimingError(
            "H3 extension would preserve its whole latent and generate zero new frames"
        )
    if generated_capacity_frames < requested_new_frames:
        raise ContinuationTimingError("H3 extension has insufficient net-new-frame capacity")
    trim_tail_frames = generated_capacity_frames - requested_new_frames
    return ContinuationTiming(
        fps=H3_FPS,
        source_frames=source_frames,
        requested_output_frames=requested_output_frames,
        requested_new_frames=requested_new_frames,
        context_frames=context,
        raw_extension_frames=raw_extension_frames,
        generated_capacity_frames=generated_capacity_frames,
        trim_tail_frames=trim_tail_frames,
        expected_graph_output_frames=source_frames + generated_capacity_frames,
    )


def plan_from_preparation(preparation: Mapping[str, Any]) -> ContinuationTiming | None:
    """Return timing for a continuation preparation, otherwise ``None``."""

    request = preparation.get("request")
    if not isinstance(request, Mapping) or request.get("operation") != "continue":
        return None
    source = request.get("source")
    output = request.get("output")
    if not isinstance(source, Mapping) or not isinstance(output, Mapping):
        raise ContinuationTimingError("continuation preparation is missing source/output timing")
    source_range = source.get("range")
    if not isinstance(source_range, (list, tuple)) or len(source_range) != 2:
        raise ContinuationTimingError("continuation preparation is missing source.range")
    if Fraction(str(source_range[0])) != 0:
        raise ContinuationTimingError("native continuation requires source.range to start at zero")
    if "duration" not in output:
        raise ContinuationTimingError("continuation preparation is missing output.duration")
    return plan_continuation(source_end=source_range[1], output_duration=output["duration"])


__all__ = [
    "ContinuationTiming",
    "ContinuationTimingError",
    "H3_DEFAULT_CONTEXT_FRAMES",
    "H3_FPS",
    "frame_index",
    "is_h3_run",
    "is_shared_av_context",
    "plan_continuation",
    "plan_from_preparation",
    "smallest_h3_run_at_least",
]
