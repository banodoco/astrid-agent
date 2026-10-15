"""Integer allocation for authored sequence recipes.

The legacy largest-remainder path stays separate from inverse-progress fitting.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Sequence

from astrid.core.timeline.clip_fields import _easing_name
from astrid.sdk.timeline._common import TimelineEditError


def fit_curve_name(value: str) -> str:
    name = _easing_name(value)
    if name == "step":
        raise TimelineEditError("sequence fit curve 'step' has no inverse progress")
    if name not in {"linear", "in", "out", "in-out"}:
        raise TimelineEditError(f"unknown sequence fit curve {value!r}")
    return name


def fit_curve_progress(value: str) -> Callable[[float], float] | None:
    """Resolve an admitted named function; never select a curve family by guesswork."""
    name = fit_curve_name(value)
    if name != "linear":
        raise TimelineEditError(f"sequence fit curve {value!r} is unavailable in this build; use linear")
    return None


def _inverse_progress(progress: Callable[[float], float], p: float) -> float:
    """Invert a continuous, strictly increasing unit-interval progress function."""
    if p <= 0:
        return 0.0
    if p >= 1:
        return 1.0
    lo, hi = 0.0, 1.0
    for _ in range(64):
        mid = (lo + hi) / 2
        if progress(mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def allocate_fit_frames(weights: Sequence[float], total: int,
                        progress: Callable[[float], float] | None = None) -> list[int]:
    """Allocate exactly ``total`` frames, retaining one frame per required step.

    With no progress transform this is the existing largest-remainder allocator.
    An inverse-progress transform rounds cumulative boundaries half up, then
    clamps each boundary so every remaining step retains at least one frame.
    """
    if not weights:
        raise TimelineEditError("sequence fit needs at least one step")
    if total < len(weights):
        raise TimelineEditError(f"sequence fit span has {total} frames for {len(weights)} required steps")
    if any(not math.isfinite(w) or w <= 0 for w in weights):
        raise TimelineEditError("sequence fit weights must be finite and positive")
    whole = sum(weights)
    if not math.isfinite(whole):
        raise TimelineEditError("sequence fit weights must have a finite sum")
    if progress is not None:
        frame, cumulative, plan = 0, 0.0, []
        for i, weight in enumerate(weights[:-1]):
            cumulative += weight
            ideal = total * _inverse_progress(progress, cumulative / whole)
            boundary = min(total - (len(weights) - i - 1), max(frame + 1, math.floor(ideal + 0.5)))
            plan.append(boundary - frame)
            frame = boundary
        return plan + [total - frame]
    scaled = [n * total / whole for n in weights]
    plan = [max(1, int(x)) for x in scaled]
    order = sorted(range(len(weights)), key=lambda i: scaled[i] - int(scaled[i]), reverse=True)
    k = 0
    while sum(plan) < total:
        plan[order[k % len(order)]] += 1
        k += 1
    while sum(plan) > total:
        i = max(range(len(plan)), key=lambda j: plan[j])
        plan[i] -= 1
    return plan
