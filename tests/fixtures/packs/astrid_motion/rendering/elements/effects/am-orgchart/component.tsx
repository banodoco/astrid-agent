import type {ReactElement} from 'react';
import {useCurrentFrame} from 'remotion';
import {COLOR, FAMILY, clamp, finiteNumber, narrowParams, type ElementComponentProps} from '../../_shared/am';

// am-orgchart: a recursive pixel org chart. One agent manages `branching`
// agents, each of which manages `branching` more, down `levels` rows. Rows stamp
// in level by level (left to right inside a level), the boxes shrink as the
// tree widens, and the deepest row is just dots. Optional row labels on the
// left and a rubber stamp on top. Drawn on a canvas on whole px, so edges stay
// hard; everything is a function of the frame.

type Stamp = {text?: string; at?: number; x?: number; y?: number; rotate?: number; size?: number};
type Params = {
  x?: number;
  y?: number;
  width?: number;
  height?: number;
  levels?: number;
  branching?: number;
  /** Clip frame each level starts appearing (one entry per level). */
  revealAt?: number[];
  /** Frames a level takes to fill in, left to right. */
  spread?: number;
  labels?: string[];
  stamp?: Stamp | null;
  background?: string | null;
};

const MONO = `'${FAMILY.label}', monospace`;
const INK = COLOR.ink;

// 8x8 bot head: antenna, head, two eyes.
const BOT = ['00011000', '00011000', '01111110', '11111111', '11011011', '11111111', '11100111', '01111110'];
const EYES = new Set(['4,2', '4,5']);

const drawBot = (ctx: CanvasRenderingContext2D, cx: number, cy: number, size: number, hot: boolean): void => {
  if (size < 12) {
    ctx.fillStyle = hot ? COLOR.orange : INK;
    const d = Math.max(4, Math.round(size));
    ctx.fillRect(Math.round(cx - d / 2), Math.round(cy - d / 2), d, d);
    return;
  }
  const cell = Math.max(1, Math.floor(size / 8));
  const ox = Math.round(cx - 4 * cell);
  const oy = Math.round(cy - 4 * cell);
  for (let r = 0; r < 8; r += 1) {
    for (let c = 0; c < 8; c += 1) {
      if (BOT[r][c] !== '1') continue;
      const eye = EYES.has(`${r},${c}`);
      ctx.fillStyle = eye ? (hot ? COLOR.orange : COLOR.panel) : INK;
      ctx.fillRect(ox + c * cell, oy + r * cell, cell, cell);
    }
  }
};

