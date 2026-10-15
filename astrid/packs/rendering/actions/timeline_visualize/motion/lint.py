"""Composition and timing checks over picture cuts, from timeline data alone.

Shared by ``timelines lint`` (every cut, client-side, ~1 s) and the motion
sheet's ``lint`` layer (one cut). Every finding is one line an agent can act
on without opening an image:

    FACE   cut 17 +0.00s am-sprite D-06 covers 41% of the presenter face (box 564,0–1380,744)
    SAFE   cut 21 +1.00s am-type "Until I finally…" is 38 px outside title-safe (right)
    SMALL  cut 6 +1.13s am-callout "all functional now!" body text 24 px < 32 px
    COVER  cut 30 am-callout … fully covers am-type …
    SYNC   cut 17 +5.27s am-sprite D-07 enters 0.39 s before "viral" (onset 51.16)
    SHORT  cut 9 0.43 s (< 0.5 s)
    HOLD   cut 14 4.70 s with no element event, no presenter (not marked deliberate_hold)
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from ..layers.base import Finding
from . import model

# Defaults; every one is a threshold key a project rules file may set
# (astrid-lint.toml), read through ``params``.
DEFAULTS = {
    "min_text_px": 32.0,
    "stamp_to_word_max_s": 0.10,
    "sync_search_s": 0.35,
    "short_cut_s": 0.5,
    "hold_s": 2.5,
    "face_fraction": 0.05,
    "beat_near_miss_s": 0.2,
}
MIN_TEXT_PX = DEFAULTS["min_text_px"]
SETTLE_S = 0.4

__all__ = ["Finding", "DEFAULTS", "composition", "timing", "lint_cuts"]


def _p(params: Mapping[str, Any] | None, key: str) -> float:
    value = (params or {}).get(key)
    return float(DEFAULTS[key] if value is None else value)


def _z(element: model.Element, track_order: Sequence[str]) -> int:
    """Higher draws on top. Tracks listed first draw on top (plate is the bed)."""
    try:
        return len(track_order) - list(track_order).index(element.track)
    except ValueError:
        return 0


def _sample_times(cut: Mapping[str, Any], elements: Sequence[model.Element], fps: float) -> list[float]:
    start, end = float(cut["start"]), float(cut["end"])
    last = end - 1.0 / fps
    times = {round(start + (end - start) / 2, 4)}
    for element in elements:
        if element.audio:
            continue
        t = element.start + SETTLE_S if element.start > start + 1e-6 else start + SETTLE_S
        if element.start - 1e-6 <= t < element.end and t <= last:
            times.add(round(min(t, last), 4))
        for event in model.events(element, fps):
            if event.kind == "key" and start <= event.t + SETTLE_S <= last:
                times.add(round(event.t + SETTLE_S, 4))
    return sorted(t for t in times if start <= t <= last)


def _fit_hint(box: model.Box, side: str, overshoot: float) -> tuple[str, dict | None]:
    """``; set params.x ≤ 1104`` (and the matching fix) for a text/card box placed by params.x/y px."""
    params = box.element.params
    if side in ("left", "right") and isinstance(params.get("x"), (int, float)):
        target = params["x"] - overshoot if side == "right" else params["x"] + overshoot
        return (f"; set params.x {'≤' if side == 'right' else '≥'} {target:.0f}",
                {"clip": box.element.short_id, "set": {"params.x": round(target)}})
    if side in ("top", "bottom") and isinstance(params.get("y"), (int, float)):
        target = params["y"] - overshoot if side == "bottom" else params["y"] + overshoot
        return (f"; set params.y {'≤' if side == 'bottom' else '≥'} {target:.0f}",
                {"clip": box.element.short_id, "set": {"params.y": round(target)}})
    return "", None


def _clear_face(box: model.Box, face: model.Box) -> tuple[str, dict | None]:
    """The smallest move that clears the face box, in the element's own units (and the fix)."""
    moves = {
        "left": box.rect[2] - face.rect[0], "right": face.rect[2] - box.rect[0],
        "down": face.rect[3] - box.rect[1], "up": box.rect[3] - face.rect[1],
    }
    side = min(moves, key=lambda key: moves[key])
    distance = moves[side]
    params = box.element.params
    axis = "x" if side in ("left", "right") else "y"
    sign = -1 if side in ("left", "up") else 1
    if box.element.type == "am-sprite" and isinstance(params.get(axis), (int, float)):
        steps = -(-distance // model.LOGICAL_PX)
        value = params[axis] + sign * steps
        return (f"; clear it: params.{axis} {params[axis]:g} → {value:g} (logical px)",
                {"clip": box.element.short_id, "set": {f"params.{axis}": value}})
    if isinstance(params.get(axis), (int, float)):
        value = round(params[axis] + sign * distance)
        return (f"; clear it: params.{axis} {params[axis]:g} → {value:.0f}",
                {"clip": box.element.short_id, "set": {f"params.{axis}": value}})
    return f"; clear it: move {distance:.0f} px {side}", None


def _data_faces(tracks: Sequence[Any], t: float) -> list[model.Box]:
    """Face boxes from data tracks named ``face`` (a face tracker's boxes on real footage)."""
    found = []
    for track in tracks:
        if track.name == "face" and track.kind == "boxes":
            for rect in track.boxes_at(t):
                stub = model.Element(track.clip_id, "face-track", "", t, t, {}, {})
                found.append(model.Box(stub, "face", rect, "tracked face", estimated=False))
    return found


def composition(
    cut: Mapping[str, Any], elements: Sequence[model.Element], fps: float, *,
    track_order: Sequence[str] = (), min_text_px: float | None = None,
    params: Mapping[str, Any] | None = None, tracks: Sequence[Any] = (),
) -> list[Finding]:
    """FACE / FRAME / SAFE / SMALL / COVER / EDGE for one cut.

    A ``face`` boxes data track (real footage) wins over faces derived from
    presenter anchors.
    """
    index = int(cut["index"])
    title = model.safe_rect(model.TITLE_SAFE)
    min_text_px = float(min_text_px) if min_text_px is not None else _p(params, "min_text_px")
    face_fraction = _p(params, "face_fraction")
    found: dict[tuple[str, str, str], Finding] = {}

    def add(code: str, key: str, t: float, message: str, severity: str = "warn", fix: dict | None = None,
            *, estimated: bool = False) -> None:
        if estimated:
            message += " (estimated geometry)"
        found.setdefault((code, key, message.split(" (")[0]), Finding(code, index, t, message, severity, fix, "composition"))

    for t in _sample_times(cut, elements, fps):
        boxes = [box for element in elements for box in model.boxes_at(element, t, fps)]
        tracked = _data_faces(tracks, t)
        faces = tracked or [box for box in boxes if box.kind == "face"]
        for box in boxes:
            if box.kind in ("plate", "face"):
                continue
            for face in faces:
                overlap = model.intersection(box.rect, face.rect)
                share = overlap / max(1.0, model.area(face.rect))
                if share >= face_fraction:
                    x0, y0, x1, y1 = face.rect
                    hint, fix = _clear_face(box, face)
                    whose = "tracked face" if face.label == "tracked face" else "presenter face"
                    add("FACE", box.element.id, t,
                        f"{box.label} covers {share:.0%} of the {whose} "
                        f"(face {x0:.0f},{max(0, y0):.0f}–{x1:.0f},{min(1080, y1):.0f}; {box.kind} "
                        f"{box.rect[0]:.0f},{box.rect[1]:.0f}–{box.rect[2]:.0f},{box.rect[3]:.0f}){hint}", fix=fix,
                        estimated=box.estimated or face.estimated)
            if box.kind in ("text", "card"):
                off = max(-box.rect[0], -box.rect[1], box.rect[2] - model.CANVAS[0], box.rect[3] - model.CANVAS[1])
                if off > 2:
                    add("FRAME", box.element.id, t, f"{box.label} is cut off: {off:.0f} px past the frame edge "
                        f"(box {box.rect[0]:.0f},{box.rect[1]:.0f}–{box.rect[2]:.0f},{box.rect[3]:.0f})",
                        estimated=box.estimated)
                out = {
                    "left": title[0] - box.rect[0], "top": title[1] - box.rect[1],
                    "right": box.rect[2] - title[2], "bottom": box.rect[3] - title[3],
                }
                worst = max(out, key=lambda side: out[side])
                if out[worst] > 2:
                    hint, fix = _fit_hint(box, worst, out[worst])
                    add("SAFE", box.element.id, t, f"{box.label} is {out[worst]:.0f} px outside title-safe ({worst})"
                        f"{hint}", fix=fix, estimated=box.estimated)
            if box.text_px is not None and box.text_px < min_text_px:
                what = "body text" if box.kind == "card" else "text"
                add("SMALL", box.element.id, t,
                    f"{box.label} {what} {box.text_px:.0f} px < {min_text_px:.0f} px at 1080p",
                    estimated=box.estimated)
            if box.kind == "sprite":
                over = {
                    "left": -box.rect[0], "top": -box.rect[1],
                    "right": box.rect[2] - model.CANVAS[0], "bottom": box.rect[3] - model.CANVAS[1],
                }
                worst = max(over, key=lambda side: over[side])
                if over[worst] > 6:
                    add("EDGE", box.element.id, t, f"{box.label} runs {over[worst]:.0f} px past the {worst} edge "
                        "(a crop; fine if intended)", "info", estimated=box.estimated)
        opaque = [b for b in boxes if b.kind in ("card", "panel") and b.rect != (0, 0, model.CANVAS[0], model.CANVAS[1])]
        for upper in opaque:
            for lower in boxes:
                if lower is upper or lower.kind in ("plate", "face"):
                    continue
                if _z(upper.element, track_order) < _z(lower.element, track_order):
                    continue
                inside = model.intersection(upper.rect, lower.rect) / max(1.0, model.area(lower.rect))
                if inside >= 0.98:
                    add("COVER", lower.element.id + upper.element.id, t, f"{upper.label} fully covers {lower.label}",
                        estimated=upper.estimated or lower.estimated)
        for a in boxes:
            for b in boxes:
                if a is b or a.kind in ("plate", "face") or b.kind in ("plate", "face") or a.element.id >= b.element.id:
                    continue
                inter = model.intersection(a.rect, b.rect)
                if inter >= 0.95 * model.area(a.rect) and inter >= 0.95 * model.area(b.rect) and model.area(a.rect) > 0:
                    add("COVER", a.element.id + b.element.id, t, f"{a.label} and {b.label} occupy the same box",
                        estimated=a.estimated or b.estimated)
    return sorted(found.values(), key=lambda f: (f.t, f.code))


def nearest_word(words: Sequence[model.Word], t: float) -> model.Word | None:
    return min(words, key=lambda word: abs(word.start - t), default=None)


def _next_word(words: Sequence[model.Word], word: model.Word) -> str:
    later = [w for w in words if w.start > word.start + 1e-6]
    if not later:
        return ""
    following = min(later, key=lambda w: w.start)
    return f'; next word "{following.text}" at {following.start:.2f}'



def _accent_events(cut: Mapping[str, Any], elements: Sequence[model.Element], fps: float) -> list[model.Event]:
    """The cut itself plus entrances/keyframes inside it that an editor places on a word or beat."""
    start, end = float(cut["start"]), float(cut["end"])
    frame = 1.0 / fps
    found = [model.Event(start, "cut", str(cut.get("clip_id") or ""), "the cut", "cut")]
    for element in elements:
        if element.audio or element.type in model.PLATE_TYPES or element.sequence:
            continue  # the picture and a sequence's steps are the cut itself, not accents on it
        # a chained segment (the same layer continuing after a blink or a split) has no entrance of its own
        chained = any(other is not element and other.type == element.type and other.track == element.track
                      and other.asset == element.asset and abs(other.end - element.start) <= frame + 1e-6
                      for other in elements)
        for event in model.accents(element, fps):
            if chained and event.kind != "key" and abs(event.t - element.start) <= frame + 1e-6:
                continue
            if start + frame - 1e-6 < event.t < end - frame + 1e-6:
                found.append(event)
    return sorted(found, key=lambda e: e.t)


def timing(
    cut: Mapping[str, Any], elements: Sequence[model.Element], words: Sequence[model.Word], fps: float, *,
    beats: Mapping[str, list] | None = None, sfx: Sequence[tuple[float, float, str]] = (),
    params: Mapping[str, Any] | None = None,
) -> list[Finding]:
    """SYNC / BEAT / SFX / SHORT / HOLD for one cut (data only)."""
    index = int(cut["index"])
    start, end = float(cut["start"]), float(cut["end"])
    tolerance, search = _p(params, "stamp_to_word_max_s"), _p(params, "sync_search_s")
    short, hold = _p(params, "short_cut_s"), _p(params, "hold_s")
    near_miss = _p(params, "beat_near_miss_s")
    found: list[Finding] = []

    def finding(code: str, t: float, message: str, severity: str = "warn", fix: dict | None = None) -> Finding:
        return Finding(code, index, t, message, severity, fix, "timing")

    if end - start < short - 1e-6:
        found.append(finding("SHORT", start, f"{end - start:.2f} s (< {short:g} s)"))
    accents = _accent_events(cut, elements, fps)
    onsets = [w for w in words if start - 0.5 <= w.start <= end + 0.5]
    for event in accents:
        if event.kind == "cut":
            continue
        word = nearest_word(onsets, event.t)
        if word is None:
            continue
        offset = event.t - word.start
        if tolerance < abs(offset) <= search:
            side = "after" if offset > 0 else "before"
            frames = int(round(-offset * fps))
            fix = {"clip": event.element.rsplit(":", 1)[-1], "move_s": round(-offset, 3), "move_frames": frames}
            if event.kind == "key":
                fix["key"] = event.label
            found.append(finding("SYNC", event.t,
                                 f"{event.label} {event.kind}s {abs(offset):.2f} s {side} \"{word.text}\" "
                                 f"(onset {word.start:.2f}; move {-offset:+.2f} s = {frames:+d}f){_next_word(onsets, word)}",
                                 fix=fix))
    # Music accents: hits (stab/thud/blip) and downbeats. A near miss (0.1–0.2 s)
    # reads as late/early; further away it is simply not on the beat.
    marks = [(t, kind) for t, kind in (beats or {}).get("hits") or []]
    marks += [(t, "downbeat") for t in (beats or {}).get("downbeats") or []]
    for event in accents:
        if not marks:
            break
        mark, kind = min(marks, key=lambda m: abs(m[0] - event.t))
        offset = event.t - mark
        if tolerance < abs(offset) <= near_miss:
            found.append(finding("BEAT", event.t,
                                 f"{event.label} is {offset:+.2f} s off the music {kind} at {mark:.2f}", "info"))
    for hit_start, _hit_end, name in sfx:
        if not start - 1e-6 <= hit_start < end:
            continue
        visual = min(accents, key=lambda e: abs(e.t - hit_start), default=None)
        if visual is not None and tolerance < abs(hit_start - visual.t) <= search:
            found.append(finding("SFX", hit_start,
                                 f"sfx {name} at {hit_start:.2f} is {hit_start - visual.t:+.2f} s from {visual.label} "
                                 f"{visual.kind} ({visual.t:.2f})"))
    if not cut.get("deliberate_hold"):
        moments = sorted({start, end} | {e.t for el in elements for e in model.events(el, fps) if start <= e.t <= end})
        talking = [el for el in elements if el.type == "am-presenter" and model.presenter_words(el)]
        if not talking:
            gaps = [(b - a, a) for a, b in zip(moments, moments[1:])]
            longest, at = max(gaps, default=(0.0, start))
            if longest >= hold:
                found.append(finding("HOLD", at,
                                     f"{longest:.2f} s with no element event and no presenter "
                                     "(not marked deliberate_hold; check stillness in --view motion)"))
    return sorted(found, key=lambda f: (f.t, f.code))


def lint_cuts(
    cuts: Iterable[Mapping[str, Any]], elements: Sequence[model.Element], fps: float, *,
    track_order: Sequence[str] = (), beats: Mapping[str, Any] | None = None,
    min_text_px: float | None = None, params: Mapping[str, Any] | None = None,
    severity: Mapping[str, str] | None = None,
) -> list[tuple[Mapping[str, Any], list[Finding]]]:
    """Every registered doc check (built-in and pack) over ``cuts``; see ``conditions.run_checks``."""
    from .conditions import run_checks

    merged = dict(params or {})
    if min_text_px is not None:
        merged["min_text_px"] = min_text_px
    per_cut, _timeline = run_checks(list(cuts), elements, fps, track_order=track_order, beats=beats,
                                    params=merged, severity=severity)
    return per_cut
