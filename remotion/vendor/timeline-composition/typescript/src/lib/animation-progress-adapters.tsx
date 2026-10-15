import type {CSSProperties, ReactNode} from 'react';
import type {WrapperAnimationComponent, HookAnimationProps, HookAnimationResult, WrapperAnimationProps} from '../effects-types';

const wrapperIds = new Set(['fade', 'fade-up', 'scale-in', 'slide-left', 'slide-up']);
export const supportsAnimationProgress = (id: string): boolean => wrapperIds.has(id) || id === 'type-on';

// Explicit progress bypasses the legacy component's spring/interpolation. The
// absent path remains the exact registered component supplied by the caller.
export const progressWrapper = (id: string, legacy: WrapperAnimationComponent) => {
  const Legacy = legacy;
  return function ProgressWrapper(props: WrapperAnimationProps): ReactNode {
    if (props.progress === undefined) return <Legacy {...props} />;
    const progress = props.progress;
    const style: CSSProperties = {
      width: '100%', height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center',
    };
    if (id === 'fade') style.opacity = props.phase === 'exit' ? 1 - progress : progress;
    else if (id === 'slide-up') {
      style.opacity = 1 - progress;
      style.transform = `translateY(${-30 * progress}px)`;
    } else {
      style.opacity = progress;
      if (id === 'fade-up') style.transform = `translateY(${40 * (1 - progress)}px)`;
      if (id === 'slide-left') style.transform = `translateX(${80 * (1 - progress)}px)`;
      if (id === 'scale-in') {
        style.transform = `scale(${0.85 + 0.15 * progress})`;
        style.transformOrigin = 'center center';
      }
    }
    return <div style={style}>{props.children}</div>;
  };
};

export const progressTypeOn = ({text, elapsedFrames, fps, params, progress}: HookAnimationProps): HookAnimationResult => {
  if (!text) return {};
  if (progress === undefined) throw new Error('Type-on progress adapter requires explicit progress');
  const revealCount = Math.max(0, Math.min(text.length, Math.ceil(text.length * progress)));
  const shouldShowCaret = (params.showCaret ?? true) && progress < 1;
  const caretOpacity = shouldShowCaret
    ? (Math.floor(elapsedFrames / Math.max(1, Math.round(fps / 3))) % 2 === 0 ? 1 : 0)
    : 0;
  return {content: (
    <span>
      <span>{text.slice(0, revealCount)}</span>
      <span aria-hidden style={{display: 'inline-block', width: '0.05em', marginLeft: shouldShowCaret ? '0.05em' : 0,
        opacity: caretOpacity, backgroundColor: 'currentColor', height: '1em', verticalAlign: '-0.15em'}} />
      <span style={{opacity: 0}}>{text.slice(revealCount)}</span>
    </span>
  )};
};
