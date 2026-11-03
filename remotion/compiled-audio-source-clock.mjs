import {
  computeMediaTrim, getSanitizedPlaybackRate,
} from '@banodoco/timeline-composition/evaluation';

// Input must be the ONE Agent compiler's output. Occurrence offsets and speed
// inheritance belong to that compiler; this adapter only reads its media clock.
export const compiledAudioSourceClock = (compiledClip, fps) => {
  if (!Number.isFinite(fps) || fps <= 0) throw new Error('resolved fps required');
  return {
    source_from: computeMediaTrim(compiledClip, fps).trimBefore / fps,
    playback_rate: getSanitizedPlaybackRate(compiledClip.speed),
  };
};
