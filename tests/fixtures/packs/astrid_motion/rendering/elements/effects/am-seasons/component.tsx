import type {ReactElement} from 'react';
import {AbsoluteFill, useCurrentFrame} from 'remotion';
import {LOGICAL_H, LOGICAL_W, hashUnit, narrowParams, type ElementComponentProps} from '../../_shared/am';

// am-seasons: a window onto a street through the seasons, drawn on the
// 320x180 logical grid (no assets). A locked camera: the frame, the sill, the
// buildings and the tree stay put; the sky, the leaves, the weather, the plant
// on the sill and the pile of mugs change. Use it as the procedural stand-in
// for the S-00..S-04 window plates, or as a visualization in its own right.
// Weather particles step on twos and are seeded, so frames are repeatable.

type Season = 'autumn' | 'winter' | 'spring' | 'summer' | 'autumn2';
type Key = {at?: number; season?: string; growth?: number; mugs?: number};
type Params = {season?: string; growth?: number; mugs?: number; sequence?: Key[]; seed?: number; label?: boolean};

const SEASONS: readonly Season[] = ['autumn', 'winter', 'spring', 'summer', 'autumn2'];
const DEFAULT_GROWTH: Record<Season, number> = {autumn: 0, winter: 1, spring: 2, summer: 3, autumn2: 4};
const DEFAULT_MUGS: Record<Season, number> = {autumn: 1, winter: 1, spring: 2, summer: 3, autumn2: 6};

type Look = {sky: [string, string]; sun: [number, number, string] | null; leaves: string[]; build: string[]; street: string; wall: string; wallShade: string; lit: number};
const LOOK: Record<Season, Look> = {
  autumn: {sky: ['#F4C08F', '#F8DAB6'], sun: [252, 60, '#FFE6B0'], leaves: ['#ED6B23', '#A94714', '#F2A15A', '#C9541A'], build: ['#C79C80', '#B4836A', '#D6B39A', '#A87A62'], street: '#8B7E72', wall: '#6E5241', wallShade: '#5E4434', lit: 0.1},
  winter: {sky: ['#A9B4BF', '#C8CFD6'], sun: null, leaves: [], build: ['#A79A92', '#968A84', '#B7ACA4', '#8C807A'], street: '#E9ECEE', wall: '#5A4A40', wallShade: '#4C3D35', lit: 0.45},
  spring: {sky: ['#B9D6E4', '#E2EDF0'], sun: null, leaves: ['#F4C3CF', '#FADDE4', '#E9A9BA', '#9CC77A', '#B7D98F'], build: ['#D1AE95', '#BF957C', '#DFC2AA', '#B08872'], street: '#8E8A80', wall: '#6E5241', wallShade: '#5E4434', lit: 0},
  summer: {sky: ['#79B4DE', '#A7D1EC'], sun: [86, 24, '#FFF4C8'], leaves: ['#3D7838', '#559248', '#2E5C2B', '#6AA651'], build: ['#D9B79C', '#C59E84', '#E6CBB3', '#B79079'], street: '#9A9387', wall: '#7A5C48', wallShade: '#684C3A', lit: 0},
  autumn2: {sky: ['#F4C08F', '#F8DAB6'], sun: [252, 60, '#FFE6B0'], leaves: ['#ED6B23', '#A94714', '#F2A15A', '#C9541A'], build: ['#C79C80', '#B4836A', '#D6B39A', '#A87A62'], street: '#8B7E72', wall: '#6E5241', wallShade: '#5E4434', lit: 0.1},
};

const INK = '#25241F';
const CREAM = '#F4E9D7';
const CREAM2 = '#D9C9AE';
// Window opening (logical px).
const WX = 44;
const WY = 12;
const WW = 232;
const WH = 118;

const seasonOf = (v: unknown, fallback: Season): Season =>
  typeof v === 'string' && (SEASONS as readonly string[]).includes(v) ? (v as Season) : fallback;

