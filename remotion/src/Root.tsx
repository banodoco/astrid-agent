import type {ReactElement} from 'react';
import {Composition, Sequence, useVideoConfig} from 'remotion';
import {Video} from '@remotion/media';
import {
  TimelineComposition,
  getTimelineDurationInFrames,
} from '@banodoco/timeline-composition';
import type {TimelineCompositionProps} from '@banodoco/timeline-composition';
import type {
  CanvasOverride,
  TimelineThemeOverrides,
  VisualOverrides,
} from './types.augmentations';
import {FontProvider} from './fonts';
import {ReviewOverlay} from './ReviewOverlay';
import {FrameDiagnostics} from './FrameDiagnostics';
import {BoundsProbe} from './BoundsProbe';
import type {ReviewContext} from './ReviewOverlay';
import {resolveInvocationAsset} from './asset-source';

type ReviewProps = TimelineCompositionProps & {
  review?: ReviewContext | null;
  astridFrameDiagnostics?: boolean;
  astridBounds?: boolean;
};

type RenderClock = {
  authored_duration_frames: number;
  render_duration_frames: number;
  tail: {
    policy: 'unmapped_excess_rendered_region';
    source_asset: string;
    start_frame: number;
    end_frame: number;
    source_start_frame: number;
    source_end_frame: number;
  };
};

const getRenderClock = (timeline: TimelineCompositionProps['timeline']): RenderClock | null => {
  const app = (timeline as TimelineCompositionProps['timeline'] & {app?: Record<string, unknown>}).app;
  const value = app?.astrid_render_clock;
  return value && typeof value === 'object' ? value as RenderClock : null;
};

// Bounds are requested either by the render-props flag or by the timeline's
// top-level app flag (the fast lane sets app.astrid_bounds on its config copy).
const boundsRequested = (props: TimelineCompositionProps & {astridBounds?: boolean}): boolean => {
  const app = (props.timeline as TimelineCompositionProps['timeline'] & {app?: Record<string, unknown>}).app;
  return props.astridBounds === true || app?.astrid_bounds === true;
};

const RenderClockTail = ({props}: {props: TimelineCompositionProps}): ReactElement | null => {
  const clock = getRenderClock(props.timeline);
  const {fps} = useVideoConfig();
  if (!clock || clock.tail.end_frame <= clock.tail.start_frame) {
    return null;
  }
  const asset = props.assets.assets[clock.tail.source_asset];
  if (!asset?.file) {
    return null;
  }
  const authoredFrames = getTimelineDurationInFrames(props.timeline, fps);
  const renderedFrames = clock.render_duration_frames;
  return (
    <Sequence
      from={authoredFrames}
      durationInFrames={renderedFrames - authoredFrames}
    >
      <Video
        src={resolveInvocationAsset(asset.file)}
        trimBefore={clock.tail.source_start_frame}
        trimAfter={clock.tail.source_end_frame}
        muted
        style={{width: '100%', height: '100%', objectFit: 'cover'}}
      />
    </Sequence>
  );
};

const ReviewedTimelineWithRenderTail = (props: ReviewProps): ReactElement => <>
  <TimelineComposition {...props} />
  <RenderClockTail props={props} />
  <ReviewOverlay review={props.review} />
  {props.astridFrameDiagnostics && <FrameDiagnostics />}
  {boundsRequested(props) && <BoundsProbe />}
</>;

// Remotion's lazyComponent waits on Suspense with a delayRender handle. Keep
// the Three.js scene out of the 2D film's page load and JS parse path.
const loadReviewedThree = () => import('./ThreeTimelineComposition').then(({ThreeTimelineComposition}) => ({
  default: (props: ReviewProps): ReactElement => <>
    <ThreeTimelineComposition {...props} />
    <RenderClockTail props={props} />
    <ReviewOverlay review={props.review} />
    {props.astridFrameDiagnostics && <FrameDiagnostics />}
    {boundsRequested(props) && <BoundsProbe />}
  </>,
}));

const DEFAULT_PROPS: TimelineCompositionProps = {
  timeline: {
    theme: 'banodoco-default',
    theme_overrides: {
      visual: {
        canvas: {
          width: 1920,
          height: 1080,
          fps: 30,
        },
      },
    },
    clips: [],
  },
  assets: {
    assets: {},
  },
};

const DEFAULT_CANVAS: CanvasOverride = {width: 1920, height: 1080, fps: 30};

const getCanvas = (props: TimelineCompositionProps): CanvasOverride => {
  const overrides = props.timeline.theme_overrides as
    | TimelineThemeOverrides
    | undefined;
  const visual = overrides?.visual as VisualOverrides | undefined;
  return (
    visual?.canvas ??
    (props.theme?.visual?.canvas as CanvasOverride | undefined) ??
    DEFAULT_CANVAS
  );
};

// Single canvas-selection + timeline-duration authority shared by both
// compositions. durationInFrames is clamped to at least 1 (an empty
// timeline is exactly 1 frame).
const getMetadata = async ({
  props,
}: {
  props: TimelineCompositionProps;
}): Promise<{
  width: number;
  height: number;
  fps: number;
  durationInFrames: number;
}> => {
  const canvas = getCanvas(props);
  const fps = canvas.fps ?? 30;
  const clock = getRenderClock(props.timeline);
  const metadata = (props.timeline as typeof props.timeline & {metadata?: Record<string, unknown>}).metadata;
  const windowDuration = metadata?.source_window_start_seconds !== undefined
    && typeof metadata.duration_seconds === 'number'
    ? Math.round(metadata.duration_seconds * fps) : undefined;
  return {
    width: canvas.width ?? 1920,
    height: canvas.height ?? 1080,
    fps,
    durationInFrames: Math.max(1, windowDuration ?? clock?.render_duration_frames ?? getTimelineDurationInFrames(props.timeline, fps)),
  };
};

export const Root = (): ReactElement => {
  return (
    <>
      <FontProvider />
      <Composition
        id="TimelineComposition"
        component={ReviewedTimelineWithRenderTail}
        defaultProps={DEFAULT_PROPS}
        calculateMetadata={getMetadata}
      />
      <Composition
        id="ThreeTimelineComposition"
        lazyComponent={loadReviewedThree}
        defaultProps={DEFAULT_PROPS}
        calculateMetadata={getMetadata}
      />
    </>
  );
};
