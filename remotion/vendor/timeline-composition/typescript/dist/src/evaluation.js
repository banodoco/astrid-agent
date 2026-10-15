// Pure public planner API. Keep this module independent of the React/Remotion
// entry point and generated component registries.
import { layerOpacity, ownFrame, planTrack, } from './lib/transition-spans';
import { admitDeclaredTransitions } from './lib/transition-declarations';
export { declareAppTransition, TransitionAdmissionError } from './lib/transition-declarations';
// Spans and resolved transitions must already be in track order and in frames.
// Plan once per input revision, then query only the frames the caller needs.
export const prepareTrack = (spans, transitions) => ({ plans: planTrack([...spans], [...transitions]) });
// Strict authoring admission. Legacy prepareTrack inputs retain their behavior.
export const prepareDeclaredTrack = (trackId, clips, declarations, host) => prepareTrack(clips, admitDeclaredTransitions(trackId, clips, declarations, host));
// The renderer calls this inside a clip's draw Sequence. Mounting is owned by
// that Sequence; this query intentionally does not filter by the draw interval.
export const queryDrawFrame = (plan, drawLocalFrame) => ({
    drawLocalFrame,
    ownFrame: ownFrame(plan, drawLocalFrame),
    opacity: layerOpacity(plan, drawLocalFrame),
});
// Track-absolute frame, half-open draw intervals, in input/painter order.
// Include mounted zero-opacity layers: mounting does not establish visibility.
export const queryTrackFrame = (track, frame) => ({
    frame,
    layers: track.plans.flatMap((plan, clipIndex) => {
        if (frame < plan.drawStart || frame >= plan.drawStart + plan.drawDuration) {
            return [];
        }
        return [{ clipIndex, ...queryDrawFrame(plan, frame - plan.drawStart) }];
    }),
});
export { keyframeOrigin, timelineKeyframeTime, spriteKeyframeFrame, declareKeyframeOrigin } from './lib/keyframe-origin';
export { loopPhase, positiveModulo, spriteSheetIndex } from './lib/beat-loop';
export { mediaModifierBounds } from './lib/modifier-bounds';
export { compileAudioEnvelope, validateAudioEnvelope, audioEnvelopeAt, applyAudioEnvelope, audioEnvelopeWav, AudioEnvelopeError } from './lib/audio-envelope';
export { prepareScene, querySceneFrame, querySceneFrame as queryCompositionSceneFrame } from './scene-evaluation';
