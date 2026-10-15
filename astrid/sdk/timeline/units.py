"""Units (canvas px vs an element's stored grid) and formula/moment values as the sheet and --set write them."""
from __future__ import annotations

import re
from typing import Any, Mapping

from astrid.core.timeline import moments as mo

from astrid.sdk.timeline._common import (LOGICAL_PX, POSITION_KEYS, TimelineEditError, _num)

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
    from astrid.sdk.timeline_address import element_schema

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
    from astrid.sdk.timeline_address import element_schema

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
        return [{**k, **{a: k[a] / scale for a in subs if _number(k.get(a))}} if isinstance(k, Mapping) else k
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
