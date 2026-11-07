"""Canonical audio gain curves shared with the timeline composition renderer."""

from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path

_REGISTRY = Path(__file__).with_name("audio_curves.json")


@lru_cache(maxsize=1)
def audio_curve_registry() -> dict[str, dict[str, float | str]]:
    data = json.loads(_REGISTRY.read_text(encoding="utf-8"))
    if data.get("version") != 1 or not isinstance(data.get("curves"), dict):
        raise ValueError("invalid audio curve registry")
    return data["curves"]


def names() -> tuple[str, ...]:
    return tuple(audio_curve_registry())


def validate(name: str) -> str:
    if name not in audio_curve_registry():
        raise ValueError(f"unknown audio curve {name!r}; choose {', '.join(names())}")
    return name


def gain(name: str, progress: float, *, fade_in: bool = True) -> float:
    """Return a clamped amplitude multiplier at normalized fade progress."""
    spec = audio_curve_registry()[validate(name)]
    t = min(1.0, max(0.0, float(progress)))
    if not fade_in:
        t = 1.0 - t
    kind = spec["kind"]
    if kind == "linear":
        return t
    if kind == "equal-power":
        return math.sin(t * math.pi / 2)
    if kind == "exponential":
        exponent = float(spec["exponent"])
        return (math.exp(t * exponent) - 1) / (math.exp(exponent) - 1)
    if kind == "s-curve":
        return t * t * (3 - 2 * t)
    raise ValueError(f"unsupported audio curve kind {kind!r}")
