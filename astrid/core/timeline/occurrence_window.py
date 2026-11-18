"""The one rule for an occurrence's `source_offset`: the window origin into its shot's timeline.

An occurrence shows the window [source_offset, source_offset + duration] of its shot's internal
timeline, starting at the occurrence's placement. A child at internal time tau therefore appears at
`start - source_offset + tau`. Every guard and projection asks this module; none keeps its own copy.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

MIN_OCCURRENCE_DURATION_MS = 100
_EPS = 1e-6


def source_offset_ms(value: Any, *, duration_ms: float | None = None,
                     content_extent_ms: float | None = None) -> float:
    """Return the offset in ms, or raise ValueError when it is not a valid window origin.

    Valid: absent/None (0), a finite number >= 0 (never clamped), or the {start, end} mapping whose
    start is such a number. When the content extent is known, a positive offset must
    leave its window inside that content (offset + duration <= extent).
    """
    if value is None:
        return 0.0
    if isinstance(value, Mapping):
        # Stored heads spell it {"start": ms, "end": ms}; "start" is the window origin and "end" is
        # legacy: it is never read, and a trim leaves it unchanged. (Every stored head has 0/0.)
        value = value.get("start", value.get("start_ms", 0))
        if value is None:
            return 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("source_offset must be a finite number of milliseconds")
    if value < 0:
        raise ValueError("source_offset must not be negative")
    if (value > 0 and duration_ms is not None and content_extent_ms is not None
            and window_overrun_ms(value, duration_ms, content_extent_ms)):
        raise ValueError("source_offset window runs past the shot's content")
    return float(value)


def window_overrun_ms(offset_ms: float, duration_ms: float, content_extent_ms: float, *,
                      previous: tuple[float, float] | None = None) -> float:
    """How far (ms) the window [offset, offset + duration] runs past the shot's content; 0 when it does not.

    THE overrun rule, used by every guard. A child that extends past the window is not an overrun (it is
    cropped, see `visible_span_ms`); only a window showing time the shot has no content for is. A TRIMMED
    window (offset > 0) must fit the content. An untrimmed one (offset 0) may carry an empty tail, as stored
    documents do; it counts only when it grows its end past `previous` = the (offset, duration) it had before.
    """
    excess = offset_ms + duration_ms - content_extent_ms
    if excess <= _EPS:
        return 0.0
    if offset_ms > 0 or (previous is not None and offset_ms + duration_ms > previous[0] + previous[1] + _EPS):
        return float(excess)
    return 0.0


def visible_span_ms(start_ms: float, end_ms: float, offset_ms: float,
                    duration_ms: float) -> tuple[float, float] | None:
    """A child's shot-local span [start, end] cut to the window; None when none of it is visible."""
    low, high = max(start_ms, offset_ms), min(end_ms, offset_ms + duration_ms)
    return (low, high) if high > low + _EPS else None


def cropped_ms(start_ms: float, end_ms: float, offset_ms: float, duration_ms: float) -> tuple[float, float]:
    """(hidden before the window, hidden after it) in ms for a child span; (0, 0) when fully inside."""
    return (max(0.0, min(end_ms, offset_ms) - start_ms) if start_ms < offset_ms else 0.0,
            max(0.0, end_ms - max(start_ms, offset_ms + duration_ms)))
