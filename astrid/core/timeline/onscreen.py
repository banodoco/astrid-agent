"""The on-screen model (TRUTH.md §2.2): what is visible on a frame, and how every frame is covered.

Pure: no I/O in ``visible_at`` / ``coverage`` (``element_coverage`` reads the element registry, and only that).
Opacity, fades and the transition windows follow timeline-composition ``lib/fade.ts``, ``lib/clip-look.ts``
and ``lib/transition-spans.ts``; the universal clip fields are read through ``clip_fields.read``.

Frame arithmetic: a clip covers ``[start, end)`` with ``start = clip_start_frame`` and ``end = clip_end_frame``
(``duration.py``). Fade ramps and entrance/exit windows are in absolute frames, the same arithmetic as the
composition's per-Sequence (draw-local) frame.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astrid.core.timeline import clip_fields
from astrid.core.timeline.astrid_ns import read_app
from astrid.core.timeline.duration import clip_end_frame, clip_start_frame

COVER_OPACITY = 0.999
CONTENT_OPACITY = 0.05
FULL_FRAME_TOLERANCE = 1.0
DEFAULT_ANIMATION_FRAMES = 8
DEFAULT_TRANSITION_FRAMES = 12
FADE_TRANSITION_IDS = frozenset({"fade", "dip", "cross-fade", "crossfade"})
LAYOUT_KEYS = ("x", "y", "width", "height", "scale")
CROP_FIELDS = ("crop_top", "crop_right", "crop_bottom", "crop_left")
_ELEMENT_KINDS = ("effects", "animations", "transitions")


@dataclass(frozen=True)
class Visible:
    clip_id: str
    address: str
    track: str
    z: int  # 0 = top
    role: str  # "picture" | "background" | "content"
    opacity: float
    bounds: tuple[float, float, float, float] | None  # x0, y0, x1, y1 canvas px
    bounds_source: str  # "declared" | "full" | "unknown"
    full_frame: bool


@dataclass(frozen=True)
class Span:
    start_frame: int
    end_frame: int  # exclusive
    cls: str  # "COVERED" | "DIP" | "GAP"
    content: str | None  # COVERED only: "CONTENT" | "EMPTY"
    clips: tuple[str, ...]


@dataclass
class _Layer:
    """One listed clip, precomputed: everything that is constant or piecewise-linear in the frame."""

    clip_id: str
    address: str
    track: str
    z: int
    role: str
    start: int
    end: int
    base_opacity: float
    ramps: list[tuple[int, int, float, float]] = field(default_factory=list)  # lo, hi, y at lo, y at hi
    blocks: list[tuple[int, int]] = field(default_factory=list)  # full_frame off (scale/slide entrances)
    dips: list[tuple[int, int]] = field(default_factory=list)  # DIP-capable windows [lo, hi)
    bounds: tuple[float, float, float, float] | None = None
    bounds_source: str = "unknown"
    static_full: bool = False


# ---------------------------------------------------------------- element metadata


_PACKS_ROOT = Path(__file__).resolve().parents[2] / "packs"
_COVERAGE_CACHE: dict[str, dict] = {}
# Sealed S0's V3 rendering-element declarations omit these metadata values,
# though the frozen donor declarations used them to determine frame coverage.
# Keep this narrow semantic projection here until those unowned declarations
# are joined; a declaration present in the V3 manifest always takes priority.
_COVERAGE_COMPAT = {
    # The generic media clip (a video or image from the project's media) is the picture wherever it sits: its track
    # is the author's choice (`plate` is only the convention of Astrid's own cuts), and with no layout fields it fills
    # the canvas. Without an asset it shows nothing: covered, but empty.
    "media": {"coverage": "picture", "background_unless": ["asset"]},
    "am-snap-plate": {"coverage": "picture", "background_unless": ["asset", "params.src"]},
    "am-footage": {"coverage": "picture"},
    "am-presenter": {"coverage": "picture"},
}


def element_coverage(element_ids: Iterable[str]) -> dict[str, dict]:
    """``metadata.coverage`` (+ ``background_unless``) per element id; ``{}`` when an element declares none.

    Reads each element's ``element.yaml`` directly (cached per process): loading the whole element
    registry cost 2-4 s per CLI process, and check() runs on every edit.
    """
    return {element_id: _coverage_of(element_id) for element_id in element_ids}


def _coverage_of(element_id: str) -> dict:
    if element_id in _COVERAGE_CACHE:
        return _COVERAGE_CACHE[element_id]
    import yaml

    found: dict = {}
    # Receiving V3 packs put renderer-owned manifests under
    # ``<pack>/rendering/elements``; older packs use ``<pack>/elements``.
    paths = set(_PACKS_ROOT.glob(f"*/elements/**/{element_id}/element.yaml"))
    paths.update(_PACKS_ROOT.glob(f"*/rendering/elements/**/{element_id}/element.yaml"))
    for path in sorted(paths):
        try:
            doc = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except Exception:  # noqa: BLE001 - an unreadable manifest just means "declares nothing"
            continue
        meta = doc.get("metadata") if isinstance(doc.get("metadata"), Mapping) else {}
        if meta.get("coverage") is not None:
            found = {"coverage": meta["coverage"]}
            if "background_unless" in meta:
                found["background_unless"] = list(meta["background_unless"] or [])
        break
    if not found and element_id in _COVERAGE_COMPAT:
        found = dict(_COVERAGE_COMPAT[element_id])
    _COVERAGE_CACHE[element_id] = found
    return found


def _has_value(clip: Mapping[str, Any], path: str) -> bool:
    node: Any = clip
    for part in path.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return False
        node = node[part]
    return node is not None and node != ""


def role_of(clip: Mapping[str, Any], track: Any, elements: Mapping[str, Mapping[str, Any]]) -> str:
    """picture | background | content | none, from the element's declared coverage, else the track convention."""
    meta = elements.get(clip.get("clipType")) or {}
    coverage = meta.get("coverage")
    if coverage is not None:
        if coverage == "picture" and "background_unless" in meta:
            unless = meta.get("background_unless") or []
            if not any(_has_value(clip, path) for path in unless):
                return "background"
        return str(coverage)
    track_id = track if isinstance(track, str) else (track or {}).get("id")
    return "picture" if track_id == "plate" else "content"


