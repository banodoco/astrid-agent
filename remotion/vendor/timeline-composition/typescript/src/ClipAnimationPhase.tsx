// One phase only: no clip opacity, crop, modifiers, speed remap, or new clock.
import type {ReactNode} from 'react';
import {useCurrentFrame} from 'remotion';
import {useTheme} from './ThemeContext';
import {composeAnimations, resolveAnimationReferences, type ComposeAnimationsInput} from './lib/animations';
import {animationElapsed, normalizeAnimationRefs} from './lib/clip-look';

export type ClipAnimationPhaseProps = Pick<ComposeAnimationsInput, 'clip' | 'fps' | 'theme'> & {
  phase: 'entrance' | 'exit';
  reference: unknown;
  clipFrames: number;
  children: ReactNode;
};

export const composeClipAnimationPhase = ({clip, fps, theme, phase, reference, clipFrames,
  children}: ClipAnimationPhaseProps, frame: number): ReactNode => {
  const refs = normalizeAnimationRefs(reference, fps);
  if (refs === undefined) {
    return children;
  }
  const resolved = resolveAnimationReferences(refs, phase);
  if (resolved.length === 0) {
    return children;
  }
  const durationFrames = Math.max(...resolved.map((animation) => animation.durationFrames));
  const explicitZeroBoundary = durationFrames === 0 && resolved.some((animation) => animation.easing !== undefined)
    && frame === (phase === 'entrance' ? 0 : clipFrames);
  const elapsedFrames = explicitZeroBoundary ? 0 : animationElapsed(phase, frame, clipFrames, durationFrames);
  if (elapsedFrames === null) {
    return children;
  }
  return composeAnimations({
    clip,
    refs,
    phase,
    content: children,
    theme,
    fps,
    elapsedFrames,
  });
};

// Host adapter reads only the existing clip-local clock.
export const ClipAnimationPhase = (props: Omit<ClipAnimationPhaseProps, 'theme'>) =>
  composeClipAnimationPhase({...props, theme: useTheme()}, useCurrentFrame());
