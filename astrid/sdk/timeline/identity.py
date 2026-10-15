"""Cut-family high-water reservations, independent of reversible authored content.

The numeric mark reserves all plain cut numbers up to it. Each numeric family
also has a suffix mark (a=1 … z=26), preserving the established c1a spelling.
Marks are maxima, never lists of deleted entities. Legacy documents seed them
from present cuts; unknowable deletions before reservation was introduced cannot
be recovered. Persistence/history owners must retain the maxima on restoration.
"""
from __future__ import annotations

import re
from collections.abc import Mapping

from astrid.core.timeline import astrid_ns as ns
from astrid.sdk.timeline._common import TimelineEditError

KEY = "cut_id_high_water"
_PATTERN = re.compile(r"c0*(\d+)([a-z]?)")


def cut_parts(value):
    match = _PATTERN.fullmatch(str(value))
    if not match:
        raise TimelineEditError(f"{value!r} is not a cut id like c33")
    return int(match[1]), ord(match[2]) - 96 if match[2] else 0


def cut_reservations(bundle):
    config = (bundle.get("parent") or {}).get("config") or {}
    raw = ns.film_intent(config).get(KEY, {})
    if not isinstance(raw, Mapping) or set(raw) - {"number", "suffixes"}:
        raise TimelineEditError(f"app.astrid.{KEY} is not a reservation object")
    number, suffixes = raw.get("number", 0), raw.get("suffixes", {})
    if type(number) is not int or number < 0 or not isinstance(suffixes, Mapping):
        raise TimelineEditError(f"invalid app.astrid.{KEY}")
    result = {"number": number, "suffixes": dict(suffixes)}
    for key, value in suffixes.items():
        if not str(key).isdigit() or str(int(key)) != key or type(value) is not int or not 1 <= value <= 26:
            raise TimelineEditError(f"invalid app.astrid.{KEY} suffix reservation")
        result["number"] = max(result["number"], int(key))
    for shot in (bundle.get("shots") or {}).values():
        for clip in (shot.get("internal_timeline") or {}).get("clips") or []:
            app = ns.read_app(clip)
            cid = app.get("cut")
            if cid and _PATTERN.fullmatch(str(cid)):
                num, suffix = cut_parts(cid)
                result["number"] = max(result["number"], num)
                if suffix:
                    key = str(num)
                    result["suffixes"][key] = max(result["suffixes"].get(key, 0), suffix)
    return result


def retain_cut_reservation_mark(restored, previous):
    """Join a previously captured mark at the real command/history boundary."""
    mark = cut_reservations(restored)
    mark["number"] = max(mark["number"], previous["number"])
    for key, value in previous["suffixes"].items():
        mark["suffixes"][key] = max(mark["suffixes"].get(key, 0), value)
    if mark["number"] or mark["suffixes"]:
        config = restored.setdefault("parent", {}).setdefault("config", {})
        ns.film_ns(config)[KEY] = mark
    return restored


def retain_cut_reservations(restored, *previous):
    """Join reservation maxima in a detached restored/merged bundle, in place.

    No file, journal, checkout lifecycle or additional writer is owned here.
    """
    if not previous:
        return retain_cut_reservation_mark(restored, cut_reservations(restored))
    for bundle in previous:
        retain_cut_reservation_mark(restored, cut_reservations(bundle))
    return restored


def next_cut_id(bundle, source):
    number, suffix = cut_parts(source)
    mark = cut_reservations(bundle)
    suffix = max(suffix, mark["suffixes"].get(str(number), 0)) + 1
    if suffix <= 26:
        stem = _PATTERN.fullmatch(str(source))[0].rstrip("abcdefghijklmnopqrstuvwxyz")
        return f"{stem}{chr(96 + suffix)}"
    return f"c{mark['number'] + 1}"


def reserve_cut_id(bundle, value):
    number, suffix = cut_parts(value)
    mark = cut_reservations(bundle)
    used = suffix <= mark["suffixes"].get(str(number), 0) if suffix else number <= mark["number"]
    if used:
        raise TimelineEditError(f"cut {value} is already reserved; cut ids are never reused")
    mark["number"] = max(mark["number"], number)
    if suffix:
        mark["suffixes"][str(number)] = suffix
    ns.film_ns(bundle.setdefault("parent", {}).setdefault("config", {}))[KEY] = mark
