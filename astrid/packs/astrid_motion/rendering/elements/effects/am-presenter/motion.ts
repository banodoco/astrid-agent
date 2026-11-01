// am-presenter's motion as data. The component draws with presenterAt(), and
// `timelines visualize` evaluates motionAt() on the frames it charts (curves,
// bounds, lip sync, lint), so the chart is this element's own maths, not a copy.
// Pure: no React, no DOM, no Math.random.
import {LOGICAL_H, LOGICAL_W, clamp, finiteNumber, integerIn, narrowParams} from '../../_shared/am';
import {
  type Point,
  type PresenterFrame,
  type PresenterTiming,
  type PresenterView,
  type Span,
  blinkClosedAt,
  mouthStateAt,
  presenterView,
} from './presenter-core';

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

const MOUTH_LEVEL = {closed: 0, half: 0.5, open: 1} as const;

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
