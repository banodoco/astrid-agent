// Sprint 5: physically moved + renamed from
// tools/remotion/src/HypeComposition.tsx → TimelineComposition.tsx.
//
// Composition id rename: `HypeComposition` → `TimelineComposition`.
// Banodoco's CLI render path passes the new id; the in-tree shell at
// tools/remotion/src/Root.tsx imports this composition from the package.

import type {ReactElement} from 'react';
import {AbsoluteFill, Sequence, useVideoConfig} from 'remotion';
import {AudioTrack} from './AudioTrack';
import {ClipLook} from './ClipLook';
import {CLIP_TYPE_ALIASES, EFFECT_REGISTRY, type EffectId} from './effects.generated';
import {DEFAULT_THEME, ThemeProvider, useTheme} from './ThemeContext';
import {VisualClip} from './VisualClip';
import {resolveParams} from './lib/effect-params';
import {getClipDurationInFrames} from './lib/duration';
import {getAudioTracks, getVisualTracks} from './lib/tracks';
import {resolveTransitionReference} from './lib/transitions';
import {
  prepareTrack,
  type ClipSpan,
  type DrawPlan,
  type TransitionSpec,
} from './evaluation';
import {TransitionLayer} from './TransitionLayer';
import type {
  AssetRegistryEntry,
  HypeCompositionProps,
  TimelineClip,
  TimelineCompositionProps,
  TrackDefinition,
} from './types';

const sortClipsByAt = (clips: TimelineClip[]): TimelineClip[] => {
  return [...clips].sort((left, right) => left.at - right.at);
};

const hasPositionOverride = (clip: TimelineClip): boolean => {
  return (
    clip.x !== undefined
    || clip.y !== undefined
    || clip.width !== undefined
    || clip.height !== undefined
    || clip.cropTop !== undefined
    || clip.cropBottom !== undefined
    || clip.cropLeft !== undefined
    || clip.cropRight !== undefined
  );
};

const getAssetEntry = (
  clip: TimelineClip,
  assets: TimelineCompositionProps['assets'],
): AssetRegistryEntry | undefined => {
  return clip.asset ? assets.assets[clip.asset] : undefined;
};

const renderVisualClipContent = (
  track: TrackDefinition,
  clip: TimelineClip,
  assets: TimelineCompositionProps['assets'],
  fps: number,
  theme: ReturnType<typeof useTheme>,
): ReactElement | null => {
  if (clip.clipType === 'effect-layer') {
    return null;
  }

  // Sprint 5 EFFECT_REGISTRY dispatch (the migration target cited at
  // tools/remotion/src/HypeComposition.tsx:58-64). Same shape; the
  // registry now lives inside the package.
  const effectId = (
    clip.clipType && clip.clipType in EFFECT_REGISTRY
      ? clip.clipType as EffectId
      : clip.clipType ? CLIP_TYPE_ALIASES[clip.clipType] : undefined
  );
  if (effectId) {
    const EffectComponent = EFFECT_REGISTRY[effectId];
    const assetEntry = getAssetEntry(clip, assets);
    return (
      <ClipLook clip={clip} fps={fps} kind="element">
        <EffectComponent
          clip={clip}
          params={resolveParams(clip)}
          theme={theme}
          fps={fps}
          assetEntry={assetEntry}
        />
      </ClipLook>
    );
  }

  const assetEntry = getAssetEntry(clip, assets);
  const baseVisual = (
    <VisualClip clip={clip} track={track} assetEntry={assetEntry} fps={fps} />
  );

  if (hasPositionOverride(clip)) {
    return <ClipLook clip={clip} fps={fps} kind="media">{baseVisual}</ClipLook>;
  }

  if ((track.scale ?? 1) !== 1) {
    return (
      <ClipLook clip={clip} fps={fps} kind="media">
        <AbsoluteFill
          style={{
            transform: `scale(${track.scale})`,
            transformOrigin: 'center center',
            overflow: 'hidden',
            isolation: 'isolate',
          }}
        >
          {baseVisual}
        </AbsoluteFill>
      </ClipLook>
    );
  }

  return <ClipLook clip={clip} fps={fps} kind="media">{baseVisual}</ClipLook>;
};

const renderVisualSequence = (
  track: TrackDefinition,
  clip: TimelineClip,
  assets: TimelineCompositionProps['assets'],
  fps: number,
  theme: ReturnType<typeof useTheme>,
  plan: DrawPlan,
): ReactElement | null => {
  const content = renderVisualClipContent(track, clip, assets, fps, theme);
  if (!content) {
    return null;
  }
  // A clip with no transition on either side renders exactly as before.
  const needsLayer = plan.drawDuration > plan.ownDuration || plan.fadeIn !== null || plan.fadeOut !== null;
  return (
    <Sequence
      key={clip.id}
      from={plan.drawStart}
      durationInFrames={plan.drawDuration}
    >
      {needsLayer ? <TransitionLayer plan={plan}>{content}</TransitionLayer> : content}
    </Sequence>
  );
};

