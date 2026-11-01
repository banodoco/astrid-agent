"""Timeline data → what moves, where, and when (no pixels).

A small, pure model of a timeline window that the motion layers and
``timelines lint`` share:

- :class:`Element`: one clip with absolute timeline seconds, its params and
  its asset's pixel size (registry ``resolution``).
- :func:`events`: entrances, exits and element keyframes in timeline seconds.
- :func:`props_at`: an element's animated properties on one frame (x/y,
  visibility, reveal progress, zoom, mouth state …). An element that ships
  ``motion.ts`` answers itself (``element_motion.py``: the component's own
  maths, evaluated with Node); the tables below are the fallback mirror.
- :func:`boxes_at`: declared on-screen bounds (screen px at the canvas size),
  including a presenter's face box derived from its eye/mouth anchors.
- :func:`words`, :func:`beats_in`, :func:`sfx`: the audio side, in timeline seconds.

Element knowledge is a table keyed by ``clipType``; an element without an
entry still has entrances/exits and a visibility curve.
"""
from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from astrid.core.timeline.astrid_ns import read_app

CANVAS = (1920, 1080)
LOGICAL_PX = 6  # astrid_motion: one logical px = 6 screen px at 1920x1080
LOGICAL_W, LOGICAL_H = 320, 180
TITLE_SAFE = 0.90
ACTION_SAFE = 0.93
FRAME_KEYS = ("at", "appearAt", "atFrame", "startAt")


