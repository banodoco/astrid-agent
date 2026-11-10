import type {ReactElement} from 'react';
import {AbsoluteFill, Img, useCurrentFrame} from 'remotion';
import {
  COLOR,
  LOGICAL_H,
  LOGICAL_W,
  clamp,
  easeOutCubic,
  elementSource,
  finiteNumber,
  narrowParams,
  pixelImage,
  type ElementComponentProps,
} from '../../_shared/am';

// am-droste: an endless zoom into a screen inside the plate. The plate's
// screen rect (logical px) holds the whole plate again, which holds it again,
// and so on. The camera zooms about the recursion's fixed point, so arriving at
// level n frames level n exactly as level 0 was framed: the loop never ends.
//
// Moves are stepped: each move jumps between levels on `stepFrames` steps with
// ease-out spacing (hard frames, no blending). `creep` adds a slow stepped
// drift between moves. Deeper levels can darken (depthTint) to read the depth.

type Move = {at?: number; to?: number; frames?: number};
type Params = {
  src?: string;
  /** The screen inside the plate that holds the next level, logical px. */
  screen?: {x: number; y: number; w: number; h: number};
  start?: number;
  moves?: Move[];
  creep?: number;
  stepFrames?: number;
  depthTint?: number;
  /** Paint the innermost screens this colour instead of recursing past maxLevel. */
  maxLevel?: number;
  background?: string;
};

const BASE = 6; // screen px per logical px at level 0, zoom 1

export default function AmDroste(props: ElementComponentProps): ReactElement | null {
  const frame = useCurrentFrame();
  const p = narrowParams<Params>(props.params);
  const url = elementSource(props.assetEntry, p.src);
  if (!url) return null;
  const fps = finiteNumber(props.fps, 30);
  const sc = p.screen ?? {x: 83, y: 38, w: 147, h: 75};
  const r = sc.w / LOGICAL_W;
  const offY = (sc.h - LOGICAL_H * r) / 2;
  const s = {x: sc.x, y: sc.y + offY};
  const fixed = {x: s.x / (1 - r), y: s.y / (1 - r)};
  const stepFrames = Math.max(1, Math.round(finiteNumber(p.stepFrames, 2)));
  const f = Math.floor(frame / stepFrames) * stepFrames;

  // Level (float) on this frame: stepped moves plus a stepped creep.
  let level = finiteNumber(p.start, 0);
  const moves = (Array.isArray(p.moves) ? p.moves : []).slice().sort((a, b) => finiteNumber(a.at, 0) - finiteNumber(b.at, 0));
  for (const m of moves) {
    const at = finiteNumber(m.at, 0);
    if (f < at) break;
    const to = finiteNumber(m.to, level + 1);
    const frames = Math.max(1, Math.round(finiteNumber(m.frames, 8)));
    const t = clamp((f - at) / frames, 0, 1);
    level = level + (to - level) * easeOutCubic(t);
  }
  level += finiteNumber(p.creep, 0) * (f / fps);
  const maxLevel = Math.round(finiteNumber(p.maxLevel, 99));
  const zoom = Math.pow(1 / r, level);
  const depthTint = clamp(finiteNumber(p.depthTint, 0), 0, 0.5);

  // Canvas px of level k's logical point q.
  const toCanvas = (k: number, qx: number, qy: number): {x: number; y: number} => {
    const rk = Math.pow(r, k);
    return {
      x: BASE * fixed.x + BASE * zoom * rk * (qx - fixed.x),
      y: BASE * fixed.y + BASE * zoom * rk * (qy - fixed.y),
    };
  };

  const first = Math.max(0, Math.floor(level) - 1);
  const last = Math.min(maxLevel, Math.floor(level) + 5);
  const layers: ReactElement[] = [];
  for (let k = first; k <= last; k += 1) {
    const rk = Math.pow(r, k);
    const tl = toCanvas(k, 0, 0);
    const w = LOGICAL_W * BASE * zoom * rk;
    const h = LOGICAL_H * BASE * zoom * rk;
    if (w < 8) break;
    const img = (
      <Img
        src={url}
        crossOrigin="anonymous"
        style={{position: 'absolute', left: 0, top: 0, width: Math.round(w), height: Math.round(h), maxWidth: 'none', maxHeight: 'none', ...pixelImage}}
      />
    );
    const tint = depthTint > 0 && k > 0 ? (
      <div style={{position: 'absolute', inset: 0, background: COLOR.charcoal, opacity: Math.min(0.7, depthTint * k)}} />
    ) : null;
    if (k === first && k === 0) {
      layers.push(
        <div key={k} style={{position: 'absolute', left: Math.round(tl.x), top: Math.round(tl.y), width: Math.round(w), height: Math.round(h)}}>
          {img}
        </div>,
      );
      continue;
    }
    // Clip to the screen of level k-1.
    const c0 = toCanvas(k - 1, sc.x, sc.y);
    const c1 = toCanvas(k - 1, sc.x + sc.w, sc.y + sc.h);
    const cx = Math.round(c0.x);
    const cy = Math.round(c0.y);
    layers.push(
      <div key={k} style={{position: 'absolute', left: cx, top: cy, width: Math.round(c1.x) - cx, height: Math.round(c1.y) - cy, overflow: 'hidden'}}>
        <div style={{position: 'absolute', left: Math.round(tl.x) - cx, top: Math.round(tl.y) - cy, width: Math.round(w), height: Math.round(h)}}>
          {img}
          {tint}
        </div>
      </div>,
    );
  }

  return <AbsoluteFill style={{overflow: 'hidden', background: p.background ?? COLOR.charcoal}}>{layers}</AbsoluteFill>;
}