const clipsCanTransition = (fromClip: TimelineClip, toClip: TimelineClip, fps: number): boolean => {
  if (!fromClip.transition || fromClip.clipType === 'effect-layer' || toClip.clipType === 'effect-layer') {
    return false;
  }
  const fromStart = Math.round(fromClip.at * fps);
  const fromEnd = fromStart + getClipDurationInFrames(fromClip, fps);
  const toStart = Math.round(toClip.at * fps);
  return toStart >= fromStart && toStart <= fromEnd;
};

const resolveTransitionSpec = (
  fromClip: TimelineClip,
  toClip: TimelineClip,
  fps: number,
  theme: ReturnType<typeof useTheme>,
): TransitionSpec | null => {
  if (!clipsCanTransition(fromClip, toClip, fps)) {
    return null;
  }
  const resolved = resolveTransitionReference(fromClip.transition as never, theme, fps);
  if (!resolved) {
    return null;
  }
  return {
    T: resolved.durationFrames,
    kind: resolved.id === 'fade' ? 'dip' : 'dissolve',
    align: resolved.align,
  };
};

const renderVisualTrack = (
  track: TrackDefinition,
  clips: TimelineClip[],
  assets: TimelineCompositionProps['assets'],
  fps: number,
  theme: ReturnType<typeof useTheme>,
): ReactElement | null => {
  const sortedClips = sortClipsByAt(clips);
  if (sortedClips.length === 0) {
    return null;
  }

  const spans: ClipSpan[] = sortedClips.map((clip) => ({
    start: Math.round(clip.at * fps),
    duration: getClipDurationInFrames(clip, fps),
  }));
  const transitions = sortedClips
    .slice(0, -1)
    .map((clip, index) => resolveTransitionSpec(clip, sortedClips[index + 1], fps, theme));
  const {plans} = prepareTrack(spans, transitions);

  const rendered = sortedClips.map((clip, index) =>
    renderVisualSequence(track, clip, assets, fps, theme, plans[index]),
  );

  return (
    <AbsoluteFill
      key={track.id}
      style={{
        opacity: track.opacity ?? 1,
        mixBlendMode: track.blendMode && track.blendMode !== 'normal' ? track.blendMode : undefined,
      }}
    >
      {rendered}
    </AbsoluteFill>
  );
};

const renderAudioTrack = (
  track: TrackDefinition,
  clips: TimelineClip[],
  assets: TimelineCompositionProps['assets'],
  fps: number,
): ReactElement[] => {
  return sortClipsByAt(clips).map((clip) => {
    const assetEntry = getAssetEntry(clip, assets);
    return (
      <Sequence
        key={clip.id}
        from={Math.round(clip.at * fps)}
        durationInFrames={getClipDurationInFrames(clip, fps)}
      >
        <AudioTrack clip={clip} track={track} assetEntry={assetEntry} fps={fps} />
      </Sequence>
    );
  });
};

export const TimelineComposition = (props: TimelineCompositionProps): ReactElement => {
  const {timeline, assets} = props;
  const {fps, width, height} = useVideoConfig();
  const visualTracks = [...getVisualTracks(timeline)].reverse();
  const audioTracks = getAudioTracks(timeline);

  return (
    <AbsoluteFill
      style={{
        backgroundColor: props.theme?.visual.color.bg ?? 'black',
        width,
        height,
        overflow: 'hidden',
      }}
    >
      <ThemeProvider value={props.theme ?? DEFAULT_THEME}>
        <TimelineCompositionBody
          timeline={timeline}
          assets={assets}
          visualTracks={visualTracks}
          audioTracks={audioTracks}
          fps={fps}
        />
      </ThemeProvider>
    </AbsoluteFill>
  );
};

// Backwards-compatibility alias for callers that haven't migrated off the
// HypeComposition name yet. The exported component identity is the same.
export const HypeComposition = TimelineComposition;
export type {HypeCompositionProps, TimelineCompositionProps};

const TimelineCompositionBody = ({
  timeline,
  assets,
  visualTracks,
  audioTracks,
  fps,
}: {
  timeline: TimelineCompositionProps['timeline'];
  assets: TimelineCompositionProps['assets'];
  visualTracks: TrackDefinition[];
  audioTracks: TrackDefinition[];
  fps: number;
}): ReactElement => {
  const theme = useTheme();
  return (
    <>
      <AbsoluteFill style={{position: 'relative', overflow: 'hidden'}}>
        {visualTracks.map((track) => {
          const clips = timeline.clips.filter((clip) => clip.track === track.id);
          return renderVisualTrack(track, clips, assets, fps, theme);
        })}
      </AbsoluteFill>
      {audioTracks.map((track) => {
        const clips = timeline.clips.filter((clip) => clip.track === track.id);
        return renderAudioTrack(track, clips, assets, fps);
      })}
    </>
  );
};
