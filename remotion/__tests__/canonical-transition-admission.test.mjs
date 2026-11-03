import assert from 'node:assert/strict';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import test from 'node:test';
import {DEFAULT_THEME} from '@banodoco/timeline-composition';

const project = fileURLToPath(new URL('..', import.meta.url));
const helper = `${project}/scene-query.mjs`;
const props = {
  timeline: {theme: 'default', tracks: [{id: 'v', kind: 'visual', label: 'Visual'}], clips: [
    {id: 'a', track: 'v', clipType: 'media', asset: 'a', at: 0, hold: 1},
    {id: 'b', track: 'v', clipType: 'media', asset: 'b', at: 1, hold: 1,
      transition: {id: 'fade', durationFrames: 12}},
  ]},
  assets: {assets: {a: {file: '/a.png'}, b: {file: '/b.png'}}},
  theme: DEFAULT_THEME,
};

const query = (payload) => {
  const result = spawnSync(process.execPath, [helper, project], {
    cwd: project, input: JSON.stringify(payload), encoding: 'utf8', timeout: 30000,
  });
  assert.equal(result.status, 0, result.stderr);
  return JSON.parse(result.stdout);
};

test('verified canonical Root props compile once for renderer and scene query; native props keep legacy plan', () => {
  const compiled = query({operation: 'compile-canonical-transitions', props, basis: {fps: 30}});
  assert.deepEqual(compiled, {fps: 30, tracks: [{trackId: 'v', clipIds: ['a', 'b'],
    incomingTransitions: [null, {T: 12}]}]});

  const roles = {a: 'visual', b: 'visual'};
  const basis = {fps: 30, width: 1920, height: 1080};
  const canonical = query({props: {...props, compiledTransitions: compiled}, basis, roles, frames: [24]})[0];
  assert.deepEqual(canonical.layers.map(layer => [layer.clipId, layer.transitionOpacity]),
    [['a', 1], ['b', 0.5]]);

  // Native TimelineConfig keeps the legacy outgoing interpretation. Neither
  // occurrence-looking metadata nor an authored alias can enable compilation.
  const nativeProps = {...props, timeline: {...props.timeline,
    app: {occurrence_id: 'looks-managed', canonicalTiming: true}}};
  const native = query({props: nativeProps, basis, roles, frames: [24]})[0];
  assert.deepEqual(native.layers.map(layer => [layer.clipId, layer.transitionOpacity]), [['a', 1]]);
});
