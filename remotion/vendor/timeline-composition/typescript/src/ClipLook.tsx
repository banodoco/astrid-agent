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
import {useTheme, type RuntimeTheme} from './ThemeContext';
import type {TimelineClip} from './types';
import {MODIFIER_REGISTRY, composeModifiers} from './modifiers';
import type {ModifierBounds, ModifierFinding, ModifierRegistry} from './modifier-types';
import {spriteKeyframeFrame, timelineKeyframeTime} from './lib/keyframe-origin';

const IDENTITY_CROP_PATH = 'inset(0% 0% 0% 0%)';

export type ClipLookProps = {
  clip: TimelineClip;
  fps: number;
  kind: 'element' | 'media';
  children: ReactNode;
  modifierRegistry?: ModifierRegistry;
  modifierBounds?: ModifierBounds;
  globalFrame?: number;
  onModifierFinding?: (finding: ModifierFinding) => void;
};

export const composeClipLook = ({clip, fps, kind, children, modifierRegistry = MODIFIER_REGISTRY,
  modifierBounds = {status: 'unknown', reason: 'Target has no declared bounds evaluator'},
  globalFrame, onModifierFinding}: ClipLookProps, frame: number, theme: RuntimeTheme) => {
  const clipFrames = getClipDurationInFrames(clip, fps);
  const hasModifiers = (clip.modifiers?.length ?? 0) > 0;

  // Media clips already get opacity (with fade) and crop from VisualClip, so
  // they must not be styled here as well.
  const style: CSSProperties = {};
  // Element speed remaps time (see README "Speed"). Decided by speed, not by the
  // frame, so the tree shape stays the same for the whole clip.
  const remapElement = kind === 'element' && clipSpeed(clip.speed) !== 1;
  if (kind === 'element' || hasModifiers) {
    const opacity = clipOpacity(clip, frame, fps);
    if (opacity !== 1) {
      style.opacity = opacity;
    }
    const clipPath = kind === 'element' && !hasModifiers ? cropClipPath(clip) : undefined;
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
    const explicitZeroBoundary = durationFrames === 0 && resolved.some((animation) => animation.easing !== undefined)
      && frame === (phase === 'entrance' ? 0 : clipFrames);
    const elapsedFrames = explicitZeroBoundary ? 0 : animationElapsed(phase, frame, clipFrames, durationFrames);
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

  // Spatial crop is already inside VisualClip for media. Move element crop
  // inside the modifier only on the new path; legacy clip tree stays unchanged.
  const elementCrop = kind === 'element' && hasModifiers ? cropClipPath(clip) : undefined;
  const spatialContent = elementCrop && elementCrop !== IDENTITY_CROP_PATH
    ? <AbsoluteFill style={{clipPath: elementCrop}}>{children}</AbsoluteFill>
    : children;
  const targetFrame = remapElement ? remappedFrame(frame, clip.speed) : frame;
  const modified = hasModifiers ? composeModifiers({
    instances: clip.modifiers ?? [],
    registry: modifierRegistry,
    context: {clip, fps, frame: targetFrame, keyframeFrame: spriteKeyframeFrame(clip, targetFrame, fps),
      keyframeTime: timelineKeyframeTime(clip, clip.at + targetFrame / fps),
      globalFrame: globalFrame ?? Math.round(clip.at * fps) + frame, bounds: modifierBounds, theme},
    content: spatialContent,
    onFinding: onModifierFinding,
  }) : spatialContent;
  const body: ReactNode = remapElement
    ? <Freeze frame={targetFrame}>{modified}</Freeze>
    : modified;

  let content: ReactNode = wrapPhase('entrance', clip.entrance, body);
  content = wrapPhase('exit', clip.exit, content);

  return (
    <AbsoluteFill data-clip-id={clip.id} style={style}>
      {content}
    </AbsoluteFill>
  );
};

// A single hook adapter: no modifier component starts another clip clock.
export const ClipLook = (props: ClipLookProps) => composeClipLook(props, useCurrentFrame(), useTheme());
