"""V3 shared rhythm model behind the editorial.pacing rhythm sheet.

The input is the canonical authoring bundle opened by
``astrid.sdk.authoring_bundle.open_authoring_bundle``: occurrence placements on
the parent picture track, per-shot text bindings, and each shot's internal
timeline clips. Nothing here calls the runtime; ``load_beats`` is the only
function that may read a file.

Conventions (seconds on the timeline; intervals are closed-open):

- A visual cut is ``astrid.core.timeline.cuts.picture_cuts``: one clip on a
  shot's picture-bed track (``plate``, else the widest visual track), the one
  definition ``timelines show`` and ``timelines visualize`` also use. A shot
  with no picture clip counts as one cut spanning its occurrence.
- Cut kind: ``app.kind`` when set; else ``presenter`` when an ``am-presenter``
  clip overlaps the cut; else ``silent`` when no speech overlaps it; else
  ``illustrative``.
- Word timings, in order of preference: ``app.words`` on a VO clip (relative to
  that clip's start); ``params.words`` on a shot element (the lip-sync list that
  AM clips carry, relative to that element's start); otherwise the shot's
  text-binding word count, spread over the VO clips that have no timed words,
  in proportion to their duration.
- Speech density is words per second over a 2 s window centred on each grid
  point (a word counts when its midpoint falls inside the window).
"""

from __future__ import annotations

import bisect
import json
import math
import statistics
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

SCHEMA = "editorial.pacing/v1"
CUT_TRACK = "plate"
PRESENTER_CLIP = "am-presenter"
STALL_SECONDS = 6.0
SILENCE_GAP_SECONDS = 0.4
TARGET_RANGE = (1.5, 4.0)
DENSITY_WINDOW_SECONDS = 2.0
DENSEST_WINDOW_SECONDS = 10.0
CUT_RATE_WINDOW_SECONDS = 10.0
SLOW_SPEECH_WPS = 1.0
GRID_STEP_SECONDS = 0.1
SILENT_COVERAGE_SECONDS = 0.05
KINDS = ("presenter", "illustrative", "silent", "joke")
DELIBERATE_KEYS = ("deliberate_hold", "deliberate")


class PacingError(ValueError):
    """The bundle, window or beats cannot be read into a rhythm model."""


