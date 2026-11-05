// Pure queries of the actual generated host; no capture or source admission.
import assert from 'node:assert/strict';
import {basis, params, prepareRoot, propsFor, seriesInspection} from './presenter_crop_geometry.mjs';

const props = propsFor();
const original = JSON.stringify(props);
const scene = prepareRoot(props, basis, {});
let reads = 0;
for (const capability of Object.values(scene.capabilities.components)) {
  const frame = capability.frame;
  capability.frame = contentFrame => { reads++; return frame(contentFrame); };
}
const zero = seriesInspection(scene, props);
assert.equal(reads, 1);
assert.equal(zero.frame, 0);
assert.equal(zero.componentFrame, 0);
assert.deepEqual(zero.mouth.rect, {x: 894, y: 606, width: 138, height: 48});
assert.equal(JSON.stringify(props), original);

// Selection failures never read another region or silently choose a presenter.
assert.throws(() => seriesInspection(scene, props, 'missing'));
assert.throws(() => seriesInspection(scene, {...props, timeline: {...props.timeline,
  clips: [...props.timeline.clips, props.timeline.clips[0]]}}));
assert.throws(() => seriesInspection(scene, propsFor(params,
  {elementRef: {kind: 'effect', id: 'am-presenter', packId: 'probe'}})));
assert.throws(() => seriesInspection(scene, propsFor({...params, seed: 9})));
assert.equal(reads, 1);

// The public clock, not authored zero or a guessed source offset, locates zero.
const delayed = propsFor(params, {at: 5, from: 17, to: 18, speed: 2});
assert.equal(seriesInspection(prepareRoot(delayed, basis, {}), delayed).frame, 150);
const clipped = propsFor(params, {at: -1});
assert.throws(() => seriesInspection(prepareRoot(clipped, basis, {}), clipped));

// Unknown diagnostics and an empty scene cannot become a known replacement ROI.
const unknown = propsFor({...params, mosaicIn: {frames: 8}});
assert.throws(() => seriesInspection(prepareRoot(unknown, basis, {}), unknown));
const empty = {...props, timeline: {...props.timeline, clips: []}};
assert.throws(() => seriesInspection(prepareRoot(empty, basis, {}), empty));
assert.throws(() => seriesInspection(undefined, props));
console.log(JSON.stringify({ok: true, reads, zero}));
