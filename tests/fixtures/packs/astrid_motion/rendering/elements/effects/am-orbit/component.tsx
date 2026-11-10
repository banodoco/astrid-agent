import type {ReactElement} from 'react';
import {useCurrentFrame} from 'remotion';
import {COLOR, FAMILY, clamp, finiteNumber, narrowParams, type ElementComponentProps} from '../../_shared/am';

// am-orbit: a small pixel orbit HUD. The Earth steps round the Sun past twelve
// month ticks while a mono readout names the month. Drawn on the 6 px grid.

type Params = {
  x?: number;
  y?: number;
  /** Orbit radius in logical px. */
  radius?: number;
  /** Month index the Earth starts on (0 = January). */
  fromMonth?: number;
  fromYear?: number;
  /** Months travelled over `frames`. */
  months?: number;
  at?: number;
  frames?: number;
  stepFrames?: number;
  dark?: boolean;
  caption?: string;
};

const MONTHS = ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC'];
const PX = 6;

export default function AmOrbit(props: ElementComponentProps): ReactElement {
  const frame = useCurrentFrame();
  const p = narrowParams<Params>(props.params);
  const R = Math.round(clamp(finiteNumber(p.radius, 20), 8, 60));
  const size = 2 * R + 9;
  const x = finiteNumber(p.x, 1920 - 96 - size * PX);
  const y = finiteNumber(p.y, 96);
  const fromMonth = Math.round(finiteNumber(p.fromMonth, 9));
  const fromYear = Math.round(finiteNumber(p.fromYear, 2025));
  const months = finiteNumber(p.months, 12);
  const at = finiteNumber(p.at, 0);
  const frames = Math.max(1, Math.round(finiteNumber(p.frames, 60)));
  const stepFrames = Math.max(1, Math.round(finiteNumber(p.stepFrames, 2)));
  const f = Math.floor(Math.max(0, frame - at) / stepFrames) * stepFrames;
  const progress = clamp(f / frames, 0, 1);
  const monthPos = fromMonth + months * progress;
  const monthIndex = Math.floor(monthPos + 1e-6);
  const ink = p.dark ? COLOR.panel : COLOR.ink;
  const ring = p.dark ? '#5B5850' : '#C9C3B6';
  const c = R + 4;
  const angleOf = (m: number): number => ((m - 3) / 12) * Math.PI * 2; // April at the right, January at the top

  const cells: {x: number; y: number; color: string}[] = [];
  const put = (px: number, py: number, color: string): void => {
    cells.push({x: Math.round(px), y: Math.round(py), color});
  };
  // Orbit ring, dotted.
  const steps = Math.round(2 * Math.PI * R);
  for (let i = 0; i < steps; i += 2) {
    const a = (i / steps) * Math.PI * 2;
    put(c + Math.cos(a) * R, c + Math.sin(a) * R, ring);
  }
  // Month ticks.
  for (let m = 0; m < 12; m += 1) {
    const a = angleOf(m);
    const passed = m === ((monthIndex % 12) + 12) % 12;
    put(c + Math.cos(a) * (R + 2), c + Math.sin(a) * (R + 2), passed ? COLOR.orange : ink);
    put(c + Math.cos(a) * (R + 3), c + Math.sin(a) * (R + 3), passed ? COLOR.orange : ink);
  }
  // Sun.
  for (let dy = -3; dy <= 3; dy += 1) {
    for (let dx = -3; dx <= 3; dx += 1) {
      if (dx * dx + dy * dy <= 10) put(c + dx, c + dy, dx * dx + dy * dy <= 4 ? '#FFD27A' : COLOR.orange);
    }
  }
  // Trail and Earth.
  for (let k = 3; k >= 1; k -= 1) {
    const a = angleOf(monthPos - k * 0.18);
    put(c + Math.cos(a) * R, c + Math.sin(a) * R, p.dark ? '#3E5F7E' : '#9DB7CF');
  }
  const ea = angleOf(monthPos);
  const ex = c + Math.cos(ea) * R;
  const ey = c + Math.sin(ea) * R;
  for (let dy = -1; dy <= 1; dy += 1) {
    for (let dx = -1; dx <= 1; dx += 1) {
      if (Math.abs(dx) + Math.abs(dy) <= 1) put(ex + dx, ey + dy, dx === 0 && dy === 0 ? '#6AA651' : '#3F7FC1');
    }
  }
  const year = fromYear + Math.floor(monthIndex / 12);
  const label = `${MONTHS[((monthIndex % 12) + 12) % 12]} ${String(year).slice(2)}`;

  return (
    <div style={{position: 'absolute', left: x, top: y, width: size * PX, pointerEvents: 'none'}}>
      <div style={{position: 'relative', width: size * PX, height: size * PX}}>
        {cells.map((cell, i) => (
          <div key={i} style={{position: 'absolute', left: cell.x * PX, top: cell.y * PX, width: PX, height: PX, background: cell.color}} />
        ))}
      </div>
      <div style={{marginTop: 6, textAlign: 'center', fontFamily: `'${FAMILY.label}', monospace`, fontSize: 30, letterSpacing: '0.12em', color: ink}}>
        {label}
      </div>
      {p.caption ? (
        <div style={{textAlign: 'center', fontFamily: `'${FAMILY.label}', monospace`, fontSize: 18, letterSpacing: '0.12em', color: p.dark ? '#B9B4A8' : COLOR.muted}}>
          {p.caption}
        </div>
      ) : null}
    </div>
  );
}
