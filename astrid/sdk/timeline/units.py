"""Units (canvas px vs an element's stored grid) and formula/moment values as the sheet and --set write them."""
from __future__ import annotations

import math
import re
from fractions import Fraction
from typing import Any, Mapping

from astrid.core.timeline import moments as mo
from astrid.sdk.timeline._common import LOGICAL_PX, POSITION_KEYS, TimelineEditError, _num, _r


def trim_source_interval(source_in: int | float, source_out: int | float,
                         limit: int | float) -> tuple[float, float, float]:
    """Round an already validated source interval without exceeding the raw file limit.

    Keep nearest six-decimal precision, moving an overshooting out point to
    the greatest six-decimal point inside the file. Do the floor in exact
    arithmetic: floating multiplication could round back up to the boundary.
    If converting the floor to binary64 exceeds the original integer limit,
    use its immediate binary64 predecessor (the floor itself is inside).
    Refuse collapsed intervals rather than expanding a tiny requested window.
    """
    applied_in, applied_out = _r(source_in), _r(source_out)
    if applied_out > limit:
        applied_out = math.floor(Fraction(limit) * 1_000_000) / 1_000_000
        if applied_out > limit:
            applied_out = math.nextafter(applied_out, -math.inf)
    hold = _r(applied_out - applied_in)
    if applied_out <= applied_in or hold <= 0:
        raise TimelineEditError("source range collapses at authored precision")
    return applied_in, applied_out, hold


def frame_time(frame: int, fps: int | float) -> Fraction:
    if isinstance(frame, bool) or isinstance(fps, bool) or frame < 0 or fps <= 0:
        raise TimelineEditError("frame and fps must be non-negative/positive")
    return Fraction(frame) / Fraction(str(fps))


def _time_fraction(value: int | float | Fraction, label: str) -> Fraction:
    if isinstance(value, bool) or not isinstance(value, (int, float, Fraction)):
        raise TimelineEditError(f"{label} must be a number")
    return value if isinstance(value, Fraction) else Fraction(str(value))


def _playback_rate(speed: int | float | Fraction) -> Fraction:
    rate = _time_fraction(speed, "speed")
    if rate <= 0:
        raise TimelineEditError("speed must be positive")
    return rate


def source_to_timeline_time(
    source_time: int | float | Fraction,
    *,
    source_start: int | float | Fraction = 0,
    timeline_start: int | float | Fraction = 0,
    speed: int | float | Fraction = 1,
) -> Fraction:
    """Map a source timestamp into its local timeline timestamp.

    The half-open interval mapping is ``timeline_start + (source_time -
    source_start) / speed``. Fractions keep rational frame boundaries exact;
    callers can apply :func:`quantize_time` at the renderer boundary.
    """

    rate = _playback_rate(speed)
    return _time_fraction(timeline_start, "timeline_start") + (
        _time_fraction(source_time, "source_time") - _time_fraction(source_start, "source_start")
    ) / rate


def timeline_to_source_time(
    timeline_time: int | float | Fraction,
    *,
    source_start: int | float | Fraction = 0,
    timeline_start: int | float | Fraction = 0,
    speed: int | float | Fraction = 1,
) -> Fraction:
    """Map a local timeline timestamp back into source time."""

    rate = _playback_rate(speed)
    return _time_fraction(source_start, "source_start") + (
        _time_fraction(timeline_time, "timeline_time") - _time_fraction(timeline_start, "timeline_start")
    ) * rate


def quantize_time(seconds: int | float | Fraction, fps: int | float | Fraction, *, policy: str = "nearest") -> int:
    if isinstance(fps, bool) or isinstance(seconds, bool) or fps <= 0 or policy not in {"floor", "ceil", "nearest"}:
        raise TimelineEditError("invalid fps or quantization policy")
    frames = _time_fraction(seconds, "seconds") * Fraction(str(fps))
    if policy == "floor":
        return math.floor(frames)
    if policy == "ceil":
        return math.ceil(frames)
    return math.floor(frames + Fraction(1, 2))


