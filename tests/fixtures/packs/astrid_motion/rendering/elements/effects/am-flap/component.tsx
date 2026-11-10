import type {CSSProperties, ReactElement} from 'react';
import {useCurrentFrame} from 'remotion';
import {
  COLOR,
  FAMILY,
  finiteNumber,
  integerIn,
  narrowParams,
  oneOf,
  type ElementComponentProps,
} from '../../_shared/am';
import {
  FLAP_LABEL_STYLE,
  FlapStrip,
  flapChars,
  flapStripWidth,
  snapPx,
  type FlapValue,
} from './flap';

// am-flap: a split-flap readout for dates, numbers and short words. Each tile
// flips through the drum on two-frame steps; tiles start left to right.

type FlapParams = {
  values?: {text?: unknown; at?: unknown}[];
  tileW?: number;
  tileH?: number;
  gap?: number;
  color?: string;
  tileColor?: string;
  accentIndex?: number[];
  accentColor?: string;
  x?: number;
  y?: number;
  align?: 'left' | 'center' | 'right';
  label?: string;
  labelColor?: string;
  stagger?: number;
  stepFrames?: number;
};

const DEFAULTS = {
  tileW: 60,
  tileH: 84,
  gap: 6,
  color: COLOR.paper,
  tileColor: COLOR.ink,
  accentColor: COLOR.orange,
  x: 96,
  y: 96,
  align: 'left' as const,
  label: '',
  labelColor: COLOR.muted,
  stagger: 2,
  stepFrames: 2,
};

const DEFAULT_VALUES: FlapValue[] = [{text: '26.10.25', at: 0}];

const parseValues = (raw: FlapParams['values']): FlapValue[] => {
  if (!Array.isArray(raw)) return DEFAULT_VALUES;
  const parsed = raw
    .filter((item) => item && typeof item.text === 'string')
    .map((item) => ({text: String(item.text), at: finiteNumber(item.at, 0)}))
    .sort((a, b) => a.at - b.at);
  return parsed.length > 0 ? parsed : DEFAULT_VALUES;
};

export default function AmFlap(props: ElementComponentProps): ReactElement | null {
  const frame = useCurrentFrame();
  const params = narrowParams<FlapParams>(props.params);
  const values = parseValues(params.values);
  const tileW = snapPx(finiteNumber(params.tileW, DEFAULTS.tileW));
  const tileH = snapPx(finiteNumber(params.tileH, DEFAULTS.tileH));
  const gap = snapPx(finiteNumber(params.gap, DEFAULTS.gap));
  const stagger = integerIn(params.stagger, 0, 12, DEFAULTS.stagger);
  const stepFrames = integerIn(params.stepFrames, 1, 12, DEFAULTS.stepFrames);
  const width = Math.max(...values.map((value) => value.text.length), 1);
  const chars = flapChars(values, frame, {width, stagger, step: stepFrames});
  const stripW = flapStripWidth(width, tileW, gap);
  const align = oneOf(params.align, ['left', 'center', 'right'] as const, DEFAULTS.align);
  const anchorX = snapPx(finiteNumber(params.x, DEFAULTS.x));
  const anchorY = snapPx(finiteNumber(params.y, DEFAULTS.y));
  const left = align === 'right' ? anchorX - stripW : align === 'center' ? anchorX - Math.round(stripW / 2) : anchorX;
  const label = typeof params.label === 'string' ? params.label : '';
  const labelGap = label ? 18 : 0;
  const root: CSSProperties = {
    position: 'absolute',
    left,
    top: anchorY,
    width: stripW,
    fontFamily: `'${FAMILY.label}', monospace`,
  };
  return (
    <div style={root}>
      {label ? (
        <div
          style={{
            ...FLAP_LABEL_STYLE,
            color: typeof params.labelColor === 'string' ? params.labelColor : DEFAULTS.labelColor,
            marginBottom: labelGap,
            textAlign: align,
          }}
        >
          {label}
        </div>
      ) : null}
      <FlapStrip
        chars={chars}
        tileW={tileW}
        tileH={tileH}
        gap={gap}
        color={typeof params.color === 'string' ? params.color : DEFAULTS.color}
        tileColor={typeof params.tileColor === 'string' ? params.tileColor : DEFAULTS.tileColor}
        accentIndex={Array.isArray(params.accentIndex) ? params.accentIndex.filter((n) => Number.isInteger(n)) : []}
        accentColor={typeof params.accentColor === 'string' ? params.accentColor : DEFAULTS.accentColor}
      />
    </div>
  );
}
