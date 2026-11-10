import type {CSSProperties, ReactElement} from 'react';
import {Img, useCurrentFrame} from 'remotion';
import {
  COLOR,
  FAMILY,
  elementSource,
  finiteNumber,
  hashUnit,
  integerIn,
  narrowParams,
  type ElementComponentProps,
} from '../../_shared/am';

// am-burst: a pop-up burst for asides and celebration beats. Items stamp in on
// an arc, one after another, with a two-frame flicker. Each item throws a ring of
// 6 px squares that expands in three steps behind it. Confetti is optional and
// seeded. Positions snap to the 6 px grid; the bob is one logical px, on threes.

type BurstItem = {
  src?: string;
  glyph?: string;
  label?: string;
  angle?: number;
  distance?: number;
  at?: number;
  size?: number;
};
type Confetti = {at?: number; count?: number; seed?: number; spread?: number; life?: number; colors?: string[]};
type Params = {
  items?: BurstItem[];
  center?: {x?: number; y?: number};
  stepFrames?: number;
  x?: number;
  y?: number;
  width?: number;
  height?: number;
  seed?: number;
  bob?: number;
  ring?: boolean;
  color?: string;
  confetti?: Confetti | null;
};

const PIXEL = 6;
const DEFAULT_CONFETTI_COLORS = [COLOR.ink, COLOR.orange, COLOR.rust, COLOR.rule, '#FF7A2E'];
const MONO = `'${FAMILY.label}', monospace`;

const snap = (value: number): number => Math.round(value / PIXEL) * PIXEL;

// Squares on a pixel circle of `radius` cells, centred on the origin.
export const ringCells = (radius: number): [number, number][] => {
  const cells: [number, number][] = [];
  for (let j = -radius; j <= radius; j += 1) {
    for (let i = -radius; i <= radius; i += 1) {
      const d = Math.sqrt(i * i + j * j);
      if (Math.abs(d - radius) < 0.5) cells.push([i, j]);
    }
  }
  return cells;
};

// Ring step for an item `age` frames after it stamps: steps 1..3 on twos.
const ringStep = (age: number): number | null => {
  if (age < 0) return null;
  const step = Math.floor(age / 2) + 1;
  return step <= 3 ? step : null;
};

// Entrance: hidden for two frames, visible for two, then solid.
const entranceVisible = (age: number): boolean => age >= 0 && (age >= 4 || Math.floor(age / 2) % 2 === 1);

const ItemVisual = ({item, size, color}: {item: BurstItem; size: number; color: string}): ReactElement => {
  if (typeof item.src === 'string' && item.src.trim() !== '') {
    const url = elementSource(undefined, item.src);
    if (url) {
      return <Img src={url} style={{width: size, height: 'auto', imageRendering: 'pixelated', display: 'block'}} />;
    }
  }
  if (typeof item.glyph === 'string' && item.glyph !== '') {
    return (
      <div style={{fontFamily: MONO, fontSize: size, lineHeight: 1, color, whiteSpace: 'pre'}}>{item.glyph}</div>
    );
  }
  // Placeholder: an ink square with an orange pixel, so an empty item is still visible.
  return (
    <div style={{position: 'relative', width: size, height: size, background: color}}>
      <div style={{position: 'absolute', left: size / 2 - PIXEL / 2, top: size / 2 - PIXEL / 2, width: PIXEL, height: PIXEL, background: COLOR.orange}} />
    </div>
  );
};