const paint = (ctx: CanvasRenderingContext2D, season: Season, growth: number, mugs: number, frame: number, seed: number): void => {
  const L = LOOK[season];
  const R = (x: number, y: number, w: number, h: number, c: string): void => {
    ctx.fillStyle = c;
    ctx.fillRect(Math.round(x), Math.round(y), Math.round(w), Math.round(h));
  };
  const disc = (cx: number, cy: number, rx: number, ry: number, c: string): void => {
    for (let dy = -ry; dy <= ry; dy += 1) {
      const span = Math.round(rx * Math.sqrt(Math.max(0, 1 - (dy * dy) / (ry * ry + 0.01))));
      R(cx - span, cy + dy, span * 2 + 1, 1, c);
    }
  };
  const t = Math.floor(frame / 2) * 2;

  // Room wall.
  R(0, 0, LOGICAL_W, LOGICAL_H, L.wall);
  for (let y = 0; y < LOGICAL_H; y += 6) R(0, y, LOGICAL_W, 1, L.wallShade);

  // ---- Outside (clipped to the opening).
  ctx.save();
  ctx.beginPath();
  ctx.rect(WX, WY, WW, WH);
  ctx.clip();
  R(WX, WY, WW, 46, L.sky[0]);
  R(WX, WY + 46, WW, WH - 46, L.sky[1]);
  if (L.sun) disc(L.sun[0], L.sun[1], 7, 7, L.sun[2]);
  // Buildings (fixed silhouettes, colours by season).
  const blocks = [
    [44, 52, 48, 80],
    [92, 40, 40, 92],
    [132, 58, 30, 74],
    [226, 46, 54, 86],
  ];
  blocks.forEach(([bx, by, bw, bh], i) => {
    const c = L.build[i % L.build.length];
    R(bx, by, bw, bh, c);
    R(bx, by, bw, 2, INK);
    R(bx, by, 1, bh, INK);
    if (season === 'winter') R(bx, by - 2, bw, 3, '#F7F8F8');
    for (let wy = by + 7; wy < by + bh - 10; wy += 11) {
      for (let wx = bx + 5; wx < bx + bw - 6; wx += 9) {
        const on = hashUnit(seed + 11, wx * 31 + wy) < L.lit;
        R(wx, wy, 4, 6, on ? '#FFD98A' : '#5B4A42');
      }
    }
  });
  R(WX, 124, WW, 8, L.street);
  // Tree: trunk and branches.
  R(198, 70, 7, 62, '#4A3226');
  R(196, 120, 11, 12, '#4A3226');
  const branch = (x0: number, y0: number, x1: number, y1: number): void => {
    const n = Math.max(Math.abs(x1 - x0), Math.abs(y1 - y0));
    for (let i = 0; i <= n; i += 1) {
      const x = x0 + ((x1 - x0) * i) / n;
      const y = y0 + ((y1 - y0) * i) / n;
      R(x, y, 2, 2, '#4A3226');
      if (season === 'winter' && i % 2 === 0) R(x, y - 1, 2, 1, '#F7F8F8');
    }
  };
  branch(200, 80, 172, 50);
  branch(203, 76, 232, 44);
  branch(201, 72, 196, 34);
  branch(186, 64, 166, 66);
  branch(218, 60, 240, 70);
  // Canopy: seeded blobs, density by season.
  const density = season === 'winter' ? 0 : season === 'autumn2' ? 0.55 : season === 'spring' ? 0.85 : 1;
  if (density > 0) {
    for (let i = 0; i < 46; i += 1) {
      if (hashUnit(seed, i) > density) continue;
      const a = hashUnit(seed + 1, i) * Math.PI * 2;
      const d = Math.sqrt(hashUnit(seed + 2, i));
      const cx = 202 + Math.cos(a) * d * 40;
      const cy = 50 + Math.sin(a) * d * 24;
      const c = L.leaves[Math.floor(hashUnit(seed + 3, i) * L.leaves.length)];
      disc(cx, cy, 6, 5, c);
    }
  }
  // Weather.
  if (season === 'winter') {
    for (let i = 0; i < 90; i += 1) {
      const x0 = WX + hashUnit(seed + 4, i) * WW;
      const speed = 0.5 + hashUnit(seed + 5, i) * 0.6;
      const y = WY + ((hashUnit(seed + 6, i) * WH + t * speed) % WH);
      const x = x0 + Math.round(Math.sin((t + i * 7) / 9) * 2);
      R(x, y, 1, 1, '#FFFFFF');
    }
  } else if (season === 'autumn' || season === 'autumn2') {
    for (let i = 0; i < 16; i += 1) {
      const x0 = 150 + hashUnit(seed + 4, i) * 110;
      const speed = 0.35 + hashUnit(seed + 5, i) * 0.4;
      const y = 50 + ((hashUnit(seed + 6, i) * 80 + t * speed) % 80);
      const x = x0 - ((t * speed * 0.6) % 40);
      R(x, y, 2, 1, L.leaves[i % L.leaves.length]);
    }
  } else if (season === 'spring') {
    for (let i = 0; i < 40; i += 1) {
      const x = WX + hashUnit(seed + 4, i) * WW;
      const y = WY + ((hashUnit(seed + 6, i) * WH + t * 2.5) % WH);
      R(x, y, 1, 3, '#9FB8C8');
    }
  }
  ctx.restore();

  // ---- Window frame, mullions, sill.
  R(WX - 6, WY - 6, WW + 12, 6, CREAM);
  R(WX - 6, WY + WH, WW + 12, 4, CREAM);
  R(WX - 6, WY - 6, 6, WH + 10, CREAM);
  R(WX + WW, WY - 6, 6, WH + 10, CREAM);
  R(WX + WW / 2 - 2, WY, 4, WH, CREAM);
  R(WX, WY + 56, WW, 3, CREAM);
  R(WX - 7, WY - 7, WW + 14, 1, INK);
  R(WX - 7, WY - 7, 1, WH + 12, INK);
  R(WX + WW + 6, WY - 7, 1, WH + 12, INK);
  // Sill.
  R(20, 134, 280, 6, CREAM);
  R(20, 140, 280, 5, CREAM2);
  R(20, 133, 280, 1, INK);
  R(20, 145, 280, 1, INK);
  R(0, 146, LOGICAL_W, LOGICAL_H - 146, L.wallShade);
  // Festive lights in winter.
  if (season === 'winter') {
    const colours = ['#ED6B23', '#FFD27A', '#E5382B', '#9CC77A'];
    for (let i = 0; i * 10 < WW + 8; i += 1) {
      const on = (Math.floor(t / 6) + i) % 3 !== 0;
      R(WX - 2 + i * 10, WY - 3 + (i % 2), 2, 2, on ? colours[i % colours.length] : '#5B4A42');
    }
  }
  // Plant on the sill: pot plus foliage that grows through the year.
  const g = Math.max(0, Math.min(4, growth));
  R(70, 122, 18, 12, '#A94714');
  R(70, 122, 18, 2, '#7E3410');
  R(69, 121, 20, 1, INK);
  const fw = 10 + g * 9;
  const fh = 6 + g * 8;
  for (let i = 0; i < 10 + g * 10; i += 1) {
    const cx = 79 + (hashUnit(seed + 8, i) - 0.5) * fw;
    const cy = 120 - hashUnit(seed + 9, i) * fh;
    disc(cx, cy, 2 + (i % 2), 2, i % 3 === 0 ? '#559248' : '#3D7838');
  }
  if (g >= 4) {
    for (let v = 0; v < 5; v += 1) {
      const vx = 64 + v * 7;
      for (let y = 134; y < 134 + 8 + v * 3; y += 2) R(vx + ((y >> 1) % 2), y, 2, 2, '#3D7838');
    }
  }
  // Mugs: a pile that grows through the year.
  for (let m = 0; m < Math.max(0, Math.min(8, mugs)); m += 1) {
    const col = m % 3;
    const row = Math.floor(m / 3);
    const mx = 232 + col * 11 + row * 5;
    const my = 124 - row * 10;
    R(mx, my, 8, 10, CREAM);
    R(mx, my, 8, 1, INK);
    R(mx, my, 1, 10, INK);
    R(mx + 7, my, 1, 10, INK);
    R(mx + 8, my + 3, 2, 4, INK);
    R(mx + 1, my + 3, 6, 2, m % 2 ? '#ED6B23' : '#A94714');
  }
};

