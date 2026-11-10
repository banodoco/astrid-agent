// Pure presenter math shared by am-presenter (the Remotion overlay) and by any
// plate that sits under a presenter (am-snap-plate). Both call presenterView()
// and presenterSchedule() with the SAME params, so the plate and the overlay
// stay registered at every zoom, focus, bob and punch. No Math.random, no
// React, no DOM: this file must stay importable from any element.
import {clamp, hashUnit, LOGICAL_H, LOGICAL_W} from '../../_shared/am';

const SCREEN_W = 1920;
const SCREEN_H = 1080;

// One logical px is 6 screen px at zoom 1. zoom 2 is 12 px, zoom 3 is 18 px.
export const BASE_SCALE = 6;
// A punch lifts zoom by one step, capped here so a zoom-3 punch stops at 4.
export const PUNCH_MAX_ZOOM = 4;
// Frames a punch holds (snap up on its first frame, snap back after the 6th).
export const PUNCH_FRAMES = 6;
// Gaps between words no longer than this hold the mouth state; longer gaps close it.
export const GAP_HOLD_S = 0.12;
// Blinks are 2 closed frames.
export const BLINK_FRAMES = 2;

export type Point = {x: number; y: number};
// Mouth and eye anchors: x,y is the top-left logical px of the feature, w its width.
export type Anchor = {x: number; y: number; w: number};
// [start_s, end_s] relative to the clip start, in seconds.
export type Span = [number, number];
export type MouthState = 'closed' | 'half' | 'open';

export type PresenterFrame = {
  zoom: number; // 1 | 2 | 3
  focus: Point; // logical px at the centre of the view (320x180 space)
  punchAt: number[]; // clip frames where zoom+1 snaps on for PUNCH_FRAMES
  // Slow continuous push: zoom moves linearly from `zoom` to `to` over `frames`,
  // starting at clip frame `at` (default 0). This is the camera push that a real
  // take gets; it replaces hard crop jumps. Scale is fractional while it runs.
  push?: {to: number; frames: number; at?: number} | null;
};

// Zoom on a clip frame, before any punch: the base zoom, or the push in progress.
export const pushedZoom = (params: PresenterFrame, clipFrame: number): number => {
  const base = clamp(params.zoom, 1, 3);
  const push = params.push;
  if (!push || !(push.frames > 0) || !Number.isFinite(push.to)) return Math.round(base);
  const t = clamp((clipFrame - (push.at ?? 0)) / push.frames, 0, 1);
  return Math.round(base) + (clamp(push.to, 1, 4) - Math.round(base)) * t;
};

export type PresenterTiming = {
  fps: number;
  words: Span[];
  seed: number;
  blinkEvery: number; // seconds; <= 0 disables blinks
  noBlink: Span[];
  bob: number; // 0 | 1 logical px nudge on stressed words
};

// Geometry is am-snap-plate's: the focus point lands on the frame centre, the
// focus is rounded to whole logical px after clamping, and the plate is drawn
// at (originX, originY) with scale screen px per logical px.
export type PresenterView = {
  zoom: number; // effective zoom for this frame (after a punch)
  scale: number; // screen px per logical px: 6 * zoom
  focusX: number; // integer logical focus actually used (clamped to the plate)
  focusY: number;
  originX: number; // screen px of logical x = 0
  originY: number; // screen px of logical y = 0 (a bob pushes it down by one logical px)
  punched: boolean;
};

const MOUTH_CYCLE: readonly MouthState[] = ['closed', 'half', 'open'];

const orderedWords = (t: PresenterTiming): {start: number; end: number}[] =>
  t.words
    .map(([s, e]) => {
      const start = Math.round(s * t.fps);
      return {start, end: Math.max(start + 1, Math.round(e * t.fps))};
    })
    .sort((a, b) => a.start - b.start);

