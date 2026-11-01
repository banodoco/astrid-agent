"""Built-in visualize layers (the first entries in the registry).

Data layers read only the timeline: ``sync``, ``curves``, ``lipsync``,
``lint``. Pixel layers read the bounded frames the motion planner captured:
``strip``, ``onion``, ``diff``, ``still``. ``bounds`` draws declared element
bounds and safe areas over frames; ``labels`` adds visible clip addresses to
motion and contact tiles.
"""
from __future__ import annotations

import math
from typing import Any, Sequence

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from ..motion import model
from .base import (
    PALETTE,
    SERIES,
    Check,
    Layer,
    LayerContext,
    LayerResult,
    draw_text,
    register,
    text_width,
)

# Pixel-change thresholds on 8-bit luminance differences at review size.
CHANGE_LEVEL = 18
FROZEN_FRACTION = 0.0005  # < 0.05 % of pixels changed: frozen
NEAR_STILL_FRACTION = 0.004  # < 0.4 %: only a mouth/eye-sized region moves


# ---------------------------------------------------------------- helpers

def _sorted_frames(ctx: LayerContext) -> list[int]:
    return sorted(ctx.frames)


def _diff_fraction(a: Image.Image, b: Image.Image) -> tuple[float, Image.Image]:
    diff = ImageChops.difference(a, b).convert("L")
    mask = diff.point(lambda v: 255 if v > CHANGE_LEVEL else 0)
    changed = mask.histogram()[255]
    return changed / float(a.size[0] * a.size[1]), mask


def _motion_series(ctx: LayerContext) -> list[dict[str, Any]]:
    """Consecutive sampled frame pairs: changed fraction and its bounding box (cached in ctx.shared)."""
    if "motion_series" in ctx.shared:
        return ctx.shared["motion_series"]
    frames = _sorted_frames(ctx)
    series = []
    previous = None
    for frame in frames:
        image = ctx.frame_image(frame)
        if image is None:
            continue
        if previous is not None:
            fraction, mask = _diff_fraction(previous[1], image)
            series.append({"a": previous[0], "b": frame, "fraction": fraction, "bbox": mask.getbbox()})
        previous = (frame, image)
    ctx.shared["motion_series"] = series
    return series


def _event_label(event: model.Event) -> str:
    glyph = {"enter": "▶", "exit": "◀", "key": "◆", "cut": "┃"}.get(event.kind, "•")
    return f"{glyph}{event.label}"


def _focus_events(ctx: LayerContext, limit: int = 4) -> list[model.Event]:
    """The events worth an onion skin: entrances/keys inside the cut, then the cut-in."""
    start, end = float(ctx.cut["start"]), float(ctx.cut["end"])
    frame = 1.0 / ctx.fps
    picked: list[model.Event] = []
    for event in ctx.events:
        if event.kind in ("enter", "key") and start + frame - 1e-6 <= event.t < end - frame:
            if any(abs(event.t - other.t) < 4 * frame for other in picked):
                continue
            picked.append(event)
    if ctx.cut.get("window"):
        return picked[:limit]  # a window's start is not a cut
    picked = picked[: limit - 1]
    picked.insert(0, model.Event(start, "cut", str(ctx.cut.get("clip_id") or ""), f"cut {ctx.cut['index']} in", "cut"))
    return picked[:limit]


def _nearest_word(ctx: LayerContext, t: float) -> model.Word | None:
    return min(ctx.words, key=lambda word: abs(word.start - t), default=None)


def _offset_note(ctx: LayerContext, t: float, *, context: bool = False) -> str:
    word = _nearest_word(ctx, t)
    if word is None or abs(word.start - t) > 0.6:
        return ""
    offset = t - word.start
    note = f'{abs(offset):.2f}s {"after" if offset >= 0 else "before"} "{word.text}"'
    if context:
        later = [w for w in ctx.words if w.start > word.start + 1e-6]
        if later:
            following = min(later, key=lambda w: w.start)
            note += f' (next "{following.text}" {following.start - t:+.2f}s)'
    return note


# ---------------------------------------------------------------- sync (data)

