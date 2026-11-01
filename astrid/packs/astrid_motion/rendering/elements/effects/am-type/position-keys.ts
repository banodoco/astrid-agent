export type PositionKey = {at: number; x?: number; y?: number; ease?: 'linear' | 'easeOut' | 'easeInOut'};

const easeOutCubic = (t: number): number => 1 - Math.pow(1 - Math.min(1, Math.max(0, t)), 3);

const EASES = {
  linear: (t: number): number => t,
  easeOut: easeOutCubic,
  easeInOut: (t: number): number => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2),
};

// Smooth (not stepped) interpolation of a position channel. `at` is the clip
// frame. Each segment eases with the ease of the key it arrives at (default
// easeOut); before the first key and after the last the value holds. Returns
// undefined when there are no usable keys, so callers keep their plain x/y.
export const interpolateKeys = (
  keys: unknown,
  channel: 'x' | 'y',
  frame: number,
): number | undefined => {
  if (!Array.isArray(keys)) return undefined;
  const points = keys
    .map((k) => ({at: (k as PositionKey)?.at, v: (k as PositionKey)?.[channel], ease: (k as PositionKey)?.ease}))
    .filter((k) => Number.isFinite(k.at) && Number.isFinite(k.v))
    .sort((a, b) => (a.at as number) - (b.at as number)) as {at: number; v: number; ease?: keyof typeof EASES}[];
  if (points.length === 0) return undefined;
  if (frame <= points[0].at) return points[0].v;
  const last = points[points.length - 1];
  if (frame >= last.at) return last.v;
  for (let i = 1; i < points.length; i += 1) {
    const to = points[i];
    if (frame <= to.at) {
      const from = points[i - 1];
      const ease = EASES[to.ease && to.ease in EASES ? to.ease : 'easeOut'];
      return from.v + (to.v - from.v) * ease((frame - from.at) / (to.at - from.at));
    }
  }
  return last.v;
};

