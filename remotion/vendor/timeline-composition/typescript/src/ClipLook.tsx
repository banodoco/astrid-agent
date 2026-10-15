// Clip-level look for a single timeline clip: opacity / crop for element clips,
// and entrance / exit animations for every visual clip. Rendered inside the
// clip's Sequence, so useCurrentFrame() is clip-local.

import type {CSSProperties, ReactNode} from 'react';
import {AbsoluteFill, Freeze, useCurrentFrame} from 'remotion';
import {composeAnimations, resolveAnimationReferences} from './lib/animations';
import {
  animationElapsed,
  clipOpacity,
  clipSpeed,
  cropClipPath,
  normalizeAnimationRefs,
  remappedFrame,
} from './lib/clip-look';
import {getClipDurationInFrames} from './lib/duration';
import {useTheme} from './ThemeContext';
import type {TimelineClip} from './types';

const IDENTITY_CROP_PATH = 'inset(0% 0% 0% 0%)';

type ClipLookProps = {
  clip: TimelineClip;
  fps: number;
  kind: 'element' | 'media';
  children: ReactNode;
};

export const ClipLook = ({clip, fps, kind, children}: ClipLookProps) => {
  const frame = useCurrentFrame();
  const theme = useTheme();
  const clipFrames = getClipDurationInFrames(clip, fps);

  // Media clips already get opacity (with fade) and crop from VisualClip, so
  // they must not be styled here as well.
  const style: CSSProperties = {};
  // Element speed remaps time (see README "Speed"). Decided by speed, not by the
  // frame, so the tree shape stays the same for the whole clip.
  const remapElement = kind === 'element' && clipSpeed(clip.speed) !== 1;
  if (kind === 'element') {
    const opacity = clipOpacity(clip, frame, fps);
    if (opacity !== 1) {
      style.opacity = opacity;
    }
    const clipPath = cropClipPath(clip);
    if (clipPath !== undefined && clipPath !== IDENTITY_CROP_PATH) {
      style.clipPath = clipPath;
    }
  }

  const wrapPhase = (phase: 'entrance' | 'exit', refsSource: unknown, content: ReactNode): ReactNode => {
    const refs = normalizeAnimationRefs(refsSource, fps);
    if (refs === undefined) {
      return content;
    }
    const resolved = resolveAnimationReferences(refs, phase);
    if (resolved.length === 0) {
      return content;
    }
    const durationFrames = Math.max(...resolved.map((animation) => animation.durationFrames));
    const elapsedFrames = animationElapsed(phase, frame, clipFrames, durationFrames);
    if (elapsedFrames === null) {
      return content;
    }
    return composeAnimations({
      clip,
      refs,
      phase,
      content,
      theme,
      fps,
      elapsedFrames,
    });
  };

  const body: ReactNode = remapElement
    ? <Freeze frame={remappedFrame(frame, clip.speed)}>{children}</Freeze>
    : children;

  let content: ReactNode = wrapPhase('entrance', clip.entrance, body);
  content = wrapPhase('exit', clip.exit, content);

  return (
    <AbsoluteFill data-clip-id={clip.id} style={style}>
      {content}
    </AbsoluteFill>
  );
};