export default function AmOrgchart(props: ElementComponentProps): ReactElement {
  const frame = useCurrentFrame();
  const p = narrowParams<Params>(props.params);
  const x = finiteNumber(p.x, 420);
  const y = finiteNumber(p.y, 150);
  const width = finiteNumber(p.width, 1400);
  const height = finiteNumber(p.height, 760);
  const levels = Math.round(clamp(finiteNumber(p.levels, 5), 1, 7));
  const b = Math.round(clamp(finiteNumber(p.branching, 3), 2, 5));
  const spread = Math.max(1, Math.round(finiteNumber(p.spread, 8)));
  const reveal = Array.isArray(p.revealAt) ? p.revealAt : [];
  const revealOf = (k: number): number => finiteNumber(reveal[k], k * 12);
  const rowH = height / levels;
  const labels = Array.isArray(p.labels) ? p.labels : [];

  const paint = (ctx: CanvasRenderingContext2D): void => {
    ctx.clearRect(0, 0, 1920, 1080);
    // Box size per level: the widest row decides; the top rows stay readable.
    const sizeOf = (k: number): number => {
      const n = Math.pow(b, k);
      return Math.min(96, Math.max(4, Math.floor((width / n) * 0.62)), rowH * 0.55);
    };
    const posOf = (k: number, i: number): [number, number] => {
      const n = Math.pow(b, k);
      return [x + ((i + 0.5) * width) / n, y + (k + 0.5) * rowH];
    };
    const shownCount = (k: number): number => {
      const n = Math.pow(b, k);
      const t = frame - revealOf(k);
      if (t < 0) return 0;
      return Math.min(n, Math.floor(((t + 1) * n) / spread));
    };
    for (let k = 0; k < levels; k += 1) {
      const n = Math.pow(b, k);
      const shown = shownCount(k);
      const size = sizeOf(k);
      const line = Math.max(2, Math.round(6 / (k + 1)));
      ctx.fillStyle = INK;
      // Connectors from each shown box's parent.
      if (k > 0) {
        const psize = sizeOf(k - 1);
        for (let i = 0; i < shown; i += 1) {
          const [cx, cy] = posOf(k, i);
          const [px, py] = posOf(k - 1, Math.floor(i / b));
          const mid = Math.round((py + psize / 2 + cy - size / 2) / 2);
          ctx.fillRect(Math.round(px - line / 2), Math.round(py + psize / 2), line, Math.max(0, mid - Math.round(py + psize / 2)));
          ctx.fillRect(Math.round(Math.min(px, cx) - line / 2), mid, Math.round(Math.abs(cx - px)) + line, line);
          ctx.fillRect(Math.round(cx - line / 2), mid, line, Math.max(0, Math.round(cy - size / 2) - mid));
        }
      }
      for (let i = 0; i < shown; i += 1) {
        const [cx, cy] = posOf(k, i);
        const fresh = frame - revealOf(k) < spread + 2 && i >= shown - Math.max(1, Math.ceil(n / spread));
        if (size >= 24) {
          const pad = Math.round(size * 0.2);
          ctx.fillStyle = INK;
          ctx.fillRect(Math.round(cx - size / 2 - pad - 3), Math.round(cy - size / 2 - pad - 3), Math.round(size + 2 * pad + 6), Math.round(size + 2 * pad + 6));
          ctx.fillStyle = COLOR.panel;
          ctx.fillRect(Math.round(cx - size / 2 - pad), Math.round(cy - size / 2 - pad), Math.round(size + 2 * pad), Math.round(size + 2 * pad));
        }
        drawBot(ctx, cx, cy, size, fresh || k === 0);
      }
    }
  };

  const stamp = p.stamp && typeof p.stamp === 'object' && p.stamp.text ? p.stamp : null;
  let stampNode: ReactElement | null = null;
  if (stamp) {
    const age = frame - finiteNumber(stamp.at, 0);
    if (age >= 0) {
      const sc = age === 0 ? 1.6 : age === 1 ? 1.18 : 1;
      const jolt = age === 1 ? 6 : 0;
      stampNode = (
        <div
          style={{
            position: 'absolute',
            left: finiteNumber(stamp.x, 1040),
            top: finiteNumber(stamp.y, 420),
            transform: `translate(${jolt}px, ${-jolt}px) rotate(${finiteNumber(stamp.rotate, -6)}deg) scale(${sc})`,
            transformOrigin: '50% 50%',
            fontFamily: MONO,
            fontSize: finiteNumber(stamp.size, 96),
            lineHeight: 1,
            letterSpacing: '0.1em',
            color: COLOR.rust,
            border: `8px solid ${COLOR.rust}`,
            outline: `3px solid ${COLOR.rust}`,
            outlineOffset: 6,
            padding: '18px 28px 14px',
            whiteSpace: 'nowrap',
            background: 'rgba(247, 244, 237, 0.86)',
            boxShadow: '8px 8px 0 0 rgba(37, 36, 31, 0.9)',
          }}
        >
          {stamp.text}
        </div>
      );
    }
  }

  return (
    <div style={{position: 'absolute', inset: 0, pointerEvents: 'none', background: p.background ?? undefined}}>
      <canvas
        ref={(node) => {
          if (!node) return;
          const ctx = node.getContext('2d');
          if (ctx) paint(ctx);
        }}
        width={1920}
        height={1080}
        style={{position: 'absolute', left: 0, top: 0, width: 1920, height: 1080}}
      />
      {labels.map((text, k) =>
        typeof text === 'string' && k < levels && frame >= revealOf(k) ? (
          <div
            key={k}
            style={{
              position: 'absolute',
              left: 96,
              top: Math.round(y + (k + 0.5) * rowH - 16),
              width: Math.max(120, x - 130),
              fontFamily: MONO,
              fontSize: 26,
              lineHeight: '32px',
              letterSpacing: '0.1em',
              color: k === 0 ? COLOR.orange : COLOR.muted,
              textTransform: 'uppercase',
            }}
          >
            {text}
          </div>
        ) : null,
      )}
      {stampNode}
    </div>
  );
}