def _num(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _map(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def _app(clip: Mapping[str, Any]) -> dict[str, Any]:
    return {**_map(_map(clip.get("params")).get("app")), **_map(clip.get("app"))}


def _is_deliberate(clip: Mapping[str, Any]) -> bool:
    app = _app(clip)
    params = _map(clip.get("params"))
    return any(bool(app.get(key) or params.get(key)) for key in DELIBERATE_KEYS)


def clip_duration(clip: Mapping[str, Any]) -> float:
    """Timeline seconds a clip occupies: ``hold`` for AM clips, else ``to - from``."""
    if clip.get("hold") is not None:
        return max(0.0, _num(clip.get("hold")))
    if clip.get("from") is not None and clip.get("to") is not None:
        return max(0.0, _num(clip["to"]) - _num(clip["from"]))
    return 0.0


def _word_pair(item: Any) -> tuple[float, float] | None:
    if isinstance(item, Mapping):
        first, second = item.get("start"), item.get("end")
    elif isinstance(item, (list, tuple)) and len(item) >= 2:
        first, second = item[0], item[1]
    else:
        return None
    start, end = _num(first, -1.0), _num(second, -1.0)
    if start < 0 or end < start:
        return None
    return start, end


def _shot_name(shot: Mapping[str, Any], shot_id: str) -> str:
    payload = _map(shot.get("payload"))
    base = _map(shot.get("base_payload"))
    return str(payload.get("name") or base.get("name") or shot_id)


def _text_word_count(shot: Mapping[str, Any]) -> int:
    payload = _map(shot.get("payload")) or _map(shot.get("base_payload"))
    total = 0
    for binding in _list(payload.get("text_bindings")):
        text = _map(binding).get("text")
        if isinstance(text, str):
            total += len(text.split())
    return total


def _apportion(total: int, weights: Sequence[float]) -> list[int]:
    """Split ``total`` integer units by weight (largest remainder)."""
    if not weights:
        return []
    weight_sum = sum(weights)
    if weight_sum <= 0:
        weights = [1.0] * len(weights)
        weight_sum = float(len(weights))
    raw = [total * weight / weight_sum for weight in weights]
    counts = [int(math.floor(value)) for value in raw]
    remainder = total - sum(counts)
    order = sorted(range(len(raw)), key=lambda i: raw[i] - counts[i], reverse=True)
    for index in order[:remainder]:
        counts[index] += 1
    return counts


def _speech(
    shot_id: str,
    start: float,
    clips: Sequence[Mapping[str, Any]],
    audio_tracks: set[str],
    text_words: int,
) -> tuple[list[tuple[float, float, str]], list[str], list[str]]:
    """Return (timed word intervals, source labels, notes) for one occurrence."""
    notes: list[str] = []
    voice: list[tuple[float, float, Mapping[str, Any]]] = []
    for clip in clips:
        if clip.get("track") in audio_tracks and clip.get("clipType") == "media":
            length = clip_duration(clip)
            if length > 0:
                clip_start = start + _num(clip.get("at"))
                voice.append((clip_start, clip_start + length, clip))
    if not voice:
        if text_words:
            notes.append(f"{shot_id}: {text_words} text words have no VO clip; no speech counted")
        return [], [], notes

    timed: list[tuple[float, float, str]] = []
    sources: list[str] = []
    app_hit = False
    for clip_start, _clip_end, clip in voice:
        for item in _list(_app(clip).get("words")):
            pair = _word_pair(item)
            if pair is not None:
                timed.append((clip_start + pair[0], clip_start + pair[1], "app.words"))
                app_hit = True
    if app_hit:
        sources.append("app.words")
    else:
        candidates: list[tuple[float, list[tuple[float, float]]]] = []
        for clip in clips:
            if clip.get("track") in audio_tracks:
                continue
            pairs = [
                pair
                for pair in (_word_pair(item) for item in _list(_map(clip.get("params")).get("words")))
                if pair is not None
            ]
            if pairs:
                candidates.append((start + _num(clip.get("at")), pairs))
        if candidates:
            base, pairs = max(candidates, key=lambda candidate: len(candidate[1]))
            timed.extend((base + first, base + second, "params.words") for first, second in pairs)
            sources.append("params.words")

    midpoints = [(first + second) / 2 for first, second, _ in timed]
    residual = max(0, text_words - len(timed))
    uncovered = [
        (clip_start, clip_end)
        for clip_start, clip_end, _clip in voice
        if not any(clip_start <= mid <= clip_end for mid in midpoints)
    ]
    if residual and uncovered:
        counts = _apportion(residual, [end - begin for begin, end in uncovered])
        for (begin, end), count in zip(uncovered, counts):
            if count:
                step = (end - begin) / count
                timed.extend((begin + k * step, begin + (k + 1) * step, "text-binding") for k in range(count))
        sources.append("text-binding")
    elif residual:
        notes.append(f"{shot_id}: {residual} text words not placed (every VO clip already has timings)")
    timed.sort()
    return timed, sources, notes


def _coverage(words: Sequence[tuple[float, float, str]], start: float, end: float) -> float:
    total = 0.0
    for first, second, _source in words:
        overlap = min(second, end) - max(first, start)
        if overlap > 0:
            total += overlap
    return total


def _classify(stub: Mapping[str, Any], presenters: Sequence[tuple[float, float]], words: Sequence[tuple[float, float, str]]) -> tuple[str, str]:
    declared = str(stub["app"].get("kind") or "").strip().lower()
    if declared in KINDS:
        return declared, "app"
    start, end = stub["start"], stub["end"]
    if any(p_start < end and p_end > start for p_start, p_end in presenters):
        return "presenter", "element"
    if _coverage(words, start, end) < SILENT_COVERAGE_SECONDS:
        return "silent", "speech"
    return "illustrative", "element"


def _grid(duration: float, step: float) -> list[float]:
    count = int(math.floor(duration / step + 1e-9))
    points = [round(index * step, 6) for index in range(count + 1)]
    if points[-1] < duration - 1e-9:
        points.append(round(duration, 6))
    return points


def _count(values: Sequence[float], low: float, high: float) -> int:
    """Count sorted ``values`` in the closed interval [low, high]."""
    return bisect.bisect_right(values, high) - bisect.bisect_left(values, low)


def _silences(intervals: Sequence[tuple[float, float]], low: float, high: float, min_length: float) -> list[tuple[float, float]]:
    gaps: list[tuple[float, float]] = []
    cursor = low
    for first, second in sorted(intervals):
        first, second = max(first, low), min(second, high)
        if second <= first:
            continue
        if first - cursor >= min_length:
            gaps.append((cursor, first))
        cursor = max(cursor, second)
    if high - cursor >= min_length:
        gaps.append((cursor, high))
    return [(round(a, 3), round(b, 3)) for a, b in gaps if b > a]


def _runs(points: Sequence[float], flags: Sequence[bool], step: float) -> list[tuple[float, float]]:
    runs: list[tuple[float, float]] = []
    start: float | None = None
    last = 0.0
    for point, flag in zip(points, flags):
        if flag:
            if start is None:
                start = point
            last = point
        elif start is not None:
            runs.append((start, last + step))
            start = None
    if start is not None:
        runs.append((start, last + step))
    return runs


def load_beats(value: Any) -> list[float] | None:
    """Beat times in seconds from a list, a ``{"beats": [...]}`` object, or a JSON path."""
    if value is None or value == "":
        return None
    if isinstance(value, str):
        path = Path(value).expanduser()
        if not path.is_file():
            raise PacingError(f"beats file not found: {path}")
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise PacingError(f"beats file is not JSON: {path}") from exc
    if isinstance(value, Mapping):
        for key in ("beats", "times", "beat_times", "onsets"):
            if key in value:
                value = value[key]
                break
        else:
            raise PacingError("beats object needs a 'beats' (or 'times') list")
    if not isinstance(value, list):
        raise PacingError("beats must be a list of seconds or an object with a list")
    times: list[float] = []
    for item in value:
        if isinstance(item, Mapping):
            item = item.get("time", item.get("t", item.get("start", item.get("seconds"))))
        seconds = _num(item, -1.0)
        if seconds >= 0:
            times.append(seconds)
    return sorted(times)


def build_rhythm(
    bundle: Mapping[str, Any],
    *,
    timeline: Mapping[str, Any] | None = None,
    window: Sequence[float] | None = None,
    beats: Sequence[float] | None = None,
) -> dict[str, Any]:
    """Build the JSON-shaped rhythm model for one authoring bundle."""
    if not isinstance(bundle, Mapping):
        raise PacingError("authoring bundle must be an object")
    shots = _map(bundle.get("shots"))
    notes: list[str] = []
    records: list[dict[str, Any]] = []
    chapters: list[dict[str, Any]] = []
    from astrid.core.timeline.cuts import bundle_fps, occurrences_from_bundle
    from astrid.core.timeline.cut_index import cut_index

    shared_cuts: dict[str, list[dict[str, Any]]] = {}
    for cut in cut_index(occurrences_from_bundle(bundle), fps=bundle_fps(bundle)).rows:
        shared_cuts.setdefault(cut["occurrence_id"], []).append(cut)

    for occurrence in (_map(item) for item in _list(bundle.get("placements")) if isinstance(item, Mapping)):
        shot_id = str(occurrence.get("shot_id") or "")
        occurrence_id = str(occurrence.get("occurrence_id") or shot_id)
        start = _num(_map(occurrence.get("placement")).get("start_ms")) / 1000.0
        length = _num(occurrence.get("duration_ms")) / 1000.0
        if length <= 0:
            notes.append(f"{occurrence_id}: zero-length placement skipped")
            continue
        shot = _map(shots.get(shot_id))
        name = _shot_name(shot, shot_id)
        chapters.append({"shot_id": shot_id, "occurrence_id": occurrence_id, "name": name, "start": start, "end": start + length})

        speed = _map(occurrence.get("speed"))
        ratio = _num(speed.get("numerator"), 1.0) / (_num(speed.get("denominator"), 1.0) or 1.0)
        if abs(ratio - 1.0) > 1e-9:
            notes.append(f"{occurrence_id}: speed {ratio:g}x is not applied to clip times")

        internal = _map(shot.get("internal_timeline")) or _map(shot.get("base_internal_timeline"))
        clips = [_map(item) for item in _list(internal.get("clips")) if isinstance(item, Mapping)]
        audio_tracks = {
            str(track.get("id"))
            for track in _list(internal.get("tracks"))
            if isinstance(track, Mapping) and track.get("kind") == "audio"
        } | {"vo"}
        presenters = [
            (start + _num(clip.get("at")), start + _num(clip.get("at")) + clip_duration(clip))
            for clip in clips
            if clip.get("clipType") == PRESENTER_CLIP and clip_duration(clip) > 0
        ]
        # One cut definition for show, visualize and pacing (astrid.core.timeline.cuts).
        stubs = [
            {
                "start": cut["start"],
                "end": cut["end"],
                "clip_id": str(cut["clip_id"] or occurrence_id),
                "app": _app(cut["clip"]) if cut["clip"] else {},
                "deliberate": bool(cut["deliberate_hold"]),
            }
            for cut in shared_cuts.get(occurrence_id, [])
        ] or [{"start": start, "end": start + length, "clip_id": occurrence_id, "app": {}, "deliberate": False}]

        words, sources, shot_notes = _speech(shot_id, start, clips, audio_tracks, _text_word_count(shot))
        notes.extend(shot_notes)
        records.append(
            {
                "occurrence_id": occurrence_id,
                "shot_id": shot_id,
                "chapter": name,
                "start": start,
                "end": start + length,
                "presenters": presenters,
                "stubs": stubs,
                "words": words,
                "speech": "+".join(sources) or "none",
            }
        )

    if not records:
        raise PacingError("timeline has no placed occurrences to read")

    cuts: list[dict[str, Any]] = []
    for record in records:
        for stub in record["stubs"]:
            kind, kind_source = _classify(stub, record["presenters"], record["words"])
            start, end = stub["start"], stub["end"]
            cuts.append(
                {
                    "clip_id": stub["clip_id"],
                    "shot_id": record["shot_id"],
                    "occurrence_id": record["occurrence_id"],
                    "chapter": record["chapter"],
                    "start": start,
                    "end": end,
                    "duration": end - start,
                    "kind": kind,
                    "kind_source": kind_source,
                    "speech_seconds": _coverage(record["words"], start, end),
                    "deliberate_hold": bool(stub["deliberate"]),
                }
            )
    cuts.sort(key=lambda cut: (cut["start"], cut["end"]))
    all_words = sorted(word for record in records for word in record["words"])
    timeline_end = max(
        [record["end"] for record in records]
        + [cut["end"] for cut in cuts]
        + [second for _first, second, _source in all_words]
        + [0.0]
    )

    low, high = 0.0, timeline_end
    if window is not None:
        if len(window) != 2:
            raise PacingError("window must be [start, end] in seconds")
        low, high = _num(window[0], -1.0), _num(window[1], -1.0)
        if not 0 <= low < high:
            raise PacingError("window must satisfy 0 <= start < end")
        if low >= timeline_end:
            raise PacingError(f"window starts after the timeline ends at {timeline_end:.2f} s")
        high = min(high, timeline_end)
    span = high - low

    # Full-timeline series (with context outside the window), then sliced.
    mids = sorted((first + second) / 2 for first, second, _source in all_words)
    cut_starts = sorted(cut["start"] for cut in cuts)
    grid_full = _grid(timeline_end, GRID_STEP_SECONDS)
    density_full = [
        _count(mids, t - DENSITY_WINDOW_SECONDS / 2, t + DENSITY_WINDOW_SECONDS / 2) / DENSITY_WINDOW_SECONDS
        for t in grid_full
    ]
    half_rate = CUT_RATE_WINDOW_SECONDS / 2
    rate_full = [
        float(_count(cut_starts, max(0.0, t - half_rate), min(timeline_end, t + half_rate)))
        for t in grid_full
    ]

    indices = [i for i, t in enumerate(grid_full) if low - 1e-9 <= t <= high + 1e-9]
    grid = [grid_full[i] for i in indices]
    density = [density_full[i] for i in indices]
    cut_rate = [rate_full[i] for i in indices]
    peak = max(density) if density else 0.0
    density_norm = [round(value / peak, 4) if peak > 0 else 0.0 for value in density]

    windowed_cuts: list[dict[str, Any]] = []
    for cut in cuts:
        if cut["end"] <= low or cut["start"] >= high:
            continue
        clipped = dict(cut)
        clipped["start"] = max(cut["start"], low)
        clipped["end"] = min(cut["end"], high)
        clipped["duration"] = clipped["end"] - clipped["start"]
        clipped["stall"] = False
        windowed_cuts.append(clipped)
    for index, cut in enumerate(windowed_cuts, start=1):
        cut["index"] = index

    stalls: list[dict[str, Any]] = []
    for cut in windowed_cuts:
        if cut["duration"] > STALL_SECONDS and not cut["deliberate_hold"]:
            cut["stall"] = True
            stalls.append(
                {
                    "kind": "visual",
                    "start": round(cut["start"], 3),
                    "end": round(cut["end"], 3),
                    "duration": round(cut["duration"], 3),
                    "clip_id": cut["clip_id"],
                    "note": f"no visual change for {cut['duration']:.2f} s",
                }
            )
    deliberate = [(cut["start"], cut["end"]) for cut in windowed_cuts if cut["deliberate_hold"]]
    slow = [
        value < SLOW_SPEECH_WPS and not any(a <= t < b for a, b in deliberate)
        for t, value in zip(grid, density)
    ]
    for run_start, run_end in _runs(grid, slow, GRID_STEP_SECONDS):
        run_end = min(run_end, high)
        if run_end - run_start > STALL_SECONDS:
            stalls.append(
                {
                    "kind": "speech",
                    "start": round(run_start, 3),
                    "end": round(run_end, 3),
                    "duration": round(run_end - run_start, 3),
                    "clip_id": None,
                    "note": f"under {SLOW_SPEECH_WPS:g} words/s for {run_end - run_start:.1f} s",
                }
            )
    stalls.sort(key=lambda stall: (stall["start"], stall["kind"]))

    words_windowed = [(first, second, source) for first, second, source in all_words if second > low and first < high]
    silences = _silences([(first, second) for first, second, _source in words_windowed], low, high, SILENCE_GAP_SECONDS)

    densest: dict[str, Any] | None = None
    if span <= DENSEST_WINDOW_SECONDS:
        densest = {"start": round(low, 3), "end": round(high, 3), "words_per_s": round(_count(mids, low, high) / span, 3)}
    else:
        best_start, best_count = low, -1
        for t in [low] + [t for t in grid_full if low < t <= high - DENSEST_WINDOW_SECONDS + 1e-9]:
            count = _count(mids, t, t + DENSEST_WINDOW_SECONDS)
            if count > best_count:
                best_start, best_count = t, count
        densest = {
            "start": round(best_start, 3),
            "end": round(best_start + DENSEST_WINDOW_SECONDS, 3),
            "words_per_s": round(best_count / DENSEST_WINDOW_SECONDS, 3),
        }

    durations = [cut["duration"] for cut in windowed_cuts]
    kinds = Counter(cut["kind"] for cut in windowed_cuts)
    presenter_time = sum(cut["duration"] for cut in windowed_cuts if cut["kind"] == "presenter")
    longest = sorted(windowed_cuts, key=lambda cut: (-cut["duration"], cut["start"]))[:3]
    word_sources = Counter(source for _first, _second, source in words_windowed)
    beats_windowed = None
    if beats is not None:
        beats_windowed = [round(beat, 3) for beat in beats if low <= beat <= high]

    summary = {
        "duration_s": round(timeline_end, 3),
        "window_span_s": round(span, 3),
        "cut_count": len(windowed_cuts),
        "cut_kinds": dict(sorted(kinds.items())),
        "median_cut_s": round(statistics.median(durations), 3) if durations else None,
        "mean_cut_s": round(statistics.fmean(durations), 3) if durations else None,
        "presenter_share": round(presenter_time / span, 4) if span > 0 else 0.0,
        "target_share": round(
            sum(1 for value in durations if TARGET_RANGE[0] <= value <= TARGET_RANGE[1]) / len(durations), 4
        ) if durations else None,
        "longest_holds": [
            {
                "clip_id": cut["clip_id"],
                "shot_id": cut["shot_id"],
                "start": round(cut["start"], 3),
                "duration": round(cut["duration"], 3),
                "kind": cut["kind"],
            }
            for cut in longest
        ],
        "densest_window": densest,
        "stall_count": len(stalls),
        "word_count": len(words_windowed),
        "word_sources": dict(sorted(word_sources.items())),
        "mean_words_per_s": round(len(words_windowed) / span, 3) if span > 0 else 0.0,
        "beat_count": len(beats_windowed) if beats_windowed is not None else None,
    }

    return {
        "schema": SCHEMA,
        "timeline": dict(timeline or {}),
        "duration_s": round(timeline_end, 3),
        "window_s": [round(low, 3), round(high, 3)],
        "chapters": [
            {
                "shot_id": chapter["shot_id"],
                "occurrence_id": chapter["occurrence_id"],
                "name": chapter["name"],
                "start": round(max(chapter["start"], low), 3),
                "end": round(min(chapter["end"], high), 3),
                "speech": next(r["speech"] for r in records if r["occurrence_id"] == chapter["occurrence_id"]),
            }
            for chapter in chapters
            if chapter["end"] > low and chapter["start"] < high
        ],
        "cuts": [
            {
                "index": cut["index"],
                "clip_id": cut["clip_id"],
                "shot_id": cut["shot_id"],
                "occurrence_id": cut["occurrence_id"],
                "chapter": cut["chapter"],
                "start": round(cut["start"], 3),
                "end": round(cut["end"], 3),
                "duration": round(cut["duration"], 3),
                "kind": cut["kind"],
                "kind_source": cut["kind_source"],
                "speech_seconds": round(cut["speech_seconds"], 3),
                "deliberate_hold": cut["deliberate_hold"],
                "stall": cut["stall"],
            }
            for cut in windowed_cuts
        ],
        "words": [
            {"start": round(first, 3), "end": round(second, 3), "source": source}
            for first, second, source in words_windowed
        ],
        "silences": [[a, b] for a, b in silences],
        "grid": {
            "step_s": GRID_STEP_SECONDS,
            "t": [round(t, 3) for t in grid],
            "words_per_s": [round(value, 3) for value in density],
            "words_per_s_norm": density_norm,
            "cuts_per_10s": [round(value, 3) for value in cut_rate],
        },
        "stalls": stalls,
        "beats": beats_windowed,
        "summary": summary,
        "notes": notes,
    }


__all__ = [
    "SCHEMA", "PacingError", "TARGET_RANGE", "clip_duration", "load_beats", "build_rhythm",
    "make_visualizer_layer",
]


def make_visualizer_layer():
    """Return the editorial rhythm layer through the V3 visualization contract.

    The import is lazy so the pure pacing model remains usable in a renderer-
    free read path. A pack-owned discovery shim can expose the returned Layer
    as ``LAYER`` under the V3 ``visualize_layers`` convention.
    """

    from PIL import ImageDraw

    from astrid.packs.rendering.actions.timeline_visualize.layers import Layer, LayerResult
    from astrid.packs.rendering.actions.timeline_visualize.layers.base import PALETTE, draw_text

    neighbours = 6

    def render(ctx) -> LayerResult:
        index = int(ctx.cut["index"])
        cuts = [cut for cut in ctx.cuts if abs(int(cut["index"]) - index) <= neighbours]
        image = ctx.panel(120, title="rhythm", axis=False)
        draw = ImageDraw.Draw(image)
        low, high = TARGET_RANGE
        longest = max(float(cut["duration"]) for cut in cuts) or 1.0
        top, bottom = 34, 104
        left, right = ctx.plot_left, ctx.plot_right
        width = (right - left) / max(1, len(cuts))

        def y_of(seconds: float) -> float:
            return bottom - min(1.0, seconds / max(longest, high)) * (bottom - top)

        draw.rectangle((left, y_of(high), right, y_of(low)), fill="#1d3a2f")
        draw_text(draw, (10, 30), f"target {low:g}–{high:g} s", 11, PALETTE["muted"])
        for position, cut in enumerate(cuts):
            x0 = left + position * width + 3
            seconds = float(cut["duration"])
            colour = PALETTE["cut"] if int(cut["index"]) == index else (
                PALETTE["good"] if low <= seconds <= high else PALETTE["warn"]
            )
            draw.rectangle((x0, y_of(seconds), x0 + width - 6, bottom), fill=colour)
            draw_text(draw, (x0, bottom + 2), f"#{cut['index']} {seconds:.1f}s", 11, PALETTE["ink"])
        durations = [float(cut["duration"]) for cut in cuts]
        seconds = float(ctx.cut["duration"])
        rank = sorted(durations, reverse=True).index(seconds) + 1
        verdict = "in range" if low <= seconds <= high else ("long" if seconds > high else "short")
        finding = (
            f"RHYTHM cut {index} is {seconds:.2f} s ({verdict}; target {low:g}–{high:g} s), "
            f"#{rank} longest of cuts {cuts[0]['index']}–{cuts[-1]['index']} "
            f"(median {statistics.median(durations):.2f} s)"
        )
        return LayerResult(image, [finding], "rhythm")

    return Layer(
        "rhythm",
        "this cut's length against its neighbours and the pacing target (editorial pack)",
        render,
        needs=("doc",),
        order=15,
    )
