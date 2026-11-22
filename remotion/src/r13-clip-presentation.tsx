import { isValidElement, type CSSProperties, type ReactElement, type ReactNode } from 'react';
import { AbsoluteFill, interpolate, spring } from 'remotion';
import legacyElements from '../../astrid/core/timeline/schema/r13-legacy-elements.json';

type LegacyEffect = {
  type?: unknown;
  id?: unknown;
  duration?: unknown;
  durationFrames?: unknown;
  intensity?: unknown;
  params?: unknown;
  align?: unknown;
  incomingPolicy?: unknown;
};

type LegacyClip = {
  transition?: unknown;
  entrance?: unknown;
  exit?: unknown;
  continuous?: unknown;
  [key: string]: unknown;
};

const transitionIds = new Set(legacyElements.transitionIds);
const phaseIds = {
  entrance: new Set(legacyElements.phaseEffectIds.entrance),
  exit: new Set(legacyElements.phaseEffectIds.exit),
  continuous: new Set(legacyElements.phaseEffectIds.continuous),
} as const;

const clampedProgress = (value: number, input: number): number => interpolate(
  value, [0, Math.max(1, input)], [0, 1], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' },
);

const durationFrames = (value: LegacyEffect, fps: number): number => {
  if (typeof value.durationFrames === 'number' && Number.isFinite(value.durationFrames)) return Math.max(0, value.durationFrames);
  if (typeof value.duration === 'number' && Number.isFinite(value.duration)) return Math.max(0, Math.round(value.duration * fps));
  return Math.max(0, Math.round(0.4 * fps));
};

const legacyRecord = (value: unknown): LegacyEffect | undefined => {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return undefined;
  const record = value as LegacyEffect;
  // `type` is the saved R13 shape. Canonical references use `id` (or a string).
  if ('id' in record || typeof record.type !== 'string') return undefined;
  return record;
};

const transitionReference = (raw: unknown): LegacyEffect | undefined =>
  typeof raw === 'string' ? {id: raw}
    : raw && typeof raw === 'object' && !Array.isArray(raw) ? raw as LegacyEffect : undefined;

export function isLegacyIncomingTransition(raw: unknown): boolean {
  const reference = transitionReference(raw);
  const id = reference?.id ?? reference?.type;
  return typeof id === 'string' && (id === 'crossfade' || isLegacyTransitionId(id));
}

export function isLegacyTransitionId(id: unknown): id is string {
  return typeof id === 'string' && transitionIds.has(id);
}

export function legacyTransitionFrames(raw: unknown, fps: number): number | undefined {
  const reference = transitionReference(raw);
  const id = reference?.id ?? reference?.type;
  if (!reference || !isLegacyTransitionId(id)) return undefined;
  if ((reference.id !== undefined && reference.type !== undefined && reference.id !== reference.type)
      || (reference.align !== undefined && reference.align !== 'end')
      || (reference.incomingPolicy !== undefined && reference.incomingPolicy !== 'preroll-crossfade')) {
    throw new Error('transition/conflicting-policy: Legacy incoming reference conflicts with its policy');
  }
  const {duration, durationFrames: authoredFrames} = reference;
  const frames = authoredFrames !== undefined ? authoredFrames
    : duration === undefined ? 8 : typeof duration === 'number' ? duration * fps : NaN;
  if (typeof frames !== 'number' || !Number.isSafeInteger(Math.round(frames)) || Math.round(frames) <= 0) {
    throw new Error('transition/invalid-duration: Expected positive safe transition frames');
  }
  return Math.round(frames);
}

export function legacyTransitionStyle(clip: LegacyClip, frame: number, fps: number): CSSProperties | undefined {
  const frames = legacyTransitionFrames(clip.transition, fps);
  if (frames === undefined) return undefined;
  const reference = transitionReference(clip.transition);
  const id = reference?.id ?? reference?.type;
  const progress = clampedProgress(frame, frames);
  switch (id) {
    case 'wipe':
      return { clipPath: `inset(0 ${Math.max(0, (1 - progress) * 100)}% 0 0)` };
    case 'slide-push':
      return { transform: `translateX(${interpolate(progress, [0, 1], [100, 0])}%)` };
    case 'zoom-through':
      return { opacity: interpolate(progress, [0, 1], [0, 1]), transform: `scale(${interpolate(progress, [0, 1], [1.12, 1])})` };
    default:
      return undefined;
  }
}

