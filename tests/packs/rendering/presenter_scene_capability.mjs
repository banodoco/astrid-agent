// Actual discovered AM presenter through current host maps and registered SSR.
// These inspect markup and declared regions, never decoded pixels or lip-sync statistics.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFileSync} from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {build} from 'esbuild';
import {createElement as h} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {Internals} from 'remotion';
import {DEFAULT_THEME, TimelineComposition, compileCanonicalTransitions, toRuntimeTheme, prepareCompositionScene, getTimelineDurationInFrames} from '@banodoco/timeline-composition';
import {querySceneFrame} from '@banodoco/timeline-composition/evaluation';
import {remotionPrimitiveAliasesFor} from './webpack-alias.mjs';

Error.stackTraceLimit = 0;
const project = process.cwd();
const presenter = '../astrid/packs/astrid_motion/rendering/elements/effects/am-presenter';
const aliases = remotionPrimitiveAliasesFor(project)({}).resolve.alias;
const bundle = await build({stdin: {contents: `
export {resolveHostRegistries} from './src/host-registries';
export * from '${presenter}/motion';
export * from '${presenter}/presenter-core';
export {default as Presenter} from '${presenter}/component';`, resolveDir: project},
bundle: true, write: false, platform: 'node', format: 'esm', jsx: 'automatic',
nodePaths: [path.join(project, 'node_modules')], logLevel: 'silent',
plugins: [{name: 'renderer-aliases', setup(builder) {
  builder.onResolve({filter: /^[^./]/}, async args => {
    if (args.pluginData?.resolvingPackage) return;
    const alias = Object.keys(aliases).find(key => args.path === key || args.path.startsWith(`${key}/`));
    if (alias) return builder.resolve(path.join(aliases[alias], args.path.slice(alias.length).split('?')[0]),
      {kind: args.kind, resolveDir: project});
    const resolved = await builder.resolve(args.path, {kind: args.kind, resolveDir: args.resolveDir,
      pluginData: {resolvingPackage: true}});
    if (resolved.errors.length) return {errors: resolved.errors};
    return {path: path.isAbsolute(resolved.path) ? pathToFileURL(resolved.path).href : resolved.path, external: true};
  });
}}]});
export const source = await import(`data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`);
// The current host has no native-scene adapter. Call the public evaluator with
// the same generated maps and visual track order used by TimelineComposition.
export const prepareHostScene = (props, b) => {
  const tracks = [...props.timeline.tracks].reverse().filter(track => track.kind === 'visual')
    .map(track => ({track, clips: props.timeline.clips.filter(clip => clip.track === track.id)}));
  const theme = toRuntimeTheme(props.theme);
  return prepareCompositionScene(tracks, props.assets, b.fps, theme, b.width, b.height,
    undefined, undefined, {}, '', {}, source.resolveHostRegistries(theme), props.compiledTransitions);
};
const basis = {width: 1920, height: 1080, fps: 30};
// Composition 0.0.13's package default is Agent's navy theme. The historical
// SSR hashes intentionally use this explicit pre-12 theme so the presenter
// preservation check does not silently turn an old fixture into a new default.
assert.deepEqual(DEFAULT_THEME, JSON.parse(readFileSync(path.join(project, 'builtin-default-theme.json'), 'utf8')));
const theme = {
  id: 'banodoco-default',
  visual: {
    color: {fg: '#ffffff', bg: '#000000', accent: '#ffffff'},
    type: {families: {heading: 'Georgia, serif', body: 'Georgia, serif'},
      size: {base: 64, small: 36, large: 96}, weight: {normal: 400, bold: 700}, lineHeight: 1.1},
    motion: {fadeMs: 250}, canvas: {width: 1920, height: 1080, fps: 30},
  },
};
const runtimeTheme = toRuntimeTheme(theme);
const registries = source.resolveHostRegistries(runtimeTheme);
const owner = {kind: 'effect', id: 'am-presenter', packId: 'astrid_motion'};
const clip = (params = {}, extra = {}) => ({id: 'presenter', track: 'overlay', clipType: 'am-presenter',
  at: 0, hold: 2, params, elementRef: owner, ...extra});
