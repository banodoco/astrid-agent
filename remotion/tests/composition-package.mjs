import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {build} from 'esbuild';
import {createElement} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {Internals} from 'remotion';
import {TimelineComposition, prepareCompositionScene, querySceneFrame, toRuntimeTheme, DEFAULT_THEME} from '@banodoco/timeline-composition';
import {composeAnimations, resolveAnimationReferences} from '@banodoco/timeline-composition/theme-api';
import {resolveElementRegistries} from '@banodoco/timeline-composition/registries';
import {BUNDLED_THEME_VERSIONS} from '@banodoco/timeline-composition/registry.generated';
import {ANIMATION_REGISTRY} from '@banodoco/timeline-composition/animations.generated';
import {TRANSITION_REGISTRY} from '@banodoco/timeline-composition/transitions.generated';

const fixture = process.argv[2];
const output = path.join(fixture, 'host.mjs');
await build({entryPoints: [path.join(fixture, 'host-registries.ts')], outfile: output,
  bundle: true, platform: 'node', format: 'esm', packages: 'external', plugins: [{
    name: 'declared-test-pack', setup(build) {
      build.onResolve({filter: /^@pack-probe-elements-/}, ({path: name}) => ({
        path: path.join(fixture, name.split('-elements-')[1].split('/component?')[0], 'component.tsx'),
      }));
    },
  }]});
// Resolve peers from this host install, including for a fixture outside the checkout.
fs.symlinkSync(path.resolve('node_modules'), path.join(fixture, 'node_modules'), 'dir');
const {resolveHostRegistries} = await import(pathToFileURL(output));
const theme = toRuntimeTheme(DEFAULT_THEME);
const registries = resolveHostRegistries(theme);
const track = {id: 'visual', kind: 'visual', label: 'Visual'};
const clip = {id: 'first', track: 'visual', at: 0, hold: 2, clipType: 'custom-alias',
  params: {nested: {value: 7}, opaque: ['preserve']}};
const prepare = (clips, maps) => prepareCompositionScene([{track, clips}], {assets: {}}, 30,
  theme, 1920, 1080, undefined, {first: 'picture'}, {}, 'host', {}, maps);
assert.equal(BUNDLED_THEME_VERSIONS['2rp'], '0.1.0');
assert.ok(resolveHostRegistries({id: '2rp'}).effects['art-card']);
assert.equal(resolveHostRegistries({id: 'external-theme'}).effects['art-card'], undefined);
const prepared = prepare([clip], registries);
assert.equal(prepared.capabilities.components.first.identity, JSON.stringify(clip.params));
assert.equal(querySceneFrame(prepared, 0).coverage, 'COVERED');
assert.equal(querySceneFrame(prepare([clip]), 0).coverage, 'UNKNOWN');
assert.throws(() => prepare([{...clip, elementRef: {kind: 'effect', id: 'custom', packId: 'missing'}}], registries), /Unknown effect owner/);
assert.equal(resolveAnimationReferences('fade-up', 'entrance', registries)[0].component, registries.animations['fade-up']);
const animated = composeAnimations({clip, refs: 'fade-up', phase: 'entrance', content: 'preserved',
  theme, fps: 30, elapsedFrames: 1, registries});
assert.equal(renderToStaticMarkup(animated), '<mark>preserved</mark>');
assert.equal(resolveElementRegistries(theme).animations['fade-up'], ANIMATION_REGISTRY['fade-up']);
assert.notEqual(registries.animations['fade-up'], ANIMATION_REGISTRY['fade-up']);
assert.throws(() => resolveAnimationReferences('missing', 'entrance', registries), /Unknown animation/);
const pair = [{...clip, transition: {id: 'fade', params: {opaque: ['transition']}}}, {...clip, id: 'next', at: 2}];
assert.equal(prepare(pair, registries).tracks[0].prepared.plans[1].drawStart, 52);
assert.deepEqual(globalThis.transitionProbe.params, {durationFrames: 8, opaque: ['transition']});
assert.equal(resolveElementRegistries(theme).transitions.fade, TRANSITION_REGISTRY.fade);
assert.notEqual(registries.transitions.fade, TRANSITION_REGISTRY.fade);
assert.throws(() => prepare([{...clip, transition: 'missing'}, {...clip, id: 'next', at: 2}], registries), /Unknown transition/);

const video = {id: 'test', width: 1920, height: 1080, fps: 30, durationInFrames: 60,
  defaultProps: {}, folderName: null, parentFolderName: null, component: () => null,
  nonce: [], schema: null, calculateMetadata: null, stack: null};
const render = (content) => renderToStaticMarkup(createElement(Internals.CompositionManager.Provider, {
  value: {compositions: [video], currentCompositionMetadata: null, folders: [],
    canvasContent: {type: 'composition', compositionId: 'test'}},
}, createElement(Internals.TimelineContext.Provider, {
  value: {frame: {test: 1}, playing: false, imperativePlaying: {current: false}, audioAndVideoTags: {current: []}},
}, createElement(Internals.CanUseRemotionHooksProvider, null, content))));
const html = render(createElement(TimelineComposition, {timeline: {theme: 'default', tracks: [track],
  clips: [{...clip, entrance: 'fade-up'}]}, assets: {assets: {}}, registries}));
assert.match(html, /<mark>/);
assert.match(html, /custom element/);
assert.match(html, /preserve/);
console.log('Built package custom component, capability, animation, transition and isolation OK');
