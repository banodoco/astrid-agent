import {readFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

try {
  // Resolve the public export from the validated host install, never this checkout.
  const require = createRequire(path.join(process.argv[2], 'package.json'));
  const {prepareScene, querySceneFrame} = await import(pathToFileURL(
    require.resolve('@banodoco/timeline-composition/evaluation')));
  const input = JSON.parse(readFileSync(0, 'utf8'));
  const {scene, props, basis, roles: hostRoles, frames} = input;
  if (input.operation === 'compile-canonical-transitions') {
    if (!props || !Number.isFinite(basis?.fps) || basis.fps <= 0) {
      throw new Error('canonical_transition_inputs_invalid');
    }
    const {loadHostTransitionCompiler} = await import('./native-scene.mjs');
    const compile = await loadHostTransitionCompiler(process.argv[2]);
    process.stdout.write(JSON.stringify(compile(props, basis.fps)));
  } else {
    if (!Array.isArray(frames)) throw new Error('scene_frames_invalid');
    let prepared;
    if (props) {
      const {loadHostScene} = await import('./native-scene.mjs');
      const prepare = await loadHostScene(process.argv[2]);
      prepared = prepare(props, basis, hostRoles ?? {});
    } else {
      const {tracks, assets, fps, width, height, roles, elements} = scene;
      if (!Array.isArray(tracks) || !Array.isArray(frames) || !assets?.assets) {
        throw new Error('scene_inputs_invalid');
      }
      for (const {track, clips, transitions} of tracks) {
        if (track?.kind !== 'visual' || !Array.isArray(clips) || !Array.isArray(transitions)
          || transitions.length !== Math.max(0, clips.length - 1)
          || clips.some((clip, i) => i > 0 && clip.at < clips[i - 1].at)
          || transitions.some(t => t !== null && (!Number.isFinite(t.T) || t.T <= 0
            || !['dip', 'dissolve'].includes(t.kind) || !['start', 'center', 'end'].includes(t.align)))) {
          throw new Error('scene_requires_renderer_order_and_resolved_transitions');
        }
      }
      prepared = prepareScene(tracks, assets, fps, width, height,
        {roles: Object.assign(Object.create(null), roles), elements: new Set(elements ?? [])});
    }
    process.stdout.write(JSON.stringify(frames.map(frame => querySceneFrame(prepared, frame))));
  }
} catch (error) {
  process.stderr.write(`scene evaluation unavailable: ${error.message}\n`);
  process.exitCode = 1;
}
