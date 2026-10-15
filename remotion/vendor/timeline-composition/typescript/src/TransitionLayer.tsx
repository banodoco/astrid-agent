import type {ReactElement, ReactNode} from 'react';
import {AbsoluteFill, Freeze, useCurrentFrame} from 'remotion';
import {queryDrawFrame, type DrawPlan} from './evaluation';

// Wraps one clip's content inside its draw Sequence. Frames here are draw-local.
// Extension frames (added by a transition) show the clip frozen at its boundary
// frame; opacity applies the transition ramp.
export const TransitionLayer = ({
  plan,
  children,
}: {
  plan: DrawPlan;
  children: ReactNode;
}): ReactElement => {
  const frame = useCurrentFrame();
  const evaluated = queryDrawFrame(plan, frame);
  const extended = plan.drawDuration > plan.ownDuration;
  const body = extended
    ? <Freeze frame={evaluated.ownFrame}>{children}</Freeze>
    : children;

  // Decided per plan, not per frame, so the wrapper does not mount and unmount
  // (and remount media) when a ramp reaches 1.
  if (!plan.fadeIn && !plan.fadeOut) {
    return <>{body}</>;
  }
  return (
    <AbsoluteFill style={{opacity: evaluated.opacity}}>
      {body}
    </AbsoluteFill>
  );
};