def quantize_interval(
    start: int | float | Fraction,
    end: int | float | Fraction,
    fps: int | float,
    *,
    policy: str = "nearest",
) -> dict[str, Any]:
    """Quantize a half-open interval and report requested/applied boundaries.

    The returned frame numbers are renderer-facing values; the Fraction
    values are the exact seconds represented by those frames. A collapsed
    interval is rejected instead of becoming a zero-frame clip.
    """

    start_value = _time_fraction(start, "start")
    end_value = _time_fraction(end, "end")
    if end_value <= start_value:
        raise TimelineEditError("interval must be non-empty")
    if isinstance(fps, bool) or fps <= 0:
        raise TimelineEditError("fps must be positive")
    fps_value = Fraction(str(fps))
    start_frame = quantize_time(start_value, fps_value, policy=policy)
    end_frame = quantize_time(end_value, fps_value, policy=policy)
    if end_frame <= start_frame:
        raise TimelineEditError(
            "quantization collapsed the interval; choose a wider interval or a different policy"
        )
    applied_start = Fraction(start_frame, 1) / fps_value
    applied_end = Fraction(end_frame, 1) / fps_value
    return {
        "policy": policy,
        "fps": fps_value,
        "requested_start": start_value,
        "requested_end": end_value,
        "start_frame": start_frame,
        "end_frame": end_frame,
        "applied_start": applied_start,
        "applied_end": applied_end,
        "rounded": applied_start != start_value or applied_end != end_value,
    }


def grid(rows: int, columns: int, *, gap: float = 0.0) -> list[dict[str, float]]:
    if rows <= 0 or columns <= 0 or gap < 0 or gap >= 1:
        raise TimelineEditError("invalid grid dimensions or gap")
    width, height = (1 - gap * (columns - 1)) / columns, (1 - gap * (rows - 1)) / rows
    return [
        {"x": c * (width + gap), "y": r * (height + gap), "width": width, "height": height}
        for r in range(rows) for c in range(columns)
    ]

# The frozen donor declared these units in each element's metadata. The current
# V3 element declarations do not carry that metadata yet, so the SDK preserves
# the admitted editing units here until the declarations are joined by their owner.
_DONOR_UNIT_DECLARATIONS = {
    "am-discord": {"frames": ["camera[].at", "circle[].at", "composer.startAt", "messages[].appearAt"]},
    "am-orgchart": {"frames": ["revealAt[]", "stamp.at"]},
    "am-terminal": {"frames": ["lines[].at", "badges[].at"]},
    "am-flap": {"frames": ["values[].at"]},
    "am-pixel-shape": {"scale": 6, "logical": ["x", "y"]},
    "am-sprite": {"scale": 6, "logical": ["x", "y", "keyframes[].x", "keyframes[].y"],
                  "frames": ["keyframes[].frame"]},
    "am-quote": {"frames": ["mark.at", "stamp.at", "strikeAt"]},
    "am-ui-sketch": {"frames": ["prompt.at", "states[].at", "allAt"]},
}


def _declared_units(element: str) -> Mapping[str, Any]:
    from astrid.sdk.timeline_address import element_schema

    return element_schema(element).get("units") or _DONOR_UNIT_DECLARATIONS.get(element, {})


def element_units(element: str) -> tuple[float, frozenset[str]]:
    """``(canvas px per stored unit, the params stored in those units)`` as the element DECLARES it in its
    element.yaml (``metadata.units: {scale: 6, logical: [x, y, keyframes[].x, keyframes[].y]}``). An element
    that declares nothing stores canvas px. Every surface (show, the sheet, --set, formulas) reads this."""
    units = _declared_units(element)
    paths = frozenset(str(p) for p in units.get("logical") or [])
    scale = float(units.get("scale") or LOGICAL_PX) if paths else 1.0
    return (int(scale) if scale.is_integer() else scale), paths


def _unit_path(path: str) -> str:
    """``params.keyframes[2].x`` → ``keyframes[].x`` (how units name a param)."""
    return re.sub(r"\[\d+\]", "[]", path.removeprefix("params."))


def is_logical(element: str, path: str) -> bool:
    return _unit_path(path) in element_units(element)[1]


def param_unit(element: str, path: str) -> str | None:
    """What a param's number means, as the element declares it: ``canvas px`` (stored on a grid or not),
    ``clip frames``, ``clip seconds``; None when the element says nothing about it."""
    units = _declared_units(element)
    key = _unit_path(path)
    for name, label in (("frames", "clip frames"), ("seconds", "clip seconds")):
        if key in [str(p) for p in units.get(name) or []]:
            return label
    if key in element_units(element)[1] or key in POSITION_KEYS:
        return "canvas px"
    return None


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def to_canvas(element: str, key: str, value: Any) -> Any:
    """Stored → canvas px (what every surface shows), by the element's declared units (``element_units``)."""
    scale, paths = element_units(element)
    if not paths:
        return value
    if key in paths and _number(value):
        return value * scale
    subs = [p.split("[].", 1)[1] for p in paths if p.startswith(f"{key}[].")]
    if subs and isinstance(value, list):
        return [{**k, **{a: k[a] * scale for a in subs if _number(k.get(a))}} if isinstance(k, Mapping) else k for k in value]
    return value