def _map(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _num(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _int(value: Any, low: int, high: int, default: int) -> int:
    number = _num(value, default)
    return int(min(high, max(low, round(number))))


@dataclass
class Element:
    id: str
    type: str
    track: str
    start: float
    end: float
    params: Mapping[str, Any]
    clip: Mapping[str, Any]
    audio: bool = False
    asset: str | None = None
    asset_size: tuple[int, int] | None = None
    occurrence_id: str = ""
    # the element's own motion maths (element_motion.ElementMaths), shared by one run
    maths: Any = field(default=None, repr=False, compare=False)

    @property
    def key(self) -> str:
        return f"{self.id}@{self.start:.4f}"

    @property
    def sequence(self) -> str | None:
        """``app.sequence``: this clip is one step of a sequence (one picture cut, many clips)."""
        value = read_app(self.clip).get("sequence")
        return str(value) if isinstance(value, (str, int)) and str(value) else None

    @property
    def short_id(self) -> str:
        return self.id.rsplit(":", 1)[-1]

    @property
    def label(self) -> str:
        """``am-sprite C-01`` / ``am-type "2"``: what an editor calls it."""
        name = self.type
        if self.asset:
            name += " " + self.asset.rsplit(":", 1)[-1]
        chip = _map(self.params.get("chip")).get("text")
        text = chip if self.type == "am-presenter" and isinstance(chip, str) else _text(self.params)
        if text:
            name += f' "{text[:18]}{"…" if len(text) > 18 else ""}"'
        return name


def _text(params: Mapping[str, Any]) -> str:
    for key in ("text", "title", "label"):
        value = params.get(key)
        if isinstance(value, str) and value.strip():
            return " ".join(value.split())
    chip = _map(params.get("chip")).get("text")
    return chip.strip() if isinstance(chip, str) else ""


def _resolution(info: Mapping[str, Any] | None) -> tuple[int, int] | None:
    raw = _map(info).get("resolution")
    if isinstance(raw, str) and "x" in raw:
        try:
            width, height = (int(part) for part in raw.lower().split("x", 1))
            return width, height
        except ValueError:
            return None
    width, height = _map(info).get("width"), _map(info).get("height")
    if isinstance(width, int) and isinstance(height, int):
        return width, height
    return None


def elements_from_occurrences(
    occurrences: Iterable[Mapping[str, Any]],
    registry: Mapping[str, Any] | None = None,
    *,
    declarations: Iterable[Mapping[str, Any]] | Mapping[str, Any] | None = None,
    bindings: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[Element]:
    """Every clip of ``astrid.core.timeline.cuts`` occurrences as an :class:`Element`."""
    from . import element_motion

    occurrences = list(occurrences)
    admitted = bindings
    if admitted is not None:
        clip_ids = {
            clip.get("id")
            for occurrence in occurrences
            for span in occurrence.get("spans") or ()
            for clip in (_map(span.get("clip")),)
            if isinstance(clip.get("id"), str) and clip.get("id")
        }
        # A native map that names a clip outside this immutable snapshot is not
        # an admissible partial handoff; keep every clip on the mirror.
        if any(not isinstance(key, str) or key not in clip_ids for key in admitted):
            admitted = {}
    declaration_rows: list[Mapping[str, Any]] = []
    declaration_keys: set[str] = set()
    found: list[Element] = []
    for occurrence in occurrences:
        local = _map(occurrence.get("registry")) or _map(registry)
        for span in occurrence.get("spans") or ():
            source_clip = _map(span.get("clip"))
            clip: Mapping[str, Any] = source_clip
            clip_id = source_clip.get("id")
            binding = admitted.get(clip_id) if admitted is not None and isinstance(clip_id, str) else None
            if isinstance(binding, Mapping):
                authored_ref = source_clip.get("elementRef")
                authored_identity = element_motion.canonical_element_identity(authored_ref) if isinstance(authored_ref, Mapping) else None
                bound_identity = element_motion.canonical_element_identity(binding.get("element_ref"))
                if ("elementRef" in source_clip and not isinstance(authored_ref, Mapping)) or (
                    isinstance(authored_ref, Mapping) and authored_identity != bound_identity
                ):
                    binding = None
                else:
                    projected = deepcopy(dict(source_clip))
                    projected["elementRef"] = deepcopy(binding["element_ref"])
                    projected["elementDescriptor"] = deepcopy(binding["declaration"])
                    projected["elementResources"] = deepcopy(binding["declaration"].get("resources", []))
                    clip = projected
                    identity = element_motion.canonical_element_identity(binding["element_ref"])
                    if identity and identity not in declaration_keys:
                        declaration_rows.append(binding["declaration"])
                        declaration_keys.add(identity)
            asset = clip.get("asset") if isinstance(clip.get("asset"), str) else None
            info = local.get(asset) if asset else None
            if info is None and asset and ":" in asset:
                info = local.get(asset.rsplit(":", 1)[-1])
            found.append(Element(
                id=str(span.get("id") or clip.get("id") or ""),
                type=str(span.get("type") or clip.get("clipType") or "media"),
                track=str(span.get("track") or ""),
                start=float(span["start"]),
                end=float(span["end"]),
                params=_map(clip.get("params")),
                clip=clip,
                audio=bool(span.get("audio")),
                asset=asset,
                asset_size=_resolution(info),
                occurrence_id=str(occurrence.get("occurrence_id") or ""),
            ))
    found.sort(key=lambda element: (element.start, element.id))
    from .element_motion import ElementMaths

    if declarations is None:
        # This is an action/model projection, not the standalone declaration
        # inventory API.  An absent producer handoff is an explicit empty
        # admission, so ElementMaths cannot promote a current pack by lookup.
        declarations = tuple(declaration_rows) if bindings is not None else ()
    maths = ElementMaths(found, declarations=declarations)
    for element in found:
        if maths.wants(element):
            element.maths = maths
    return found


def own_motion(element: Element, clip_frame: int, fps: float | None = None) -> dict[str, float] | None:
    """The element's own values (its ``motion.ts``) on a clip frame, or ``None``."""
    return element.maths.at(element, clip_frame, fps) if element.maths is not None else None


def view_of(element: Element, clip_frame: int, fps: float | None = None) -> tuple[float, float, float]:
    """``(scale, originX, originY)``: the element's own view, else the mirrored ``presenter_view``."""
    own = own_motion(element, clip_frame, fps)
    if own and all(k in own for k in ("zoom", "pan_x", "pan_y")):
        return LOGICAL_PX * own["zoom"], -own["pan_x"], -own["pan_y"]
    return presenter_view(element.params, clip_frame)


def in_window(elements: Iterable[Element], start: float, end: float, *, tolerance: float = 0.01) -> list[Element]:
    """Elements on screen in ``[start, end)``; edges within ``tolerance`` s (float noise, < 1 frame) don't count."""
    return [e for e in elements if e.end > start + tolerance and e.start < end - tolerance]


# ---------------------------------------------------------------- audio side

@dataclass(frozen=True)
class Word:
    start: float
    end: float
    text: str


def words(elements: Iterable[Element]) -> list[Word]:
    """VO words from clip ``app.words`` (clip-relative ``[start, end, text?]``)."""
    found: list[Word] = []
    for element in elements:
        if not element.audio:
            continue
        for item in read_app(element.clip).get("words") or ():
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                text = item[2] if len(item) > 2 and isinstance(item[2], str) else "·"
                found.append(Word(element.start + _num(item[0]), element.start + _num(item[1]), text))
            elif isinstance(item, Mapping):
                found.append(Word(element.start + _num(item.get("start")), element.start + _num(item.get("end")),
                                  str(item.get("text") or "·")))
    return sorted(found, key=lambda word: word.start)


def sfx(elements: Iterable[Element]) -> list[tuple[float, float, str]]:
    """Sound effects: clips on an ``sfx`` track, named by their asset."""
    return sorted(
        (e.start, e.end, (e.asset or e.short_id).rsplit(":", 1)[-1].replace("sfx-", "").lstrip("0123456789-"))
        for e in elements if e.audio and e.track == "sfx"
    )


def beats_in(beats: Mapping[str, Any] | None, elements: Iterable[Element], start: float, end: float) -> dict[str, list]:
    """Music beats/downbeats/hits mapped through the music clips into timeline seconds.

    ``beats`` is a cue's beats.json (cue seconds). A music clip at timeline
    ``t0`` playing the cue from ``from`` maps cue time ``c`` to ``t0 + c - from``.
    Without a music clip the cue is assumed to start at 0.
    """
    if not isinstance(beats, Mapping):
        return {"beats": [], "downbeats": [], "hits": []}
    music = [e for e in elements if e.audio and e.track == "music"]
    windows = [(e.start, e.end, _num(e.clip.get("from"))) for e in music] or [(0.0, float("inf"), 0.0)]

    def mapped(cue: float) -> float | None:
        for t0, t1, offset in windows:
            t = t0 + cue - offset
            if t0 - 1e-6 <= t < t1 and start - 1e-6 <= t <= end + 1e-6:
                return t
        return None

    out: dict[str, list] = {"beats": [], "downbeats": [], "hits": []}
    for key in ("beats", "downbeats"):
        for value in beats.get(key) or ():
            t = mapped(_num(value, -1.0))
            if t is not None:
                out[key].append(t)
    for hit in beats.get("hits") or ():
        if isinstance(hit, Mapping):
            t = mapped(_num(hit.get("t"), -1.0))
            if t is not None:
                out["hits"].append((t, str(hit.get("kind") or "hit")))
    return out


def doc_beats(elements: Iterable[Element], start: float, end: float) -> dict[str, list]:
    """Beats the timeline itself carries: ``app.beats`` on music clips (cue seconds).

    The EDL builder copies each music clip's slice of the cue's beats.json there
    (as VO clips carry ``app.words``), so the sync layer and lint need no
    sidecar file. ``--beats FILE`` overrides with :func:`beats_in`.
    """
    out: dict[str, list] = {"beats": [], "downbeats": [], "hits": []}
    sources: set[str] = set()
    for element in elements:
        if not element.audio:
            continue
        beats = _map(read_app(element.clip).get("beats"))
        if not beats:
            continue
        offset = _num(element.clip.get("from"))
        if beats.get("source"):
            sources.add(str(beats["source"]))

        def mapped(cue: Any) -> float | None:
            if isinstance(cue, bool) or not isinstance(cue, (int, float)):
                return None
            t = element.start + float(cue) - offset
            inside_clip = element.start - 1e-6 <= t < element.end + 1e-6
            return t if inside_clip and start - 1e-6 <= t <= end + 1e-6 else None

        for key in ("beats", "downbeats"):
            out[key].extend(t for t in (mapped(v) for v in beats.get(key) or ()) if t is not None)
        for hit in beats.get("hits") or ():
            if isinstance(hit, (list, tuple)) and hit:
                t, kind = mapped(hit[0]), str(hit[1]) if len(hit) > 1 else "hit"
            elif isinstance(hit, Mapping):
                t, kind = mapped(hit.get("t")), str(hit.get("kind") or "hit")
            else:
                continue
            if t is not None:
                out["hits"].append((t, kind))
    for key in ("beats", "downbeats"):
        out[key] = sorted(set(round(t, 6) for t in out[key]))
    out["hits"] = sorted(set((round(t, 6), kind) for t, kind in out["hits"]))
    if sources:
        out["source"] = sorted(sources)
    return out


def timeline_beats(override: Mapping[str, Any] | None, elements: Iterable[Element], start: float, end: float) -> dict[str, list]:
    """``--beats`` when given, else the beats the music clips carry."""
    elements = list(elements)
    if isinstance(override, Mapping) and override:
        mapped = beats_in(override, elements, start, end)
        mapped["source"] = ["--beats"]
        return mapped
    return doc_beats(elements, start, end)


# ---------------------------------------------------------------- events

@dataclass(frozen=True)
class Event:
    t: float
    kind: str  # enter | exit | key | cut
    element: str
    label: str
    style: str = ""


def _enter_style(element: Element) -> str:
    params = element.params
    if element.type == "am-sprite":
        return str(params.get("enter") or "cut")
    if element.type == "am-type":
        return str(params.get("reveal") or "slideUp")
    if element.type == "am-snap-plate":
        return str(params.get("enter") or "cut")
    if element.type == "am-callout":
        return "card+connector"
    return "cut"


def accents(element: Element, fps: float) -> list[Event]:
    """Events an editor places on a word or beat: entrances, swaps, value flips,
    and keyframes of an element with at most three of them (a longer run of
    keyframes is continuous motion, judged in the motion sheet, not a hit)."""
    found = [e for e in events(element, fps) if e.kind != "exit" and e.style != "punch"]
    keys = [e for e in found if e.kind == "key" and e.style == "step" and "→ (" in e.label]
    if len(keys) > 3:
        found = [e for e in found if e not in keys]
    return found


def events(element: Element, fps: float) -> list[Event]:
    """Entrance, exit and keyframes of one visual element, in timeline seconds."""
    if element.audio:
        return []
    found = [Event(element.start, "enter", element.id, element.label, _enter_style(element)),
             Event(element.end, "exit", element.id, element.label, str(element.params.get("exitWipe") or "cut"))]
    params = element.params
    for key in params.get("keyframes") or ():
        if isinstance(key, Mapping) and _num(key.get("frame"), 0) > 0:
            found.append(Event(element.start + _num(key.get("frame")) / fps, "key", element.id,
                               f"{element.label} → ({key.get('x')},{key.get('y')})", "step"))
    for frame in params.get("punchAt") or ():
        found.append(Event(element.start + _num(frame) / fps, "key", element.id, f"{element.label} punch", "punch"))
    push = _map(params.get("push"))
    if push and _num(push.get("frames")) > 0:
        found.append(Event(element.start + _num(push.get("at")) / fps, "key", element.id,
                           f"{element.label} push to {push.get('to')}x over {_num(push.get('frames')) / fps:.1f}s", "ease"))
    if params.get("underlineAt") is not None:
        found.append(Event(element.start + _num(params.get("underlineAt")) / fps, "key", element.id,
                           f"{element.label} underline", "ease"))
    chip = _map(params.get("chip"))
    if chip.get("swapAt") is not None:
        found.append(Event(element.start + _num(chip.get("swapAt")) / fps, "key", element.id,
                           f"chip → {chip.get('swapTo')}", "swap"))

    def walk(value: Any, path: str) -> None:
        if isinstance(value, Mapping):
            key = next((k for k in FRAME_KEYS if isinstance(value.get(k), (int, float)) and not isinstance(value.get(k), bool)), None)
            if key is not None and path:
                label = next((str(value[k]) for k in ("text", "label", "title", "name") if isinstance(value.get(k), str)), path)
                found.append(Event(element.start + _num(value[key]) / fps, "key", element.id,
                                   f"{element.label.split(' ')[0]} → {label[:20]}", "step"))
            for child_key, child in value.items():
                if isinstance(child, (Mapping, list)) and child_key not in ("keyframes",):
                    walk(child, f"{path}.{child_key}" if path else str(child_key))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                if isinstance(child, Mapping):
                    walk(child, f"{path}[{index}]")

    walk(params, "")
    return sorted({(e.t, e.kind, e.label): e for e in found}.values(), key=lambda e: (e.t, e.kind))


# ---------------------------------------------------------------- presenter

def hash_unit(seed: int, index: int) -> float:
    """``hashUnit`` from astrid_motion/_shared/am.tsx (32-bit integer hash)."""
    def imul(a: int, b: int) -> int:
        return (a * b) & 0xFFFFFFFF

    h = (int(seed) ^ imul(index + 1, 0x9E3779B1)) & 0xFFFFFFFF
    h = imul(h ^ (h >> 16), 0x85EBCA6B)
    h = imul(h ^ (h >> 13), 0xC2B2AE35)
    h = (h ^ (h >> 16)) & 0xFFFFFFFF
    return h / 4294967296


MOUTH_CYCLE = ("closed", "half", "open")
GAP_HOLD_S = 0.12


def presenter_words(element: Element) -> list[tuple[float, float]]:
    """The presenter's own lip-sync spans (clip-relative seconds)."""
    spans = []
    for item in element.params.get("words") or ():
        if isinstance(item, (list, tuple)) and len(item) >= 2:
            spans.append((_num(item[0]), _num(item[1])))
    return spans


def mouth_state(element: Element, clip_frame: int, fps: float) -> str:
    """``mouthStateAt`` from am-presenter/presenter-core.ts (the element's own value when it can answer)."""
    own = own_motion(element, clip_frame, fps)
    if own is not None and "mouth" in own:
        return {0.0: "closed", 0.5: "half", 1.0: "open"}.get(own["mouth"], "closed")
    ordered = sorted(
        (round(s * fps), max(round(s * fps) + 1, round(e * fps))) for s, e in presenter_words(element)
    )
    index = -1
    for i, (start, _end) in enumerate(ordered):
        if start <= clip_frame:
            index = i
        else:
            break
    if index < 0:
        return "closed"
    start, end = ordered[index]
    offset = math.floor(hash_unit(_int(element.params.get("seed"), -2**31, 2**31, 7), index) * 3)
    if clip_frame < end:
        return MOUTH_CYCLE[(clip_frame - start + offset) % 3]
    gap = ordered[index + 1][0] - end if index + 1 < len(ordered) else float("inf")
    if gap / fps <= GAP_HOLD_S:
        return MOUTH_CYCLE[(end - 1 - start + offset) % 3]
    return "closed"


def pushed_zoom(params: Mapping[str, Any], clip_frame: int) -> float:
    """``pushedZoom`` from presenter-core.ts: the base zoom, or a slow push in progress."""
    base = round(min(3.0, max(1.0, _num(params.get("zoom"), 1.0))))
    push = _map(params.get("push"))
    frames = _num(push.get("frames"), 0.0)
    if not push or frames <= 0 or not isinstance(push.get("to"), (int, float)):
        return float(base)
    t = min(1.0, max(0.0, (clip_frame - _num(push.get("at"), 0.0)) / frames))
    return base + (min(4.0, max(1.0, float(push["to"]))) - base) * t


def presenter_view(params: Mapping[str, Any], clip_frame: int) -> tuple[float, float, float]:
    """``presenterView`` (no bob): ``(scale, originX, originY)`` in screen px."""
    base = pushed_zoom(params, clip_frame)
    punched = any(_num(p) <= clip_frame < _num(p) + 6 for p in params.get("punchAt") or ())
    zoom = min(base + 1, 4) if punched else base
    scale = LOGICAL_PX * zoom
    view_w, view_h = CANVAS[0] / scale, CANVAS[1] / scale
    focus = _map(params.get("focus"))
    pan = _map(params.get("pan"))
    steps = clip_frame // _int(params.get("stepFrames"), 2, 3, 2)
    fx = _num(focus.get("x"), LOGICAL_W / 2) + _num(pan.get("dx")) * steps
    fy = _num(focus.get("y"), LOGICAL_H / 2) + _num(pan.get("dy")) * steps
    fx = min(max(fx, view_w / 2), LOGICAL_W - view_w / 2)
    fy = min(max(fy, view_h / 2), LOGICAL_H - view_h / 2)
    if base == round(base):  # a held zoom stays on whole logical px; a push glides
        fx, fy = round(fx), round(fy)
    return scale, round(CANVAS[0] / 2 - fx * scale), round(CANVAS[1] / 2 - fy * scale)


def face_box(element: Element, clip_frame: int = 0) -> tuple[float, float, float, float] | None:
    """Screen box of a presenter's face from its eye and mouth anchors (logical px)."""
    params = element.params
    eyes = [e for e in params.get("eyes") or () if isinstance(e, Mapping)]
    mouth = _map(params.get("mouth"))
    if not eyes or not mouth:
        return None
    left = min(_num(e.get("x")) for e in eyes)
    right = max(_num(e.get("x")) + _num(e.get("w"), 3) for e in eyes)
    span = max(8.0, right - left)
    top = min(_num(e.get("y")) for e in eyes) - 0.8 * span
    bottom = _num(mouth.get("y")) + 0.5 * span
    left, right = left - 0.4 * span, right + 0.4 * span
    scale, ox, oy = view_of(element, clip_frame)
    return (ox + left * scale, oy + top * scale, ox + right * scale, oy + bottom * scale)


# Full-frame picture elements: real footage, plates and procedural backgrounds.
PLATE_TYPES = ("am-snap-plate", "am-churn", "am-footage", "am-droste", "am-seasons")


def declared_face(element: Element) -> tuple[float, float, float, float] | None:
    """A face zone declared on the clip (``params.faceZone`` {x, y, w, h}, canvas px).

    Real-footage slots (am-footage) and slot-tagged placeholders carry the zone
    where the filmed face must sit; it wins over a face derived from anchors.
    """
    zone = _map(element.params.get("faceZone"))
    if not all(key in zone for key in ("x", "y", "w", "h")):
        return None
    x, y, w, h = (_num(zone.get(key)) for key in ("x", "y", "w", "h"))
    return (x, y, x + w, y + h) if w > 0 and h > 0 else None


def mouth_box(element: Element, clip_frame: int = 0) -> tuple[float, float, float, float] | None:
    mouth = _map(element.params.get("mouth"))
    if not mouth:
        return None
    scale, ox, oy = view_of(element, clip_frame)
    x, y, w = _num(mouth.get("x")), _num(mouth.get("y")), _num(mouth.get("w"), 16)
    return (ox + (x - 2) * scale, oy + (y - 3) * scale, ox + (x + w + 2) * scale, oy + (y + 5) * scale)


# ---------------------------------------------------------------- properties

def _ease_out_cubic(t: float) -> float:
    u = 1 - min(1.0, max(0.0, t))
    return 1 - u * u * u


def _sprite_props(element: Element, frame: int, fps: float) -> dict[str, float]:
    params = element.params
    step = _int(params.get("stepFrames"), 2, 3, 2)
    sf = (frame // step) * step
    x, y = _num(params.get("x")), _num(params.get("y"))
    for key in sorted((k for k in params.get("keyframes") or () if isinstance(k, Mapping)), key=lambda k: _num(k.get("frame"))):
        if _num(key.get("frame")) <= sf:
            x, y = _num(key.get("x"), x), _num(key.get("y"), y)
    visible, dx, dy = 1.0, 0.0, 0.0
    enter = params.get("enter") or "cut"
    if enter == "stamp" and frame < 2:
        visible, dy = (1.0 if frame == 0 else 0.0), -1.0
    elif enter == "slideIn":
        distance = max(0.0, _num(params.get("slideDistance"), 24))
        k = min(4, frame // step)
        remaining = round(distance * (4 - k) / 4)
        side = params.get("slideFrom") or "left"
        dx = -remaining if side == "left" else remaining if side == "right" else 0.0
        dy = -remaining if side == "top" else remaining if side == "bottom" else dy
    blinks = params.get("blinkAt")
    blinks = blinks if isinstance(blinks, list) else ([] if blinks is None else [blinks])
    if any(_num(b) <= frame < _num(b) + 2 for b in blinks) and not params.get("frames"):
        visible = 0.0
    return {"x": (x + dx) * LOGICAL_PX, "y": (y + dy) * LOGICAL_PX, "visible": visible}


def _type_props(element: Element, frame: int, fps: float) -> dict[str, float]:
    params = element.params
    reveal = params.get("reveal") or "slideUp"
    text = _text(params)
    slide = (1 - _ease_out_cubic(frame / 8)) if reveal == "slideUp" and frame < 8 else 0.0
    chars = min(1.0, (frame // 2 + 1) / max(1, len(text))) if reveal == "type" else 1.0
    return {"y": _num(params.get("y"), 96) + slide * _num(params.get("size"), 96), "reveal": chars,
            "visible": 1.0 if chars > 0 else 0.0}


def _callout_props(element: Element, frame: int, fps: float) -> dict[str, float]:
    params = element.params
    draw = max(1, round(_num(params.get("drawFrames"), 12)))
    step = _int(params.get("stepFrames"), 2, 3, 2)
    return {"connector": min(1.0, ((frame // step) * step) / draw), "visible": 1.0}


def _presenter_props(element: Element, frame: int, fps: float) -> dict[str, float]:
    state = mouth_state(element, frame, fps)
    scale, _ox, _oy = presenter_view(element.params, frame)
    return {"mouth": {"closed": 0.0, "half": 0.5, "open": 1.0}[state], "zoom": scale / LOGICAL_PX, "visible": 1.0}


def _plate_props(element: Element, frame: int, fps: float) -> dict[str, float]:
    scale, ox, oy = presenter_view(element.params, frame)
    return {"zoom": scale / LOGICAL_PX, "pan_x": -ox, "pan_y": -oy, "visible": 1.0}


def _flap_props(element: Element, frame: int, fps: float) -> dict[str, float]:
    values = sorted((v for v in element.params.get("values") or () if isinstance(v, Mapping)), key=lambda v: _num(v.get("at")))
    index = sum(1 for v in values if _num(v.get("at")) <= frame)
    return {"value": float(index), "visible": 1.0}


PROPS: dict[str, Callable[[Element, int, float], dict[str, float]]] = {
    "am-sprite": _sprite_props,
    "am-type": _type_props,
    "am-callout": _callout_props,
    "am-presenter": _presenter_props,
    "am-snap-plate": _plate_props,
    "am-flap": _flap_props,
}


def props_at(element: Element, t: float, fps: float) -> dict[str, float]:
    """Animated properties at timeline second ``t`` (empty when not on screen)."""
    if not element.start - 1e-6 <= t < element.end - 1e-6:
        return {}
    frame = int(math.floor((t - element.start) * fps + 1e-6))
    own = own_motion(element, frame, fps)
    if own is not None:
        return own
    model = PROPS.get(element.type)
    return model(element, frame, fps) if model else {"visible": 1.0}


# ---------------------------------------------------------------- bounds

@dataclass
class Box:
    element: Element
    kind: str  # sprite | text | card | face | panel | plate
    rect: tuple[float, float, float, float]
    label: str
    text_px: float | None = None
    notes: list[str] = field(default_factory=list)


def _wrap_lines(text: str, size: float, width: float, char_em: float) -> int:
    if not text:
        return 0
    per_line = max(1, int(width / max(1.0, size * char_em)))
    lines = 0
    for paragraph in text.split("\n"):
        words_ = paragraph.split()
        current = 0
        lines += 1
        for word in words_:
            need = len(word) + (1 if current else 0)
            if current and current + need > per_line:
                lines += 1
                current = len(word)
            else:
                current += need
    return lines


# Body text sizes (screen px before any zoom) of elements that draw UI text.
INTERNAL_TEXT_PX = {"am-discord": 17.0, "am-ui-sketch": 14.0}


def _internal_zoom(element: "Element") -> float:
    params = element.params
    if element.type == "am-discord":
        frame = _map(params.get("frame"))
        if frame.get("width") and frame.get("height"):
            return min(CANVAS[0] / _num(frame.get("width"), CANVAS[0]), CANVAS[1] / _num(frame.get("height"), CANVAS[1]))
        return 1.0
    return _num(params.get("scale"), 1.0) or 1.0


def boxes_at(element: Element, t: float, fps: float) -> list[Box]:
    """Declared on-screen bounds of one element at ``t`` (screen px, 1920x1080)."""
    if element.audio or not element.start - 1e-6 <= t < element.end - 1e-6:
        return []
    params = element.params
    frame = int(math.floor((t - element.start) * fps + 1e-6))
    kind = element.type
    declared = declared_face(element)
    if kind == "am-sprite":
        props = _sprite_props(element, frame, fps)
        scale = _int(params.get("scale"), 1, 24, 6)
        frames = _map(params.get("frames"))
        size = ((int(_num(frames.get("frameWidth"))), int(_num(frames.get("frameHeight"))))
                if frames.get("frameWidth") else element.asset_size)
        if not size:
            return [Box(element, "sprite", (props["x"], props["y"], props["x"] + 6, props["y"] + 6), element.label,
                        notes=["asset size unknown (registry has no resolution)"])]
        return [Box(element, "sprite", (props["x"], props["y"], props["x"] + size[0] * scale, props["y"] + size[1] * scale),
                    element.label)]
    if kind == "am-type":
        from astrid.core.timeline import text_fit

        measured = text_fit.type_box({**dict(params), "text": _text(params)})
        if measured is not None:  # the fonts' own widths (the estimate below ran short: a box ending at "ADAP")
            return [Box(element, "text", (measured["x0"], measured["y0"], measured["x1"], measured["y1"]), element.label,
                        text_px=measured["size"])]
        size = _num(params.get("size"), 28 if params.get("font") == "label" else 96)
        width = _num(params.get("width"), 1200)
        x, y = _num(params.get("x"), 96), _num(params.get("y"), 96)
        line_height = _num(params.get("lineHeight"), 1.3 if params.get("font") == "label" else 0.95)
        lines = _wrap_lines(_text(params), size, width, 0.62 if params.get("font") == "label" else 0.5)
        used = min(width, max(len(line) for line in _text(params).split("\n")) * size * 0.5) if lines == 1 else width
        return [Box(element, "text", (x, y, x + used, y + max(1, lines) * size * line_height), element.label, text_px=size)]
    if kind == "am-callout":
        x, y, width = _num(params.get("x"), 1200), _num(params.get("y"), 160), max(1.0, _num(params.get("width"), 520))
        inner = width - 52
        title_px, body_px = _num(params.get("titleSize"), 40.0), _num(params.get("bodySize"), 24.0)
        title_lines = _wrap_lines(str(params.get("title") or ""), title_px, inner, 0.55)
        body_lines = _wrap_lines(str(params.get("body") or ""), body_px, inner, 0.5)
        height = (22 + 24 + title_lines * title_px * 1.1 + (10 if title_lines and body_lines else 0)
                  + body_lines * body_px * 1.4)
        box = Box(element, "card", (x, y, x + width, y + height), element.label, text_px=body_px if body_lines else title_px)
        anchor = _map(params.get("anchor"))
        if anchor:
            ax, ay = _num(anchor.get("x"), 960), _num(anchor.get("y"), 540)
            box.notes.append(f"connector → ({ax:.0f},{ay:.0f})")
        return [box]
    if kind == "am-flap":
        values = [v for v in params.get("values") or () if isinstance(v, Mapping)]
        chars = max([len(str(v.get("text") or "")) for v in values] or [1])
        tile_w, tile_h, gap = _num(params.get("tileW"), 60), _num(params.get("tileH"), 90), _num(params.get("gap"), 6)
        width = chars * (tile_w + gap) - gap
        x, y = _num(params.get("x"), 96), _num(params.get("y"), 96)
        label_h = 40 if params.get("label") else 0
        return [Box(element, "text", (x, y, x + width, y + label_h + tile_h), element.label, text_px=tile_h * 0.7)]
    if kind == "am-presenter":
        face = declared or face_box(element, frame)
        return [Box(element, "face", face, "face")] if face else []
    if kind in PLATE_TYPES:
        plate = [Box(element, "plate", (0, 0, CANVAS[0], CANVAS[1]), element.label)]
        return plate + ([Box(element, "face", declared, "face")] if declared else [])
    if all(key in params for key in ("x", "y", "width", "height")):
        x, y = _num(params.get("x")), _num(params.get("y"))
        box = Box(element, "panel", (x, y, x + _num(params.get("width")), y + _num(params.get("height"))), element.label)
    else:
        box = Box(element, "panel", (0, 0, CANVAS[0], CANVAS[1]), element.label)
    if kind in INTERNAL_TEXT_PX:
        box.text_px = INTERNAL_TEXT_PX[kind] * _internal_zoom(element)
    return [box]


def safe_rect(fraction: float) -> tuple[float, float, float, float]:
    margin_x, margin_y = CANVAS[0] * (1 - fraction) / 2, CANVAS[1] * (1 - fraction) / 2
    return (margin_x, margin_y, CANVAS[0] - margin_x, CANVAS[1] - margin_y)


def intersection(a: Sequence[float], b: Sequence[float]) -> float:
    width = min(a[2], b[2]) - max(a[0], b[0])
    height = min(a[3], b[3]) - max(a[1], b[1])
    return max(0.0, width) * max(0.0, height)


def area(rect: Sequence[float]) -> float:
    return max(0.0, rect[2] - rect[0]) * max(0.0, rect[3] - rect[1])
