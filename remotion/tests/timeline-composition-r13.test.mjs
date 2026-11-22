import assert from 'node:assert/strict';
import {dirname, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';
import test from 'node:test';
import {DEFAULT_THEME} from '@banodoco/timeline-composition';
import {loadHostScene, loadHostTransitionCompiler} from '../native-scene.mjs';

const project = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const compile = await loadHostTransitionCompiler(project);
const prepare = await loadHostScene(project);

const variant = (identity) => ({
  theme: DEFAULT_THEME,
  assets: {assets: {}},
  timeline: {
    tracks: [{id: 'v', kind: 'visual'}],
    clips: [
      {id: 'a', clipType: 'image', track: 'v', at: 0, hold: 2},
      {id: 'b', clipType: 'image', track: 'v', at: 2, hold: 2, transition: {type: identity, duration: 0.4}},
    ],
  },
});

for (const identity of ['crossfade', 'wipe', 'slide-push', 'zoom-through']) {
  test(`the host compiles ${identity} on the incoming clip using R13 timing`, () => {
    const props = variant(identity);
    const compiledTransitions = compile(props, 30);
    assert.deepEqual(compiledTransitions.tracks[0].incomingTransitions, [null, {T: 12}]);
    const scene = prepare({...props, compiledTransitions}, {fps: 30, width: 320, height: 180}, {});
    const {drawStart, drawDuration} = scene.tracks[0].prepared.plans[1];
    assert.deepEqual([drawStart, drawDuration], [48, 72]);
    assert.deepEqual(props.timeline.clips[1].transition, {type: identity, duration: 0.4});
  });
}

test('the host rejects an unknown transition identity', () => {
  assert.throws(() => compile(variant('crossfadee'), 30), /Unknown transition id/);
});
