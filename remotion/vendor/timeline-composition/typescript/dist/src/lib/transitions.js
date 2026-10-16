import { TRANSITION_DEFAULTS, TRANSITION_REGISTRY, } from '../transitions.generated';
const transitionRegistry = TRANSITION_REGISTRY;
const transitionDefaults = TRANSITION_DEFAULTS;
const isObjectReference = (value) => {
    return typeof value === 'object' && value !== null && !Array.isArray(value);
};
const readAlign = (ref) => {
    const align = isObjectReference(ref) ? ref.align : undefined;
    return align === 'start' || align === 'center' || align === 'end' ? align : 'end';
};
export const resolveTransitionReference = (ref, theme, fps) => {
    if (ref === undefined) {
        return null;
    }
    const id = typeof ref === 'string' ? ref : isObjectReference(ref) ? (ref.id ?? ref.type ?? '') : '';
    if (!id || !transitionRegistry[id]) {
        throw new Error(`Unknown transition id '${id}'`);
    }
    const defaults = transitionDefaults[id] ?? {};
    const params = typeof ref === 'string'
        ? { ...defaults }
        : { ...defaults, ...(ref.params ?? {}) };
    const rawDurationFrames = typeof ref === 'string' ? undefined : ref.durationFrames;
    const durationSeconds = typeof ref === 'string' ? undefined : ref.duration;
    const durationFrames = rawDurationFrames
        ?? (typeof durationSeconds === 'number' ? Math.round(durationSeconds * fps) : undefined)
        ?? (typeof defaults.durationFrames === 'number' ? defaults.durationFrames : 12);
    const component = transitionRegistry[id];
    // The registry component is called to validate the reference; the visual
    // result is produced by transition-spans (dissolve or dip), not by this call.
    component({ transitionId: id, params, theme, fps, durationFrames });
    return { id, durationFrames, align: readAlign(ref), params };
};
