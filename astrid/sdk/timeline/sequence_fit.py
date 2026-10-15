"""Integer allocation for authored sequence recipes.

The legacy largest-remainder path stays separate from inverse-progress fitting.
"""
from __future__ import annotations

import math
from collections.abc import Callable, Sequence

from functools import partial

from astrid.core.timeline.motion_curves import MOTION_CURVES, motion_curve_inverse, motion_curve_name
from astrid.sdk.timeline._common import TimelineEditError


def fit_curve_name(value: str) -> str:
    try:
        name = motion_curve_name(value)
    except ValueError as exc:
        raise TimelineEditError(str(exc)) from exc
    if not MOTION_CURVES[name]["inverse"]:
        raise TimelineEditError(f"sequence fit curve {name!r} has no inverse progress")
    return name


def fit_curve_inverse(value: str) -> Callable[[float], float] | None:
    """Resolve the declared analytic inverse; linear keeps its legacy allocator."""
    name = fit_curve_name(value)
    return None if name == "linear" else partial(motion_curve_inverse, name)


def allocate_fit_frames(weights: Sequence[float], total: int,
                        inverse: Callable[[float], float] | None = None) -> list[int]:
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
    if inverse is not None:
        frame, cumulative, plan = 0, 0.0, []
        for i, weight in enumerate(weights[:-1]):
            cumulative += weight
            ideal = total * inverse(cumulative / whole)
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