def render_sync(ctx: LayerContext) -> LayerResult:
    """One time axis: words, music beats/hits, sfx, element events and cuts, with offsets."""
    audio = ctx.shared.get("audio")
    # any app.data.* track (not the derived words/beats already drawn) gets its own lane
    extra = [t for t in ctx.tracks if not t.derived and t.kind in ("points", "intervals", "series", "boxes")][:6]
    rows = ([("VO words", 34), ("music", 26), ("sfx", 24)] + ([("audio", 34)] if audio is not None else [])
            + [(f"data:{t.name}", 26) for t in extra])
    event_rows = sorted({e.element for e in ctx.events if e.kind != "cut"})
    height = 30 + sum(h for _n, h in rows) + 22 * max(1, len(event_rows)) + 12
    image = ctx.panel(height, title="sync")
    draw = ImageDraw.Draw(image)
    y = 30
    findings: list[str] = []
    # words
    draw_text(draw, (10, y + 8), "VO words", 13, PALETTE["muted"])
    for index, word in enumerate(w for w in ctx.words if ctx.visible(w.start, w.end)):
        x0, x1 = ctx.x_of(word.start), max(ctx.x_of(word.end), ctx.x_of(word.start) + 2)
        draw.rectangle((x0, y + 2, x1, y + 30), fill=PALETTE["word"])
        label = word.text
        if text_width(draw, label, 12) <= x1 - x0 + 30:
            draw_text(draw, (x0 + 2, y + (4 if index % 2 == 0 else 16)), label, 12, PALETTE["word_ink"])
    y += rows[0][1]
    # music
    draw_text(draw, (10, y + 1), "music", 13, PALETTE["muted"])
    for t in ctx.beats.get("beats") or []:
        draw.line((ctx.x_of(t), y + 12, ctx.x_of(t), y + 24), fill=PALETTE["beat"], width=1)
    for t in ctx.beats.get("downbeats") or []:
        draw.line((ctx.x_of(t), y + 4, ctx.x_of(t), y + 24), fill=PALETTE["downbeat"], width=2)
    for t, kind in ctx.beats.get("hits") or []:
        x = ctx.x_of(t)
        draw.polygon(((x - 5, y + 2), (x + 5, y + 2), (x, y + 12)), fill=PALETTE["hit"])
        draw_text(draw, (x + 6, y), kind, 11, PALETTE["hit"])
    if not any(ctx.beats.get(key) for key in ("beats", "downbeats", "hits")):
        draw_text(draw, (ctx.plot_left + 4, y + 6),
                  "no beats: the music clip carries no app.beats (rebuild with the EDL builder) and no --beats was given",
                  11, PALETTE["muted"])
    elif ctx.beats.get("source"):
        draw_text(draw, (10, y + 15), ("beats: " + ", ".join(ctx.beats["source"]))[:26], 10, PALETTE["muted"])
    y += rows[1][1]
    # sfx
    draw_text(draw, (10, y + 5), "sfx", 13, PALETTE["muted"])
    for start, end, name in (item for item in ctx.sfx if ctx.visible(item[0], item[1])):
        x0 = ctx.x_of(start)
        draw.rectangle((x0, y + 4, max(ctx.x_of(end), x0 + 3), y + 18), fill=PALETTE["sfx"])
        draw_text(draw, (x0 + 5, y + 3), name, 11, PALETTE["ink"])
    y += rows[2][1]
    if audio is not None:
        from ..motion.audio import draw_lane

        draw_text(draw, (10, y + 2), "audio", 13, PALETTE["muted"])
        draw_text(draw, (10, y + 17), "music up · VO down", 10, PALETTE["muted"])
        draw_lane(draw, audio, x0=ctx.plot_left, x1=ctx.plot_right, y0=y + 2, y1=y + 32,
                  colours={"music": "#5b4a86", "vo": "#5fd4c4", "sfx": PALETTE["hit"]}, silence_colour=PALETTE["warn"])
        y += rows[3][1]
    for track in extra:
        _draw_track_lane(ctx, draw, track, y)
        y += 26
    # element events
    labels = {e.id: e.label for e in ctx.elements}
    labels.update({e.element: e.label for e in ctx.events if e.element not in labels})
    for row, element_id in enumerate(event_rows):
        ry = y + row * 22
        label = labels.get(element_id, element_id)
        draw_text(draw, (10, ry + 3), label[:20], 12, PALETTE["muted"])
        for event in (e for e in ctx.events if e.element == element_id):
            x = ctx.x_of(event.t)
            colour = PALETTE[{"enter": "enter", "exit": "exit"}.get(event.kind, "key")]
            if event.kind == "enter":
                draw.polygon(((x, ry + 2), (x + 9, ry + 9), (x, ry + 16)), fill=colour)
            elif event.kind == "exit":
                draw.polygon(((x, ry + 2), (x - 9, ry + 9), (x, ry + 16)), fill=colour)
            else:
                draw.polygon(((x, ry + 3), (x + 5, ry + 9), (x, ry + 15), (x - 5, ry + 9)), fill=colour)
        accents = [e for e in ctx.events if e.element == element_id and e.kind == "enter"
                   and float(ctx.cut["start"]) + 1 / ctx.fps < e.t < float(ctx.cut["end"])]
        for event in accents[:1]:
            note = _offset_note(ctx, event.t)
            if note:
                draw_text(draw, (ctx.x_of(event.t) + 12, ry + 3), note, 11, PALETTE["ink"])
    # findings: every accent's distance to the nearest word onset and music mark
    start, end = float(ctx.cut["start"]), float(ctx.cut["end"])
    marks = [(t, kind) for t, kind in ctx.beats.get("hits") or []] + [(t, "downbeat") for t in ctx.beats.get("downbeats") or []]
    for element in ctx.elements:
        if element.sequence or element.type in model.PLATE_TYPES:
            continue  # the picture (and a sequence's steps) is the cut, not an accent on it
        for event in model.accents(element, ctx.fps):
            if not start + 1 / ctx.fps <= event.t < end - 1 / ctx.fps:
                continue
            parts = [f"{event.label} {event.kind} {ctx.rel(event.t)} ({event.style})"]
            note = _offset_note(ctx, event.t, context=True)
            if note:
                parts.append(note)
            if marks:
                mark, kind = min(marks, key=lambda m: abs(m[0] - event.t))
                if abs(mark - event.t) <= 0.3:
                    parts.append(f"{event.t - mark:+.2f}s vs music {kind}")
            hit = min(ctx.sfx, key=lambda s: abs(s[0] - event.t), default=None)
            if hit is not None and abs(hit[0] - event.t) <= 0.3:
                parts.append(f"sfx {hit[2]} {hit[0] - event.t:+.2f}s")
            findings.append("TIME   " + "; ".join(parts))
    if audio is not None:
        for begin, finish in audio.silences("vo"):
            lo, hi = max(begin, start), min(finish, end)
            if hi - lo >= 0.4:
                music = audio.level_db("music", lo, hi)
                under = f"music under it at {music:.0f} dBFS" if music > -60 else "and no music: dead air"
                findings.append(f"AUDIO  no VO {ctx.rel(lo)}…{ctx.rel(hi)} ({hi - lo:.2f} s); {under}")
        for begin, finish in audio.dead_air():
            lo, hi = max(begin, start), min(finish, end)
            if hi - lo >= 0.3:
                findings.append(f"AUDIO  dead air {ctx.rel(lo)}…{ctx.rel(hi)} ({hi - lo:.2f} s): no VO, music or sfx")
        findings.extend(f"AUDIO  {note}" for note in audio.notes[:2])
    return LayerResult(image, findings[:12], "sync")