# ---------------------------------------------------------------- building the layers


def _address(clip: Mapping[str, Any]) -> str:
    app = read_app(clip)  # dual read: app.astrid, else the flat legacy keys
    cut, layer = app.get("cut"), app.get("layer")
    if cut and layer:
        return f"{cut}.{layer}"
    return str(clip.get("id", "")).split(":")[-1]


def _frames(seconds: Any, fps: float) -> int:
    """Math.round(seconds * fps), the composition's secondsToFrames."""
    return math.floor(float(seconds) * fps + 0.5)


def _effect_seconds(clip: Mapping[str, Any], name: str) -> float | None:
    effects = clip.get("effects")
    raw: Any = None
    if isinstance(effects, Mapping):
        raw = effects.get(name)
    elif isinstance(effects, list):
        raw = next(
            (e[name] for e in effects if isinstance(e, Mapping) and _is_num(e.get(name))),
            None,
        )
    return float(raw) if _is_num(raw) and math.isfinite(float(raw)) and float(raw) > 0 else None


def _is_num(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _clamped(x: float, x0: float, x1: float, y0: float, y1: float) -> float:
    if x <= x0:
        return y0
    if x >= x1:
        return y1
    return y0 + (x - x0) / (x1 - x0) * (y1 - y0)


def layout_bounds(clip: Mapping[str, Any], canvas_w: float, canvas_h: float):
    """(bounds, source): no layout fields -> the canvas ("full"); else the declared rectangle."""
    params = clip.get("params") or {}
    src: dict[str, Any] = {}
    for key in LAYOUT_KEYS:
        if key in clip:
            src[key] = clip[key]
        elif isinstance(params, Mapping) and key in params:
            src[key] = params[key]
    if not src:
        return (0.0, 0.0, canvas_w, canvas_h), "full"
    try:
        x = float(src.get("x", 0))
        y = float(src.get("y", 0))
        scale = float(src.get("scale", 1))
        width = float(src.get("width", canvas_w)) * scale
        height = float(src.get("height", canvas_h)) * scale
    except (TypeError, ValueError):
        return None, "unknown"
    values = (x, y, width, height)
    if not all(math.isfinite(v) for v in values):
        return None, "unknown"
    return (x, y, x + width, y + height), "declared"


_layout_bounds = layout_bounds  # internal compatibility for existing callers


def _covers_canvas(bounds, canvas_w: float, canvas_h: float) -> bool:
    if bounds is None:
        return False
    x0, y0, x1, y1 = bounds
    tol = FULL_FRAME_TOLERANCE
    return x0 <= tol and y0 <= tol and x1 >= canvas_w - tol and y1 >= canvas_h - tol


def _is_cropped(clip: Mapping[str, Any]) -> bool:
    for name in CROP_FIELDS:
        value = clip_fields.read(clip, name)
        if _is_num(value) and float(value) > 0:
            return True
    return False


def _animation_frames(value: Mapping[str, Any], fps: float) -> int:
    duration = value.get("duration")
    if not _is_num(duration):
        return DEFAULT_ANIMATION_FRAMES
    return max(1, _frames(duration, fps))


def _build(config: Mapping[str, Any], fps: float, canvas: Mapping[str, Any], elements: Mapping[str, Any]) -> list[_Layer]:
    canvas_w = float(canvas["width"])
    canvas_h = float(canvas["height"])
    tracks = list(config.get("tracks") or [])
    track_index: dict[Any, int] = {}
    track_by_id: dict[Any, Mapping[str, Any]] = {}
    for index, track in enumerate(tracks):
        track_index.setdefault(track.get("id"), index)
        track_by_id.setdefault(track.get("id"), track)

    clips = list(config.get("clips") or [])
    listed: list[tuple[int, int, Mapping[str, Any], Mapping[str, Any], str, int, int]] = []
    for position, clip in enumerate(clips):
        track = track_by_id.get(clip.get("track")) or {}
        if track.get("kind") == "audio":
            continue
        role = role_of(clip, track, elements)
        if role == "none":
            continue
        listed.append((
            track_index.get(clip.get("track"), len(tracks)), -position, clip, track, role,
            clip_start_frame(clip, fps), clip_end_frame(clip, fps),
        ))
    listed.sort(key=lambda item: (item[0], item[1]))

    # Same-track adjacency, as the composition's transition planner: a transition needs a clip that starts
    # within a frame of its end (the last clip of a track has none, so no dip there).
    starts_by_track: dict[Any, list[int]] = {}
    for item in listed:
        starts_by_track.setdefault(item[2].get("track"), []).append(item[5])

    layers: list[_Layer] = []
    for z, (_, _, clip, track, role, start, end) in enumerate(listed):
        opacity = clip_fields.read(clip, "opacity")
        track_opacity = track.get("opacity", 1)
        layer = _Layer(
            clip_id=str(clip.get("id", "")),
            address=_address(clip),
            track=str(clip.get("track", "")),
            z=z,
            role=role,
            start=start,
            end=end,
            base_opacity=float(opacity if _is_num(opacity) else 1) * float(track_opacity),
        )
        picture = role == "picture"

        for name, edge in (("fade_in", "in"), ("fade_out", "out")):
            seconds = _effect_seconds(clip, name)
            frames = _frames(seconds, fps) if seconds is not None else 0
            if frames > 0:
                if edge == "in":
                    layer.ramps.append((start, start + frames, 0.0, 1.0))
                    if picture:
                        layer.dips.append((start, start + frames))
                else:
                    layer.ramps.append((end - frames, end, 1.0, 0.0))
                    if picture:
                        layer.dips.append((end - frames, end))

        entrance = clip_fields.read(clip, "entrance")
        if isinstance(entrance, Mapping) and isinstance(entrance.get("type"), str):
            frames = _animation_frames(entrance, fps)
            if entrance["type"].startswith("fade"):
                layer.ramps.append((start, start + frames, 0.0, 1.0))
                if picture:
                    layer.dips.append((start, start + frames))
            else:
                layer.blocks.append((start, start + frames))
                if picture:  # a declared entrance (scale-in, slide): the uncovered frames are intended
                    layer.dips.append((start, start + frames))

        exit_ = clip_fields.read(clip, "exit")
        if isinstance(exit_, Mapping) and isinstance(exit_.get("type"), str):
            frames = _animation_frames(exit_, fps)
            if exit_["type"].startswith("fade"):
                layer.ramps.append((end - frames, end, 1.0, 0.0))
                if picture:
                    layer.dips.append((end - frames, end))
            else:
                layer.blocks.append((end - frames, end))
                if picture:  # a declared exit animation: intended, not a tear
                    layer.dips.append((end - frames, end))

        transition = clip_fields.read(clip, "transition")
        if isinstance(transition, Mapping) and transition.get("id") in FADE_TRANSITION_IDS:
            has_next = any(end - 1 <= s <= end for s in starts_by_track.get(clip.get("track"), []) if s != start)
            if has_next and clip.get("clipType") != "effect-layer":
                frames = transition.get("durationFrames")
                width = int(frames) if _is_num(frames) and int(frames) > 0 else DEFAULT_TRANSITION_FRAMES
                layer.dips.append((end - width, end))

        layer.bounds, layer.bounds_source = layout_bounds(clip, canvas_w, canvas_h)
        layer.static_full = (
            role in ("picture", "background")
            and _covers_canvas(layer.bounds, canvas_w, canvas_h)
            and not _is_cropped(clip)
        )
        layers.append(layer)
    return layers


# ---------------------------------------------------------------- per-frame evaluation


def _opacity(layer: _Layer, frame: int) -> float:
    value = layer.base_opacity
    for lo, hi, y0, y1 in layer.ramps:
        value *= _clamped(frame, lo, hi, y0, y1)
    return value


def _in_any(windows: list[tuple[int, int]], frame: int) -> bool:
    return any(lo <= frame < hi for lo, hi in windows)


def _visible(layer: _Layer, frame: int) -> Visible:
    return Visible(
        clip_id=layer.clip_id,
        address=layer.address,
        track=layer.track,
        z=layer.z,
        role=layer.role,
        opacity=_opacity(layer, frame),
        bounds=layer.bounds,
        bounds_source=layer.bounds_source,
        full_frame=layer.static_full and not _in_any(layer.blocks, frame),
    )


def visible_at(config, frame, fps, canvas, elements) -> list[Visible]:
    """Every listed layer on screen at ``frame``, top first (z ascending)."""
    return [
        _visible(layer, frame)
        for layer in _build(config, fps, canvas, elements)
        if layer.start <= frame < layer.end
    ]


def _intersects_canvas(bounds, canvas_w: float, canvas_h: float) -> bool:
    if bounds is None:
        return True  # unknown: do not call it empty
    x0, y0, x1, y1 = bounds
    return x1 > 0 and y1 > 0 and x0 < canvas_w and y0 < canvas_h


def _classify(items: list[tuple[_Layer, Visible]], frame: int, canvas_w: float, canvas_h: float):
    """(cls, content, addresses) for one frame, given the visible layers (z order)."""
    visible = [v for _, v in items]
    full = sorted((v.opacity for v in visible if v.full_frame), reverse=True)
    covered = bool(full) and (
        full[0] >= COVER_OPACITY or (len(full) > 1 and full[0] + full[1] >= COVER_OPACITY)
    )
    addresses = tuple(v.address for v in visible)
    if covered:
        has_content = any(
            v.role in ("picture", "content")
            and v.opacity >= CONTENT_OPACITY
            and _intersects_canvas(v.bounds, canvas_w, canvas_h)
            for v in visible
        )
        return "COVERED", ("CONTENT" if has_content else "EMPTY"), addresses
    if any(_in_any(layer.dips, frame) for layer, _ in items):
        return "DIP", None, addresses
    return "GAP", None, addresses


def _change_points(layers: list[_Layer], lo: int, hi: int) -> set[int]:
    points = {lo, hi}
    for layer in layers:
        for p in (layer.start, layer.end):
            points.add(p)
        for a, b, _, _ in layer.ramps:
            points.update(range(a, b + 1))  # per-frame inside a ramp (the opacity moves every frame)
        for a, b in layer.blocks + layer.dips:
            points.add(a)
            points.add(b)
    return {p for p in points if lo <= p <= hi}


def coverage(config, fps, canvas, elements, *, start_frame=0, end_frame=None) -> list[Span]:
    """Run-length encoded per-frame coverage over ``[start_frame, end_frame)``."""
    layers = _build(config, fps, canvas, elements)
    if end_frame is None:  # the film's length: every clip counts, audio included (duration.py)
        end_frame = max((clip_end_frame(clip, fps) for clip in config.get("clips") or []), default=0)
    if end_frame <= start_frame:
        return []
    canvas_w = float(canvas["width"])
    canvas_h = float(canvas["height"])
    points = sorted(_change_points(layers, start_frame, end_frame))
    pending = sorted(layers, key=lambda layer: layer.start)
    nxt = 0
    active: list[_Layer] = []
    spans: list[Span] = []
    for index in range(len(points) - 1):
        frame = points[index]
        seg_end = points[index + 1]
        while nxt < len(pending) and pending[nxt].start <= frame:
            active.append(pending[nxt])
            nxt += 1
        active = [layer for layer in active if layer.end > frame]
        items = sorted(((layer, _visible(layer, frame)) for layer in active), key=lambda item: item[1].z)
        cls, content, addresses = _classify(items, frame, canvas_w, canvas_h)
        if spans and spans[-1].end_frame == frame and spans[-1].cls == cls and spans[-1].content == content:
            last = spans[-1]
            spans[-1] = Span(last.start_frame, seg_end, cls, content, last.clips)
        else:
            spans.append(Span(frame, seg_end, cls, content, addresses))
    return spans
