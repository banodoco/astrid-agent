// Pure public planner API. Keep this module independent of the React/Remotion
// entry point and generated component registries.
import {
  layerOpacity,
  ownFrame,
  planTrack,
  type ClipSpan,
  type DrawPlan,
  type TransitionSpec,
} from './lib/transition-spans';
import {admitDeclaredTransitions, type TransitionDeclaration} from './lib/transition-declarations';

export {declareAppTransition, admitTransitionContribution, transitionContributionIdentity, TransitionAdmissionError} from './lib/transition-declarations';
export type {AppTransition, TransitionDeclaration, TransitionImplementation, TransitionContributionResource, TransitionContributionReference, AdmittedTransitionContribution} from './lib/transition-declarations';

export type {Align, ClipSpan, DrawPlan, FadeWindow, TransitionSpec} from './lib/transition-spans';

export type PreparedTrack = {
  readonly plans: readonly DrawPlan[];
};

export type DrawFrame = {
  drawLocalFrame: number;
  ownFrame: number;
  // Transition opacity only; excludes track/clip/component opacity and occlusion.
  opacity: number;
};

export type TrackFrameLayer = DrawFrame & {clipIndex: number};

export type TrackFrame = {
  frame: number;
  layers: TrackFrameLayer[];
};

// Spans and resolved transitions must already be in track order and in frames.
// Plan once per input revision, then query only the frames the caller needs.
export const prepareTrack = (
  spans: readonly ClipSpan[],
  transitions: readonly (TransitionSpec | null)[],
): PreparedTrack => ({plans: planTrack([...spans], [...transitions])});

// Strict authoring admission. Legacy prepareTrack inputs retain their behavior.
export const prepareDeclaredTrack = (
  trackId: string,
  clips: readonly (ClipSpan & {id: string})[],
  declarations: readonly TransitionDeclaration[],
  host: string,
): PreparedTrack => prepareTrack(clips, admitDeclaredTransitions(trackId, clips, declarations, host));

// The renderer calls this inside a clip's draw Sequence. Mounting is owned by
// that Sequence; this query intentionally does not filter by the draw interval.
export const queryDrawFrame = (plan: DrawPlan, drawLocalFrame: number): DrawFrame => ({
  drawLocalFrame,
  ownFrame: ownFrame(plan, drawLocalFrame),
  opacity: layerOpacity(plan, drawLocalFrame),
});

// Track-absolute frame, half-open draw intervals, in input/painter order.
// Include mounted zero-opacity layers: mounting does not establish visibility.
export const queryTrackFrame = (track: PreparedTrack, frame: number): TrackFrame => ({
  frame,
  layers: track.plans.flatMap((plan, clipIndex) => {
    if (frame < plan.drawStart || frame >= plan.drawStart + plan.drawDuration) {
      return [];
    }
    return [{clipIndex, ...queryDrawFrame(plan, frame - plan.drawStart)}];
  }),
});

export {keyframeOrigin, timelineKeyframeTime, spriteKeyframeFrame, declareKeyframeOrigin} from './lib/keyframe-origin';
export type {KeyframeClock} from './lib/keyframe-origin';
export {loopPhase, positiveModulo, spriteSheetIndex} from './lib/beat-loop';
export type {ResolvedLoop} from './lib/beat-loop';

export {mediaModifierBounds} from './lib/modifier-bounds';
export type {ModifierBounds, ModifierContext, ModifierFinding} from './modifier-types';

export {compileAudioEnvelope, validateAudioEnvelope, audioEnvelopeAt, applyAudioEnvelope, audioEnvelopeWav, AudioEnvelopeError} from './lib/audio-envelope';
export type {AudioEnvelope, AudioCurve} from './lib/audio-envelope';

export {prepareScene, querySceneFrame, querySceneFrame as queryCompositionSceneFrame} from './scene-evaluation';
export type {PreparedScene, SceneFrame, SceneLayer, SceneFinding, SceneTrackInput} from './scene-evaluation';
