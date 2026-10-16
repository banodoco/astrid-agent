// Sprint 5: physically moved + renamed from
// tools/remotion/src/HypeComposition.tsx → TimelineComposition.tsx.
//
// Composition id rename: `HypeComposition` → `TimelineComposition`.
// Banodoco's CLI render path passes the new id; the in-tree shell at
// tools/remotion/src/Root.tsx imports this composition from the package.

import type {ReactElement} from 'react';
import {AbsoluteFill, Sequence, useCurrentFrame, useVideoConfig} from 'remotion';
import {AudioTrack} from './AudioTrack';
import {ClipLook} from './ClipLook';
import {CLIP_TYPE_ALIASES, EFFECT_REGISTRY, type EffectId} from './effects.generated';
import {DEFAULT_THEME, ThemeProvider, useTheme} from './ThemeContext';
import {VisualClip} from './VisualClip';
import {resolveParams} from './lib/effect-params';
import {getClipDurationInFrames} from './lib/duration';
import {assertLoopCoverage, type ResolvedLoop} from './lib/beat-loop';
import {getAudioTracks, getVisualTracks} from './lib/tracks';
import {resolveTransitionReference} from './lib/transitions';
import {
  prepareScene,
  querySceneFrame,
  type DrawPlan,
  type TransitionSpec,
} from './evaluation';
import {TransitionLayer} from './TransitionLayer';
import {mediaModifierBounds} from './lib/modifier-bounds';
import type {ModifierRegistry, ModifierFinding, ModifierBounds} from './modifier-types';
import {ModifierAdmissionError} from './modifiers';
import type {
  AssetRegistryEntry,
  HypeCompositionProps,
  TimelineClip,
  TimelineCompositionProps,
  TrackDefinition,
} from './types';

type ModifierHostOptions = {
  width: number;
  height: number;
  registry?: ModifierRegistry;
  onFinding?: (finding: ModifierFinding) => void;
};

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

export const renderVisualClipContent = (
  track: TrackDefinition,
  clip: TimelineClip,
  assets: TimelineCompositionProps['assets'],
  fps: number,
  theme: ReturnType<typeof useTheme>,
  globalFrame: number,
  modifiers: ModifierHostOptions,
): ReactElement | null => {
  if (clip.clipType === 'effect-layer') {
    if (clip.modifiers?.length) throw new ModifierAdmissionError('unsupported-host', 'Effect layers are not owning visual clip subtrees');
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
      <ClipLook clip={clip} fps={fps} kind="element" globalFrame={globalFrame}
        modifierRegistry={modifiers.registry} onModifierFinding={modifiers.onFinding}>
        <EffectComponent
          clip={clip}
          params={resolveParams(clip)}
          theme={theme}
          fps={fps}
          assetEntry={assetEntry}
          globalFrame={globalFrame}
        />
      </ClipLook>
    );
  }

  const assetEntry = getAssetEntry(clip, assets);
  const modifierBounds: ModifierBounds = mediaModifierBounds(clip, track, assetEntry, modifiers.width, modifiers.height);
  const lookProps = {clip, fps, globalFrame, modifierBounds, modifierRegistry: modifiers.registry,
    onModifierFinding: modifiers.onFinding};
  const baseVisual = (
    <VisualClip clip={clip} track={track} assetEntry={assetEntry} fps={fps}
      applyOpacity={(clip.modifiers?.length ?? 0) === 0} />
  );

  if (hasPositionOverride(clip)) {
    return <ClipLook {...lookProps} kind="media">{baseVisual}</ClipLook>;
  }

  if ((track.scale ?? 1) !== 1) {
    return (
      <ClipLook {...lookProps} kind="media">
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

  return <ClipLook {...lookProps} kind="media">{baseVisual}</ClipLook>;
};

export const renderVisualSequence = (
  track: TrackDefinition,
  clip: TimelineClip,
  assets: TimelineCompositionProps['assets'],
  fps: number,
  theme: ReturnType<typeof useTheme>,
  plan: DrawPlan,
  globalFrame: number,
  modifiers: ModifierHostOptions,
): ReactElement | null => {
  if (clip.clipType === 'am-sprite' && clip.params?.loop !== undefined) {
    assertLoopCoverage(clip.params.loop as ResolvedLoop, plan.drawStart, plan.drawDuration);
  }
  const content = renderVisualClipContent(track, clip, assets, fps, theme, globalFrame, modifiers);
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

// Both native requested-frame metadata and drawing use this exact preparation boundary.
export const prepareCompositionScene = (tracks: {track: TrackDefinition; clips: TimelineClip[]}[],
  assets: TimelineCompositionProps['assets'], fps: number, theme: ReturnType<typeof useTheme>,
  width: number, height: number, registry?: ModifierRegistry, roles?: Readonly<Record<string, string>>) =>
  prepareScene(tracks.map(({track, clips}) => {
    const sorted = sortClipsByAt(clips);
    return {track, clips: sorted, transitions: sorted.slice(0, -1).map((clip, index) =>
      resolveTransitionSpec(clip, sorted[index + 1], fps, theme))};
  }), assets, fps, width, height, {elements: new Set(Object.keys(EFFECT_REGISTRY).concat(Object.keys(CLIP_TYPE_ALIASES))),
    modifiers: registry, roles});

const renderVisualTrack = (
  track: TrackDefinition,
  clips: TimelineClip[],
  assets: TimelineCompositionProps['assets'],
  fps: number,
  theme: ReturnType<typeof useTheme>,
  globalFrame: number,
  modifiers: ModifierHostOptions,
): ReactElement | null => {
  const sortedClips = sortClipsByAt(clips);
  if (sortedClips.length === 0) {
    return null;
  }

  const prepared = prepareCompositionScene([{track, clips: sortedClips}], assets, fps, theme,
    modifiers.width, modifiers.height, modifiers.registry);
  const evaluated = querySceneFrame(prepared, globalFrame);
  const rendered = evaluated.layers.map(layer => renderVisualSequence(track, sortedClips[layer.clipIndex],
    assets, fps, theme, prepared.tracks[0].prepared.plans[layer.clipIndex], globalFrame, modifiers));

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

export const renderAudioTrack = (
  track: TrackDefinition,
  clips: TimelineClip[],
  assets: TimelineCompositionProps['assets'],
  fps: number,
): ReactElement[] => {
  return sortClipsByAt(clips).map((clip) => {
    if (clip.modifiers?.length) throw new ModifierAdmissionError('unsupported-host', 'Audio tracks do not support visual modifiers');
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
          modifiers={{width, height, registry: props.modifierRegistry, onFinding: props.onModifierFinding}}
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
  modifiers,
}: {
  timeline: TimelineCompositionProps['timeline'];
  assets: TimelineCompositionProps['assets'];
  visualTracks: TrackDefinition[];
  audioTracks: TrackDefinition[];
  fps: number;
  modifiers: ModifierHostOptions;
}): ReactElement => {
  const theme = useTheme();
  const globalFrame = useCurrentFrame();
  return (
    <>
      <AbsoluteFill style={{position: 'relative', overflow: 'hidden'}}>
        {visualTracks.map((track) => {
          const clips = timeline.clips.filter((clip) => clip.track === track.id);
          return renderVisualTrack(track, clips, assets, fps, theme, globalFrame, modifiers);
        })}
      </AbsoluteFill>
      {audioTracks.map((track) => {
        const clips = timeline.clips.filter((clip) => clip.track === track.id);
        return renderAudioTrack(track, clips, assets, fps);
      })}
    </>
  );
};