const phaseStyle = (phase: keyof typeof phaseIds, type: string, frame: number, fps: number,
  clipFrames: number, effectFrames: number, intensity: number, children: ReactNode): CSSProperties | ReactElement | undefined => {
  const p = phase === 'exit'
    ? interpolate(frame, [Math.max(0, clipFrames - effectFrames), clipFrames], [0, 1], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' })
    : clampedProgress(frame, effectFrames);
  const full = { width: '100%', height: '100%' } as const;
  const entrance = (transform: string, opacity = p): CSSProperties => ({ ...full, transform, opacity });
  if (phase === 'entrance') {
    switch (type) {
      case 'slide-up': {
        const v = spring({ frame, fps, durationInFrames: effectFrames, config: { damping: 14, stiffness: 120 } });
        return entrance(`translateY(${interpolate(v, [0, 1], [80, 0])}px)`, v);
      }
      case 'slide-down': return entrance(`translateY(${interpolate(p, [0, 1], [-80, 0])}px)`);
      case 'slide-left': return entrance(`translateX(${interpolate(p, [0, 1], [180, 0])}px)`);
      case 'slide-right': return entrance(`translateX(${interpolate(p, [0, 1], [-180, 0])}px)`);
      case 'zoom-in': return entrance(`scale(${interpolate(p, [0, 1], [0.75, 1])})`);
      case 'zoom-spin': return entrance(`scale(${interpolate(p, [0, 1], [0.35, 1])}) rotate(${interpolate(p, [0, 1], [-10, 0])}deg)`);
      case 'pulse': return entrance(`scale(${interpolate(p, [0, 0.6, 1], [1.12, 0.95, 1])})`);
      case 'fade': return entrance('none');
      case 'flip': return <AbsoluteFill style={{ perspective: 1000 }}><AbsoluteFill style={entrance(`rotateY(${interpolate(p, [0, 1], [-90, 0])}deg)`)}>{children}</AbsoluteFill></AbsoluteFill>;
      case 'bounce': return entrance(`translateY(${interpolate(p, [0, 0.6, 0.8, 1], [120, -20, 10, 0])}px)`);
      case 'meteorite': {
        const dur = effectFrames;
        const approach = interpolate(frame, [0, dur * 0.35], [0, 1], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
        const impact = spring({ frame: Math.max(0, frame - dur * 0.35), fps, durationInFrames: dur * 0.65, config: { damping: 8, stiffness: 250, mass: 0.6 } });
        const settled = approach >= 1 ? impact : 0;
        const x = interpolate(approach, [0, 1], [600, 0]);
        const y = interpolate(approach, [0, 1], [-500, 0]);
        const rotate = interpolate(approach, [0, 1], [-35, 0]);
        const preScale = interpolate(approach, [0, 1], [0.15, 1]);
        const bounceScale = settled > 0 ? interpolate(settled, [0, 1], [1.2, 1]) : 1;
        const shake = interpolate(settled, [0, 0.15, 0.5, 1], [0, 12, 4, 0], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
        const flash = interpolate(settled, [0, 0.08, 0.3], [0, 0.9, 0], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
        const glow = interpolate(settled, [0, 0.1, 0.6], [0, 0.7, 0], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
        const glowSize = interpolate(settled, [0, 0.5], [20, 80], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
        const brightness = interpolate(settled, [0, 0.1, 0.4], [1, 1.8, 1], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
        const opacity = interpolate(approach, [0, 0.15], [0, 1], { extrapolateLeft: 'clamp', extrapolateRight: 'clamp' });
        return <AbsoluteFill style={{ transform: `translate(${shake * Math.sin(frame * 47)}px, ${shake * Math.cos(frame * 31)}px)` }}>
          <AbsoluteFill style={{ background: `rgba(255, 240, 200, ${flash})`, pointerEvents: 'none', zIndex: 10 }} />
          <AbsoluteFill style={{ background: `radial-gradient(circle at 50% 50%, rgba(255, 160, 40, ${glow}) 0%, rgba(255, 100, 20, ${glow * 0.5}) ${glowSize * 0.5}%, transparent ${glowSize}%)`, pointerEvents: 'none', zIndex: 5 }} />
          <AbsoluteFill style={{ transform: `translate(${x}px, ${y}px) rotate(${rotate}deg) scale(${preScale * bounceScale})`, opacity, filter: `brightness(${brightness})` }}>{children}</AbsoluteFill>
        </AbsoluteFill>;
      }
    }
  }
  if (phase === 'exit') {
    const fade = interpolate(p, [0, 1], [1, 0]);
    switch (type) {
      case 'slide-down': return { ...full, transform: `translateY(${interpolate(p, [0, 1], [0, 120])}px)`, opacity: fade };
      case 'zoom-out': return { ...full, transform: `scale(${interpolate(p, [0, 1], [1, 0.6])})`, opacity: fade };
      case 'flip': return <AbsoluteFill style={{ perspective: 1000 }}><AbsoluteFill style={{ ...full, transform: `rotateX(${interpolate(p, [0, 1], [0, 90])}deg)`, opacity: interpolate(p, [0, 0.85, 1], [1, 1, 0]) }}>{children}</AbsoluteFill></AbsoluteFill>;
      case 'fade-out': return { ...full, opacity: fade };
      case 'shrink': return { ...full, transform: `scale(${interpolate(p, [0, 1], [1, 0.2])})`, opacity: fade };
      case 'dissolve': return { ...full, filter: `blur(${interpolate(p, [0, 1], [0, 16])}px)`, opacity: fade };
    }
  }
  if (phase === 'continuous') {
    const intensityValue = intensity;
    switch (type) {
      case 'ken-burns': {
        const progress = clipFrames <= 1 ? 1 : frame / clipFrames;
        return { ...full, transform: `scale(${interpolate(progress, [0, 1], [1, 1 + intensityValue * 0.18])}) translate(${interpolate(progress, [0, 1], [0, -20 * intensityValue])}px, ${interpolate(progress, [0, 1], [0, -10 * intensityValue])}px)` };
      }
      case 'float': return { ...full, transform: `translateY(${Math.sin(frame * 0.06) * 8 * intensityValue}px) scale(${1 + Math.sin(frame * 0.04) * 0.02 * intensityValue})` };
      case 'glitch': {
        const active = frame % 36 < 3;
        return { ...full, transform: `translate(${active ? Math.sin(frame * 17) * 10 * intensityValue : 0}px, ${active ? Math.cos(frame * 11) * 4 * intensityValue : 0}px) skewX(${active ? Math.sin(frame * 19) * 4 * intensityValue : 0}deg)` };
      }
      case 'slow-zoom': {
        const progress = clipFrames <= 1 ? 1 : frame / clipFrames;
        return { ...full, transform: `scale(${interpolate(progress, [0, 1], [1, 1 + intensityValue * 0.1])})` };
      }
      case 'drift': {
        const progress = clipFrames <= 1 ? 1 : frame / clipFrames;
        return { ...full, transform: `translate(${interpolate(progress, [0, 1], [0, 40 * intensityValue])}px, ${Math.sin(frame * 0.025) * 10 * intensityValue}px)` };
      }
    }
  }
  return undefined;
};

export function wrapLegacyClipPhase(phase: 'entrance' | 'exit' | 'continuous', clip: LegacyClip,
  frame: number, fps: number, clipFrames: number, children: ReactNode): ReactNode | undefined {
  const effect = legacyRecord(clip[phase]);
  if (!effect || typeof effect.type !== 'string' || !phaseIds[phase].has(effect.type)) return undefined;
  const frames = durationFrames(effect, fps);
  const defaultFrames = phase === 'entrance'
    ? ({ 'slide-up': 18, 'slide-down': 18, 'slide-left': 18, 'slide-right': 18, 'zoom-in': 16, 'zoom-spin': 20, pulse: 14, fade: 12, flip: 18, bounce: 20, meteorite: 24 } as Record<string, number>)[effect.type]
    : phase === 'exit'
      ? ({ 'slide-down': 18, 'zoom-out': 14, flip: 18, 'fade-out': 12, shrink: 14, dissolve: Math.max(6, Math.round(fps * 0.3)) } as Record<string, number>)[effect.type]
      : clipFrames;
  // R13's effect wrapper always supplies duration (default .4s); preserve a
  // component's own fallback only for malformed zero-duration references.
  const effectFrames = frames || defaultFrames;
  const style = phaseStyle(phase, effect.type, frame, fps, clipFrames, effectFrames, typeof effect.intensity === 'number' ? effect.intensity : 0.5, children);
  if (!style) return undefined;
  return isValidElement(style) ? style : <AbsoluteFill style={style as CSSProperties}>{children}</AbsoluteFill>;
}
