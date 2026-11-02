// Public facts from the ordinary generated host; no renderer or clock mirror.
import assert from 'node:assert/strict';
import {DEFAULT_THEME} from '@banodoco/timeline-composition';
import {queryDrawFrame, querySceneFrame} from '@banodoco/timeline-composition/evaluation';
import {prepareHostScene as prepareRoot, source as shared} from './query.mjs';
export {prepareRoot};

// Erase control uses the actual drawing helpers, never a copied crop formula.
export const eraseRegion = (raw, frame, fps) => {
  const {left: x, top: y, width, height} = shared.pixelGeometry(
    shared.mouthEraseRect(shared.mouthAnchor(raw)), shared.presenterAt(raw, frame, fps).view);
  return {status: 'known', rect: {x, y, width, height}};
};

export const legacyInspectionRegion = (raw, frame, fps) => {
  const rect = shared.mouthInspectionRect(raw);
  if (!rect) return null;
  const {left: x, top: y, width, height} = shared.pixelGeometry(
    rect, shared.presenterAt(raw, frame, fps).view);
  return {status: 'known', rect: {x, y, width, height}};
};

export const basis = {width: 1920, height: 1080, fps: 30};
export const frames = [0, 3, 4, 5, 15, 29];
export const params = {mouth: {x: 151, y: 104, w: 19}, words: [[.1, .4]],
  chrome: 'none', blinkEvery: 0, seed: 7};
export const propsFor = (p = params, extra = {}) => ({
  timeline: {tracks: [{id: 'overlay', kind: 'visual'}], clips: [{
    id: 'presenter', track: 'overlay', clipType: 'am-presenter', at: 0, hold: 2,
    elementRef: {kind: 'effect', id: 'am-presenter', packId: 'astrid_motion'},
    params: p, ...extra,
  }]}, theme: DEFAULT_THEME, assets: {assets: {}},
});
export const inspect = (props, b = basis, selected = frames) => {
  assert.deepEqual(props.timeline.clips[0].elementRef,
    {kind: 'effect', id: 'am-presenter', packId: 'astrid_motion'});
  const scene = prepareRoot(props, b, {});
  return selected.map(frame => {
    const answer = querySceneFrame(scene, frame);
    assert.equal(answer.layers.length, 1);
    const layer = answer.layers[0];
    assert.equal(layer.clipId, props.timeline.clips[0].id);
    return {frame, mouth: layer.diagnostics.mouth,
      legacyInspection: legacyInspectionRegion(props.timeline.clips[0].params, frame, b.fps),
      motion: layer.diagnostics.motion};
  });
};

// The legacy series holds component-zero geometry. Resolve that frame through
// the existing public DrawPlan clock, then query this same prepared Root once.
// Source/owner qualification belongs to the generated-host fixture, not this
// clip lookup; this helper is not a production source admission boundary.
export const seriesInspection = (scene, props, clipId = 'presenter') => {
  const targets = props.timeline.clips.filter(clip => clip.id === clipId);
  assert.equal(targets.length, 1);
  const target = targets[0];
  assert.deepEqual(target.elementRef, {kind: 'effect', id: 'am-presenter', packId: 'astrid_motion'});
  const matches = scene.tracks.flatMap(track => track.clips.flatMap((clip, clipIndex) =>
    clip.id === target.id ? [{track, clip, clipIndex, plan: track.prepared.plans[clipIndex]}] : []));
  assert.equal(matches.length, 1);
  const {track, clip, clipIndex, plan} = matches[0];
  assert.deepEqual(clip, target);
  assert.equal(track.track.id, target.track);
  const frame = Math.max(0, plan.drawStart);
  assert.ok(frame < plan.drawStart + plan.drawDuration);
  assert.equal(queryDrawFrame(plan, frame - plan.drawStart).contentFrame, 0);
  const answer = querySceneFrame(scene, frame); // the only series ROI read
  const layers = answer.layers.filter(layer => layer.clipId === target.id);
  assert.equal(layers.length, 1);
  const layer = layers[0];
  assert.equal(layer.track, target.track);
  assert.equal(layer.clipIndex, clipIndex);
  assert.equal(layer.contentFrame, 0);
  assert.equal(layer.diagnostics?.mouth?.status, 'known');
  return {frame, componentFrame: 0, clipId: target.id,
    mouth: layer.diagnostics.mouth, motion: layer.diagnostics.motion};
};

if (process.argv[2] === 'geometry') {
  const cases = {
    default: {mouth: undefined}, partial: {mouth: {x: 151, y: 104}}, custom: {}, fractional: {mouth: {x: 152.5, y: 103.7, w: 20.6}},
    rawInvalid: {mouth: {x: 'bad', y: null, w: 900}},
    clamps: {mouth: {x: -20, y: 999, w: 0}},
    zoom: {zoom: 2, focus: {x: 190.4, y: 120.7}},
    focusClamp: {zoom: 99, focus: {x: -100, y: 999}},
    push: {push: {to: 3.5, frames: 28, at: 2}, focus: {x: 191.2, y: 110.4}},
    punch: {zoom: 3, punchAt: [1, 10]}, bob: {bob: 1, seed: 9},
    mosaic: {mosaicIn: {frames: 8}}, outer: {},
  };
  console.log(JSON.stringify([30, 24000 / 1001].flatMap(fps => Object.entries(cases).map(([name, p]) => {
    const props = propsFor({...params, ...p}, name === 'outer' ? {cropLeft: .1} : {});
    return {name, basis: {...basis, fps}, props,
      normalizedMouth: shared.mouthAnchor(props.timeline.clips[0].params),
      rows: inspect(props, {...basis, fps}).map(row =>
        ({...row, erase: eraseRegion(props.timeline.clips[0].params, row.frame, fps)}))};
  }))));
}
