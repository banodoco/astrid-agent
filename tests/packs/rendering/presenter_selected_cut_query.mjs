// Read-only query of the exact retained four-still Root. No capture or SSR.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import {createHash} from 'node:crypto';
import {querySceneFrame} from '@banodoco/timeline-composition/evaluation';
import {basis, prepareRoot, propsFor, seriesInspection} from './presenter_crop_geometry.mjs';

const retained = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
assert.deepEqual(retained.frames, [0, 3, 4, 5]);
assert.deepEqual(retained.basis, basis);
// The retained explicit public10 theme stays exact when querying public13.
assert.equal(createHash('sha256').update(fs.readFileSync(process.argv[2])).digest('hex'),
  'eabcc179baa4b97b6bdf1c8343ca270ef090e1306ab8791ee32787c3226385af');
assert.deepEqual(retained.props.timeline, propsFor().timeline);
assert.deepEqual(retained.props.assets, propsFor().assets);
const original = JSON.stringify(retained);
const scene = prepareRoot(retained.props, retained.basis, {});
// seriesInspection resolves this exact compiled ID and registered owner,
// checks the DrawPlan's component zero, then makes its one public ROI query.
const zero = seriesInspection(scene, retained.props);
const answers = retained.frames.map(frame => querySceneFrame(scene, frame));
const {erase, ...expected} = retained.rows[0];
// inspection.json is the capture's JSON boundary, which serializes -0 as 0.
// Match that same boundary; no geometry, pixel or threshold is normalized.
assert.deepEqual(JSON.parse(JSON.stringify(zero)), expected);

// Invalid selections refuse before the helper reaches querySceneFrame.
const controls = [];
const refuse = (name, props, id) => {
  assert.throws(() => seriesInspection(scene, props, id));
  controls.push(name);
};
refuse('missing-compiled-id', retained.props, 'missing');
refuse('ambiguous-compiled-id', {...retained.props, timeline: {...retained.props.timeline,
  clips: [...retained.props.timeline.clips, retained.props.timeline.clips[0]]}});
const changed = patch => ({...retained.props, timeline: {...retained.props.timeline,
  clips: [{...retained.props.timeline.clips[0], ...patch}]}});
refuse('foreign-owner', changed({elementRef: {kind: 'effect', id: 'am-presenter', packId: 'probe'}}));
refuse('stale-params', changed({params: {...retained.props.timeline.clips[0].params, seed: 9}}));
assert.equal(JSON.stringify(retained), original);
console.log(JSON.stringify({zero, answers, controls}));
