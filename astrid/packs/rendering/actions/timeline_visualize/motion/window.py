"""Windows and presets: every motion-style view is "some frames of one time window, on one page".

A window is a cut (``--cut N``), a range (``--range A..B``), a moment
(``--at T``, centred) or one frame (``--frame N``). A preset says how to sample
it and how big to show it. Every preset uses the same fast capture (the
Remotion frame provider at review size) and fits ONE page; when it cannot, it
says so and names the preset that fits.

| preset   | window          | frames                               | layout                 |
|----------|-----------------|--------------------------------------|------------------------|
| scan     | any (≤ 30 s)    | every 0.5 s                          | 8 per row, 480x270     |
| motion   | ≤ 3 s (1 s around --at) | every 2nd frame              | 3 per row, 640x360, +onion +curves |
| beat     | any (≤ 20 s)    | at each word onset, music hit, sfx, entrance | 6 per row, labelled |
| frame    | one frame       | that frame                           | 1280x720               |
| cut      | one cut         | dense after each event (the motion sheet) | all layers        |
"""
from __future__ import annotations

import math
from fractions import Fraction
from typing import Any, Mapping, Sequence

from astrid.core.timeline.cuts import occurrences_from_snapshot
from astrid.core.timeline.cut_index import resolve_cut, cut_index

from . import model

PRESETS: dict[str, dict[str, Any]] = {
    "scan": {"strategy": "every", "every_s": 0.5, "columns": 8, "tile_w": 190, "size": (480, 270), "window_s": 10.0,
             "max_window_s": 30.0, "layers": ("strip",), "budget": 64},
    "motion": {"strategy": "every", "every_frames": 2, "columns": 3, "tile_w": 640, "size": (640, 360), "window_s": 1.0,
               "max_window_s": 3.0, "layers": ("strip", "onion", "curves"), "budget": 60},
    "beat": {"strategy": "onsets", "columns": 6, "tile_w": 256, "size": (480, 270), "window_s": 6.0, "max_window_s": 20.0,
             "layers": ("strip",), "budget": 48},
    "frame": {"strategy": "single", "columns": 1, "tile_w": 1280, "size": (1280, 720), "window_s": 0.0, "max_window_s": 1.0,
              "layers": ("strip",), "budget": 1},
    "cut": {"strategy": "events", "columns": 8, "tile_w": 190, "size": (480, 270), "window_s": None, "max_window_s": None,
            "layers": None, "budget": 60},
}
WINDOW_PRESETS = tuple(PRESETS)
ALL_PRESETS = ("overview", *WINDOW_PRESETS, "compare")
MAX_COLUMNS = 16
MAX_PAGE_HEIGHT = 2600


def preset_settings(name: str | None) -> dict[str, Any]:
    if not name:
        return dict(PRESETS["cut"])
    if name not in PRESETS:
        raise ValueError(f"unknown preset {name!r}; presets: {', '.join(ALL_PRESETS)}")
    return dict(PRESETS[name])


def resolve_window(snapshot: Mapping[str, Any], options: Mapping[str, Any], fps: float, total: int) -> dict[str, Any]:
    """The window to show: ``{start, end, cut, cuts_in, preset, strategy, label}`` (``cut`` is a real cut or a stand-in)."""
    occurrences = occurrences_from_snapshot(snapshot)
    cuts = cut_index(occurrences, fps=fps).rows
    duration = total / fps
    preset = options.get("preset") or ("cut" if options.get("cut") else "scan")
    settings = preset_settings(preset)
    if options.get("cut"):
        cut = dict(resolve_cut(cuts, options["cut"]))
        start, end = float(cut["start"]), float(cut["end"])
        if preset == "cut":
            return {"start": start, "end": end, "cut": cut, "cuts_in": [cut["index"]], "preset": preset,
                    "strategy": "events", "label": f"cut {cut['index']}"}
    elif options.get("frame") is not None:
        start = int(options["frame"]) / fps
        end = start + 1.0 / fps
    elif options.get("range") is not None:
        start, end = (float(v) for v in options["range"])
    elif options.get("at") is not None:
        at = float(options["at"])
        width = float(options.get("window_s") or settings.get("window_s") or 0.0)
        if preset == "frame" or width <= 0:
            start, end = at, at + 1.0 / fps
        else:
            start, end = at - width / 2, at + width / 2
    else:
        raise ValueError("choose a window: --cut N, --range A..B, --at T or --frame N")
    start, end = max(0.0, start), min(duration, end)
    if end <= start:
        raise ValueError(f"the window {start:.2f}..{end:.2f} s is outside the timeline (0..{duration:.2f} s)")
    limit = settings.get("max_window_s")
    if limit and end - start > limit + 1e-6:
        other = "scan" if preset != "scan" else "overview"
        raise ValueError(f"preset {preset} shows at most {limit:g} s (asked {end - start:.2f} s); "
                         f"narrow --range, or use --preset {other} for this window")
    centre = (start + end) / 2
    inside = [c for c in cuts if c["end"] > start + 1e-6 and c["start"] < end - 1e-6]
    host = next((c for c in cuts if c["start"] - 1e-6 <= centre < c["end"]), inside[0] if inside else cuts[0])
    stand_in = {**host, "start": round(start, 6), "end": round(end, 6), "duration": round(end - start, 6),
                "window": True, "cut_index": host["index"]}
    return {"start": start, "end": end, "cut": stand_in, "cuts_in": [c["index"] for c in inside], "preset": preset,
            "strategy": settings["strategy"], "label": f"{start:.2f}–{end:.2f} s"}