def from_canvas(element: str, key: str, value: Any) -> Any:
    """Canvas px (what you write) → stored. ``"1290px"`` is accepted too. am-sprite snaps to its 6-px grid."""
    if isinstance(value, str) and re.fullmatch(r"-?\d+(\.\d+)?px", value.strip()):
        value = float(value.strip()[:-2])
    scale, paths = element_units(element)
    if not paths:
        return round(value) if isinstance(value, float) and key in POSITION_KEYS and value.is_integer() else value
    if key in paths and _number(value):
        return round(value / scale)
    subs = [p.split("[].", 1)[1] for p in paths if p.startswith(f"{key}[].")]
    if subs and isinstance(value, list):
        return [{**k, **{a: round(k[a] / scale) for a in subs if _number(k.get(a))}} if isinstance(k, Mapping) else k
                for k in value]
    return value


_FORMULA_VALUE = re.compile(r"^\s*(?:ƒ|f|mark)\(\s*(?:(?P<mark>[A-Za-z][\w-]*?)\s*)?(?P<off>[+−-]\s*\d+(?:\.\d+)?)?\s*\)\s*$")


def parse_moment_value(value: Any, current_expr: Mapping[str, Any] | None = None) -> str | None:
    """A value meant as a moment: ``ƒ("adapt" in w05c)``, or (for a param already on a moment, or plainly a
    moment: quoted words, ``after``/``beat``/``in``) ``'"adapt" in w05c'``. Canonical text, else None."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    m = re.fullmatch(r"(?:ƒ|f)\((.*)\)", text)
    explicit = m is not None
    if explicit:
        text = m.group(1).strip()
        if re.fullmatch(r"words of \S+", text):
            return None  # ƒ(words of LINE) is a words formula, not a moment
    looks = explicit or text.startswith(('"', "“", "after ", "beat ", "downbeat ")) or bool(current_expr and current_expr.get("moment"))
    if not looks:
        return None
    try:
        moment = mo.parse(text)
    except mo.MomentError:
        if explicit:
            raise TimelineEditError(f"{value!r} is not a moment (e.g. ƒ(\"adapt\" in w05c), ƒ(beat 2 after \"Astrid\"))") from None
        return None
    return mo.format_moment(moment)


def parse_formula_value(value: Any) -> dict[str, Any] | None:
    """``"ƒ(B2-HAND -42)"`` / ``"f(-60)"`` / ``"mark(B2-HAND +12)"`` → {mark, offset (canvas px)}; else None."""
    if not isinstance(value, str):
        return None
    m = _FORMULA_VALUE.match(value)
    if not m or (m.group("mark") is None and m.group("off") is None):
        return None
    off = (m.group("off") or "0").replace("−", "-").replace(" ", "")
    return {"mark": m.group("mark"), "offset": float(off)}


def formula_from_spec(spec: Mapping[str, Any], element: str, key: str, current: Mapping[str, Any]) -> dict[str, Any]:
    """A mark formula from a canvas-px spec, keeping the current formula's mark/axis when not given."""
    mark = spec.get("mark") or current.get("mark")
    if not mark:
        raise TimelineEditError(f"{key}: name the slot mark, e.g. {key}=ƒ(B2-HAND -42)")
    axis = current.get("axis") or (key if key in POSITION_KEYS else "x")
    if current.get("mark") == mark and abs(mark_offset_canvas(current) - float(spec["offset"])) < 0.005:
        return dict(current)  # the same formula, however it was stored: unchanged
    offset = round(float(spec["offset"]), 2)  # stored as written, in canvas px: no ÷6 round-trip noise
    return {"mark": mark, "axis": axis, "offset": int(offset) if offset == int(offset) else offset, "unit": "canvas"}


def mark_offset_canvas(expr: Mapping[str, Any]) -> float:
    """A mark formula's offset in canvas px, whatever unit it was stored in."""
    offset = _num(expr.get("offset"))
    return offset * LOGICAL_PX if expr.get("unit", "logical") == "logical" else offset


def formula_short(expr: Any, element: str) -> str:
    """A formula as the sheet writes it, in canvas px: ``ƒ(B2-HAND -42)``; a moment: ``ƒ("viral" …)``."""
    if isinstance(expr, Mapping) and "mark" in expr:
        return f"ƒ({expr['mark']} {round(mark_offset_canvas(expr), 2):+g})"
    if isinstance(expr, Mapping) and expr.get("moment"):
        return f"ƒ({expr['moment']})"
    if isinstance(expr, Mapping) and "words_of" in expr:
        return f"ƒ(words of {expr['words_of']})"
    return "ƒ"
