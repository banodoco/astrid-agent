"""``--view motion``: plan a bounded frame set for one cut and compose its motion sheet.

The sheet is two PNG pages an agent can read as stills:

- ``motion-cut-NN.png``: time-aligned data panels (sync, curves, stillness,
  pixel change, lip-sync) sharing one time axis, plus every finding as text;
- ``motion-cut-NN-frames.png``: the frame strip and onion skins.

``findings.txt`` carries the same findings, one line each.
"""
from __future__ import annotations

import math
from fractions import Fraction
from pathlib import Path
from typing import Any, Mapping, Sequence

from PIL import Image, ImageDraw

from astrid.core.timeline.cuts import find_cut, occurrences_from_snapshot, picture_cuts

from ..layers.base import PALETTE, LayerContext, LayerResult, draw_text, resolve_layers, text_width
from . import model

FRAME_BUDGET = 60
DENSE_OFFSETS = (-2, -1, 0, 1, 2, 3, 4, 6, 8, 10, 12, 14)
SPARSE_OFFSETS = (-1, 0, 2, 4, 8, 14)
PAD_SECONDS = 0.25
SHEET_WIDTH = 1600


def snapshot_elements(snapshot: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[model.Element]]:
    occurrences = occurrences_from_snapshot(snapshot)
    registry = (snapshot.get("registry") or {}).get("assets") if isinstance(snapshot.get("registry"), Mapping) else {}
    from .element_motion import admitted_element_bindings

    metadata = snapshot.get("metadata") if isinstance(snapshot.get("metadata"), Mapping) else None
    bindings = admitted_element_bindings(metadata)
    return occurrences, model.elements_from_occurrences(occurrences, registry or {}, bindings=bindings)


def select_cut(snapshot: Mapping[str, Any], selector: Any, fps: float) -> dict[str, Any]:
    occurrences, _elements = snapshot_elements(snapshot)
    cuts = picture_cuts(occurrences, fps=fps)
    return dict(find_cut(cuts, selector))