def _draw_track_lane(ctx: LayerContext, draw, track, y: float) -> None:
    """One lane for a data track: points as ticks, intervals as bars, a series as a line, boxes as ticks."""
    label = track.name + (f" ({track.units})" if track.units else "")
    draw_text(draw, (10, y + 4), label[:22], 12, PALETTE["muted"])
    colour = SERIES[sum(map(ord, track.name)) % len(SERIES)]
    if track.kind == "points":
        for t, text in track.points:
            if ctx.visible(t):
                x = ctx.x_of(t)
                draw.line((x, y + 3, x, y + 21), fill=colour, width=2)
                if text:
                    draw_text(draw, (x + 3, y + 3), text[:14], 10, colour)
    elif track.kind == "intervals":
        for t0, t1, text in track.intervals:
            if ctx.visible(t0, t1):
                draw.rectangle((ctx.x_of(t0), y + 6, max(ctx.x_of(t0) + 2, ctx.x_of(t1)), y + 20), fill=colour)
                if text:
                    draw_text(draw, (ctx.x_of(t0) + 2, y + 6), text[:14], 10, PALETTE["bg"])
    elif track.kind == "series":
        values = [(t, v) for t, v in track.series if ctx.visible(t) and math.isfinite(v)]
        if values:
            low, high = min(v for _t, v in values), max(v for _t, v in values)
            span = (high - low) or 1.0
            points = [(ctx.x_of(t), y + 22 - (v - low) / span * 18) for t, v in values]
            draw.line(points, fill=colour, width=2) if len(points) > 1 else None
            draw_text(draw, (ctx.plot_right - 120, y + 2), f"{low:.1f}…{high:.1f}", 10, PALETTE["muted"])
    else:
        for t, *_rest in track.boxes:
            if t is not None and ctx.visible(t):
                draw.line((ctx.x_of(t), y + 8, ctx.x_of(t), y + 18), fill=colour, width=1)


# ---------------------------------------------------------------- curves (data)

# plotted first when an element moves more than four ways
CURVE_ORDER = ("zoom", "x", "y", "pan_x", "pan_y", "mouth", "reveal", "connector", "value", "step", "blink", "visible")


def _zoom_finding(element, values, ctx: LayerContext) -> str | None:
    """``zoom 1.00→1.35 push over 3.33 s from +0.00`` / ``zoom punch ×2 (+1 for 6 frames)``."""
    changes = [t for (t, v), (_t2, v2) in zip(values, values[1:]) if abs(v2 - v) > 1e-6]
    if not changes:
        return None
    first, last = values[0][1], values[-1][1]
    source = "element maths" if element.maths is not None and element.maths.note is None else "visualize mirror"
    if abs(last - first) < 1e-6:
        rises = sum(1 for (_t, v), (_t2, v2) in zip(values, values[1:]) if v2 > v + 1e-6)
        return f"CURVE  {element.label} zoom punch ×{rises} from {ctx.rel(changes[0])} ({source})"
    span = changes[-1] - changes[0] + 1 / ctx.fps
    kind = "push" if len(changes) > 2 else "zoom cut"
    return (f"CURVE  {element.label} zoom {first:.2f}→{last:.2f} {kind} over {span:.2f} s from "
            f"{ctx.rel(changes[0])} ({source})")


def render_curves(ctx: LayerContext) -> LayerResult:
    """Each element's animated properties per frame: the element's own motion.ts, else the visualize model."""
    elements = [e for e in ctx.elements if not e.audio and not e.sequence]
    rows = []
    start, end = ctx.window
    frames = range(int(math.floor(start * ctx.fps)), int(math.ceil(end * ctx.fps)))
    for element in elements:
        samples = [(f / ctx.fps, model.props_at(element, f / ctx.fps + 1e-6, ctx.fps)) for f in frames]
        keys = sorted({k for _t, props in samples for k in props})
        moving = [k for k in keys if len({round(props.get(k, math.nan), 3) for _t, props in samples if k in props}) > 1]
        moving.sort(key=lambda k: (CURVE_ORDER.index(k) if k in CURVE_ORDER else len(CURVE_ORDER), k))
        rows.append((element, samples, moving or (["visible"] if "visible" in keys else [])))
    # one row per sequence: which step is on screen (its steps are one picture cut)
    sequences: dict[str, list] = {}
    for element in ctx.elements:
        if element.sequence and not element.audio:
            sequences.setdefault(element.sequence, []).append(element)
    for name, steps in sequences.items():
        steps.sort(key=lambda e: e.start)
        lead = model.Element(f"sequence:{name}", f"sequence ({len(steps)} steps)", steps[0].track, steps[0].start,
                             steps[-1].end, {}, {})
        samples = []
        for f in frames:
            t = f / ctx.fps + 1e-6
            index = next((i for i, step in enumerate(steps) if step.start <= t < step.end), None)
            samples.append((t, {"step": float(index + 1)} if index is not None else {}))
        rows.append((lead, samples, ["step"]))
        holds = [round(step.end - step.start, 3) for step in steps]
        findings_steps = (f"CURVE  sequence {name}: {len(steps)} steps over {steps[-1].end - steps[0].start:.2f} s, "
                          f"{min(holds) * ctx.fps:.0f}–{max(holds) * ctx.fps:.0f} frames each")
        ctx.shared.setdefault("sequence_findings", []).append(findings_steps)
    row_h = 40
    image = ctx.panel(30 + row_h * max(1, len(rows)) + 8, title="curves (from data)")
    draw = ImageDraw.Draw(image)
    findings = []
    for index, (element, samples, keys) in enumerate(rows):
        top = 30 + index * row_h
        draw.line((ctx.plot_left, top + row_h - 2, ctx.plot_right, top + row_h - 2), fill=PALETTE["grid"])
        draw_text(draw, (10, top + 4), element.label[:20], 12, PALETTE["ink"])
        draw_text(draw, (10, top + 20), ",".join(keys)[:22], 11, PALETTE["muted"])
        for series_index, key in enumerate(keys[:4]):
            values = [(t, props[key]) for t, props in samples if key in props]
            if not values:
                continue
            low, high = min(v for _t, v in values), max(v for _t, v in values)
            span = (high - low) or 1.0
            colour = SERIES[series_index % len(SERIES)]
            points = []
            for t, v in values:
                y = top + row_h - 6 - (v - low) / span * (row_h - 12) if high != low else top + row_h / 2
                points.append((ctx.x_of(t), y))
            # step drawing: horizontal then vertical, so stepped motion reads as stairs
            for (x0, y0), (x1, y1) in zip(points, points[1:]):
                draw.line((x0, y0, x1, y0), fill=colour, width=2)
                if y1 != y0:
                    draw.line((x1, y0, x1, y1), fill=colour, width=1)
            changes = sum(1 for (_x0, y0), (_x1, y1) in zip(points, points[1:]) if abs(y1 - y0) > 0.01)
            distance = high - low
            if key == "zoom":
                line = _zoom_finding(element, values, ctx)
                findings.extend([line] if line else [])
            # the 1-logical-px landing of a stamp is not travel; a pan that follows a zoom is the zoom
            if key in ("pan_x", "pan_y") and "zoom" in keys:
                continue
            if key in ("x", "y", "pan_x", "pan_y") and changes and distance > 2 * model.LOGICAL_PX:
                moving_frames = [t for (t, v), (_t2, v2) in zip(values, values[1:]) if v2 != v]
                span = (max(moving_frames) - min(moving_frames)) if len(moving_frames) > 1 else 0.0
                findings.append(f"CURVE  {element.label} {key}: {distance:.0f} px in {changes} steps over "
                                f"{span + 1 / ctx.fps:.2f} s from {ctx.rel(min(moving_frames))} "
                                f"({'stepped' if changes <= 12 else 'continuous'}; no easing)" if element.type == "am-sprite"
                                else f"CURVE  {element.label} {key}: {distance:.0f} px in {changes} steps from "
                                f"{ctx.rel(min(moving_frames))}")
        if not keys:
            draw_text(draw, (ctx.plot_left + 4, top + 12), "static (no animated property in its model)", 11, PALETTE["muted"])
    findings.extend(ctx.shared.pop("sequence_findings", []))
    stamps = [e.label for e in elements if e.type == "am-sprite" and (e.params.get("enter") == "stamp")
              and float(ctx.cut["start"]) + 1 / ctx.fps < e.start < float(ctx.cut["end"])]
    if stamps:
        findings.append(f"CURVE  {len(stamps)} stamp entrance{'s' if len(stamps) > 1 else ''} (2-frame flicker, 1 px land): "
                        f"{', '.join(label.split(' ', 1)[-1] for label in stamps[:8])}")
    static = [e.label for e, _s, keys in rows if keys in ([], ["visible"])]
    if static:
        findings.append(f"CURVE  static over the cut: {', '.join(static[:6])}{' …' if len(static) > 6 else ''}")
    notes = {e.maths.note for e in elements if e.maths is not None and e.maths.note}
    if notes:
        findings.insert(0, f"CURVE  element maths unavailable ({sorted(notes)[0]}); these curves are the visualize mirror")
    return LayerResult(image, findings[:8], "curves")


