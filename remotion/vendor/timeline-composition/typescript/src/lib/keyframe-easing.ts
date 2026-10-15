import {motionCurveName, motionCurveProgress} from './motion-curves.generated';

export type KeyframeValue = number | string | boolean;

// Shared interval consumer. Callers supply their already-evaluated local time;
// this helper neither selects an origin nor changes media/animation clocks.
export const interpolateKeyframeInterval = (
  a: KeyframeValue,
  b: KeyframeValue,
  time: number,
  start: number,
  end: number,
  interpolation: 'linear' | 'hold' = 'hold',
  easing?: string,
): KeyframeValue => {
  if (![time, start, end].every(Number.isFinite) || end < start) {
    throw new Error('Keyframe interval must have finite time and ordered endpoints');
  }
  const name = easing === undefined ? undefined : motionCurveName(easing);
  const numeric = typeof a === 'number' && typeof b === 'number';
  if ((typeof a === 'number' && !Number.isFinite(a)) || (typeof b === 'number' && !Number.isFinite(b))) {
    throw new Error('Numeric keyframe values must be finite');
  }
  if (!numeric && name !== undefined && name !== 'linear' && name !== 'step') {
    throw new Error('Nonlinear easing requires numeric keyframe values');
  }
  const u = end === start ? (time < start ? 0 : 1) : Math.min(1, Math.max(0, (time - start) / (end - start)));
  if (!numeric || (name === undefined && interpolation === 'hold')) return u < 1 ? a : b;
  const progress = name === undefined ? u : motionCurveProgress(name, u);
  return a + (b - a) * progress;
};
