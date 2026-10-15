import { motionCurveName, motionCurveProgress } from './motion-curves.generated';
export const normalizeAnimationEasing = motionCurveName;
const optionalEasing = (value) => {
    if (value === undefined)
        return undefined;
    if (typeof value !== 'string')
        throw new Error('Animation easing must be a declared curve name');
    return motionCurveName(value);
};
// Defaults do not create an explicit author request. Two authored spellings
// must agree after normalization, rather than silently overriding one another.
export const animationEasingParams = (defaults, params, easing) => {
    const referenceName = optionalEasing(easing);
    const parameterName = optionalEasing(params.easing);
    if (referenceName !== undefined && parameterName !== undefined && referenceName !== parameterName) {
        throw new Error('Conflicting reference.easing and params.easing');
    }
    const merged = { ...defaults, ...params };
    const declared = referenceName ?? parameterName;
    if (declared !== undefined)
        merged.easing = declared;
    return merged;
};
// This returns progress, never an altered elapsed frame or clock. Undefined is
// deliberately distinct from explicit linear and selects a component's legacy path.
export const animationProgress = (elapsedFrames, durationFrames, easing) => {
    const name = optionalEasing(easing);
    if (name === undefined)
        return undefined;
    if (!Number.isFinite(elapsedFrames) || !Number.isFinite(durationFrames) || durationFrames < 0) {
        throw new Error('Animation elapsed and non-negative duration must be finite');
    }
    return motionCurveProgress(name, durationFrames === 0 ? (elapsedFrames < 0 ? 0 : 1) : elapsedFrames / durationFrames);
};