# ---------------------------------------------------------------- lipsync (data + pixels)

def render_lipsync(ctx: LayerContext) -> LayerResult:
    """Presenter mouth state per frame vs VO word intervals (and the presenter's own word list)."""
    presenters = [e for e in ctx.elements if e.type == "am-presenter"]
    if not presenters:
        return LayerResult(None, [], "lipsync")
    image = ctx.panel(30 + 70 * len(presenters), title="lip-sync")
    draw = ImageDraw.Draw(image)
    findings = []
    start, end = float(ctx.cut["start"]), float(ctx.cut["end"])
    for index, presenter in enumerate(presenters):
        top = 30 + index * 70
        draw_text(draw, (10, top + 4), "mouth", 12, PALETTE["muted"])
        draw_text(draw, (10, top + 26), "VO words", 12, PALETTE["muted"])
        draw_text(draw, (10, top + 48), "presenter words", 12, PALETTE["muted"])
        word_frames = moving_in_words = open_in_silence = 0
        first = int(math.ceil(max(start, presenter.start) * ctx.fps))
        last = int(math.floor(min(end, presenter.end) * ctx.fps))
        vo = [w for w in ctx.words if w.end > start and w.start < end]
        for frame in range(first, last):
            t = frame / ctx.fps
            state = model.mouth_state(presenter, frame - int(round(presenter.start * ctx.fps)), ctx.fps)
            colour = {"closed": "#334155", "half": "#f59e0b", "open": "#ef4444"}[state]
            x0, x1 = ctx.x_of(t), ctx.x_of(t + 1 / ctx.fps)
            draw.rectangle((x0, top + 2, max(x0 + 1, x1 - 1), top + 20), fill=colour)
            speaking = any(w.start <= t < w.end for w in vo)
            if speaking:
                word_frames += 1
                moving_in_words += state != "closed"
            elif state != "closed" and not any(w.start - 0.12 <= t < w.end + 0.12 for w in vo):
                open_in_silence += 1
        for word in vo:
            if ctx.visible(word.start, word.end):
                draw.rectangle((ctx.x_of(word.start), top + 26, ctx.x_of(word.end), top + 42), fill=PALETTE["word"])
        own = [(presenter.start + s, presenter.start + e) for s, e in model.presenter_words(presenter)]
        for s, e in own:
            if ctx.visible(s, e):
                draw.rectangle((ctx.x_of(s), top + 48, ctx.x_of(e), top + 64), fill="#475569")
        name = presenter.short_id
        if word_frames:
            findings.append(f"LIPSYNC {name}: mouth moves in {moving_in_words / word_frames:.0%} of {word_frames} speaking frames; "
                            f"{open_in_silence} frames open in silence")
        else:
            findings.append(f"LIPSYNC {name}: no VO words under this cut; mouth open {open_in_silence} frames")
        pairs = []
        for s, _e in own:
            near = min(vo, key=lambda w: abs(w.start - s), default=None)
            if near is not None and abs(near.start - s) < 0.3:
                pairs.append(s - near.start)
        if pairs:
            mean = sum(pairs) / len(pairs)
            if abs(mean) > 1.5 / ctx.fps:
                findings.append(f"LIPSYNC {name}: presenter words {'lead' if mean < 0 else 'lag'} the VO by {abs(mean):.2f} s "
                                f"(shift params.words by {-mean:+.2f} s)")
            else:
                findings.append(f"LIPSYNC {name}: presenter words match VO onsets (mean {mean:+.3f} s, {len(pairs)} words)")
        elif own or vo:
            findings.append(f"LIPSYNC {name}: presenter has {len(own)} words, VO has {len(vo)} under the cut; no pairs within 0.3 s")
    # pixel check: does the mouth region actually change in the captured frames?
    series = [f for f in _sorted_frames(ctx) if start <= f / ctx.fps < end]
    presenter = presenters[0]
    box = model.mouth_box(presenter, 0)
    if box and len(series) >= 3:
        sx, sy = ctx.frame_size[0] / model.CANVAS[0], ctx.frame_size[1] / model.CANVAS[1]
        crop = (max(0, int(box[0] * sx)), max(0, int(box[1] * sy)), int(box[2] * sx) + 1, int(box[3] * sy) + 1)
        changes = 0
        previous = None
        for frame in series:
            image_frame = ctx.frame_image(frame)
            region = image_frame.crop(crop) if image_frame else None
            if region is not None and previous is not None:
                fraction, _mask = _diff_fraction(previous, region)
                changes += fraction > 0.02
            previous = region
        findings.append(f"LIPSYNC pixels: mouth region changed between {changes} of {len(series) - 1} sampled frame pairs")
    return LayerResult(image, findings, "lipsync")


