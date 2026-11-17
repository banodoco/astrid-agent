"""Pictures for understanding one clip: where it moves, whether a region drifts, whether a mask fits, and
how a take differs from its source.

Pure PIL/numpy over frames that are already captured (no Runtime, no capture): the pack's ``heat`` layer
and the client's ``--region``/``--mask``/``--compare-media`` pages both draw with these.

Conventions: every box is canvas px ``(x0, y0, x1, y1)``; a mask is an ``L`` image at the frame's size where
white (> 127) marks the edited area; a change map is a float array in 0..1 giving, per pixel, the share of
consecutive frame steps in which that pixel visibly changed.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

import numpy as np
from PIL import Image, ImageChops, ImageDraw

CHANGE_LEVEL = 18  # the same visible-change threshold as the diff layer (0..255 luma)
WORK_WIDTH = 960  # change maps and fit numbers are measured at this width (fast, and finer than a tile)
HEAT_STOPS = (  # share of frame steps that changed -> colour (dark violet .. pale yellow, readable on grey)
    (0.00, (59, 15, 112)),
    (0.15, (140, 41, 129)),
    (0.35, (222, 73, 104)),
    (0.60, (254, 159, 109)),
    (1.00, (252, 253, 191)),
)
MASK_COLOUR = "#22d3ee"
REGION_COLOUR = "#facc15"
HOLDS_PX = 4
TIGHT_PX = 12


# ---------------------------------------------------------------- motion map

def _grey(image: Image.Image, width: int | None) -> np.ndarray:
    if width and image.size[0] > width:
        image = image.resize((width, max(1, round(width * image.size[1] / image.size[0]))), Image.BILINEAR)
    return np.asarray(image.convert("L"), dtype=np.int16)


def change_map(images: Sequence[Image.Image], *, width: int | None = WORK_WIDTH) -> tuple[np.ndarray, int]:
    """(share of frame steps each pixel visibly changed in, number of steps). Steps = consecutive pairs."""
    previous = None
    counts = None
    steps = 0
    for image in images:
        current = _grey(image, width)
        if previous is not None and previous.shape == current.shape:
            changed = np.abs(current - previous) > CHANGE_LEVEL
            counts = changed.astype(np.float32) if counts is None else counts + changed
            steps += 1
        previous = current
    if counts is None:
        shape = previous.shape if previous is not None else (1, 1)
        return np.zeros(shape, dtype=np.float32), 0
    return counts / float(steps), steps


def _ramp(values: np.ndarray) -> np.ndarray:
    stops = np.array([s for s, _c in HEAT_STOPS], dtype=np.float32)
    colours = np.array([c for _s, c in HEAT_STOPS], dtype=np.float32)
    out = np.empty(values.shape + (3,), dtype=np.float32)
    for channel in range(3):
        out[..., channel] = np.interp(values, stops, colours[:, channel])
    return out


def heat_overlay(base: Image.Image, freq: np.ndarray, *, floor: float = 0.03) -> Image.Image:
    """``base`` greyed and dimmed, with colour where pixels change: violet = now and then, yellow = every frame.

    Pixels that change in fewer than ``floor`` of the steps stay grey (noise and grain)."""
    base = base.convert("RGB")
    heat = Image.fromarray(np.clip(freq * 255, 0, 255).astype(np.uint8), "L").resize(base.size, Image.BILINEAR)
    values = np.asarray(heat, dtype=np.float32) / 255.0
    grey = np.asarray(base.convert("L").convert("RGB"), dtype=np.float32) * 0.45
    alpha = np.clip((values - floor) / 0.12, 0, 1)[..., None] * 0.9
    mixed = grey * (1 - alpha) + _ramp(np.sqrt(values)) * alpha  # sqrt: 1 frame in 4 already reads warm
    return Image.fromarray(mixed.astype(np.uint8), "RGB")


def draw_heat_legend(draw: ImageDraw.ImageDraw, xy: tuple[int, int], *, width: int = 260, text=None) -> int:
    """A ramp bar 'changes now and then … every frame'; returns its height."""
    from .layers.base import PALETTE, draw_text

    x, y = xy
    ramp = _ramp(np.linspace(0, 1, width, dtype=np.float32))
    for i in range(width):
        draw.line((x + i, y + 18, x + i, y + 30), fill=tuple(int(v) for v in ramp[i]))
    draw_text(draw, (x, y), text or "how often each spot changes", 12, PALETTE["muted"])
    for share, label in ((0.03, "rarely"), (0.25, "1 frame in 4"), (1.0, "every frame")):
        at = x + int(np.sqrt(share) * (width - 1))
        draw.line((at, y + 30, at, y + 34), fill=PALETTE["muted"])
        draw_text(draw, (min(at, x + width - 64) if share == 1.0 else max(x, at - 20), y + 35), label, 11,
                  PALETTE["muted"])
    return 52


def changed_box(freq: np.ndarray, size: tuple[int, int], *, floor: float = 0.03) -> tuple[int, int, int, int] | None:
    """Canvas-px bounds of where pixels change (``size`` = the canvas the map was measured on)."""
    ys, xs = np.nonzero(freq > floor)
    if not len(xs):
        return None
    kx, ky = size[0] / freq.shape[1], size[1] / freq.shape[0]
    return int(xs.min() * kx), int(ys.min() * ky), int((xs.max() + 1) * kx), int((ys.max() + 1) * ky)


def hotspot(freq: np.ndarray, size: tuple[int, int]) -> tuple[int, int, int, int] | None:
    """Canvas-px bounds of the strongest change (spots changing at least half as often as the most)."""
    if not freq.size or float(freq.max()) <= 0.03:
        return None
    small = np.asarray(Image.fromarray((freq * 255).astype(np.uint8), "L").resize(
        (max(1, freq.shape[1] // 8), max(1, freq.shape[0] // 8)), Image.BOX), dtype=np.float32) / 255.0
    hot = small >= max(0.03, 0.5 * float(small.max()))
    start = np.unravel_index(int(np.argmax(small)), small.shape)
    seen = np.zeros_like(hot)
    stack, cells = [start], []
    while stack:  # the connected patch around the strongest spot, not every warm pixel in the frame
        y, x = stack.pop()
        if y < 0 or x < 0 or y >= hot.shape[0] or x >= hot.shape[1] or seen[y, x] or not hot[y, x]:
            continue
        seen[y, x] = True
        cells.append((y, x))
        stack.extend(((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)))
    patch = np.zeros_like(small)
    for y, x in cells:
        patch[y, x] = 1.0
    return changed_box(patch, size, floor=0.5)


def where_words(box: tuple[int, int, int, int] | None, size: tuple[int, int]) -> str:
    """'the lower centre' style words for a box on the canvas."""
    if box is None:
        return "nowhere"
    cx, cy = (box[0] + box[2]) / 2 / size[0], (box[1] + box[3]) / 2 / size[1]
    if (box[2] - box[0]) > 0.7 * size[0] and (box[3] - box[1]) > 0.7 * size[1]:
        return "across the whole frame"
    row = "upper" if cy < 0.36 else "lower" if cy > 0.64 else "middle"
    col = "left" if cx < 0.36 else "right" if cx > 0.64 else "centre"
    return f"the {row} {col}" if (row, col) != ("middle", "centre") else "the centre"


# ---------------------------------------------------------------- masks

def load_mask(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    """An image mask (white = edited) at ``size`` as a hard 0/255 ``L`` image; alpha counts when present.

    Fitted the way a ``cover`` clip is (scaled to fill, centred, overflow cropped), never stretched, then
    thresholded; a mask already at the canvas aspect is simply scaled."""
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        luma = ImageChops.multiply(rgba.convert("L"), rgba.getchannel("A"))
    else:
        luma = image.convert("L")
    from PIL import ImageOps

    fitted = luma if luma.size == tuple(size) else ImageOps.fit(luma, tuple(size), Image.BILINEAR)
    return fitted.point(lambda v: 255 if v > 127 else 0)


def _grow(on: np.ndarray, radius: int) -> np.ndarray:
    """Binary dilation by a (2r+1)-square, separable (numpy only; PIL's rank filters are avoided)."""
    if radius <= 0:
        return on.copy()
    out = on.copy()
    for axis in (0, 1):
        src, acc = out, out.copy()
        n = src.shape[axis]
        for step in range(1, min(radius, n - 1) + 1):
            lo = [slice(None)] * 2
            hi = [slice(None)] * 2
            lo[axis], hi[axis] = slice(step, None), slice(None, n - step)
            acc[tuple(lo)] |= src[tuple(hi)]
            acc[tuple(hi)] |= src[tuple(lo)]
        out = acc
    return out


def _shrink(on: np.ndarray, radius: int) -> np.ndarray:
    return ~_grow(~on, radius)


def edge_band(mask: Image.Image, radius: int) -> Image.Image:
    """The band ``radius`` px either side of the mask's edge (what a fixed mask must stay on)."""
    on = np.asarray(mask.convert("L"), dtype=np.uint8) > 127
    band = _grow(on, radius) & ~_shrink(on, radius)
    return Image.fromarray((band * 255).astype(np.uint8), "L")


def mask_outline(mask: Image.Image, *, width: int = 3) -> Image.Image:
    """The mask's edge as a ``width``-px ``L`` line (for drawing over frames at the mask's size)."""
    on = np.asarray(mask.convert("L"), dtype=np.uint8) > 127
    outside = width // 2
    edge = _grow(on, outside) & ~_shrink(on, max(1, width - outside))
    return Image.fromarray((edge * 255).astype(np.uint8), "L")


def draw_outline(image: Image.Image, mask: Image.Image, *, colour: str = MASK_COLOUR, width: int = 3) -> Image.Image:
    """``image`` with the mask's edge drawn on it (the mask is resized to the image first)."""
    image = image.convert("RGB")
    fitted = mask.resize(image.size, Image.NEAREST) if mask.size != image.size else mask
    edge = mask_outline(fitted, width=width)
    image.paste(Image.new("RGB", image.size, colour), (0, 0), edge)
    return image


@dataclass
class MaskFit:
    """How the motion of a span sits against a mask; shares are of the summed change (0..1)."""
    mask_area: float  # share of the frame the mask covers
    inside: float  # share of all motion that falls inside the mask
    edge_out: float  # mean change rate just outside the mask edge (motion crossing out from under it)
    edge_in: float  # mean change rate just inside the edge
    outside_box: tuple[int, int, int, int] | None  # canvas px where motion outside the mask is
    lines: list[str] = field(default_factory=list)


def mask_fit(freq: np.ndarray, mask: Image.Image, canvas: tuple[int, int], *, band_px: int = 24) -> MaskFit:
    """Motion inside vs outside the mask, and right at its edge, from a change map measured on ``canvas``."""
    work = mask.resize((freq.shape[1], freq.shape[0]), Image.NEAREST)
    radius = max(1, round(band_px * freq.shape[1] / canvas[0]))
    inside = np.asarray(work, dtype=np.uint8) > 127
    grown, shrunk = _grow(inside, radius), _shrink(inside, radius)
    total = float(freq.sum())
    share_in = float(freq[inside].sum()) / total if total > 0 else 0.0
    ring_out, ring_in = grown & ~inside, inside & ~shrunk
    edge_out = float(freq[ring_out].mean()) if ring_out.any() else 0.0
    edge_in = float(freq[ring_in].mean()) if ring_in.any() else 0.0
    outside = np.where(inside, 0.0, freq)
    return MaskFit(float(inside.mean()), share_in, edge_out, edge_in, changed_box(outside, canvas))


def mask_box(mask: Image.Image, *, pad: float = 0.12) -> tuple[int, int, int, int] | None:
    """The mask's bounds padded by ``pad`` of its size, in the mask's px (canvas px for a canvas mask)."""
    box = mask.getbbox()
    if box is None:
        return None
    w, h = box[2] - box[0], box[3] - box[1]
    px, py = round(w * pad), round(h * pad)
    return (max(0, box[0] - px), max(0, box[1] - py), min(mask.size[0], box[2] + px), min(mask.size[1], box[3] + py))


# ---------------------------------------------------------------- drift (edge matching)

def _edges(image: Image.Image, size: tuple[int, int]) -> np.ndarray:
    grey = np.asarray(image.convert("L").resize(size, Image.BILINEAR), dtype=np.float32)
    gx = np.zeros_like(grey)
    gy = np.zeros_like(grey)
    gx[:, 1:-1] = grey[:, 2:] - grey[:, :-2]
    gy[1:-1, :] = grey[2:, :] - grey[:-2, :]
    return np.hypot(gx, gy)


def _best_shift(reference: np.ndarray, moving: np.ndarray, weights: np.ndarray, centre: tuple[int, int],
                reach: int, margin: int) -> tuple[int, int]:
    """The (dx, dy) within ``reach`` of ``centre`` where ``moving`` best lines up with ``reference``."""
    h, w = reference.shape
    core = (slice(margin, h - margin), slice(margin, w - margin))
    ref, wt = reference[core], weights[core]
    norm = float((wt * ref).sum()) or 1.0
    best, best_cost = centre, None
    for dy in range(centre[1] - reach, centre[1] + reach + 1):
        for dx in range(centre[0] - reach, centre[0] + reach + 1):
            if abs(dx) > margin or abs(dy) > margin:
                continue
            window = moving[margin + dy:h - margin + dy, margin + dx:w - margin + dx]
            cost = float((wt * np.abs(window - ref)).sum()) / norm
            if best_cost is None or cost < best_cost - 1e-9 or (
                    abs(cost - best_cost) <= 1e-9 and abs(dx) + abs(dy) < abs(best[0]) + abs(best[1])):
                best, best_cost = (dx, dy), cost
    return best


def drift_precision(size: tuple[int, int]) -> float:
    """The px resolution of :func:`region_drift` for a crop of ``size`` (the fine pass works at <= 640 px)."""
    return max(1.0, max(size) / 640.0)


def region_drift(crops: Sequence[Image.Image], *, search_px: float = 60.0,
                 weight: Image.Image | None = None) -> list[tuple[float, float]]:
    """How far each crop's content sits from the first crop's (crop px), by matching edges: a coarse search
    over every shift within ``search_px``, then a fine one at up to 640 px wide (precision:
    :func:`drift_precision`, 1 px for crops up to 640 px, coarser for wider ones).

    ``weight`` (an ``L`` image at the crop's size, e.g. the band around a mask's edge) says what to follow."""
    if not crops:
        return []
    levels = []
    for work in (160, 640):
        scale = min(1.0, work / float(max(crops[0].size)))
        size = (max(16, round(crops[0].size[0] * scale)), max(16, round(crops[0].size[1] * scale)))
        weights = (np.asarray(weight.convert("L").resize(size, Image.BILINEAR), dtype=np.float32) / 255.0
                   if weight is not None else np.ones((size[1], size[0]), dtype=np.float32))
        levels.append((scale, size, weights, _edges(crops[0], size)))
    (cs, csize, cw, cref), (fs, fsize, fw, fref) = levels
    coarse_margin = max(2, min(int(round(search_px * cs)), min(csize) // 3))
    fine_margin = max(2, min(int(round(search_px * fs)) + 2, min(fsize) // 3))
    step = max(1, int(np.ceil(fs / cs)))
    out = [(0.0, 0.0)]
    for crop in crops[1:]:
        cx, cy = _best_shift(cref, _edges(crop, csize), cw, (0, 0), coarse_margin, coarse_margin)
        centre = (round(cx * fs / cs), round(cy * fs / cs))
        fx, fy = _best_shift(fref, _edges(crop, fsize), fw, centre, step + 1, fine_margin)
        out.append((fx / fs, fy / fs))
    return out


def worst_moment(drift: Sequence[tuple[float, float]]) -> int:
    """The index of the largest move (Euclidean: the same measure the verdict reports)."""
    return int(np.argmax([float(np.hypot(dx, dy)) for dx, dy in drift])) if drift else 0


def drift_verdict(drift: Sequence[tuple[float, float]], times: Sequence[float]) -> tuple[str, str, float]:
    """(level, sentence, max px): HOLDS / TIGHT / MOVES for a fixed box or mask over these moments."""
    if not drift:
        return "UNKNOWN", "no frames to measure", 0.0
    sizes = [float(np.hypot(dx, dy)) for dx, dy in drift]
    worst = worst_moment(drift)
    most = sizes[worst]
    at = f" (most at {times[worst]:.2f} s)" if times else ""
    dx, dy = drift[worst]
    direction = _direction(dx, dy)
    if most <= HOLDS_PX:
        return "HOLDS", f"what is in the box stays put: it moves at most {most:.0f} px over the span", most
    if most <= TIGHT_PX:
        return ("TIGHT", f"it moves up to {most:.0f} px{at}, {direction}: a fixed mask needs about {most:.0f} px "
                f"of margin or feather", most)
    return ("MOVES", f"it moves up to {most:.0f} px{at}, {direction}: a fixed mask will not stay on it; track it "
            "or key the mask per frame", most)


def _direction(dx: float, dy: float) -> str:
    parts = []
    if abs(dx) >= 2:
        parts.append(f"{abs(dx):.0f} px {'right' if dx > 0 else 'left'}")
    if abs(dy) >= 2:
        parts.append(f"{abs(dy):.0f} px {'down' if dy > 0 else 'up'}")
    return " and ".join(parts) or "steady"


# ---------------------------------------------------------------- named regions

NAMED_REGIONS = {
    "top": (0.0, 0.0, 1.0, 0.5), "bottom": (0.0, 0.5, 1.0, 1.0), "left": (0.0, 0.0, 0.5, 1.0),
    "right": (0.5, 0.0, 1.0, 1.0), "centre": (0.25, 0.25, 0.75, 0.75), "center": (0.25, 0.25, 0.75, 0.75),
    "top-left": (0.0, 0.0, 0.5, 0.5), "top-right": (0.5, 0.0, 1.0, 0.5),
    "bottom-left": (0.0, 0.5, 0.5, 1.0), "bottom-right": (0.5, 0.5, 1.0, 1.0),
}


def named_region(name: str, canvas: tuple[int, int]) -> tuple[int, int, int, int] | None:
    rel = NAMED_REGIONS.get(str(name).strip().lower())
    if rel is None:
        return None
    w, h = canvas
    return round(rel[0] * w), round(rel[1] * h), round(rel[2] * w), round(rel[3] * h)


# ---------------------------------------------------------------- pages

def _page(width: int, height: int) -> tuple[Image.Image, ImageDraw.ImageDraw]:
    from .layers.base import PALETTE

    page = Image.new("RGB", (width, height), PALETTE["bg"])
    return page, ImageDraw.Draw(page)


def _header(draw, title: str, what: str, sub: Sequence[str], verdicts: Sequence[tuple[str, str]], width: int) -> int:
    """Title, the one-line 'what you are looking at', detail lines, then coloured verdict lines; returns y."""
    from .layers.base import PALETTE, draw_text

    draw_text(draw, (12, 10), title, 22, "white")
    draw_text(draw, (12, 42), what, 16, PALETTE["ink"])
    y = 66
    for line in sub:
        draw_text(draw, (12, y), line, 13, PALETTE["muted"])
        y += 19
    colours = {"HOLDS": PALETTE["good"], "MATCH": PALETTE["good"], "TIGHT": PALETTE["warn"],
               "MOVES": PALETTE["bad"], "DIFFERS": PALETTE["bad"], "SEAM": PALETTE["warn"], "INFO": PALETTE["ink"]}
    for level, sentence in verdicts:
        y += 4
        colour = colours.get(level, PALETTE["ink"])
        draw.rectangle((12, y + 3, 22, y + 15), fill=colour)
        draw_text(draw, (30, y), f"{level.lower()}: {sentence}" if level != "INFO" else sentence, 16, colour)
        y += 24
    return y + 8


def _fit(image: Image.Image, width: int, height: int | None = None) -> Image.Image:
    scale = min(width / image.size[0], (height or 10**6) / image.size[1])
    if abs(scale - 1) < 1e-6:
        return image
    size = (max(1, round(image.size[0] * scale)), max(1, round(image.size[1] * scale)))
    return image.resize(size, Image.NEAREST if scale > 1 else Image.LANCZOS)


def locator(frame: Image.Image, box: tuple[int, int, int, int], canvas: tuple[int, int], *, width: int = 360,
            mask: Image.Image | None = None) -> Image.Image:
    """A small whole frame with the box (yellow) and the mask edge (cyan) where they sit."""
    thumb = frame.convert("RGB").resize((width, round(width * frame.size[1] / frame.size[0])), Image.LANCZOS)
    if mask is not None:
        thumb = draw_outline(thumb, mask, width=1)
    k = width / canvas[0]
    ImageDraw.Draw(thumb).rectangle(tuple(v * k for v in box), outline=REGION_COLOUR, width=2)
    return thumb


def _crop_at_canvas(frame: Image.Image, box: tuple[int, int, int, int], canvas: tuple[int, int]) -> Image.Image:
    """``box`` (canvas px) out of a frame captured at any size, returned at canvas scale."""
    x0, y0, x1, y1 = box
    k = frame.size[0] / canvas[0]
    crop = frame.convert("RGB").crop((round(x0 * k), round(y0 * k), round(x1 * k), round(y1 * k)))
    return crop if crop.size == (x1 - x0, y1 - y0) else crop.resize((x1 - x0, y1 - y0), Image.LANCZOS)


def region_strip_page(*, title: str, what: str, sub: Sequence[str], frames: Sequence[Image.Image],
                      times: Sequence[float], box: tuple[int, int, int, int], canvas: tuple[int, int],
                      mask: Image.Image | None = None, verdicts: Sequence[tuple[str, str]] = (),
                      drift: Sequence[tuple[float, float]] = (), heat: Image.Image | None = None,
                      max_width: int = 1900) -> Image.Image:
    """Verdicts, then the region's motion map (mask edge in cyan) beside where the box sits in the frame,
    then the region at each moment in one row, the mask edge at the same canvas place on each so drift reads
    against a fixed line."""
    from .layers.base import PALETTE, draw_text

    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    crops = []
    for frame in frames:
        crop = _crop_at_canvas(frame, box, canvas)
        if mask is not None:
            crop = draw_outline(crop, mask.crop(box), width=2)
        crops.append(crop)
    gap = 10
    tile_w = min(w, (max_width - 24 - gap * max(0, len(crops) - 1)) // max(1, len(crops)))
    shown = [_fit(c, tile_w) for c in crops]
    th = shown[0].size[1] if shown else 0
    scale_note = "1:1" if tile_w == w else f"shown at {tile_w / w:.2f}x"
    loc = locator(frames[len(frames) // 2], box, canvas, mask=mask) if frames else None
    heat_w = min(w, max_width - 24 - ((loc.size[0] + 24) if loc else 0), 1100)
    heat_tile = _fit(heat, heat_w) if heat is not None else None
    width = max(24 + len(shown) * (tile_w + gap), 24 + (heat_tile.size[0] if heat_tile else 0)
                + ((loc.size[0] + 24) if loc else 0), 900)
    header_h = 66 + 19 * len(sub) + 28 * len(verdicts) + 16
    top_h = max((heat_tile.size[1] + 82) if heat_tile else 0, (loc.size[1] + 24) if loc else 0)
    page, draw = _page(width, header_h + top_h + 24 + th + 40)
    y = _header(draw, title, what, sub, verdicts, width)
    x = 12
    if heat_tile is not None:
        draw_text(draw, (x, y), "where it moves over the span" + (" (cyan = mask edge)" if mask is not None else ""),
                  14, PALETTE["ink"])
        page.paste(heat_tile, (x, y + 20))
        draw_heat_legend(draw, (x, y + 26 + heat_tile.size[1]), width=min(260, heat_tile.size[0]))
        x += heat_tile.size[0] + 24
    if loc is not None:
        draw_text(draw, (x, y), "where the box is in the frame", 13, PALETTE["muted"])
        page.paste(loc, (x, y + 20))
    y += top_h
    draw_text(draw, (12, y), f"the region at {len(shown)} moments ({scale_note}; the cyan line stays put, the "
                             "picture may not)" if mask is not None else
              f"the region at {len(shown)} moments ({scale_note})", 14, PALETTE["ink"])
    y += 22
    for i, crop in enumerate(shown):
        x = 12 + i * (tile_w + gap)
        label = f"{times[i]:.2f} s" if i < len(times) else ""
        if i < len(drift) and i:
            moved = _direction(*drift[i])
            label += "  steady" if moved == "steady" else f"  moved {moved}"
        draw_text(draw, (x, y), label, 12, PALETTE["muted"])
        page.paste(crop, (x, y + 16))
    return page


def heat_page(*, title: str, what: str, sub: Sequence[str], base: Image.Image, freq: np.ndarray,
              mask: Image.Image | None = None, verdicts: Sequence[tuple[str, str]] = (), width: int = 1280,
              box: tuple[int, int, int, int] | None = None, canvas: tuple[int, int] | None = None) -> Image.Image:
    """One representative frame with the motion map over it, the mask edge (cyan) and a legend."""
    picture = heat_overlay(_fit(base.convert("RGB"), width), freq)
    if mask is not None:
        picture = draw_outline(picture, mask, width=3)
    if box is not None and canvas is not None:
        k = picture.size[0] / canvas[0]
        ImageDraw.Draw(picture).rectangle(tuple(v * k for v in box), outline=REGION_COLOUR, width=2)
    hot = hotspot(freq, canvas or base.size)
    if hot is not None:  # the strongest patch, dashed white, so the eye lands on it first
        k = picture.size[0] / (canvas or base.size)[0]
        _dashed(ImageDraw.Draw(picture), tuple(round(v * k) for v in hot), "#ffffff")
    header_h = 66 + 19 * len(sub) + 28 * len(verdicts) + 16
    page, draw = _page(max(picture.size[0] + 24, 900), header_h + picture.size[1] + 80)
    y = _header(draw, title, what, sub, verdicts, page.size[0])
    page.paste(picture, (12, y))
    draw_heat_legend(draw, (12, y + picture.size[1] + 8))
    return page


def _dashed(draw, box, colour, dash: int = 8) -> None:
    x0, y0, x1, y1 = box
    for x in range(x0, x1, dash * 2):
        draw.line((x, y0, min(x + dash, x1), y0), fill=colour, width=2)
        draw.line((x, y1, min(x + dash, x1), y1), fill=colour, width=2)
    for y in range(y0, y1, dash * 2):
        draw.line((x0, y, x0, min(y + dash, y1)), fill=colour, width=2)
        draw.line((x1, y, x1, min(y + dash, y1)), fill=colour, width=2)


@dataclass
class Difference:
    """A take against its source at one moment: mean abs difference (0..255) inside/outside the mask,
    in the band along the mask edge (the seam), and the difference picture."""
    inside: float
    outside: float
    seam: float
    picture: Image.Image
    box: tuple[int, int, int, int] | None


def difference(source: Image.Image, take: Image.Image, mask: Image.Image | None, *, band_px: int = 12,
               px_per_canvas_px: float = 1.0) -> Difference:
    """How the take differs from the source (both at the same size): numbers and a heat picture of it.

    ``band_px`` is the seam band either side of the mask edge in canvas px; ``px_per_canvas_px`` says how
    many of ``source``'s px make one canvas px (1 for a frame or crop at canvas scale)."""
    take = take.convert("RGB").resize(source.size, Image.LANCZOS) if take.size != source.size else take.convert("RGB")
    a = np.asarray(source.convert("RGB"), dtype=np.int16)
    b = np.asarray(take, dtype=np.int16)
    diff = np.abs(a - b).max(axis=2).astype(np.float32)
    if mask is not None:
        fitted = mask.resize(source.size, Image.NEAREST)
        radius = max(1, round(band_px * px_per_canvas_px))
        inside = np.asarray(fitted, dtype=np.uint8) > 127
        grown, shrunk = _grow(inside, radius), _shrink(inside, radius)
        seam_ring = grown & ~shrunk
        mean_in = float(diff[inside].mean()) if inside.any() else 0.0
        beyond = ~grown  # outside the mask AND past the seam band: what the edit should leave alone
        mean_out = float(diff[beyond].mean()) if beyond.any() else 0.0
        seam = float(diff[seam_ring].mean()) if seam_ring.any() else 0.0
    else:
        mean_in, mean_out, seam = float(diff.mean()), 0.0, 0.0
    visible = diff > CHANGE_LEVEL
    ys, xs = np.nonzero(visible)
    box = (int(xs.min()), int(ys.min()), int(xs.max()) + 1, int(ys.max()) + 1) if len(xs) else None
    strength = np.clip(diff / 96.0, 0, 1)
    grey = np.asarray(source.convert("L").convert("RGB"), dtype=np.float32) * 0.4
    alpha = np.clip((diff - CHANGE_LEVEL / 2) / 24.0, 0, 1)[..., None]
    mixed = grey * (1 - alpha) + _ramp(strength) * alpha
    picture = Image.fromarray(mixed.astype(np.uint8), "RGB")
    if mask is not None:
        picture = draw_outline(picture, mask, width=2)
    return Difference(mean_in, mean_out, seam, picture, box)


def compare_verdicts(rows: Iterable[Difference], *, masked: bool) -> list[tuple[str, str]]:
    """Plain verdicts over all compared moments: does the take leave the outside alone, is there a seam."""
    rows = list(rows)
    if not rows:
        return [("INFO", "no moments to compare")]
    outside = max(r.outside for r in rows)
    inside = max(r.inside for r in rows)
    seam = max(r.seam for r in rows)
    if not masked:
        level = "MATCH" if inside < 3 else "DIFFERS"
        return [(level, f"the take differs from the source by {inside:.1f}/255 on average (worst moment)")]
    out = []
    if outside < 3:
        out.append(("MATCH", f"away from the mask the take matches the source (mean difference {outside:.1f}/255)"))
    else:
        out.append(("DIFFERS", f"the take also changes the picture away from the mask (mean {outside:.1f}/255 at the "
                               "worst moment): look at the coloured areas well outside the cyan line"))
    if seam > max(6.0, 2.5 * outside):
        out.append(("SEAM", f"along the mask edge (12 px either side) the take differs by {seam:.1f}/255: the edit "
                            "spills over or stops short of the line, so a seam may show; feather or grow the mask"))
    out.append(("INFO", f"inside the mask the take differs by up to {inside:.1f}/255 on average (the edit itself)"))
    return out


def compare_page(*, title: str, what: str, sub: Sequence[str], rows: Sequence[tuple[float, Image.Image, Image.Image,
                 Difference]], verdicts: Sequence[tuple[str, str]], labels: tuple[str, str] = ("source", "take"),
                 max_width: int = 1900) -> Image.Image:
    """Verdicts; then where take and source differ at every moment, in one row (colour = how much, cyan =
    mask edge); then the evidence: source and take side by side per moment, smaller."""
    from .layers.base import PALETTE, draw_text

    n = max(1, len(rows))
    gap = 10
    diff_w = min(rows[0][1].size[0] if rows else 600, (max_width - 24 - gap * (n - 1)) // n)
    diffs = [_fit(d.picture, diff_w) for _t, _s, _k, d in rows]
    dh = diffs[0].size[1] if diffs else 0
    pair_w = min(diff_w, 460)
    pairs = [(_fit(src, pair_w), _fit(take, pair_w)) for _t, src, take, _d in rows]
    ph = pairs[0][0].size[1] if pairs else 0
    per_row = max(1, (max_width - 24) // (2 * pair_w + 3 * gap + 20))
    width = max(24 + n * (diff_w + gap), 24 + per_row * (2 * pair_w + 3 * gap + 20), 900)
    header_h = 66 + 19 * len(sub) + 28 * len(verdicts) + 16
    evidence_rows = (n + per_row - 1) // per_row
    page, draw = _page(width, header_h + 24 + dh + 40 + 24 + evidence_rows * (ph + 30) + 20)
    y = _header(draw, title, what, sub, verdicts, width)
    draw_text(draw, (12, y), "where the take differs from the source (violet = slightly … yellow = very; "
                             "cyan = mask edge)", 14, PALETTE["ink"])
    y += 22
    for i, ((t, _s, _k, numbers), tile) in enumerate(zip(rows, diffs)):
        x = 12 + i * (diff_w + gap)
        note = (f"{t:.2f} s · away {numbers.outside:.1f} · edge {numbers.seam:.1f} · inside {numbers.inside:.1f}"
                if numbers.outside or numbers.seam else f"{t:.2f} s · mean {numbers.inside:.1f}/255")
        draw_text(draw, (x, y), note, 12, PALETTE["muted"])
        page.paste(tile, (x, y + 16))
    y += dh + 40
    draw_text(draw, (12, y), f"the evidence: {labels[0]} | {labels[1]}", 14, PALETTE["ink"])
    y += 22
    for i, ((t, *_rest), (src, take)) in enumerate(zip(rows, pairs)):
        col, row = i % per_row, i // per_row
        x, yy = 12 + col * (2 * pair_w + 3 * gap + 20), y + row * (ph + 30)
        draw_text(draw, (x, yy), f"{t:.2f} s", 12, PALETTE["muted"])
        page.paste(src, (x, yy + 16))
        page.paste(take, (x + pair_w + gap, yy + 16))
    return page


def representative(frames: Sequence[Image.Image], freq: np.ndarray | None = None) -> int:
    """The index of the frame to draw a motion map on: the middle one (stable, and inside the action)."""
    return len(frames) // 2 if frames else 0


def as_images(paths: Iterable[Any]) -> list[Image.Image]:
    out = []
    for path in paths:
        with Image.open(path) as image:
            out.append(image.convert("RGB"))
    return out
