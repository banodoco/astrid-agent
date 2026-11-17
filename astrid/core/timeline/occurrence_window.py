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

    Valid: absent/None (0), or a finite number >= 0 (never clamped); a zero start/end mapping
    is the legacy spelling of zero. When the content extent is known, a positive offset must
    leave its window inside that content (offset + duration <= extent).
    """
    if value is None:
        return 0.0
    if isinstance(value, Mapping):
        if value.get("start", 0) == 0 and value.get("end", 0) == 0:
            return 0.0
        raise ValueError("source_offset mapping is not a window origin")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("source_offset must be a finite number of milliseconds")
    if value < 0:
        raise ValueError("source_offset must not be negative")
    if (value > 0 and duration_ms is not None and content_extent_ms is not None
            and value + duration_ms > content_extent_ms + _EPS):
        raise ValueError("source_offset window runs past the shot's content")
    return float(value)
