import { jsx as _jsx, jsxs as _jsxs } from "react/jsx-runtime";
const wrapperIds = new Set(['fade', 'fade-up', 'scale-in', 'slide-left', 'slide-up']);
export const supportsAnimationProgress = (id) => wrapperIds.has(id) || id === 'type-on';
// Explicit progress bypasses the legacy component's spring/interpolation. The
// absent path remains the exact registered component supplied by the caller.
export const progressWrapper = (id, legacy) => {
    const Legacy = legacy;
    return function ProgressWrapper(props) {
        if (props.progress === undefined)
            return _jsx(Legacy, { ...props });
        const progress = props.progress;
        const style = {
            width: '100%', height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center',
        };
        if (id === 'fade')
            style.opacity = props.phase === 'exit' ? 1 - progress : progress;
        else if (id === 'slide-up') {
            style.opacity = 1 - progress;
            style.transform = `translateY(${-30 * progress}px)`;
        }
        else {
            style.opacity = progress;
            if (id === 'fade-up')
                style.transform = `translateY(${40 * (1 - progress)}px)`;
            if (id === 'slide-left')
                style.transform = `translateX(${80 * (1 - progress)}px)`;
            if (id === 'scale-in') {
                style.transform = `scale(${0.85 + 0.15 * progress})`;
                style.transformOrigin = 'center center';
            }
        }
        return _jsx("div", { style: style, children: props.children });
    };
};
export const progressTypeOn = ({ text, elapsedFrames, fps, params, progress }) => {
    if (!text)
        return {};
    if (progress === undefined)
        throw new Error('Type-on progress adapter requires explicit progress');
    const revealCount = Math.max(0, Math.min(text.length, Math.ceil(text.length * progress)));
    const shouldShowCaret = (params.showCaret ?? true) && progress < 1;
    const caretOpacity = shouldShowCaret
        ? (Math.floor(elapsedFrames / Math.max(1, Math.round(fps / 3))) % 2 === 0 ? 1 : 0)
        : 0;
    return { content: (_jsxs("span", { children: [_jsx("span", { children: text.slice(0, revealCount) }), _jsx("span", { "aria-hidden": true, style: { display: 'inline-block', width: '0.05em', marginLeft: shouldShowCaret ? '0.05em' : 0,
                        opacity: caretOpacity, backgroundColor: 'currentColor', height: '1em', verticalAlign: '-0.15em' } }), _jsx("span", { style: { opacity: 0 }, children: text.slice(revealCount) })] })) };
};
