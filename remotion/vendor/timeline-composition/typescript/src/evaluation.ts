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