def plan_motion_frames(
    cut: Mapping[str, Any], elements: Sequence[model.Element], fps: Fraction, total: int, *, budget: int = FRAME_BUDGET,
) -> dict[int, set[str]]:
    """Frames for one cut: dense after the cut-in and each entrance/keyframe, then an even fill.

    Dense runs use every frame for t-2…t+4 and every second frame to t+14
    (the first half second, where stamps and slides happen); the even fill
    gives the stillness/pixel-change panels a sample every ~0.1–0.2 s. The
    whole set never exceeds ``budget``.
    """
    rate = float(fps)
    start_f = max(0, int(round(float(cut["start"]) * rate)))
    end_f = min(total, max(start_f + 1, int(round(float(cut["end"]) * rate))))
    low, high = max(0, start_f - 2), min(total, end_f + 2)
    events = [start_f]
    steps: list[int] = []
    for element in model.in_window(elements, float(cut["start"]), float(cut["end"])):
        if element.sequence:
            # a sequence's steps get one frame each (its first), not a dense run apiece
            steps.append(int(round(element.start * rate)))
            continue
        for event in model.events(element, rate):
            frame = int(round(event.t * rate))
            if event.kind in ("enter", "key") and start_f < frame < end_f - 1:
                events.append(frame)
    chosen_steps: dict[int, set[str]] = {}
    if steps:
        steps = sorted(set(f for f in steps if start_f <= f < end_f))
        room = max(3, budget * 2 // 3 - 1)
        stride = max(1, math.ceil(len(steps) / room))
        for frame in steps[::stride] + steps[-1:]:
            chosen_steps.setdefault(frame, set()).add("motion")
        budget = max(1, budget - len(chosen_steps))
    events = sorted(set(events))
    collapsed: list[int] = []
    for frame in events:
        if not collapsed or frame - collapsed[-1] >= 6:
            collapsed.append(frame)
    fill_reserve = min(24, budget // 3)
    dense_budget = budget - fill_reserve
    offsets = DENSE_OFFSETS if len(collapsed) * len(DENSE_OFFSETS) <= dense_budget else SPARSE_OFFSETS
    chosen: dict[int, set[str]] = {}
    for frame in collapsed:
        for offset in offsets:
            candidate = frame + offset
            if low <= candidate < high and len(chosen) < dense_budget:
                chosen.setdefault(candidate, set()).add("motion")
    remaining = budget - len(chosen)
    if remaining > 0:
        step = max(1, math.ceil((end_f - start_f) / remaining))
        frame = start_f
        while frame < end_f and len(chosen) < budget:
            chosen.setdefault(frame, set()).add("motion")
            frame += step
        if end_f - 1 >= start_f and len(chosen) < budget:
            chosen.setdefault(end_f - 1, set()).add("motion")
    for frame, why in chosen_steps.items():
        chosen.setdefault(frame, set()).update(why)
    return chosen


def build_context(
    snapshot: Mapping[str, Any], cut: Mapping[str, Any], frames: Mapping[int, Path], *, fps: float,
    frame_size: tuple[int, int], beats: Mapping[str, Any] | None = None, layer_names: Sequence[str] = (),
    frame_provider: Any | None = None,
) -> LayerContext:
    occurrences, elements = snapshot_elements(snapshot)
    cuts = picture_cuts(occurrences, fps=fps)
    start, end = float(cut["start"]), float(cut["end"])
    window = (max(0.0, start - PAD_SECONDS), end + PAD_SECONDS)
    inside = model.in_window(elements, start, end)
    visual_events = [event for element in inside if not element.sequence for event in model.events(element, fps)
                     if window[0] <= event.t <= window[1]]
    # A sequence is one picture cut: its steps are keys on one lane, not entrances of many clips.
    steps = sorted((e for e in inside if e.sequence), key=lambda e: e.start)
    for index, step in enumerate(steps):
        if window[0] <= step.start <= window[1]:
            visual_events.append(model.Event(step.start, "key", f"sequence:{step.sequence}",
                                             f"sequence step {index + 1}/{len(steps)}", "step"))
    visual_events.sort(key=lambda event: event.t)
    ctx = LayerContext(
        cut=cut,
        cuts=cuts,
        window=window,
        fps=fps,
        elements=inside,
        all_elements=elements,
        words=[w for w in model.words(elements) if w.end > window[0] - 0.5 and w.start < window[1] + 0.5],
        beats=model.timeline_beats(beats, elements, window[0], window[1]),
        sfx=[s for s in model.sfx(elements) if s[1] > window[0] and s[0] < window[1]],
        events=visual_events,
        frames=dict(frames),
        frame_size=frame_size,
        width=SHEET_WIDTH,
    )
    from . import data as data_tracks

    ctx.tracks = data_tracks.tracks(model.in_window(elements, window[0], window[1]), start=window[0], end=window[1])
    track_order = []
    for occurrence in occurrences:
        if occurrence["occurrence_id"] == cut.get("occurrence_id"):
            track_order = occurrence.get("track_order") or []
    ctx.shared["track_order"] = track_order
    ctx.shared["layer_names"] = tuple(layer_names)
    config = snapshot.get("config")
    ctx.shared["config"] = config if isinstance(config, Mapping) else {}
    # Internal handoff only: facts come from the exact successful fresh Root
    # callback or this owner's trusted cache entry. Bind the whole handoff to
    # one evidence mode; a mixed batch must stay on the private fallback even
    # when every individual row is valid.
    public_scene = {}
    public_scene_evidence_mode = None
    if frame_provider is not None:
        source_identity = getattr(frame_provider, "source_identity", "")
        scene_for_frame = getattr(frame_provider, "scene_for_frame", None)
        if source_identity and callable(scene_for_frame):
            fresh_frames = getattr(frame_provider, "fresh_scene_frames", ())
            cached_scenes = getattr(frame_provider, "_cached_scenes", {})
            modes = set()
            for frame in sorted(frames):
                capture = scene_for_frame(frame)
                facts = getattr(capture, "_frame_facts", None) if capture is not None else None
                if not callable(facts):
                    continue
                if frame in fresh_frames:
                    mode = "fresh_capture"
                elif frame in cached_scenes:
                    mode = "cache"
                else:
                    continue
                try:
                    public_scene[frame] = facts(frame, source_identity=source_identity)
                    modes.add(mode)
                except Exception:  # noqa: BLE001 - public facts are optional
                    public_scene.pop(frame, None)
            if len(modes) == 1 and len(public_scene) == len(frames):
                public_scene_evidence_mode = modes.pop()
            else:
                public_scene.clear()
    ctx.shared["public_scene"] = public_scene
    ctx.shared["public_scene_evidence_mode"] = public_scene_evidence_mode
    return ctx


def _summary_lines(ctx: LayerContext) -> list[str]:
    cut = ctx.cut
    layers = []
    for span in cut.get("layers") or ():
        element = next((e for e in ctx.elements if e.id == span.get("id")), None)
        if element is None:
            continue
        enters = element.start - float(cut["start"])
        layers.append(element.label + (f" @+{enters:.2f}" if enters > 1 / ctx.fps else ""))
    lo, hi = float(cut["start"]), float(cut["end"])
    if cut.get("window"):  # a window (--at, --range): the words spoken IN it, however short it is
        said = " ".join(w.text for w in ctx.words if w.end > lo + 1e-6 and w.start < max(hi, lo + 1 / ctx.fps) - 1e-6)
        silent = "VO (nobody is speaking in this window)"
    else:
        said = " ".join(w.text for w in ctx.words if lo <= (w.start + w.end) / 2 < hi)
        silent = "VO (none under this cut)"
    picture = next((e.label for e in ctx.elements if e.id == cut.get("clip_id")), cut.get("clip_id") or "(no picture clip)")
    sequence = cut.get("sequence") or {}
    if sequence:
        picture = f"sequence {sequence.get('id')}, {sequence.get('steps')} steps (from {picture})"
    lines = [
        f"picture {picture}" + (f"  +  {' · '.join(layers)}" if layers else ""),
        f'VO "{said}"' if said else silent,
    ]
    if ctx.frame_size and ctx.frame_size[0]:
        w, h = ctx.frame_size
        scale = model.CANVAS[0] / float(w)
        lines.append(f"canvas {model.CANVAS[0]}×{model.CANVAS[1]} px, frames shown at {w}×{h}: one shown px = {scale:g} canvas px "
                     "(every position is in canvas px; one art px of a sprite = its scale in canvas px)")
    return lines


def _wrap(draw, text: str, size: int, width: int) -> list[str]:
    words = text.split(" ")
    lines, current = [], ""
    for word in words:
        trial = f"{current} {word}".strip()
        if text_width(draw, trial, size) <= width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


# Findings read top-down by what to fix first: composition and timing
# problems, then holds and sync detail, then descriptive lines, then info.
_RANK = ("ERROR", "FACE", "FRAME", "SAFE", "SMALL", "COVER", "SYNC", "SFX", "SHORT", "HOLD", "STILL", "LIPSYNC",
         "AUDIO", "STRIP", "TIME", "CURVE", "DIFF", "ONION", "BEAT", "EDGE")


def _finding_rank(line: str) -> tuple[int, int]:
    code = line.split(" ", 1)[0]
    rank = _RANK.index(code) if code in _RANK else len(_RANK)
    # lint lines ("SYNC   cut 17 …") carry a decision; layer lines describe
    return rank, 0 if " cut " in line[:16] else 1


def _busiest(ctx: LayerContext) -> float | None:
    """Midpoint of the sampled frame pair with the most pixel change that does not span a picture cut."""
    series = ctx.shared.get("motion_series") or []
    cut_frames = {int(round(float(c["start"]) * ctx.fps)) for c in ctx.cuts}
    best = None
    for item in series:
        if any(item["a"] < f <= item["b"] for f in cut_frames):
            continue
        if best is None or item["fraction"] > best["fraction"]:
            best = item
    if best is None or best["fraction"] <= 0:
        return None
    return (best["a"] + best["b"]) / 2 / ctx.fps


def compose_motion_sheet(
    ctx: LayerContext, layer_names: Sequence[str] | None, out_root: Path, *, timeline_label: str,
) -> dict[str, Any]:
    """Run the layers and write the page(s): ONE page when only frame layers run, else data + frames.

    Returns ``{png, findings, layers, preview, navigation}``.
    """
    import json

    from .window import fit_columns, navigation

    layers = resolve_layers(layer_names, view="motion")
    ctx.shared["layer_names"] = tuple(layer.name for layer in layers)
    window = ctx.shared.get("window") or {"preset": "cut", "start": ctx.cut["start"], "end": ctx.cut["end"],
                                          "cuts_in": [ctx.cut["index"]]}
    columns = int(ctx.shared.get("columns") or 8)
    tile_w = int(ctx.shared.get("tile_w") or ctx.frame_size[0])
    count = max(1, len(ctx.frames))
    columns, shown_w = fit_columns(count, columns, tile_w, int(tile_w * ctx.frame_size[1] / max(1, ctx.frame_size[0])))
    ctx.shared["columns"], ctx.shared["tile_w"] = columns, shown_w
    ctx.width = max(SHEET_WIDTH, columns * shown_w + 20 + (columns - 1) * 8)
    results: list[tuple[str, LayerResult]] = []
    for layer in layers:
        try:
            result = layer.render(ctx)
        except Exception as exc:  # noqa: BLE001 - one broken layer must not lose the sheet
            result = LayerResult(None, [f"ERROR  layer {layer.name}: {type(exc).__name__}: {exc}"], layer.name)
        results.append((layer.name, result))
    cut = ctx.cut
    preset = window["preset"]
    ids = {int(c["index"]): c.get("cut_id") for c in ctx.cuts or [] if c.get("cut_id")}

    def name(n: int) -> str:  # one numbering: the cut ids the sheet and show print (c38), else the position
        return ids.get(int(n)) or f"cut {n}"

    if cut.get("window"):
        numbers = list(window.get("cuts_in") or [])
        span = name(numbers[0]) if len(numbers) == 1 else (f"{name(numbers[0])}–{name(numbers[-1])}" if numbers else "")
        head = (f"{preset} · {float(cut['start']):.2f}–{float(cut['end']):.2f} s ({float(cut['duration']):.2f} s) · "
                f"{span} · {cut.get('shot') or ''}")
        findings = [f"VIEW   {preset} {float(cut['start']):.2f}–{float(cut['end']):.2f}s ({span}); "
                    f"{len(ctx.frames)} frames at {ctx.frame_size[0]}x{ctx.frame_size[1]}, {columns} per row"]
        stem = f"view-{preset}-{float(cut['start']):.2f}-{float(cut['end']):.2f}"
    else:
        number = int(cut["index"])
        head = (f"{cut.get('cut_id') or name(number)} · {float(cut['start']):.2f}–{float(cut['end']):.2f} s ({float(cut['duration']):.2f} s) · "
                f"{cut.get('shot') or ''}")
        findings = [f"MOTION {cut.get('cut_id') or name(number)} {float(cut['start']):.2f}–{float(cut['end']):.2f}s ({float(cut['duration']):.2f}s) "
                    f"{cut.get('shot') or ''}; {len(ctx.frames)} frames at {ctx.frame_size[0]}x{ctx.frame_size[1]}"]
        stem = f"motion-cut-{number:02d}"
    collected = [line for _name, result in results for line in result.findings]
    findings.extend(sorted(collected, key=_finding_rank))
    data_panels = [r.image for _n, r in results if r.page == "data" and r.image is not None]
    frame_panels = [r.image for _n, r in results if r.page == "frames" and r.image is not None]
    if cut.get("window"):
        # a window preset is ONE page: frames first, then whatever data layers were asked for
        plan = [("one", "", frame_panels + data_panels, True)]
    else:
        plan = ([("data", "", data_panels, True)] if data_panels else []) + (
            [("frames", "-frames" if data_panels else "", frame_panels, not data_panels)] if frame_panels else [])
    if not plan:
        plan = [("data", "", [], True)]
    pages: list[str] = []
    width = ctx.width
    for _page, suffix, panels, with_findings in plan:
        measure = ImageDraw.Draw(Image.new("RGB", (1, 1)))
        header = [head] + _summary_lines(ctx)
        header_lines = [line for text in header for line in _wrap(measure, text, 15, width - 20)]
        finding_lines = []
        if with_findings:
            for text in findings:
                finding_lines.extend(_wrap(measure, text, 13, width - 20))
        height = 16 + 24 + 20 * (len(header_lines) - 1) + 10 + sum(p.size[1] + 6 for p in panels)
        height += (24 + 18 * len(finding_lines)) if finding_lines else 0
        sheet = Image.new("RGB", (width, height + 10), PALETTE["bg"])
        draw = ImageDraw.Draw(sheet)
        y = 10
        draw_text(draw, (10, y), f"{timeline_label} · {header_lines[0]}", 20, "white")
        y += 28
        for line in header_lines[1:]:
            draw_text(draw, (10, y), line, 14, PALETTE["muted"])
            y += 20
        y += 6
        for panel in panels:
            sheet.paste(panel, (0, y))
            y += panel.size[1] + 6
        if finding_lines:
            draw_text(draw, (10, y + 2), "findings (also in findings.txt)", 15, PALETTE["ink"])
            y += 24
            for line in finding_lines:
                colour = PALETTE["warn"] if line.split(" ", 1)[0] in ("FACE", "SAFE", "FRAME", "SMALL", "SYNC", "HOLD", "SHORT", "COVER") else PALETTE["ink"]
                draw_text(draw, (10, y), line, 13, colour)
                y += 18
        path = Path(out_root) / f"{stem}{suffix}.png"
        sheet.save(path)
        pages.append(str(path))
    preview = write_preview(ctx, out_root) if ctx.shared.get("preview") else None
    duration = max([float(c["end"]) for c in ctx.cuts] or [float(cut["end"])])
    nav = navigation(window, duration=duration, busiest=_busiest(ctx), cuts=ctx.cuts)
    (Path(out_root) / "window.json").write_text(json.dumps(nav, sort_keys=True), encoding="utf-8")
    return {"png": pages, "findings": findings, "layers": [name for name, _r in results], "preview": preview,
            "navigation": nav}


PREVIEW_MAX_SIZE = (480, 270)


def write_preview(ctx: LayerContext, out_root: Path) -> str | None:
    """A timing-faithful animated GIF of the cut for humans, from the frames already captured.

    Each sampled frame is shown until the next one (so dense runs after an
    entrance play at their real speed and holds hold); no extra capture.
    """
    frames = sorted(f for f in ctx.frames if float(ctx.cut["start"]) * ctx.fps - 2 <= f < float(ctx.cut["end"]) * ctx.fps)
    if len(frames) < 2:
        return None
    images, durations = [], []
    for index, frame in enumerate(frames):
        image = ctx.frame_image(frame)
        if image is None:
            continue
        image.thumbnail(PREVIEW_MAX_SIZE)
        draw = ImageDraw.Draw(image)
        label = ctx.rel(frame / ctx.fps)
        draw.rectangle((0, 0, 8 + 7 * len(label), 16), fill="#000000")
        draw_text(draw, (4, 1), label, 12, "#ffffff")
        following = frames[index + 1] if index + 1 < len(frames) else frame + int(ctx.fps // 2)
        durations.append(max(20, int(round((following - frame) / ctx.fps * 1000))))
        images.append(image.convert("P", palette=Image.ADAPTIVE, colors=128))
    path = Path(out_root) / f"motion-cut-{int(ctx.cut['index']):02d}.gif"
    images[0].save(path, save_all=True, append_images=images[1:], duration=durations, loop=0, optimize=True)
    return str(path)