# ---------------------------------------------------------------- strip (pixels)

def _strip_groups(ctx: LayerContext) -> list[list[int]]:
    """Sampled frames grouped so a run of identical frames shows once (a hold)."""
    frozen_pairs = {(item["a"], item["b"]) for item in _motion_series(ctx) if item["fraction"] < FROZEN_FRACTION}
    groups: list[list[int]] = []
    for frame in _sorted_frames(ctx):
        if groups and (groups[-1][-1], frame) in frozen_pairs:
            groups[-1].append(frame)
        else:
            groups.append([frame])
    # a "group" of two identical frames is not worth collapsing
    out: list[list[int]] = []
    for group in groups:
        if len(group) >= 3:
            out.append(group)
        else:
            out.extend([frame] for frame in group)
    return out


def render_strip(ctx: LayerContext) -> LayerResult:
    """Sampled frames in time order; runs of identical frames collapse into one tile marked HOLD."""
    groups = _strip_groups(ctx)
    if not groups:
        return LayerResult(None, ["STRIP  no frames captured"], "strip", page="frames")
    columns = int(ctx.shared.get("columns") or 8)
    thumb_w = int(ctx.shared.get("tile_w") or (ctx.width - 20 - (columns - 1) * 8) // columns)
    thumb_h = int(thumb_w * ctx.frame_size[1] / ctx.frame_size[0])
    labels = ctx.shared.get("frame_labels") or {}
    rows = math.ceil(len(groups) / columns)
    cell_h = thumb_h + 34
    image = Image.new("RGB", (ctx.width, 30 + rows * cell_h), PALETTE["bg"])
    draw = ImageDraw.Draw(image)
    total = sum(len(group) for group in groups)
    window = ctx.shared.get("window") or {}
    how = {"scan": "every 0.5 s", "motion": "every 2nd frame", "beat": "at each word, hit and sfx",
           "frame": "one frame"}.get(window.get("preset"), "dense after each event")
    draw_text(draw, (10, 6), f"frames ({total} sampled {how}; identical runs collapse to one HOLD tile; "
              "times relative to the window start)", 15, PALETTE["ink"])
    event_frames: dict[int, list[model.Event]] = {}
    for event in ctx.events:
        event_frames.setdefault(int(round(event.t * ctx.fps)), []).append(event)
    bounds = "bounds" in ctx.shared.get("layer_names", ())
    labels_config = ctx.shared.get("config") if "labels" in ctx.shared.get("layer_names", ()) else None
    cut_frame = int(round(float(ctx.cut["start"]) * ctx.fps))
    findings = []
    for index, group in enumerate(groups):
        frame = group[0]
        x = 10 + (index % columns) * (thumb_w + 8)
        y = 30 + (index // columns) * cell_h
        source = ctx.frame_image(frame)
        if source is None:
            continue
        thumb = source.resize((thumb_w, thumb_h), Image.LANCZOS)
        if bounds:
            thumb = overlay_bounds(thumb, ctx.elements, frame / ctx.fps, ctx.fps, labels=False,
                                   highlight=ctx.shared.get("highlight"))
        if labels_config:
            from ..overlays.labels import overlay_labels_frame

            thumb = overlay_labels_frame(thumb, labels_config, frame, ctx.fps)
        image.paste(thumb, (x, y))
        t = frame / ctx.fps
        inside = float(ctx.cut["start"]) <= t < float(ctx.cut["end"])
        colour = PALETTE["ink"] if inside else PALETTE["muted"]
        if len(group) > 1:
            t_end = group[-1] / ctx.fps
            draw.rectangle((x, y, x + thumb_w - 1, y + thumb_h - 1), outline=PALETTE["bad"], width=3)
            draw_text(draw, (x + 2, y + thumb_h + 2), f"HOLD {ctx.rel(t)}…{ctx.rel(t_end)} ({len(group)} same)", 12, PALETTE["bad"])
            if inside and t_end - t >= 0.5:
                findings.append(f"STRIP  identical frames {ctx.rel(t)}…{ctx.rel(t_end)} ({t_end - t:.2f} s, {len(group)} samples)")
        else:
            frame_label = f"f{frame}" if ctx.cut.get("window") else f"f{frame - cut_frame:+d}"
            draw_text(draw, (x + 2, y + thumb_h + 2), f"{ctx.rel(t)} {frame_label}", 12, colour)
        tags = [event for member in group for event in event_frames.get(member, [])]
        label = next((labels[member] for member in group if member in labels), None)
        if label:
            draw.rectangle((x, y, x + thumb_w - 1, y + thumb_h - 1), outline=PALETTE["hit"], width=2)
            draw_text(draw, (x + 2, y + thumb_h + 17), label[:max(8, thumb_w // 7)], 11, PALETTE["hit"])
        elif tags:
            if len(group) == 1:
                draw.rectangle((x, y, x + thumb_w - 1, y + thumb_h - 1), outline=PALETTE["enter"], width=2)
            draw_text(draw, (x + 2, y + thumb_h + 17), _event_label(tags[0])[:30], 11, PALETTE["enter"])
    return LayerResult(image, findings[:3], "strip", page="frames")


# ---------------------------------------------------------------- onion (pixels)

ONION_OFFSETS = (-2, -1, 0, 1, 2, 4, 6)
ONION_COLOURS = ("#3b82f6", "#06b6d4", "#22c55e", "#a3e635", "#facc15", "#f97316", "#ef4444")


def onion_composite(ctx: LayerContext, event_frame: int, size: tuple[int, int]) -> tuple[Image.Image | None, list[int]]:
    """Frames t-2…t+6 over a dimmed settled frame: changed pixels tinted by time, outlined per frame."""
    cut_start = int(round(float(ctx.cut["start"]) * ctx.fps))
    floor = cut_start if event_frame >= cut_start else 0
    available = [event_frame + o for o in ONION_OFFSETS if event_frame + o in ctx.frames and event_frame + o >= floor]
    if len(available) < 2:
        return None, available
    reference = ctx.frame_image(min(available))
    final = ctx.frame_image(max(available))
    base = Image.blend(final.convert("L").convert("RGB"), Image.new("RGB", final.size, "#000000"), 0.55)
    outlines = []
    for index, frame in enumerate(available):
        image = ctx.frame_image(frame)
        _fraction, mask = _diff_fraction(reference, image)
        if frame == min(available):
            continue
        mask = mask.filter(ImageFilter.MaxFilter(3))
        offset_index = ONION_OFFSETS.index(frame - event_frame)
        alpha = 0.35 + 0.65 * (index / max(1, len(available) - 1))
        tint = Image.blend(image, Image.new("RGB", image.size, ONION_COLOURS[offset_index]), 0.35)
        base = Image.composite(Image.blend(base, tint, alpha), base, mask)
        edge = ImageChops.subtract(mask.filter(ImageFilter.MaxFilter(3)), mask)
        outlines.append((edge, ONION_COLOURS[offset_index]))
    for edge, colour in outlines:
        base.paste(Image.new("RGB", base.size, colour), (0, 0), edge)
    return base.resize(size, Image.LANCZOS), available


def render_onion(ctx: LayerContext) -> LayerResult:
    """One onion skin per event: where the element was on each frame from t-2 to t+6."""
    events = _focus_events(ctx)
    count = max(1, len(events))
    cell_w = (ctx.width - 20 - (count - 1) * 10) // count
    cell_w = min(cell_w, 520)
    cell_h = int(cell_w * ctx.frame_size[1] / ctx.frame_size[0])
    image = Image.new("RGB", (ctx.width, 30 + cell_h + 46 + 22), PALETTE["bg"])
    draw = ImageDraw.Draw(image)
    draw_text(draw, (10, 6), "onion skins: changed pixels per frame, blue = t-2f … red = t+6f, over the settled frame", 15, PALETTE["ink"])
    findings = []
    for index, event in enumerate(events):
        frame = int(round(event.t * ctx.fps))
        composite, used = onion_composite(ctx, frame, (cell_w, cell_h))
        x = 10 + index * (cell_w + 10)
        if composite is None:
            draw.rectangle((x, 30, x + cell_w, 30 + cell_h), outline=PALETTE["grid_strong"])
            draw_text(draw, (x + 6, 36), "not enough frames", 12, PALETTE["muted"])
            continue
        image.paste(composite, (x, 30))
        draw_text(draw, (x, 34 + cell_h), f"{ctx.rel(event.t)} {_event_label(event)}"[:48], 12, PALETTE["ink"])
        draw_text(draw, (x, 50 + cell_h), f"frames {', '.join(f'{u - frame:+d}' for u in used)}", 11, PALETTE["muted"])
        # how far did the changed region travel, and over how many frames did it settle?
        reference = ctx.frame_image(min(used))
        boxes = []
        for frame_used in used[1:]:
            fraction, mask = _diff_fraction(reference, ctx.frame_image(frame_used))
            boxes.append((frame_used - frame, fraction, mask.getbbox()))
        sequence = " ".join(f"{offset:+d}:{fraction:.1%}" for offset, fraction, _box in boxes)
        findings.append(f"ONION  {_event_label(event)} {ctx.rel(event.t)} changed area by frame {sequence}")
    for i, colour in enumerate(ONION_COLOURS):
        draw.rectangle((10 + i * 60, 66 + cell_h, 30 + i * 60, 80 + cell_h), fill=colour)
        draw_text(draw, (34 + i * 60, 66 + cell_h), f"{ONION_OFFSETS[i]:+d}", 11, PALETTE["muted"])
    return LayerResult(image, findings[:4], "onion", page="frames")


# ---------------------------------------------------------------- diff (pixels)

def render_diff(ctx: LayerContext) -> LayerResult:
    """Where pixels change (heat over the cut) and when (changed area between sampled frames)."""
    series = _motion_series(ctx)
    frames = _sorted_frames(ctx)
    start, end = float(ctx.cut["start"]), float(ctx.cut["end"])
    inside = [f for f in frames if start <= f / ctx.fps < end]
    height = 30 + 110
    image = ctx.panel(height, title="pixel change")
    draw = ImageDraw.Draw(image)
    findings = []
    top = 30
    for item in series:
        x0, x1 = ctx.x_of(item["a"] / ctx.fps), ctx.x_of(item["b"] / ctx.fps)
        value = item["fraction"]
        # log scale: 0.01 % … 100 % of the frame, so a mouth flap and a cut both read
        bar = 0.0 if value <= 0 else min(1.0, max(0.0, (math.log10(value) + 4) / 4))
        colour = PALETTE["bad"] if value < FROZEN_FRACTION else PALETTE["warn"] if value < NEAR_STILL_FRACTION else PALETTE["good"]
        draw.rectangle((x0, top + 100 - max(2, bar * 96), max(x0 + 1, x1 - 1), top + 100), fill=colour)
    for label, value in (("100%", 1.0), ("1%", 0.01), ("0.01%", 0.0001)):
        y = top + 100 - (math.log10(value) + 4) / 4 * 96
        draw.line((ctx.plot_left - 6, y, ctx.plot_left, y), fill=PALETTE["muted"])
        draw_text(draw, (ctx.plot_left - 44, y - 7), label, 10, PALETTE["muted"])
    draw_text(draw, (10, top + 8), "changed area", 11, PALETTE["muted"])
    draw_text(draw, (10, top + 22), "(log scale)", 11, PALETTE["muted"])
    draw_text(draw, (10, top + 44), "red = frozen", 11, PALETTE["bad"])
    draw_text(draw, (10, top + 58), "amber = mouth-size", 11, PALETTE["warn"])
    # spatial heat: max change across the cut, over the middle frame
    if len(inside) >= 2:
        heat = Image.new("L", ctx.frame_size, 0)
        previous = None
        for frame in inside:
            current = ctx.frame_image(frame)
            if previous is not None:
                heat = ImageChops.lighter(heat, ImageChops.difference(previous, current).convert("L"))
            previous = current
        mid = ctx.frame_image(inside[len(inside) // 2])
        base = Image.blend(mid.convert("L").convert("RGB"), Image.new("RGB", mid.size, "#000000"), 0.5)
        red = Image.new("RGB", mid.size, "#ff3b30")
        mask = heat.point(lambda v: 0 if v <= CHANGE_LEVEL else min(255, 80 + v * 2))
        heat_image = Image.composite(red, base, mask)
        ctx.shared["heat_image"] = heat_image
        box = heat.point(lambda v: 255 if v > CHANGE_LEVEL else 0).getbbox()
        if box:
            w, h = ctx.frame_size
            findings.append(f"DIFF   pixels change inside x {box[0] / w:.0%}–{box[2] / w:.0%}, y {box[1] / h:.0%}–{box[3] / h:.0%} "
                            f"of the frame over the cut")
        else:
            findings.append("DIFF   no pixel changes at all inside the cut (sampled frames identical)")
    return LayerResult(image, findings, "diff")


def render_heat(ctx: LayerContext) -> LayerResult:
    heat = ctx.shared.get("heat_image")
    if heat is None:
        render_diff(ctx)
        heat = ctx.shared.get("heat_image")
    if heat is None:
        return LayerResult(None, [], "heat", page="frames")
    width = min(ctx.width - 20, 640)
    height = int(width * heat.size[1] / heat.size[0])
    image = Image.new("RGB", (ctx.width, 30 + height + 8), PALETTE["bg"])
    draw = ImageDraw.Draw(image)
    draw_text(draw, (10, 6), "where pixels changed during the cut (red), over its middle frame", 15, PALETTE["ink"])
    image.paste(heat.resize((width, height), Image.LANCZOS), (10, 30))
    return LayerResult(image, [], "heat", page="frames")


# ---------------------------------------------------------------- still (pixels)

def render_still(ctx: LayerContext) -> LayerResult:
    """Holds: share of the cut with ~zero pixel change and the longest frozen run."""
    series = [s for s in _motion_series(ctx) if float(ctx.cut["start"]) * ctx.fps - 1e-6 <= s["a"] and s["b"] <= float(ctx.cut["end"]) * ctx.fps + 1e-6]
    image = ctx.panel(30 + 34, title="stillness")
    draw = ImageDraw.Draw(image)
    if not series:
        return LayerResult(image, ["STILL  not enough frames inside the cut"], "still")
    covered = frozen = near = 0.0
    runs: list[tuple[float, float, str]] = []
    current: list[float] | None = None
    for item in series:
        seconds = (item["b"] - item["a"]) / ctx.fps
        covered += seconds
        state = "frozen" if item["fraction"] < FROZEN_FRACTION else "near" if item["fraction"] < NEAR_STILL_FRACTION else "moving"
        x0, x1 = ctx.x_of(item["a"] / ctx.fps), ctx.x_of(item["b"] / ctx.fps)
        colour = {"frozen": PALETTE["bad"], "near": PALETTE["warn"], "moving": PALETTE["good"]}[state]
        draw.rectangle((x0, 34, max(x0 + 1, x1), 56), fill=colour)
        if state == "frozen":
            frozen += seconds
        if state == "near":
            near += seconds
        if state != "moving":
            if current is None:
                current = [item["a"] / ctx.fps, item["b"] / ctx.fps]
            else:
                current[1] = item["b"] / ctx.fps
        elif current is not None:
            runs.append((current[0], current[1], "hold"))
            current = None
    if current is not None:
        runs.append((current[0], current[1], "hold"))
    longest = max(runs, key=lambda r: r[1] - r[0], default=None)
    draw_text(draw, (10, 30), "red frozen", 11, PALETTE["bad"])
    draw_text(draw, (10, 44), "amber mouth-only", 11, PALETTE["warn"])
    spacing = covered / max(1, len(series))
    deliberate = bool(ctx.cut.get("deliberate_hold"))
    line = (f"STILL  cut {ctx.cut['index']}: frozen {frozen / covered:.0%}, mouth/eye-only {near / covered:.0%} "
            f"of {covered:.2f} s (sampled every ~{spacing:.2f} s)")
    findings = [line]
    if longest is not None and longest[1] - longest[0] >= 1.0:
        flag = "deliberate_hold" if deliberate else "NOT marked deliberate_hold"
        findings.append(f"STILL  longest hold {longest[1] - longest[0]:.2f} s at {ctx.rel(longest[0])}…{ctx.rel(longest[1])} ({flag})")
    return LayerResult(image, findings, "still")


# ---------------------------------------------------------------- lint (data)

def render_lint(ctx: LayerContext) -> LayerResult:
    """Every registered check for this cut (``timelines lint``'s, plus checks that need the captured frames)."""
    from ..motion.conditions import run_checks

    rules = ctx.shared.get("rules") or {}
    per_cut, _timeline = run_checks(
        [ctx.cut], ctx.all_elements, ctx.fps, track_order=ctx.shared.get("track_order", ()),
        beats=ctx.shared.get("beats_override"), params=rules.get("params"), severity=rules.get("severity"),
        needs={"doc", "frames"}, frames=ctx.frames, all_cuts=ctx.cuts, shared=ctx.shared,
        disable=rules.get("disable") or (),
    )
    findings = per_cut[0][1] if per_cut else []
    rank = {"error": 0, "warn": 1, "info": 2}
    lines = [f.line(float(ctx.cut["start"])) for f in sorted(findings, key=lambda f: (rank.get(f.severity, 3), f.t or 0))]
    ctx.shared["check_findings"] = [f.as_dict() for f in findings]
    from .base import broken_line

    head = [broken_line()] if broken_line() else []
    return LayerResult(None, head + (lines or [f"LINT   cut {ctx.cut['index']}: no findings from the registered checks"]),
                       "lint")


def _frozen(ctx) -> list:
    """Pixel holds inside the cut longer than max_frozen_s, unless the picture is a deliberate hold."""
    limit = ctx.param("max_frozen_s")
    if limit is None or not ctx.frames or not ctx.cut or ctx.cut.get("deliberate_hold"):
        return []
    start, end = float(ctx.cut["start"]) * ctx.fps, float(ctx.cut["end"]) * ctx.fps
    frames = sorted(f for f in ctx.frames if start - 1e-6 <= f < end)
    runs, begin, previous = [], None, None
    for frame in frames:
        with Image.open(ctx.frames[frame]) as source:
            image = source.convert("RGB")
        if previous is not None:
            fraction, _mask = _diff_fraction(previous[1], image)
            if fraction < FROZEN_FRACTION:
                begin = previous[0] if begin is None else begin
                runs.append((begin, frame))
            else:
                begin = None
        previous = (frame, image)
    longest = max(((b - a) / ctx.fps, a) for a, b in runs) if runs else (0.0, 0)
    if longest[0] <= float(limit):
        return []
    t = longest[1] / ctx.fps
    return [ctx.finding("FROZEN", f"{longest[0]:.2f} s with no pixel change from {t - float(ctx.cut['start']):+.2f}s "
                        f"(max_frozen_s = {float(limit):g}; add motion or mark app.astrid.deliberate_hold: true)", t=t,
                        fix={"clip": str(ctx.cut.get('clip_id') or '').rsplit(':', 1)[-1],
                             "set": {"app.astrid.deliberate_hold": True}, "alternative": "add motion"})]


FROZEN_CHECK = Check("frozen", "pixel holds longer than max_frozen_s in the captured frames (motion view)",
                     _frozen, params={"max_frozen_s": 1.5}, needs=("frames",), codes=("FROZEN",))


# ---------------------------------------------------------------- bounds (overlay)

BOUND_COLOURS = {"face": "#ef4444", "text": "#facc15", "card": "#38bdf8", "sprite": "#4ade80", "panel": "#c084fc"}


def overlay_bounds(image: Image.Image, elements: Sequence[model.Element], t: float, fps: float, *, labels: bool = True,
                   highlight: str | None = None) -> Image.Image:
    """Declared element bounds plus title/action-safe margins, drawn on a frame of any size.

    ``highlight`` (a clip id) outlines that one layer thick and labelled; the others go thin and grey."""
    out = image.copy()
    draw = ImageDraw.Draw(out)
    sx, sy = out.size[0] / model.CANVAS[0], out.size[1] / model.CANVAS[1]

    def scaled(rect):
        return (rect[0] * sx, rect[1] * sy, rect[2] * sx, rect[3] * sy)

    for fraction, colour in ((model.ACTION_SAFE, "#64748b"), (model.TITLE_SAFE, "#94a3b8")):
        draw.rectangle(scaled(model.safe_rect(fraction)), outline=colour, width=1)
    for element in elements:
        chosen = bool(highlight) and (str(element.id) == highlight or str(element.id).endswith(":" + highlight)
                                      or str(element.id).endswith(highlight))
        for box in model.boxes_at(element, t, fps):
            if box.kind == "plate":
                continue
            if highlight:
                colour = "#ff7a2e" if chosen else "#64748b"
                draw.rectangle(scaled(box.rect), outline=colour, width=3 if chosen else 1)
                if chosen:
                    x, y = max(0, box.rect[0] * sx + 2), max(0, box.rect[1] * sy - 12)
                    draw_text(draw, (x, y), highlight[-24:], 11, colour)
                continue
            colour = BOUND_COLOURS.get(box.kind, "#ffffff")
            draw.rectangle(scaled(box.rect), outline=colour, width=2 if box.kind == "face" else 1)
            if labels:
                name = "face" if box.kind == "face" else box.label.split(" ", 1)[-1][:14]
                x, y = max(0, box.rect[0] * sx + 2), max(0, box.rect[1] * sy + 1)
                draw_text(draw, (x, y), name, 10, colour)
    return out


def render_bounds(ctx: LayerContext) -> LayerResult:
    """A marker layer: the strip and contact tiles draw bounds when it is selected."""
    return LayerResult(None, [], "bounds", page="frames")


def render_labels(ctx: LayerContext) -> LayerResult:
    """A marker layer: the strip and contact tiles draw visible layer addresses when selected."""
    return LayerResult(None, [], "labels", page="frames")


# ---------------------------------------------------------------- registry

register(Layer("sync", "words, music beats/hits, sfx, element events and cuts on one axis, with offsets",
               render_sync, needs=("doc", "words", "beats"), order=10))
register(Layer("curves", "x/y/scale/reveal/mouth of every element per frame, from the element models",
               render_curves, needs=("doc",), order=20))
register(Layer("still", "holds: share of the cut with ~zero pixel change, longest frozen run",
               render_still, needs=("frames",), order=30, checks=(FROZEN_CHECK,)))
register(Layer("diff", "where and when pixels change between sampled frames",
               render_diff, needs=("frames",), order=35))
register(Layer("lipsync", "presenter mouth state per frame vs VO words; mouth-region pixel check",
               render_lipsync, needs=("doc", "words", "frames"), order=40))
register(Layer("lint", "composition (face box, title-safe, text size, covering) and timing checks for this cut",
               render_lint, needs=("doc", "words", "beats"), order=50))
register(Layer("strip", "every sampled frame across the cut, dense after each entrance",
               render_strip, needs=("frames",), order=60, frame_budget=0))
register(Layer("onion", "onion skin per entrance/keyframe: frames t-2…t+6 tinted by time",
               render_onion, needs=("frames",), order=70, frame_budget=0))
register(Layer("heat", "spatial heat of pixel change over the cut's middle frame",
               render_heat, needs=("frames",), order=80, experimental=True))
register(Layer("bounds", "declared element bounds, presenter face box and safe areas over frames/tiles (opt-in)",
               render_bounds, needs=("doc",), views=("motion", "contact"), order=90, default=False))
register(Layer("labels", "canonical address (c08.loras) and box of every visible layer on the tiles (opt-in)",
               render_labels, needs=("doc", "frames"), views=("motion", "contact"), order=92, default=False))
