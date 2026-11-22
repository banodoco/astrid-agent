const {createHash} = require('node:crypto');
const path = require('node:path');

// The installed tarball stays unchanged. Both the rendering bundle and the
// host scene compiler adapt this exact package entry so their plans agree.
const COMPOSITION_ENTRY = 'node_modules/@banodoco/timeline-composition/dist/index.js';
const COMPOSITION_VERSION = '0.0.19';
const PINNED_ENTRY_HASH = 'dc5e0b3f5dd849a3c96fb94de3a4c08ade9d96163157d0d61712d6638f5bc09c';

function adaptTimelineComposition(source, root, filename) {
  if (path.resolve(filename) !== path.resolve(root, COMPOSITION_ENTRY)) {
    throw new Error('legacy composition adapter matched an unexpected module');
  }
  if (createHash('sha256').update(source).digest('hex') !== PINNED_ENTRY_HASH) {
    throw new Error(`legacy composition adapter requires the pinned ${COMPOSITION_VERSION} entry`);
  }

  const helper = path.join(root, 'src/r13-clip-presentation.tsx');
  let output = `import {isLegacyIncomingTransition, legacyTransitionFrames, legacyTransitionStyle, wrapLegacyClipPhase} from ${JSON.stringify(helper)};\n${source}`;
  const replace = (anchor, replacement) => {
    if (output.split(anchor).length !== 2) {
      throw new Error(`legacy composition adapter expected one anchor: ${anchor}`);
    }
    output = output.replace(anchor, replacement);
  };

  // The App composes continuous inside entrance, then exit around both.
  replace(
    '  let content = composeClipAnimationPhase({ clip, fps, theme, clipFrames, registries, phase: "entrance", reference: clip.entrance, children: body }, frame);',
    '  const continuous = wrapLegacyClipPhase("continuous", clip, frame, fps, clipFrames, body) ?? body;\n'
      + '  let content = wrapLegacyClipPhase("entrance", clip, frame, fps, clipFrames, continuous) '
      + '?? composeClipAnimationPhase({ clip, fps, theme, clipFrames, registries, phase: "entrance", reference: clip.entrance, children: continuous }, frame);',
  );
  replace(
    '  content = composeClipAnimationPhase({ clip, fps, theme, clipFrames, registries, phase: "exit", reference: clip.exit, children: content }, frame);',
    '  content = wrapLegacyClipPhase("exit", clip, frame, fps, clipFrames, content) '
      + '?? composeClipAnimationPhase({ clip, fps, theme, clipFrames, registries, phase: "exit", reference: clip.exit, children: content }, frame);',
  );

  // Legacy transition styles are applied to the existing incoming Sequence
  // on its local clock. Other transitions retain the package opacity layer.
  replace(
    'jsx5(TransitionLayer, { plan, children: content })',
    'jsx5(TransitionLayer, { plan, clip, fps, children: content })',
  );
  replace(
    'var composeTransitionLayer = ({\n  plan,\n  children\n}, frame) => {',
    'var composeTransitionLayer = ({\n  plan,\n  clip,\n  fps,\n  children\n}, frame) => {',
  );
  replace(
    '  return /* @__PURE__ */ jsx4(AbsoluteFill2, { style: { opacity: evaluated.opacity }, children: body });',
    '  const legacyStyle = plan.incomingPolicy === "preroll-crossfade" ? legacyTransitionStyle(clip, frame, fps) : undefined;\n'
      + '  return /* @__PURE__ */ jsx4(AbsoluteFill2, { style: legacyStyle ? { overflow: "hidden", ...legacyStyle } : { opacity: evaluated.opacity }, children: body });',
  );

  replace(
    '      const transition = resolveTransitionReference(clip.transition, theme, fps, resolved, true);',
    '      const legacyFrames = legacyTransitionFrames(clip.transition, fps);\n'
      + '      if (legacyFrames !== undefined) return { T: legacyFrames };\n'
      + '      const transition = resolveTransitionReference(clip.transition, theme, fps, resolved, true);',
  );
  // Unverified historical files use the R13 owner convention on tracks
  // containing a legacy ID. Tracks without those IDs keep the outgoing plan.
  replace(
    '    return { track, clips: sorted, transitions: sorted.slice(0, -1).map((clip, index) => resolveTransitionSpec(clip, sorted[index + 1], fps, theme, resolved)) };',
    '    if (sorted.some((clip) => isLegacyIncomingTransition(clip.transition))) {\n'
      + '      const legacyTrack = compileCanonicalTransitions([{ track, clips: sorted }], fps, theme, resolved).tracks[0];\n'
      + '      return { track, clips: sorted, transitions: [], incomingTransitions: legacyTrack.incomingTransitions };\n'
      + '    }\n'
      + '    return { track, clips: sorted, transitions: sorted.slice(0, -1).map((clip, index) => resolveTransitionSpec(clip, sorted[index + 1], fps, theme, resolved)) };',
  );
  return output;
}

module.exports = function timelineCompositionLegacyLoader(source) {
  return adaptTimelineComposition(source, this.rootContext, this.resourcePath);
};
module.exports.adaptTimelineComposition = adaptTimelineComposition;
module.exports.COMPOSITION_ENTRY = COMPOSITION_ENTRY;
module.exports.COMPOSITION_VERSION = COMPOSITION_VERSION;
