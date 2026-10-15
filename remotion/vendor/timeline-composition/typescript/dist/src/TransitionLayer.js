import { jsx as _jsx, Fragment as _Fragment } from "react/jsx-runtime";
import { AbsoluteFill, Freeze, useCurrentFrame } from 'remotion';
import { queryDrawFrame } from './evaluation';
// Wraps one clip's content inside its draw Sequence. Frames here are draw-local.
// Extension frames (added by a transition) show the clip frozen at its boundary
// frame; opacity applies the transition ramp.
export const composeTransitionLayer = ({ plan, children, }, frame) => {
    const evaluated = queryDrawFrame(plan, frame);
    const extended = plan.drawDuration > plan.ownDuration;
    const body = extended
        ? _jsx(Freeze, { frame: evaluated.ownFrame, children: children })
        : children;
    // Decided per plan, not per frame, so the wrapper does not mount and unmount
    // (and remount media) when a ramp reaches 1.
    if (!plan.fadeIn && !plan.fadeOut) {
        return _jsx(_Fragment, { children: body });
    }
    return (_jsx(AbsoluteFill, { style: { opacity: evaluated.opacity }, children: body }));
};
export const TransitionLayer = (props) => composeTransitionLayer(props, useCurrentFrame());
