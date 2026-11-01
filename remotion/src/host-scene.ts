import {compileCanonicalTransitions, prepareCompositionScene, toRuntimeTheme} from '@banodoco/timeline-composition';
import type {TimelineCompositionProps} from '@banodoco/timeline-composition';
import {resolveHostRegistries} from './host-registries';

const visualTracks = (props: TimelineCompositionProps) => {
  const {timeline} = props;
  if (!Array.isArray(timeline?.tracks) || !Array.isArray(timeline.clips)) {
    throw new Error('host scene requires explicit timeline tracks and clips');
  }
  return [...timeline.tracks].reverse().filter(track => track.kind === 'visual')
    .map(track => ({track, clips: timeline.clips.filter(clip => clip.track === track.id)}));
};

// The renderer and host-scene evaluator use this exact host registry/compiler
// boundary. Python only enables it for a Runtime-verified canonical source.
export const compileHostTransitions = (props: TimelineCompositionProps, fps: number) => {
  if (!props.theme || !Array.isArray(props.timeline?.clips) || !Array.isArray(props.timeline?.tracks)) {
    throw new Error('canonical transitions require exact Root props');
  }
  const theme = toRuntimeTheme(props.theme);
  return compileCanonicalTransitions(visualTracks(props), fps, theme, resolveHostRegistries(theme));
};

// Evaluation only: supplied props/basis do not establish render-source authority.
// Root's registries, painter order and public transition preparation are shared.
export const prepareHostScene = (props: TimelineCompositionProps,
  basis: {fps: number; width: number; height: number}, roles: Record<string, string>) => {
  if (!props.theme || !props.assets?.assets || !Array.isArray(props.timeline?.clips)
    || !Array.isArray(props.timeline.tracks)
    || ![basis.fps, basis.width, basis.height].every(n => Number.isFinite(n) && n > 0)) {
    throw new Error('scene requires exact Root props, explicit tracks and render frame basis');
  }
  const app = (props.timeline as typeof props.timeline & {app?: Record<string, unknown>}).app;
  if (app?.astrid_render_clock) {
    throw new Error('scene preparation does not evaluate Root RenderClockTail');
  }
  // TimelineComposition paints visual tracks in reverse authored order. Clip
  // sorting and all transition/default resolution belong to the public prepare.
  const tracks = visualTracks(props);
  const theme = toRuntimeTheme(props.theme);
  return prepareCompositionScene(tracks, props.assets, basis.fps, theme,
    basis.width, basis.height, undefined, roles, {}, '', {}, resolveHostRegistries(theme),
    props.compiledTransitions);
};
