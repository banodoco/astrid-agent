import type {CSSProperties, ReactElement} from 'react';
import {AbsoluteFill, useCurrentFrame, useVideoConfig} from 'remotion';
import {
  COLOR,
  FAMILY,
  clamp,
  finiteNumber,
  narrowParams,
  stepStart,
  type ElementComponentProps,
} from '../../_shared/am';

// am-callout: the site's floating callout card and its orange connector.
// The card is a cut. The connector draws in on step boundaries (stepFrames),
// so the stroke never moves between steps.

type Params = {
  title?: string;
  body?: string;
  x?: number;
  y?: number;
  width?: number;
  anchor?: {x: number; y: number};
  drawFrames?: number;
  stepFrames?: 2 | 3;
  /** Title and body type sizes in canvas px (default 40 and 24). */
  titleSize?: number;
  bodySize?: number;
};

type Pt = {x: number; y: number};

// Title line centre, measured from the card top: 22 px padding + half a line
// (22 + 0.55 * titleSize; 44 at the default 40 px title).
const CONNECTOR_SAMPLES = 96;

const bezierAt = (p0: Pt, c1: Pt, c2: Pt, p3: Pt, t: number): Pt => {
  const u = 1 - t;
  const a = u * u * u;
  const b = 3 * u * u * t;
  const c = 3 * u * t * t;
  const d = t * t * t;
  return {
    x: a * p0.x + b * c1.x + c * c2.x + d * p3.x,
    y: a * p0.y + b * c1.y + c * c2.y + d * p3.y,
  };
};

// Arc length by sampling. Deterministic, and avoids DOM measurement.
const bezierLength = (p0: Pt, c1: Pt, c2: Pt, p3: Pt): number => {
  let length = 0;
  let prev = p0;
  for (let i = 1; i <= CONNECTOR_SAMPLES; i += 1) {
    const next = bezierAt(p0, c1, c2, p3, i / CONNECTOR_SAMPLES);
    length += Math.hypot(next.x - prev.x, next.y - prev.y);
    prev = next;
  }
  return length;
};

export default function AmCallout(props: ElementComponentProps): ReactElement | null {
  const frame = useCurrentFrame();
  const {width: canvasW, height: canvasH} = useVideoConfig();
  const params = narrowParams<Params>(props.params);
  const title = typeof params.title === 'string' ? params.title : '';
  const body = typeof params.body === 'string' ? params.body : '';
  if (!title && !body) {
    return null;
  }

  const x = finiteNumber(params.x, 1200);
  const y = finiteNumber(params.y, 160);
  const cardWidth = Math.max(1, finiteNumber(params.width, 520));
  const anchor: Pt = {
    x: finiteNumber(params.anchor?.x, canvasW / 2),
    y: finiteNumber(params.anchor?.y, canvasH / 2),
  };
  const drawFrames = Math.max(1, Math.round(finiteNumber(params.drawFrames, 12)));
  const stepFrames = Math.round(clamp(finiteNumber(params.stepFrames, 2), 2, 3));

  // Attach to the card's nearer side at the title line.
  const titleSize = Math.max(12, finiteNumber(params.titleSize, 40));
  const bodySize = Math.max(12, finiteNumber(params.bodySize, 24));
  const titleLineY = 22 + Math.round(0.55 * titleSize);
  const onRight = anchor.x >= x + cardWidth / 2;
  const start: Pt = {x: onRight ? x + cardWidth : x, y: y + titleLineY};
  const dir = anchor.x >= start.x ? 1 : -1;
  const span = Math.abs(anchor.x - start.x);
  const c1: Pt = {x: start.x + dir * span * 0.5, y: start.y};
  const c2: Pt = {x: anchor.x - dir * span * 0.5, y: anchor.y};
  const length = Math.max(1, bezierLength(start, c1, c2, anchor));

  const drawP = clamp(stepStart(frame, stepFrames) / drawFrames, 0, 1);
  const done = drawP >= 1;

  const card: CSSProperties = {
    position: 'absolute',
    left: x,
    top: y,
    width: cardWidth,
    boxSizing: 'border-box',
    padding: '22px 26px 24px',
    background: COLOR.panel,
    border: `1px solid ${COLOR.rule}`,
    borderRadius: 4,
    boxShadow: '0 12px 30px rgba(31, 31, 31, 0.14)',
    color: COLOR.ink,
  };
  const titleStyle: CSSProperties = {
    margin: 0,
    fontFamily: FAMILY.display,
    fontWeight: 650,
    fontSize: titleSize,
    lineHeight: 1.1,
    letterSpacing: '-0.005em',
    color: COLOR.ink,
  };
  const bodyStyle: CSSProperties = {
    margin: title ? '10px 0 0' : 0,
    fontFamily: FAMILY.body,
    fontSize: bodySize,
    lineHeight: 1.4,
    color: COLOR.muted,
  };
  const dot = (cx: number, cy: number, opacity: number): ReactElement => (
    <circle cx={cx} cy={cy} r={6} fill={COLOR.orange} stroke={COLOR.panel} strokeWidth={2} opacity={opacity} />
  );

  return (
    <AbsoluteFill style={{pointerEvents: 'none'}}>
      <svg
        width={canvasW}
        height={canvasH}
        style={{position: 'absolute', left: 0, top: 0, overflow: 'visible'}}
      >
        <path
          d={`M ${start.x} ${start.y} C ${c1.x} ${c1.y} ${c2.x} ${c2.y} ${anchor.x} ${anchor.y}`}
          fill="none"
          stroke={COLOR.orange}
          strokeWidth={3}
          strokeDasharray={`${length} ${length}`}
          strokeDashoffset={length * (1 - drawP)}
        />
        {dot(start.x, start.y, 1)}
        {dot(anchor.x, anchor.y, done ? 1 : 0)}
      </svg>
      <div style={card}>
        {title ? <h3 style={titleStyle}>{title}</h3> : null}
        {body ? <p style={bodyStyle}>{body}</p> : null}
      </div>
    </AbsoluteFill>
  );
}