const propsFor = (clips, b = basis) => {
  const timeline = {tracks: [...new Set(clips.map(c => c.track))].reverse().map(id => ({id, kind: 'visual'})), clips};
  const tracks = [...timeline.tracks].reverse().map(track => ({track, clips: clips.filter(c => c.track === track.id)}));
  return {timeline, theme, assets: {assets: {}},
    compiledTransitions: compileCanonicalTransitions(tracks, b.fps, runtimeTheme, registries)};
};
const render = (props, frame, b = basis, selectedRegistries = registries, Component = TimelineComposition) => {
  const metadata = {id: 'presenter', component: Component, defaultProps: {}, ...b,
    durationInFrames: 300, folderName: null, parentFolderName: null, nonce: [], schema: null,
    calculateMetadata: null, stack: null};
  return renderToStaticMarkup(h(Internals.CompositionManager.Provider, {value: {
    compositions: [metadata], currentCompositionMetadata: null, folders: [],
    canvasContent: {type: 'composition', compositionId: 'presenter'},
  }}, h(Internals.TimelineContext.Provider, {value: {frame: {presenter: frame}, playing: false,
    imperativePlaying: {current: false}, audioAndVideoTags: {current: []}}},
  h(Internals.CanUseRemotionHooksProvider, null, h(Component, {...props, registries: selectedRegistries})))));
};
const query = (c, frame, b = basis) => {
  const props = propsFor([c], b);
  return querySceneFrame(prepareHostScene(props, b, {}), frame).layers[0];
};
const words = [[0, .2], [.3, .5], [.8, 1], [1.3, 1.5]];
const snapshots = () => {
  const cases = {
    defaults: {},
    empty: {chrome: 'none', blinkEvery: 0},
    poses: {words, chrome: 'none', blinkEvery: 0},
    clamps: {zoom: 99, focus: {x: -100, y: 999}, mouth: {x: -20, y: 900, w: 0}, words, bob: 9},
    malformed: {mouth: {x: 'bad', y: null, w: 900}, eyes: [null, {x: -1, y: 999, w: 0}],
      words: [null, [], [1, 0], ['0', 1], [.4, .6], [0, .2]], seed: -8.7},
    focus: {zoom: 2, focus: {x: 180.6, y: 110.2}, words, mouth: {x: 155.8, y: 107.2, w: 21.6}},
    push: {zoom: 1, push: {to: 3.5, frames: 28, at: 2}, focus: {x: 191.2, y: 110.4}, words},
    punch: {zoom: 3, punchAt: [1, 10], words},
    bob: {bob: 1, words: [[0, .1], [.2, .3], [.4, .5], [.6, .7]], seed: 9},
    mosaic: {words, mosaicIn: {frames: 8}, mosaicOut: {frames: 8}},
    chrome: {words, chrome: 'label', label: 'Take', chip: {text: 'one', swapTo: 'two', swapAt: 2}},
  };
  return Object.fromEntries([30, 24000 / 1001].flatMap(fps => Object.entries(cases).map(([name, params]) => {
    const b = {...basis, fps};
    const props = propsFor([clip(params)], b);
    return [`${fps}:${name}`, [0, 1, 2, 3, 5, 6, 7, 8, 10, 15, 29, 47, 59].map(frame =>
      createHash('sha256').update(render(props, frame, b)).digest('hex'))];
  })));
};

