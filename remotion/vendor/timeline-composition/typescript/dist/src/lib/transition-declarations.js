export class TransitionAdmissionError extends Error {
    code;
    nextStep;
    constructor(code, message, nextStep) {
        super(`${message} Next step: ${nextStep}`);
        this.name = 'TransitionAdmissionError';
        this.code = code;
        this.nextStep = nextStep;
    }
}
const refuse = (code, message, nextStep) => {
    throw new TransitionAdmissionError(code, message, nextStep);
};
const requireHost = (host) => {
    if (host !== 'timeline-composition') {
        refuse('transition/unsupported-host', `Transition host '${host}' is unsupported.`, 'Use the timeline-composition host.');
    }
};
export const declareAppTransition = (trackId, clipId, transition, fps, host) => {
    requireHost(host);
    if (!trackId || !clipId) {
        refuse('transition/invalid-owner', 'A transition needs one track and outgoing clip owner.', 'Supply the owning track and outgoing clip IDs.');
    }
    // These are the two implementations present in the admitted composition.
    // The browser's crossfade spelling names the existing cross-fade component.
    const id = transition.type === 'crossfade' ? 'cross-fade' : transition.type;
    if (id !== 'cross-fade' && id !== 'fade') {
        refuse('transition/unsupported-implementation', `Transition '${transition.type}' is unsupported by '${host}'.`, 'Choose cross-fade or fade for this host.');
    }
    const T = Math.round(transition.duration * fps);
    if (!Number.isFinite(fps) || fps <= 0 || !Number.isFinite(transition.duration)
        || transition.duration <= 0 || !Number.isSafeInteger(T) || T <= 0) {
        refuse('transition/invalid-duration', 'Transition duration must resolve to positive whole frames.', 'Supply a positive duration in seconds and a positive finite frame rate.');
    }
    const params = transition.params ?? {};
    return {
        owner: { trackId, clipId },
        implementation: { host: 'timeline-composition', id: id },
        spec: { T, kind: id === 'fade' ? 'dip' : 'dissolve', align: 'end' },
        params: { ...params },
    };
};
// Validate resolved frame spans; no sorting, clamping, shifting or timing engine.
// Gaps and touching half-open intervals are valid. Draw-window extensions are
// intentionally excluded: the shared planner owns those transition overlaps.
export const admitDeclaredTransitions = (trackId, clips, declarations, host) => {
    requireHost(host);
    const indices = new Map();
    for (let index = 0; index < clips.length; index += 1) {
        const clip = clips[index];
        if (!clip.id || indices.has(clip.id) || !Number.isSafeInteger(clip.start)
            || !Number.isSafeInteger(clip.duration) || clip.duration <= 0
            || !Number.isSafeInteger(clip.start + clip.duration)) {
            refuse('transition/invalid-span', 'Track clips need unique IDs and finite whole-frame spans.', 'Supply unique clip IDs and resolved positive whole-frame durations.');
        }
        indices.set(clip.id, index);
        const previous = clips[index - 1];
        if (previous && clip.start < previous.start + previous.duration) {
            refuse('transition/same-track-overlap', `Clips '${previous.id}' and '${clip.id}' overlap or are out of order on track '${trackId}'.`, 'Move or trim the clips into non-overlapping track order, then retry; gaps are allowed.');
        }
    }
    const transitions = clips.map(() => null);
    for (const declaration of declarations) {
        requireHost(declaration.implementation.host);
        const index = indices.get(declaration.owner.clipId);
        if (declaration.owner.trackId !== trackId || index === undefined || index >= clips.length - 1) {
            return refuse('transition/invalid-owner', 'Transition owner must be an outgoing clip on this track.', 'Attach the transition to one clip with a successor on the same track.');
        }
        if (transitions[index]) {
            refuse('transition/duplicate-owner', `Clip '${declaration.owner.clipId}' has multiple transition declarations.`, 'Keep exactly one outgoing transition declaration on this clip.');
        }
        const { id } = declaration.implementation;
        const { T, kind, align } = declaration.spec;
        if ((id !== 'cross-fade' && id !== 'fade') || kind !== (id === 'fade' ? 'dip' : 'dissolve')
            || !['end', 'start', 'center'].includes(align)
            || !Number.isSafeInteger(T) || T <= 0) {
            refuse('transition/invalid-declaration', 'Transition declaration does not match its declared implementation.', 'Recreate the declaration from the App transition for this host.');
        }
        transitions[index] = { ...declaration.spec };
    }
    return transitions;
};
