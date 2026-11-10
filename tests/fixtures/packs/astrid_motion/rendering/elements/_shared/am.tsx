import type {CSSProperties, ReactElement} from 'react';
import {staticFile} from 'remotion';
import {
  type ElementComponentProps,
  narrowParams,
} from '../../../../rendering/shared/element_contracts';

// Shared helpers for the am-* motion elements. This lives outside any element
// root, so the element fingerprint covers only each element's own folder. A
// change here will not invalidate an element's registry hash by itself.
export {narrowParams};
export type {ElementComponentProps};

// Pixel grid. The composition is 1920x1080; one logical px is 6 screen px, so
// the logical space is 320x180. Every snapped position is a multiple of 6 px.
export const LOGICAL_W = 320;
export const LOGICAL_H = 180;
export const LOGICAL_PX = 6;
export const BLOCK_PX = 6;

// Pack-owned Pixel Modernism tokens shared by the motion elements.
export const COLOR = {
  paper: '#F7F4ED',
  panel: '#FFFEFA',
  ink: '#25241F',
  muted: '#615E55',
  rust: '#A94714',
  orange: '#ED6B23',
  rule: '#D5D0C5',
  charcoal: '#1F1F1F',
} as const;

// Families registered by remotion/src/fonts.ts (LOCAL_FONT_FACES).
export const FAMILY = {
  display: 'Gelasio',
  label: 'Departure Mono',
  body: 'Inter',
} as const;

export type ClipTiming = {hold?: number; from?: number; to?: number; speed?: number};

export const clamp = (value: number, min: number, max: number): number => Math.min(max, Math.max(min, value));

export const finiteNumber = (value: unknown, fallback: number): number =>
  typeof value === 'number' && Number.isFinite(value) ? value : fallback;

export const integerIn = (value: unknown, min: number, max: number, fallback: number): number =>
  clamp(Math.round(finiteNumber(value, fallback)), min, max);

export const oneOf = <T extends string>(value: unknown, allowed: readonly T[], fallback: T): T =>
  typeof value === 'string' && (allowed as readonly string[]).includes(value) ? (value as T) : fallback;

// Clip length in frames: `hold` (seconds) wins, otherwise (to - from) / speed.
export const clipFrames = (clip: ClipTiming, fps: number): number => {
  if (typeof clip.hold === 'number' && Number.isFinite(clip.hold)) {
    return Math.max(1, Math.round(clip.hold * fps));
  }
  const speed = typeof clip.speed === 'number' && clip.speed > 0 ? clip.speed : 1;
  const span = (clip.to ?? 0) - (clip.from ?? 0);
  return Math.max(1, Math.round((span / speed) * fps));
};

// Spans as [start_s, end_s] pairs in seconds, sorted by start. Malformed or
// empty spans are dropped. Shared by the presenter overlay and its plate.
export const spanList = (value: unknown): [number, number][] => {
  if (!Array.isArray(value)) return [];
  const spans: [number, number][] = [];
  for (const item of value) {
    if (!Array.isArray(item) || item.length < 2) continue;
    const start = finiteNumber(item[0], Number.NaN);
    const end = finiteNumber(item[1], Number.NaN);
    if (Number.isFinite(start) && Number.isFinite(end) && end > start) spans.push([start, end]);
  }
  return spans.sort((a, b) => a[0] - b[0]);
};

// Clip frames: non-negative finite numbers only.
export const frameList = (value: unknown): number[] =>
  Array.isArray(value)
    ? value.filter((n): n is number => typeof n === 'number' && Number.isFinite(n) && n >= 0)
    : [];

// Start frame of the step that contains `frame`. Stepped motion reads this.
export const stepStart = (frame: number, stepFrames: number): number =>
  Math.floor(frame / stepFrames) * stepFrames;

// Deterministic unit-interval hash of (seed, index). Pure integer math, so a
// seeded order renders the same on every run.
export const hashUnit = (seed: number, index: number): number => {
  let h = (Math.trunc(seed) ^ Math.imul(index + 1, 0x9e3779b1)) >>> 0;
  h = Math.imul(h ^ (h >>> 16), 0x85ebca6b) >>> 0;
  h = Math.imul(h ^ (h >>> 13), 0xc2b2ae35) >>> 0;
  h = (h ^ (h >>> 16)) >>> 0;
  return h / 4294967296;
};

export const easeOutCubic = (t: number): number => {
  const u = 1 - clamp(t, 0, 1);
  return 1 - u * u * u;
};

// Remotion-renderable file. Managed media arrives as an absolute loopback URL
// (run.py rewrites registry `file` values); static public files are relative.
export const renderableFile = (file: string | undefined): string | null => {
  if (!file) return null;
  const value = file.trim();
  if (!value) return null;
  if (/^(https?:|data:|blob:|\/)/.test(value)) return value;
  return staticFile(value);
};

// Element sources come from clip.asset (assetEntry) first, then a static URL in
// params.src. A registry key cannot be looked up from params (see SKILL.md).
export const elementSource = (
  assetEntry: {file?: string; url?: string} | undefined,
  src: string | undefined,
): string | null => renderableFile(assetEntry?.file ?? assetEntry?.url ?? src);

export const pixelImage: CSSProperties = {imageRendering: 'pixelated'};

// ---- Pixel-block wipe -----------------------------------------------------

export type WipeMode = 'cover' | 'reveal';
export type WipePattern = 'diagonal' | 'random' | 'scan';

// Order in [0, 1) for a block. Blocks switch when progress passes their order.
export const wipeOrder = (
  pattern: WipePattern,
  col: number,
  row: number,
  cols: number,
  rows: number,
  seed: number,
): number => {
  if (pattern === 'random') return hashUnit(seed, row * cols + col);
  if (pattern === 'scan') return col / cols;
  return (col + row) / (cols + rows - 1);
};

export type BlockWipePaint = {
  mode: WipeMode;
  /** 0..1. cover: painted once progress passes the order. reveal: cleared as it passes. */
  progress: number;
  color: string;
  pattern: WipePattern;
  seed: number;
};

export const paintBlockWipe = (
  ctx: CanvasRenderingContext2D,
  width: number,
  height: number,
  paint: BlockWipePaint,
): void => {
  const cols = Math.ceil(width / BLOCK_PX);
  const rows = Math.ceil(height / BLOCK_PX);
  const p = clamp(paint.progress, 0, 1);
  ctx.clearRect(0, 0, width, height);
  ctx.fillStyle = paint.color;
  for (let row = 0; row < rows; row += 1) {
    for (let col = 0; col < cols; col += 1) {
      const order = wipeOrder(paint.pattern, col, row, cols, rows, paint.seed);
      const painted = paint.mode === 'cover' ? order < p : order >= p;
      if (painted) {
        ctx.fillRect(col * BLOCK_PX, row * BLOCK_PX, BLOCK_PX, BLOCK_PX);
      }
    }
  }
};

// Full-frame block wipe layer. Drawn synchronously in the ref callback, so a
// frame is complete when React commits it (no delayRender needed).
export const BlockWipe = ({
  width,
  height,
  paint,
}: {
  width: number;
  height: number;
  paint: BlockWipePaint;
}): ReactElement => (
  <canvas
    ref={(node) => {
      if (!node) return;
      const ctx = node.getContext('2d');
      if (ctx) paintBlockWipe(ctx, width, height, paint);
    }}
    width={width}
    height={height}
    style={{position: 'absolute', left: 0, top: 0, width, height, pointerEvents: 'none'}}
  />
);