// Mouth state for a clip frame. Inside a word: closed -> half -> open on a
// 3-frame cycle, offset per word by the seed. In a gap of at most GAP_HOLD_S the
// mouth holds the last in-word state; in a longer gap (or outside all words) it
// is closed. Pixel states only: nothing is interpolated.
export const mouthStateAt = (t: PresenterTiming, frame: number): MouthState => {
  const words = orderedWords(t);
  let index = -1;
  for (let i = 0; i < words.length; i += 1) {
    if (words[i].start <= frame) index = i;
    else break;
  }
  if (index < 0) return 'closed';
  const word = words[index];
  const offset = Math.floor(hashUnit(t.seed, index) * 3);
  if (frame < word.end) {
    return MOUTH_CYCLE[(frame - word.start + offset) % 3];
  }
  const next = words[index + 1];
  const gapFrames = next ? next.start - word.end : Number.POSITIVE_INFINITY;
  if (gapFrames / t.fps <= GAP_HOLD_S) {
    return MOUTH_CYCLE[(word.end - 1 - word.start + offset) % 3];
  }
  return 'closed';
};

// Blink start times (seconds) are blinkEvery * k, jittered +/-30% per blink by
// the seed. A blink whose start falls in a noBlink span is dropped.
const blinkStartSeconds = (t: PresenterTiming, k: number): number => {
  const jitter = (hashUnit(t.seed, 1000 + k) * 2 - 1) * 0.3 * t.blinkEvery;
  return (k + 1) * t.blinkEvery + jitter;
};

const blinkAllowed = (t: PresenterTiming, startS: number): boolean =>
  !t.noBlink.some(([s, e]) => startS >= s && startS < e);

// True on the BLINK_FRAMES closed frames of a blink.
export const blinkClosedAt = (t: PresenterTiming, frame: number): boolean => {
  if (!(t.blinkEvery > 0)) return false;
  const seconds = frame / t.fps;
  const near = Math.floor(seconds / t.blinkEvery);
  for (let k = Math.max(0, near - 1); k <= near + 1; k += 1) {
    const startS = blinkStartSeconds(t, k);
    if (!blinkAllowed(t, startS)) continue;
    const startF = Math.round(startS * t.fps);
    if (frame >= startF && frame < startF + BLINK_FRAMES) return true;
  }
  return false;
};

// Head bob: a 1 logical px nudge on the first frame of every 3rd word. The phase
// (which words are stressed) is seeded, so the same params always bob the same frames.
export const bobPxAt = (t: PresenterTiming, frame: number): number => {
  if (!(t.bob >= 1)) return 0;
  const words = orderedWords(t);
  const phase = Math.floor(hashUnit(t.seed, 77) * 3);
  for (let i = 0; i < words.length; i += 1) {
    if (words[i].start === frame && (i + phase) % 3 === 0) return 1;
  }
  return 0;
};

export const punchedAt = (punchAt: readonly number[], frame: number): boolean =>
  punchAt.some((p) => frame >= p && frame < p + PUNCH_FRAMES);

// The one view transform. The plate is drawn at (originX, originY) with size
// 320*scale by 180*scale; the overlay maps logical (x, y) to screen
// (originX + x*scale, originY + y*scale). A bob moves the plate down one
// logical px (originY += scale), so the plate and the overlay move together.
export const presenterView = (
  params: PresenterFrame,
  timing: PresenterTiming,
  clipFrame: number,
): PresenterView => {
  const base = pushedZoom(params, clipFrame);
  const punched = punchedAt(params.punchAt, clipFrame);
  const zoom = punched ? Math.min(base + 1, PUNCH_MAX_ZOOM) : base;
  const scale = BASE_SCALE * zoom;
  const viewW = SCREEN_W / scale;
  const viewH = SCREEN_H / scale;
  // A push keeps the focus exact (no rounding), so the frame glides instead of
  // snapping a logical px at a time; a held zoom stays on whole logical px.
  const pushing = base !== Math.round(base);
  const fx = clamp(params.focus.x, viewW / 2, LOGICAL_W - viewW / 2);
  const fy = clamp(params.focus.y, viewH / 2, LOGICAL_H - viewH / 2);
  const focusX = pushing ? fx : Math.round(fx);
  const focusY = pushing ? fy : Math.round(fy);
  const bob = bobPxAt(timing, clipFrame);
  return {
    zoom,
    scale,
    focusX,
    focusY,
    originX: Math.round(SCREEN_W / 2 - focusX * scale),
    originY: Math.round(SCREEN_H / 2 - focusY * scale + bob * scale),
    punched,
  };
};
