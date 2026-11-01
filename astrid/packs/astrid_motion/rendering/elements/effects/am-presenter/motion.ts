// am-presenter's motion as data. The component draws with presenterAt(), and
// `timelines visualize` evaluates motionAt() on the frames it charts (curves,
// bounds, lip sync, lint), so the chart is this element's own maths, not a copy.
// Pure: no React, no DOM, no Math.random.
import {LOGICAL_H, LOGICAL_W, clamp, finiteNumber, integerIn, narrowParams} from '../../_shared/am';
import {
  type Anchor,
  type Point,
  type PresenterFrame,
  type PresenterTiming,
  type PresenterView,
  type Span,
  blinkClosedAt,
  mouthStateAt,
  presenterView,
} from './presenter-core';

export const anchorOr = (value: unknown, fallback: Anchor): Anchor => {
  if (!value || typeof value !== 'object') return fallback;
  const a = value as Record<string, unknown>;
  return {
    x: integerIn(a.x, 0, LOGICAL_W - 1, fallback.x),
    y: integerIn(a.y, 0, LOGICAL_H - 1, fallback.y),
    w: integerIn(a.w, 1, 40, fallback.w),
  };
};

export const mouthAnchor = (raw: unknown): Anchor =>
  anchorOr(narrowParams<{mouth?: Anchor}>(raw).mouth, {x: 151, y: 104, w: 19});

export type PixelRect = {x: number; y: number; w: number; h: number};

// The drawing erases this rectangle before the half/open mouth pixels.
export const mouthEraseRect = (m: Anchor): PixelRect => ({x: m.x - 1, y: m.y - 1, w: m.w + 2, h: 4});

// Public state area around the actual drawing geometry, including closed pose.
// It deliberately follows the same normalization as drawing, so defaults,
// partial anchors and raw-invalid anchors retain their known public region.
export const mouthStateRect = (raw: unknown): PixelRect => {
  const r = mouthEraseRect(mouthAnchor(raw));
  return {x: r.x - 1, y: r.y - 2, w: r.w + 2, h: r.h + 4};
};

// Legacy compatibility inspection area around the actual drawing geometry.
// This seam keeps the old pixel consumer's input rules: an absent mouth skips
// inspection, while an authored x/y without w retains the old width-16
// footprint. Invalid or non-integral anchors do not invent a crop, even though
// drawing normalization remains safe and deterministic.
export const mouthInspectionRect = (raw: unknown): PixelRect | null => {
  const value = narrowParams<{mouth?: unknown}>(raw).mouth;
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const mouth = value as Record<string, unknown>;
  const x = mouth.x;
  const y = mouth.y;
  const width = mouth.w === undefined ? 16 : mouth.w;
  if (typeof x !== 'number' || !Number.isInteger(x) || x < 0 || x >= LOGICAL_W
    || typeof y !== 'number' || !Number.isInteger(y) || y < 0 || y >= LOGICAL_H
    || typeof width !== 'number' || !Number.isInteger(width) || width < 1 || width > 40) return null;
  const r = mouthEraseRect({...mouthAnchor(raw), w: width});
  return {x: r.x - 1, y: r.y - 2, w: r.w + 2, h: r.h + 4};
};

export const pixelGeometry = (r: PixelRect, v: PresenterView): {left: number; top: number; width: number; height: number} => ({
  left: v.originX + r.x * v.scale,
  top: v.originY + r.y * v.scale,
  width: r.w * v.scale,
  height: r.h * v.scale,
});

type MotionParams = {
  zoom?: number;
  focus?: Point;
  words?: Span[];
  blinkEvery?: number;
  noBlink?: Span[];
  seed?: number;
  bob?: number;
  punchAt?: number[];
  push?: {to: number; frames: number; at?: number} | null;
};

export const spansOf = (value: unknown): Span[] => {
  if (!Array.isArray(value)) return [];
  const spans: Span[] = [];
  for (const item of value) {
    if (!Array.isArray(item) || item.length < 2) continue;
    const start = finiteNumber(item[0], Number.NaN);
    const end = finiteNumber(item[1], Number.NaN);
    if (Number.isFinite(start) && Number.isFinite(end) && end > start) spans.push([start, end]);
  }
  return spans.sort((a, b) => a[0] - b[0]);
};

// The params that move the presenter, read with the component's defaults.
export const readMotion = (raw: unknown, fps: number): {frame: PresenterFrame; timing: PresenterTiming} => {
  const p = narrowParams<MotionParams>(raw);
  const push = p.push && typeof p.push === 'object' ? p.push : null;
  return {
    frame: {
      zoom: integerIn(p.zoom, 1, 3, 1),
      focus: {
        x: clamp(finiteNumber(p.focus?.x, 160), 0, LOGICAL_W),
        y: clamp(finiteNumber(p.focus?.y, 90), 0, LOGICAL_H),
      },
      punchAt: Array.isArray(p.punchAt)
        ? p.punchAt.filter((n): n is number => typeof n === 'number' && Number.isFinite(n) && n >= 0)
        : [],
      push,
    },
    timing: {
      fps,
      words: spansOf(p.words),
      seed: Math.trunc(finiteNumber(p.seed, 7)),
      blinkEvery: finiteNumber(p.blinkEvery, 3.4),
      noBlink: spansOf(p.noBlink),
      bob: integerIn(p.bob, 0, 1, 0),
    },
  };
};

// The view (zoom, push, punch, focus, bob) and timing on one clip frame.
export const presenterAt = (raw: unknown, clipFrame: number, fps: number): {timing: PresenterTiming; view: PresenterView} => {
  const {frame, timing} = readMotion(raw, fps);
  return {timing, view: presenterView(frame, timing, clipFrame)};
};

// This codebook belongs only to the resolved astrid_motion/effects/am-presenter
// source. Foreign motion.mouth scalars and values outside it have unknown labels.
export const MOUTH_LEVEL = {closed: 0, half: 0.5, open: 1} as const;

// What visualize charts: zoom, view offset (screen px), mouth level and blinks.
export const motionAt = (raw: unknown, clipFrame: number, fps: number): Record<string, number> => {
  const {timing, view} = presenterAt(raw, clipFrame, fps);
  return {
    zoom: view.zoom,
    pan_x: -view.originX,
    pan_y: -view.originY,
    mouth: MOUTH_LEVEL[mouthStateAt(timing, clipFrame)],
    blink: blinkClosedAt(timing, clipFrame) ? 1 : 0,
    visible: 1,
  };
};