export default function AmBurst(props: ElementComponentProps): ReactElement | null {
  const frame = useCurrentFrame();
  const params = narrowParams<Params>(props.params);
  const items = Array.isArray(params.items) ? params.items : [];
  const width = finiteNumber(params.width, 1920);
  const height = finiteNumber(params.height, 1080);
  const cx = snap(finiteNumber(params.center?.x, 960));
  const cy = snap(finiteNumber(params.center?.y, 540));
  const stepFrames = integerIn(params.stepFrames, 1, 30, 4);
  const seed = integerIn(params.seed, 0, 2 ** 30, 1);
  const bobPx = finiteNumber(params.bob, PIXEL);
  const ringOn = params.ring !== false;
  const color = typeof params.color === 'string' ? params.color : COLOR.ink;
  const conf = params.confetti ?? null;

  const placed = items.map((item, index) => {
    const size = snap(finiteNumber(item.size, 96));
    const angle = (finiteNumber(item.angle, -90) * Math.PI) / 180;
    const distance = finiteNumber(item.distance, 300);
    const at = finiteNumber(item.at, index * stepFrames);
    const ix = snap(cx + Math.cos(angle) * distance - size / 2);
    const iy = snap(cy + Math.sin(angle) * distance - size / 2);
    return {item, size, at, ix, iy, index};
  });

  const confetti: ReactElement[] = [];
  if (conf) {
    const at = finiteNumber(conf.at, 0);
    const count = integerIn(conf.count, 0, 400, 48);
    const spread = finiteNumber(conf.spread, 360);
    const life = integerIn(conf.life, 4, 240, 48);
    const palette = Array.isArray(conf.colors) && conf.colors.length > 0 ? conf.colors : DEFAULT_CONFETTI_COLORS;
    const cseed = integerIn(conf.seed, 0, 2 ** 30, seed);
    const t = frame - at;
    if (t >= 0 && t < life) {
      const steps = Math.floor(t / 2) * 2; // stepped on twos
      for (let i = 0; i < count; i += 1) {
        const a = hashUnit(cseed, i * 4) * Math.PI * 2;
        const r = (0.3 + 0.7 * hashUnit(cseed, i * 4 + 1)) * spread * 0.4;
        const speed = 2 + hashUnit(cseed, i * 4 + 2) * 6;
        const dx = Math.cos(a) * (r + speed * steps);
        const dy = Math.sin(a) * (r + speed * steps) + 0.04 * steps * steps;
        const colour = palette[Math.floor(hashUnit(cseed, i * 4 + 3) * palette.length)] ?? COLOR.ink;
        confetti.push(
          <div
            key={i}
            style={{position: 'absolute', left: snap(cx + dx), top: snap(cy + dy), width: PIXEL, height: PIXEL, background: colour}}
          />,
        );
      }
    }
  }

  return (
    <div style={{position: 'absolute', left: finiteNumber(params.x, 0), top: finiteNumber(params.y, 0), width, height, overflow: 'hidden'}}>
      {confetti}
      {placed.map(({item, size, at, ix, iy, index}) => {
        const age = frame - at;
        const visible = entranceVisible(age);
        const bob = age >= 4 && Math.floor((age - 4) / 3) % 2 === 1 ? -bobPx : 0;
        const step = ringOn ? ringStep(age) : null;
        const ringRadius = step ? step * 2 : 0;
        const ringX = ix + size / 2;
        const ringY = iy + size / 2;
        const labelOn = age >= 4 && typeof item.label === 'string' && item.label !== '';
        const itemStyle: CSSProperties = {
          position: 'absolute',
          left: ix,
          top: iy + Math.round(bob / PIXEL) * PIXEL,
          width: size,
          visibility: visible ? 'visible' : 'hidden',
        };
        return (
          <div key={index}>
            {step ? (
              ringCells(ringRadius).map(([i, j], k) => (
                <div
                  key={`${index}-${k}`}
                  style={{
                    position: 'absolute',
                    left: snap(ringX + i * PIXEL * 2) - PIXEL / 2,
                    top: snap(ringY + j * PIXEL * 2) - PIXEL / 2,
                    width: PIXEL,
                    height: PIXEL,
                    background: color,
                  }}
                />
              ))
            ) : null}
            <div style={itemStyle}>
              <ItemVisual item={item} size={size} color={color} />
              {labelOn ? (
                <div
                  style={{
                    position: 'absolute',
                    left: -size / 2,
                    width: size * 2,
                    top: size + 12,
                    textAlign: 'center',
                    fontFamily: MONO,
                    fontSize: 16,
                    lineHeight: 1,
                    letterSpacing: '0.12em',
                    textTransform: 'uppercase',
                    color,
                    whiteSpace: 'nowrap',
                  }}
                >
                  {item.label}
                </div>
              ) : null}
            </div>
          </div>
        );
      })}
    </div>
  );
}
