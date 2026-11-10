import type {CSSProperties, ReactElement} from 'react';
import {COLOR, FAMILY} from '../../_shared/am';

// Split-flap core. Pure functions drive every glyph from the frame number, so a
// frame is deterministic. am-churn imports this module for its date readout.

// Positions and sizes snap to the 6 px grid (one logical px of the 320x180 space).
export const snapPx = (value: number): number => Math.round(value / 6) * 6;

// Drum order. A glyph flips forward through this order until it reaches its
// target, so each step shows one intermediate glyph.
export const FLAP_CHARSET = " 0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ.,:-+/'&!?";
const DIGITS = '0123456789';

export type FlapValue = {text: string; at: number};

export const padFlapText = (text: string, width: number): string =>
  text.toUpperCase().padEnd(width, ' ').slice(0, width);

// Glyph shown at `elapsed` frames after one tile started flipping from `prev`
// to `next`. One drum step every `step` frames, so the flip holds on twos.
export const flapGlyph = (
  prev: string,
  next: string,
  elapsed: number,
  step: number,
  charset: string = FLAP_CHARSET,
): string => {
  if (prev === next) return next;
  if (elapsed < 0) return prev;
  // Digit-to-digit flips run round the digit ring only (at most nine steps),
  // so counters stay readable. Anything else runs the full drum forward.
  const ring = DIGITS.includes(prev) && DIGITS.includes(next) ? DIGITS : charset;
  const from = ring.indexOf(prev);
  const to = ring.indexOf(next);
  if (from < 0 || to < 0) return elapsed >= step ? next : prev;
  const n = ring.length;
  const steps = (to - from + n) % n;
  const k = Math.min(steps, Math.floor(elapsed / Math.max(1, step)));
  return ring[(from + k) % n] ?? next;
};

// Current glyph per tile for a keyframed list of targets. Tile i starts its
// flip at values[j].at + i * stagger, so the row flips left to right.
export const flapChars = (
  values: readonly FlapValue[],
  frame: number,
  options: {width: number; stagger: number; step: number; charset?: string},
): string[] => {
  const {width, stagger, step} = options;
  const charset = options.charset ?? FLAP_CHARSET;
  const targets = values.map((value) => padFlapText(value.text, width));
  const blank = ' '.repeat(width);
  const out: string[] = [];
  for (let i = 0; i < width; i += 1) {
    let current = -1;
    for (let j = 0; j < values.length; j += 1) {
      const start = values[j].at + i * stagger;
      if (start <= frame) current = j;
    }
    if (current < 0) {
      out.push(' ');
      continue;
    }
    const start = values[current].at + i * stagger;
    const prev = current > 0 ? targets[current - 1][i] : blank[i];
    out.push(flapGlyph(prev, targets[current][i], frame - start, step, charset));
  }
  return out;
};

// Tile geometry shared by the strip and by callers that align around it.
export const flapStripWidth = (count: number, tileW: number, gap: number): number =>
  count * tileW + Math.max(0, count - 1) * gap;

export type FlapStripProps = {
  chars: readonly string[];
  tileW: number;
  tileH: number;
  gap: number;
  color: string;
  tileColor: string;
  accentIndex: readonly number[];
  accentColor: string;
  splitColor?: string;
  fontSize?: number;
  style?: CSSProperties;
};

// One row of split-flap tiles. Each tile is a dark plate with a 1 px split line
// across its middle. Glyphs are Departure Mono, centred on the split.
export const FlapStrip = ({
  chars,
  tileW,
  tileH,
  gap,
  color,
  tileColor,
  accentIndex,
  accentColor,
  splitColor = '#0E0D0B',
  fontSize,
  style,
}: FlapStripProps): ReactElement => {
  const glyphSize = fontSize ?? Math.floor(tileH * 0.6);
  const accents = new Set(accentIndex);
  return (
    <div style={{display: 'flex', gap, ...style}}>
      {chars.map((char, index) => {
        const painted = accents.has(index) ? accentColor : color;
        return (
          <div
            key={index}
            style={{
              position: 'relative',
              width: tileW,
              height: tileH,
              flex: `0 0 ${tileW}px`,
              background: tileColor,
              overflow: 'hidden',
            }}
          >
            <div
              style={{
                position: 'absolute',
                left: 0,
                right: 0,
                top: Math.floor(tileH / 2),
                height: 1,
                background: splitColor,
              }}
            />
            <div
              style={{
                position: 'absolute',
                inset: 0,
                display: 'flex',
                alignItems: 'center',
                justifyContent: 'center',
                fontFamily: `'${FAMILY.label}', monospace`,
                fontSize: glyphSize,
                lineHeight: 1,
                color: painted,
                whiteSpace: 'pre',
              }}
            >
              {char === ' ' ? ' ' : char}
            </div>
          </div>
        );
      })}
    </div>
  );
};

export const FLAP_LABEL_STYLE: CSSProperties = {
  fontFamily: `'${FAMILY.label}', monospace`,
  fontSize: 18,
  letterSpacing: '0.12em',
  textTransform: 'uppercase',
  lineHeight: 1,
  whiteSpace: 'nowrap',
  color: COLOR.muted,
};