def window_frames(window: Mapping[str, Any], snapshot: Mapping[str, Any], options: Mapping[str, Any],
                  fps: Fraction, total: int) -> tuple[dict[int, set[str]], dict[int, str]]:
    """``(frames → reasons, frame → label)`` for a window, within the preset's budget (or an error)."""
    rate = float(fps)
    settings = preset_settings(window["preset"])
    budget = int(options.get("frame_budget") or settings["budget"])
    start_f = max(0, int(math.floor(window["start"] * rate + 1e-6)))
    end_f = min(total, max(start_f + 1, int(math.ceil(window["end"] * rate - 1e-6))))
    labels: dict[int, str] = {}
    strategy = window["strategy"]
    if strategy == "events":
        from .sheet import plan_motion_frames, snapshot_elements

        _occurrences, elements = snapshot_elements(snapshot)
        return plan_motion_frames(window["cut"], elements, fps, total, budget=budget), labels
    if strategy == "single":
        return {start_f: {"motion"}}, labels
    if strategy == "every" or options.get("explicit_interval"):
        if options.get("explicit_interval"):
            every_frames, every_s = options.get("every_frames"), options.get("every")
        else:
            every_frames, every_s = settings.get("every_frames"), settings.get("every_s")
        step = int(every_frames) if every_frames else max(1, round(float(every_s or 0.5) * rate))
        frames = list(range(start_f, end_f, step))
        if frames and frames[-1] != end_f - 1 and end_f - 1 - frames[-1] >= step / 2:
            frames.append(end_f - 1)
    else:  # onsets
        from .sheet import snapshot_elements

        _occurrences, elements = snapshot_elements(snapshot)
        inside = model.in_window(elements, window["start"], window["end"])
        marks: list[tuple[float, str]] = []
        for word in model.words(elements):
            if window["start"] <= word.start < window["end"]:
                marks.append((word.start, f'"{word.text}"'))
        beats = model.timeline_beats(options.get("beats"), elements, window["start"], window["end"])
        marks += [(t, kind) for t, kind in beats.get("hits") or []]
        marks += [(t, "downbeat") for t in beats.get("downbeats") or []]
        marks += [(s, f"sfx {name}") for s, _e, name in model.sfx(elements) if window["start"] <= s < window["end"]]
        for element in inside:
            if element.type in model.PLATE_TYPES or element.sequence:
                continue
            for event in model.accents(element, rate):
                if window["start"] <= event.t < window["end"]:
                    marks.append((event.t, event.label.split(" ", 1)[-1][:16]))
        for t, label in sorted(marks):
            frame = min(end_f - 1, max(start_f, int(round(t * rate))))
            labels[frame] = f"{labels[frame]} · {label}" if frame in labels else label
        frames = sorted(labels)
    if len(frames) > budget:
        hint = ("--preset scan" if window["preset"] != "scan" else "a shorter --range or a larger --every")
        raise ValueError(f"{len(frames)} frames is over the {window['preset']} budget of {budget}; try {hint}"
                         f" (or --frame-budget up to 120)")
    return {frame: {"motion"} for frame in frames}, labels


MAX_PAGE_WIDTH = 2600


def fit_columns(count: int, columns: int, tile_w: int, tile_h: int) -> tuple[int, int]:
    """Columns and tile width so ``count`` tiles fit ONE page (≤ MAX_PAGE_WIDTH wide, ≤ MAX_PAGE_HEIGHT tall).

    Tiles shrink and rows widen before anything paginates.
    """
    columns = max(1, min(MAX_COLUMNS, int(columns), max(1, count)))
    tile_w = min(int(tile_w), (MAX_PAGE_WIDTH - 20) // columns)
    width = tile_w * columns
    while True:
        rows = math.ceil(max(1, count) / columns)
        shown_w = width // columns
        shown_h = int(shown_w * tile_h / tile_w) + 34
        if rows * shown_h + 120 <= MAX_PAGE_HEIGHT or columns >= MAX_COLUMNS:
            return columns, shown_w
        columns += 1


def navigation(window: Mapping[str, Any], *, duration: float, busiest: float | None, cuts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Neighbour windows for the CLI's next: block (earlier, later, zoom in/out, the cut)."""
    start, end = float(window["start"]), float(window["end"])
    width = max(end - start, 1.0 / 30)
    centre = busiest if busiest is not None else (start + end) / 2
    half = width / 4
    host = next((c for c in cuts if c["start"] - 1e-6 <= (start + end) / 2 < c["end"]), None)
    return {
        "preset": window["preset"], "start": round(start, 3), "end": round(end, 3),
        "earlier": [round(max(0.0, start - width), 3), round(start, 3)] if start > 0 else None,
        "later": [round(end, 3), round(min(duration, end + width), 3)] if end < duration - 1e-3 else None,
        "zoom_in": [round(max(0.0, centre - half), 3), round(min(duration, centre + half), 3)],
        "zoom_out": [round(max(0.0, (start + end) / 2 - width), 3), round(min(duration, (start + end) / 2 + width), 3)],
        "busiest": None if busiest is None else round(busiest, 3),
        "cut": host["index"] if host else None,
        "cuts_in": list(window.get("cuts_in") or []),
        "duration": round(duration, 3),
    }
