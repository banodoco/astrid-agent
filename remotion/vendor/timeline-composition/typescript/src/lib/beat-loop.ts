/** Resolved by the producer's existing moment/placed-beat resolver, in global
 * composition frames. Onset and keyframeOrigin are deliberately absent. */
export type ResolvedLoop = {phaseFrame: number} & (
  | {periodFrames: number; beatFrames?: never; periodBeats?: never}
  | {beatFrames: readonly number[]; periodBeats: number; periodFrames?: never}
);

export const positiveModulo = (value: number, period: number): number => {
  if (!Number.isFinite(value) || !Number.isFinite(period) || period <= 0) {
    throw new Error('loop frame must be finite and period must be positive');
  }
  const result = ((value % period) + period) % period;
  return result === 0 ? 0 : result;
};

export const loopPhase = (globalFrame: number, loop: ResolvedLoop): {frame: number; periodFrames: number} => {
  if (!Number.isFinite(globalFrame) || !Number.isFinite(loop.phaseFrame)) {
    throw new Error('loop global frame and phase must be finite');
  }
  if (loop.periodFrames !== undefined) {
    if (!Number.isInteger(loop.periodFrames) || loop.periodFrames <= 0
        || loop.beatFrames !== undefined || loop.periodBeats !== undefined) {
      throw new Error('loop requires exactly one positive integer period');
    }
    return {frame: positiveModulo(globalFrame - loop.phaseFrame, loop.periodFrames), periodFrames: loop.periodFrames};
  }
  const beats = loop.beatFrames;
  const n = loop.periodBeats;
  if (!Array.isArray(beats) || !Number.isInteger(n) || n <= 0
      || beats.some((b, i) => !Number.isFinite(b) || (i > 0 && b <= beats[i - 1]))) {
    throw new Error('loop requires ordered placed beat frames and positive integer beats');
  }
  const anchor = beats.indexOf(loop.phaseFrame);
  if (anchor < 0) throw new Error('loop phase must be a placed beat');
  // Find the actual group surrounding F. There is no extrapolated tempo before
  // or after the producer's placed grid, including edited or variable beds.
  let index = -1;
  for (let i = 0; i < beats.length; i += 1) {
    if (beats[i] > globalFrame) break;
    index = i;
  }
  const startIndex = anchor + Math.floor((index - anchor) / n) * n;
  if (index < 0 || startIndex < 0 || startIndex + n >= beats.length) {
    throw new Error('loop bed lacks adjacent placed beats at this global frame');
  }
  const start = beats[startIndex];
  const periodFrames = beats[startIndex + n] - start;
  return {frame: positiveModulo(globalFrame - start, periodFrames), periodFrames};
};

/** Validate the actual visible draw window, including transition handles. Only
 * actual adjacent placed beats can cover it; the exclusive end is not sampled. */
export const assertLoopCoverage = (loop: ResolvedLoop, startFrame: number, durationFrames: number): void => {
  if (!Number.isFinite(startFrame) || !Number.isInteger(durationFrames) || durationFrames < 1) {
    throw new Error('loop draw window must contain at least one frame');
  }
  loopPhase(startFrame, loop);
  loopPhase(startFrame + durationFrames - 1, loop);
};

/** Sheet-frame selection shared by the actual sprite consumer and math tests.
 * Without an authored loop this is exactly its existing local-fps calculation. */
export const spriteSheetIndex = (
  localFrame: number,
  fps: number,
  frames: {count: number; fps: number; start?: number; sequence?: number[]},
  loop?: ResolvedLoop,
  globalFrame?: number,
): number => {
  if (!(frames.count >= 1 && frames.fps > 0)) return 0;
  const sequence = Array.isArray(frames.sequence) ? frames.sequence.filter(Number.isFinite) : [];
  const count = Math.max(1, Math.round(frames.count));
  let tick = Math.floor((localFrame * frames.fps) / fps);
  if (loop !== undefined) {
    if (globalFrame === undefined) throw new Error('authored loop requires the host globalFrame');
    const phase = loopPhase(globalFrame, loop);
    tick = Math.floor((phase.frame / phase.periodFrames) * (sequence.length || count));
  }
  return sequence.length > 0
    ? Math.round(sequence[tick % sequence.length])
    : Math.round(Number.isFinite(frames.start) ? frames.start! : 0) + (tick % count);
};