const mode = path.resolve(process.argv[1]) === path.join(project, 'query.mjs') ? process.argv[2] : undefined;
if (mode === 'snapshots') {
  console.log(JSON.stringify(snapshots()));
} else if (mode) {
  assert.ok(['states', 'geometry', 'unknowns', 'clock', 'registration'].includes(mode));
  const {MOUTH_LEVEL, motionAt, presenterAt, mouthStateAt} = source;
  const pixelRect = r => {
    const {left: x, top: y, width, height} = r;
    return {x, y, width, height};
  };
  const erase = (c, frame, fps) => pixelRect(source.pixelGeometry(
    source.mouthEraseRect(source.mouthAnchor(c.params)), presenterAt(c.params, frame, fps).view));
  const stateRegion = (c, frame, fps) => pixelRect(source.pixelGeometry(
    source.mouthStateRect(c.params), presenterAt(c.params, frame, fps).view));
  const support = (c, frame, b = basis) => source.Presenter.prepareSceneCapability({clip: c, ...b,
    rendererIdentity: 'registered-ssr', fontDigests: {}}).frame(frame);
  const drawnOverlay = (c, frame, b = basis) => render({clip: c, params: c.params, fps: b.fps}, frame, b,
    registries, source.Presenter);
  const px = value => value === 0 ? '0' : `${value}px`;
  const eraseStyle = rect => `position:absolute;left:${px(rect.x)};top:${px(rect.y)};width:${px(rect.width)};height:${px(rect.height)};background:`;
  const assertMotion = (c, frame, b = basis, layer = query(c, frame, b), supplied = frame) => {
    assert.deepEqual(layer.diagnostics.motion, motionAt(c.params, supplied, b.fps));
    const state = mouthStateAt(presenterAt(c.params, supplied, b.fps).timing, supplied);
    assert.equal(layer.diagnostics.motion.mouth, MOUTH_LEVEL[state]);
    return state;
  };

  if (mode === 'states') {
    assert.deepEqual(MOUTH_LEVEL, {closed: 0, half: .5, open: 1});
    const seen = new Set();
    const inputs = [undefined, null, [], 'bad', [null, [], [1], [2, 1], ['0', 1], [0, null]],
      [[.8, 1], [0, .1], [.2, .3]], [[-.1, .1], [.1001, .101]], [[.1, .10001]],
      [[0, .1], [.2, .3]], [[0, .1], [.3, .4]], [[0, .1], [.22, .3]]];
    for (const fps of [30, 24000 / 1001, 30000 / 1001]) {
      const b = {...basis, fps};
      for (const words of inputs) for (const seed of [0, 7, -8.7]) {
        const c = clip({words, seed, chrome: 'none', blinkEvery: 0});
        const props = propsFor([c], b);
        const scene = prepareHostScene(props, b, {});
        for (let frame = 0; frame < Math.round(fps * 1.5); frame++) {
          const layer = querySceneFrame(scene, frame).layers[0];
          const state = assertMotion(c, frame, b, layer);
          seen.add(state);
          const html = render(props, frame, b);
          assert.ok(html.includes(drawnOverlay(c, frame, b)), `registered drawing ${fps}:${frame}`);
          assert.deepEqual(layer.diagnostics.mouth.rect, stateRegion(c, frame, fps));
          const mouth = erase(c, frame, fps);
          assert.equal(html.includes(eraseStyle(mouth) + '#EDB98E'), state !== 'closed');
        }
      }
    }
    assert.deepEqual([...seen].sort(), ['closed', 'half', 'open']);
    // Actual rounded onset/end and the <= .12 second gap hold are retained.
    const held = clip({words: [[.1, .2], [.3, .4]], seed: 7});
    assert.equal(query(held, 2).diagnostics.motion.mouth, 0);
    const last = query(held, 5).diagnostics.motion.mouth;
    for (const f of [6, 7, 8]) assert.equal(query(held, f).diagnostics.motion.mouth, last);
    assert.equal(query(held, 12).diagnostics.motion.mouth, 0);
    const long = clip({words: [[.1, .2], [.4, .5]], seed: 7});
    for (const f of [6, 7, 8, 9, 10, 11]) assert.equal(query(long, f).diagnostics.motion.mouth, 0);
  }

  if (mode === 'geometry') {
    const inputs = [{}, {mouth: {x: 151}}, {mouth: {x: 151, y: 104}}, {mouth: {x: 'bad', y: null, w: 900}}, {mouth: null}, {mouth: {x: -20, y: 999, w: 0}},
      {mouth: {x: 999, y: -1, w: 99}}, {mouth: {x: 152.5, y: 103.7, w: 20.6}},
      {zoom: 2, focus: {x: 190.4, y: 120.7}},
      {zoom: 99, focus: {x: -100, y: 999}},
      {push: {to: 3.5, frames: 28, at: 2}, focus: {x: 191.2, y: 110.4}},
      {zoom: 3, punchAt: [1, 10]}, {bob: 1, seed: 9}];
    for (const fps of [30, 24000 / 1001]) for (const params of inputs) {
      const b = {...basis, fps};
      const c = clip({words, chrome: 'none', blinkEvery: 0, ...params});
      const props = propsFor([c], b);
      const scene = prepareHostScene(props, b, {});
      for (const frame of [0, 1, 2, 3, 5, 6, 7, 8, 10, 15, 29]) {
        const layer = querySceneFrame(scene, frame).layers[0];
        assertMotion(c, frame, b, layer);
        assert.deepEqual(layer.diagnostics.mouth, support(c, frame, b).diagnostics.mouth);
        assert.equal(layer.diagnostics.mouth.status, 'known');
        const state = mouthStateAt(presenterAt(c.params, frame, fps).timing, frame);
        const html = render(props, frame, b);
        assert.deepEqual(layer.diagnostics.mouth.rect, stateRegion(c, frame, fps));
        if (state !== 'closed') assert.ok(html.includes(eraseStyle(erase(c, frame, fps)) + '#EDB98E'));
        assert.equal(layer.visible, 'unknown');
        assert.equal(layer.opaqueSupport, 'none');
        assert.equal(layer.diagnostics.face, undefined);
        assert.equal(layer.diagnostics.text, undefined);
      }
    }
    assert.deepEqual(query(clip({mouth: {x: 151, y: 104, w: 19}}), 0).diagnostics.mouth.rect,
      {x: 894, y: 606, width: 138, height: 48});
    assert.deepEqual(query(clip({mouth: {x: 151, y: 104}}), 0).diagnostics.mouth.rect,
      {x: 894, y: 606, width: 138, height: 48});
    assert.deepEqual(source.mouthAnchor({mouth: {x: 151, y: 104}}), {x: 151, y: 104, w: 19});
    assert.deepEqual(source.mouthInspectionRect({mouth: {x: 151, y: 104}}), {x: 149, y: 101, w: 20, h: 8});
    assert.equal(source.mouthInspectionRect({}), null);
    assert.deepEqual(query(clip(), 0).diagnostics.mouth.rect,
      {x: 894, y: 606, width: 138, height: 48});
    const malformed = {mouth: {x: 'bad', y: null, w: 900}};
    assert.deepEqual(source.mouthAnchor(malformed), {x: 151, y: 104, w: 40});
    assert.deepEqual(motionAt(malformed, 0, basis.fps), motionAt({mouth: {x: 151, y: 104, w: 40}}, 0, basis.fps));
    assert.equal(source.mouthInspectionRect(malformed), null);
    assert.deepEqual(query(clip(malformed), 0).diagnostics.mouth.rect,
      {x: 894, y: 606, width: 264, height: 48});
    assert.equal(query(clip({mouth: {x: -1, y: 999, w: 0}}), 0).diagnostics.mouth.status, 'known');
    const small = {...basis, width: 960, height: 540};
    const layer = query(clip({mouth: {x: 151, y: 104, w: 19}}), 0, small);
    // Logical overlay pixels are not rescaled to an invented 320x180 viewport.
    assert.deepEqual(layer.diagnostics.mouth.rect, {x: 894, y: 606, width: 138, height: 48});
    assert.deepEqual(layer.bounds.rect, {x: 0, y: 0, width: 960, height: 540});
  }

  if (mode === 'unknowns') {
    const c = clip({chrome: 'none', blinkEvery: 0});
    const props = propsFor([c]); // deliberately no plate, no assets
    assert.equal(render(props, 0).includes('<img'), false);
    assert.equal(drawnOverlay(c, 0), '<div style="position:absolute;inset:0;overflow:hidden;pointer-events:none"></div>');
    const s = support(c, 0);
    assert.equal(s.presence, 'unknown');
    assert.equal(s.opaqueSupport, 'none');
    assert.equal(s.diagnostics.mouth.status, 'known'); // normalized footprint, not occupied pixels
    assert.equal(querySceneFrame(prepareHostScene(props, basis, {}), 0).coverage, 'UNKNOWN');
    for (const missing of [{asset: 'missing'}, {params: {...c.params, src: '/not-a-presenter-asset.png'}}]) {
      const other = {...c, ...missing};
      assert.deepEqual(support(other, 0), s);
      assert.equal(query(other, 0).diagnostics.face, undefined);
    }
    for (const ramps of [{mosaicIn: {frames: 8}}, {mosaicOut: {frames: 8}},
      {mosaicIn: {frames: 8, steps: [48, 6]}}]) {
      const m = clip({...ramps, mouth: {x: 151, y: 104, w: 19}, words, chrome: 'none', blinkEvery: 0});
      for (const frame of [0, 1, 3, 4, 6, 7, 8, 52, 53, 54, 55, 56, 57, 58, 59]) {
        const layer = query(m, frame);
        assertMotion(m, frame, basis, layer);
        const direct = support(m, frame);
        assert.deepEqual(layer.diagnostics.mouth, direct.diagnostics.mouth);
        if (layer.diagnostics.mouth.status === 'unknown') {
          assert.match(layer.diagnostics.mouth.reason, /mosaic/);
          assert.equal(drawnOverlay(m, frame), drawnOverlay(c, 0));
        }
      }
    }
    const mosaic = {mosaicIn: {frames: 8}, mouth: {x: 151, y: 104, w: 19}};
    assert.equal(query(clip(mosaic), 0).diagnostics.mouth.status, 'unknown');
    assert.equal(query(clip(mosaic), 6).diagnostics.mouth.status, 'known');
    for (const extra of [{cropLeft: .1}, {entrance: 'fade-up'}, {exit: 'fade-up'},
      {modifiers: [{id: 'unknown', element: {packId: 'missing', kind: 'modifier', id: 'missing'}, params: {}}]}]) {
      const outer = query({...c, ...extra}, 2);
      assert.equal(outer.diagnostics.mouth.status, 'unknown');
      assertMotion({...c, ...extra}, 2, basis, outer);
    }
  }

  if (mode === 'clock') {
    for (const fps of [30, 24000 / 1001]) for (const speed of [.5, 1, 2]) {
      const b = {...basis, fps};
      const outgoing = clip({chrome: 'none'}, {id: 'out', hold: 1});
      const incoming = clip({words, bob: 1, punchAt: [2, 8], chrome: 'none', blinkEvery: 0},
        {id: 'in', at: 1, from: 17, to: 18, hold: 1, speed, transition: {type: 'fade', duration: .4}});
      const props = propsFor([outgoing, incoming], b);
      const scene = prepareHostScene(props, b, {});
      let preroll = false;
      let remapDistinguished = speed === 1;
      const end = getTimelineDurationInFrames(props.timeline, fps);
      for (let frame = 0; frame <= end; frame++) {
        const row = querySceneFrame(scene, frame);
        const html = render(props, frame, b);
        for (const c of [outgoing, incoming]) {
          const layer = row.layers.find(l => l.clipId === c.id);
          assert.equal(html.includes(`data-clip-id="${c.id}"`), Boolean(layer));
          if (!layer) continue;
          const supplied = Math.floor(layer.contentFrame * (c.speed ?? 1));
          assertMotion(c, frame, b, layer, supplied);
          assert.ok(html.includes(drawnOverlay(c, supplied, b)), `actual draw clock ${fps}:${speed}:${frame}`);
          if (c === incoming && frame < Math.round(c.at * fps)) preroll = true;
          if (c === incoming && supplied !== layer.contentFrame
            && motionAt(c.params, supplied, fps).mouth !== motionAt(c.params, layer.contentFrame, fps).mouth) remapDistinguished = true;
        }
      }
      assert.equal(preroll, true);
      assert.equal(remapDistinguished, true);
      assert.ok(querySceneFrame(scene, end - 1).layers.some(layer => layer.clipId === 'in'));
      assert.equal(querySceneFrame(scene, end).layers.length, 0);
    }
  }

  if (mode === 'registration') {
    const c = clip();
    assert.equal(query(c, 0).diagnostics.motion.mouth, 0);
    assert.deepEqual(query({...c, elementRef: undefined}, 2).diagnostics, query(c, 2).diagnostics);
    assert.throws(() => query({...c, elementRef: {...owner, packId: 'missing'}}, 0), /Unknown effect owner/);
    assert.throws(() => query({...c, elementRef: {...owner, packId: 'rendering'}}, 0), /Unknown effect owner/);
    // The normal owner-qualified dispatch forwards the actual component's hook.
    assert.deepEqual(query(c, 2).diagnostics, support(c, 2).diagnostics);
    const props = propsFor([c]);
    const tracks = props.timeline.tracks.map(track => ({track, clips: props.timeline.clips}));
    const scene = prepareCompositionScene(tracks, props.assets, 30, runtimeTheme, 1920, 1080,
      undefined, undefined, {}, '', {}, registries, props.compiledTransitions);
    assert.deepEqual(querySceneFrame(scene, 2).layers[0].diagnostics, query(c, 2).diagnostics);
  }
  console.log(JSON.stringify({ok: true, case: mode}));
}