export default function AmSeasons(props: ElementComponentProps): ReactElement {
  const frame = useCurrentFrame();
  const p = narrowParams<Params>(props.params);
  const seed = Math.trunc(typeof p.seed === 'number' ? p.seed : 5);
  let season = seasonOf(p.season, 'autumn');
  let growth = typeof p.growth === 'number' ? p.growth : DEFAULT_GROWTH[season];
  let mugs = typeof p.mugs === 'number' ? p.mugs : DEFAULT_MUGS[season];
  const seq = (Array.isArray(p.sequence) ? p.sequence : []).slice().sort((a, b) => (a.at ?? 0) - (b.at ?? 0));
  for (const k of seq) {
    if ((k.at ?? 0) > frame) break;
    season = seasonOf(k.season, season);
    growth = typeof k.growth === 'number' ? k.growth : DEFAULT_GROWTH[season];
    mugs = typeof k.mugs === 'number' ? k.mugs : DEFAULT_MUGS[season];
  }
  return (
    <AbsoluteFill>
      <canvas
        ref={(node) => {
          if (!node) return;
          const ctx = node.getContext('2d');
          if (ctx) paint(ctx, season, growth, mugs, frame, seed);
        }}
        width={LOGICAL_W}
        height={LOGICAL_H}
        style={{position: 'absolute', left: 0, top: 0, width: 1920, height: 1080, imageRendering: 'pixelated'}}
      />
    </AbsoluteFill>
  );
}
