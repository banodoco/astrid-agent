import { jsx as _jsx } from "react/jsx-runtime";
import { ANIMATION_DEFAULTS, ANIMATION_META, ANIMATION_REGISTRY, } from '../animations.generated';
import { animationEasingParams, animationProgress, normalizeAnimationEasing } from './animation-easing';
import { progressTypeOn, progressWrapper, supportsAnimationProgress } from './animation-progress-adapters';
const isObjectReference = (value) => {
    return typeof value === 'object' && value !== null && !Array.isArray(value);
};
const animationRegistry = ANIMATION_REGISTRY;
const animationDefaults = ANIMATION_DEFAULTS;
const animationMeta = ANIMATION_META;
const hasAnimationId = (id) => Object.prototype.hasOwnProperty.call(animationRegistry, id);
export const normalizeAnimationReferences = (refs) => {
    if (refs === undefined) {
        return [];
    }
    const refList = Array.isArray(refs) ? refs : [refs];
    return refList.map((ref) => {
        if (typeof ref === 'string') {
            if (!hasAnimationId(ref)) {
                throw new Error(`Unknown animation id '${ref}'`);
            }
            return { id: ref, params: {} };
        }
        if (!isObjectReference(ref) || typeof ref.id !== 'string' || ref.id.length === 0) {
            throw new Error('Animation reference objects must include a non-empty string id');
        }
        if (!hasAnimationId(ref.id)) {
            throw new Error(`Unknown animation id '${ref.id}'`);
        }
        if (ref.easing !== undefined && typeof ref.easing !== 'string')
            throw new Error('Animation easing must be a declared curve name');
        const params = animationEasingParams({}, ref.params && typeof ref.params === 'object' ? { ...ref.params } : {}, ref.easing);
        return {
            id: ref.id,
            durationFrames: ref.durationFrames,
            easing: typeof ref.easing === 'string' ? normalizeAnimationEasing(ref.easing) : undefined,
            params,
        };
    });
};
export const resolveAnimationReferences = (refs, phase) => {
    return normalizeAnimationReferences(refs).map((ref) => {
        const meta = animationMeta[ref.id];
        if (!meta) {
            throw new Error(`Unknown animation id '${ref.id}'`);
        }
        const metaPhase = meta.phase ?? 'any';
        const phaseMatches = metaPhase === 'any'
            || metaPhase === phase
            || (Array.isArray(metaPhase) && metaPhase.includes(phase));
        if (!phaseMatches) {
            throw new Error(`Animation '${ref.id}' is phase '${meta.phase}', not '${phase}'`);
        }
        const defaults = animationDefaults[ref.id] ?? {};
        const defaultDuration = typeof defaults.durationFrames === 'number'
            ? defaults.durationFrames
            : meta.defaultDurationFrames;
        const durationFrames = ref.durationFrames ?? defaultDuration;
        if (typeof durationFrames !== 'number' || !Number.isFinite(durationFrames) || durationFrames < 0) {
            throw new Error(`Animation '${ref.id}' must resolve to a non-negative durationFrames value`);
        }
        const params = animationEasingParams(defaults, ref.params, ref.easing);
        const easing = ref.easing ?? (typeof ref.params.easing === 'string' ? normalizeAnimationEasing(ref.params.easing) : undefined);
        if (easing !== undefined && !supportsAnimationProgress(ref.id)) {
            throw new Error(`Animation '${ref.id}' cannot consume explicit easing progress`);
        }
        return {
            ...ref,
            easing,
            params,
            component: animationRegistry[ref.id],
            kind: meta.kind,
            phase,
            durationFrames,
        };
    });
};
const resolvedProgress = (animation, elapsedFrames) => {
    if (animation.id === 'type-on' && animation.easing !== undefined && animation.durationFrames > 0) {
        const start = animation.params.startFrame ?? 18;
        const fraction = animation.params.durationFraction ?? 0.55;
        if (typeof start !== 'number' || !Number.isFinite(start) || typeof fraction !== 'number' || !Number.isFinite(fraction)) {
            throw new Error('Type-on window must use finite startFrame and durationFraction');
        }
        const end = Math.max(start + 1, Math.floor(animation.durationFrames * fraction));
        return animationProgress(elapsedFrames - start, end - start, animation.easing);
    }
    return animationProgress(elapsedFrames, animation.durationFrames, animation.easing);
};
const isHookResultObject = (value) => {
    return typeof value === 'object' && value !== null && !Array.isArray(value);
};
export const composeAnimations = ({ clip, refs, phase, content, text, theme, fps, elapsedFrames, }) => {
    const resolved = resolveAnimationReferences(refs, phase);
    const hookAnimations = resolved.filter((animation) => animation.kind === 'hook');
    const wrapperAnimations = resolved.filter((animation) => animation.kind === 'wrapper');
    let nextContent = content;
    let nextText = text;
    let nextParams = {};
    for (const animation of hookAnimations) {
        const progress = resolvedProgress(animation, elapsedFrames);
        const HookComponent = progress !== undefined && animation.id === 'type-on'
            ? progressTypeOn : animation.component;
        const result = HookComponent({
            clip,
            params: animation.params,
            theme,
            fps,
            phase,
            durationFrames: animation.durationFrames,
            elapsedFrames,
            progress,
            animationId: animation.id,
            content: nextContent,
            text: nextText,
        });
        if (isHookResultObject(result)) {
            nextContent = result.content ?? nextContent;
            nextText = result.text ?? nextText;
            nextParams = { ...nextParams, ...(result.params ?? {}) };
        }
        else {
            nextContent = result;
        }
    }
    return wrapperAnimations.reduceRight((children, animation) => {
        const progress = resolvedProgress(animation, elapsedFrames);
        const WrapperComponent = progress === undefined
            ? animation.component
            : progressWrapper(animation.id, animation.component);
        return (_jsx(WrapperComponent, { clip: clip, params: { ...animation.params, ...nextParams }, theme: theme, fps: fps, phase: phase, durationFrames: animation.durationFrames, elapsedFrames: elapsedFrames, progress: progress, animationId: animation.id, children: children }));
    }, nextContent);
};
