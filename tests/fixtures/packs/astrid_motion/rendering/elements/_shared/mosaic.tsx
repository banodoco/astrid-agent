import {type CSSProperties, type ReactElement, useEffect, useLayoutEffect, useRef, useState} from 'react';
import {cancelRender, continueRender, delayRender} from 'remotion';

// Mosaic (pixelate) helpers shared by am-snap-plate and am-footage: the
// signature move between real footage and the pixel world. A picture is
// averaged into cells of `block` screen px (aligned to the canvas origin, so a
// 6 px block lands exactly on the 320x180 logical grid at 1920x1080) and drawn
// back up with nearest-neighbour sampling.
//
// Steps are hard: a mosaic ramp is a list of block sizes, each held for
// `stepFrames` frames. Nothing eases.

export type Rect = {x: number; y: number; w: number; h: number};
export type MosaicRamp = {frames?: number; from?: number; to?: number; steps?: number[]};

// Default ramp for "into the pixel world": the 6 px grid first (320x180), then
// 12, 24 and 48 px blocks.
export const DEFAULT_STEPS = [6, 12, 24, 48];

const finite = (value: unknown, fallback: number): number =>
  typeof value === 'number' && Number.isFinite(value) ? value : fallback;

const rampSteps = (ramp: MosaicRamp, edge: number): number[] => {
  if (Array.isArray(ramp.steps) && ramp.steps.length > 0) {
    return ramp.steps.map((s) => Math.max(1, Math.round(finite(s, 6))));
  }
  return DEFAULT_STEPS.filter((s) => s <= Math.max(6, edge));
};

/**
 * Block size (screen px) on a clip frame, or 0 for a clean picture.
 * mosaicIn: starts coarse (`from`, default 48) and resolves to clean over `frames`.
 * mosaicOut: ends coarse (`to`, default 48), starting `frames` before the clip ends.
 * The ramp's steps share the frames evenly (hard steps, no easing).
 */
export const mosaicBlockAt = (
  frame: number,
  total: number,
  mosaicIn: MosaicRamp | null | undefined,
  mosaicOut: MosaicRamp | null | undefined,
): number => {
  if (mosaicIn) {
    const frames = Math.max(1, Math.round(finite(mosaicIn.frames, 8)));
    if (frame < frames) {
      const steps = rampSteps(mosaicIn, finite(mosaicIn.from, 48)).slice().sort((a, b) => b - a);
      const k = Math.min(steps.length - 1, Math.floor((frame * steps.length) / frames));
      return steps[k];
    }
  }
  if (mosaicOut) {
    const frames = Math.max(1, Math.round(finite(mosaicOut.frames, 8)));
    const start = total - frames;
    if (frame >= start) {
      const steps = rampSteps(mosaicOut, finite(mosaicOut.to, 48)).slice().sort((a, b) => a - b);
      const k = Math.min(steps.length - 1, Math.floor(((frame - start) * steps.length) / frames));
      return steps[k];
    }
  }
  return 0;
};

let scratch: HTMLCanvasElement | null = null;
const scratchCanvas = (w: number, h: number): HTMLCanvasElement => {
  if (!scratch) scratch = document.createElement('canvas');
  if (scratch.width !== w) scratch.width = w;
  if (scratch.height !== h) scratch.height = h;
  return scratch;
};

/** Paint `source` into `rect` on a width x height canvas, as `block` px cells (0 = clean). */
export const paintMosaic = (
  ctx: CanvasRenderingContext2D,
  source: CanvasImageSource,
  rect: Rect,
  block: number,
  width: number,
  height: number,
  background?: string,
): void => {
  ctx.clearRect(0, 0, width, height);
  if (background) {
    ctx.fillStyle = background;
    ctx.fillRect(0, 0, width, height);
  }
  if (block <= 1) {
    ctx.imageSmoothingEnabled = true;
    ctx.drawImage(source, rect.x, rect.y, rect.w, rect.h);
    return;
  }
  const cols = Math.ceil(width / block);
  const rows = Math.ceil(height / block);
  const small = scratchCanvas(cols, rows);
  const sctx = small.getContext('2d');
  if (!sctx) return;
  sctx.clearRect(0, 0, cols, rows);
  if (background) {
    sctx.fillStyle = background;
    sctx.fillRect(0, 0, cols, rows);
  }
  sctx.imageSmoothingEnabled = true;
  sctx.imageSmoothingQuality = 'high';
  sctx.drawImage(source, rect.x / block, rect.y / block, rect.w / block, rect.h / block);
  ctx.imageSmoothingEnabled = false;
  ctx.drawImage(small, 0, 0, cols, rows, 0, 0, cols * block, rows * block);
};

const IMAGES = new Map<string, HTMLImageElement>();

/**
 * An image drawn into `rect` as a mosaic of `block` px cells. The first mount
 * of a url holds the frame (delayRender) until the image has decoded; later
 * frames paint synchronously from the cache.
 */
export const MosaicImage = ({
  url,
  rect,
  block,
  width,
  height,
  background,
  style,
}: {
  url: string;
  rect: Rect;
  block: number;
  width: number;
  height: number;
  background?: string;
  style?: CSSProperties;
}): ReactElement => {
  const ref = useRef<HTMLCanvasElement>(null);
  const [img, setImg] = useState<HTMLImageElement | null>(() => IMAGES.get(url) ?? null);
  const [handle] = useState<number | null>(() => (IMAGES.has(url) ? null : delayRender(`am mosaic ${url}`)));
  const released = useRef(false);

  useEffect(() => {
    if (img) return;
    const el = new Image();
    el.crossOrigin = 'anonymous';
    el.onload = () => {
      IMAGES.set(url, el);
      setImg(el);
    };
    el.onerror = () => cancelRender(new Error(`am mosaic: could not load ${url}`));
    el.src = url;
  }, [url, img]);

  useLayoutEffect(() => {
    const canvas = ref.current;
    if (!img || !canvas) return;
    const ctx = canvas.getContext('2d');
    if (ctx) paintMosaic(ctx, img, rect, block, width, height, background);
    if (handle !== null && !released.current) {
      released.current = true;
      continueRender(handle);
    }
  });

  return (
    <canvas
      ref={ref}
      width={width}
      height={height}
      style={{position: 'absolute', left: 0, top: 0, width, height, pointerEvents: 'none', ...style}}
    />
  );
};
