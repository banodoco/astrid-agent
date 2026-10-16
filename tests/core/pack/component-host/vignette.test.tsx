import assert from 'node:assert/strict';
import {test} from 'node:test';
import {pathToFileURL} from 'node:url';
import {createElement, isValidElement} from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {bindVignette, attenuationImage} from '../../../../examples/packs/clip_looks/extensions/timeline.component/vignette';
import {readFileSync} from 'node:fs';

// Read-only owner-supplied host, explicitly selected by the evidence command.
const root = process.env.ASTRID_MODIFIER_HOST_ROOT;
if (!root) throw new Error('ASTRID_MODIFIER_HOST_ROOT must select the admitted Composition source');
const host = await import(pathToFileURL(`${root}/typescript/src/modifiers.ts`).href);
const {composeClipLook} = await import(pathToFileURL(`${root}/typescript/src/ClipLook.tsx`).href);
const {DEFAULT_THEME} = await import(pathToFileURL(`${root}/typescript/src/ThemeContext.tsx`).href);
const {prepareScene, querySceneFrame} = await import(pathToFileURL(`${root}/typescript/src/scene-evaluation.ts`).href);
const binding = JSON.parse(readFileSync(process.env.ASTRID_COMPONENT_BINDING!, 'utf8'));
const declaration = bindVignette(host, binding.descriptor);
const preservedDeclaration = bindVignette(host, {...binding.descriptor, bounds: 'preserve'});
const element = binding.identity;
const registry = {[element]: declaration};
const clip = {id: 'owning-occurrence', track: 'visual', at: 2, hold: 2, keyframeOrigin: 0.25};
const bounds = {status: 'known', rect: {x: 10, y: 20, width: 80, height: 40}};
const theme = {...DEFAULT_THEME, color: DEFAULT_THEME.visual.color,
  type: DEFAULT_THEME.visual.type, motion: DEFAULT_THEME.visual.motion};
const context = {clip, fps: 20, frame: 10, keyframeFrame: 5, keyframeTime: 0.25,
  globalFrame: 50, bounds, theme};
const content = createElement('img', {src: 'target-with-alpha.png', style: {opacity: 0.4}});
const instance = (id: string, strength = 0.5) => ({id, element, params: {strength}});
const compose = (instances: unknown[], overrides = {}) => host.composeModifiers({instances, registry, context, content, ...overrides});
const node = (value: any): any => {assert.ok(isValidElement(value)); return value;};
const refuses = (fn: () => unknown, code: string) => assert.throws(fn,
  (error: any) => error instanceof host.ModifierAdmissionError && error.code === code);

test('canonical omitted bounds maps to internal unknown while explicit preserve stays preserve', () => {
  assert.equal(Object.hasOwn(binding.descriptor, 'bounds'), false);
  assert.equal(declaration.element, element);
  assert.equal(declaration.kind, 'modifier');
  assert.deepEqual([declaration.bounds, declaration.alpha, declaration.support], ['unknown', 'preserve', 'preserve']);
  assert.equal(preservedDeclaration.bounds, 'preserve');
  assert.equal(Object.hasOwn(binding.descriptor, 'bounds'), false);
  assert.equal(typeof binding.sources[binding.descriptor.implementation.path].sha256, 'string');
});

test('real host wraps one subtree B(A(content)) with stable identities and one evaluated clock/bounds', () => {
  const b = node(compose([instance('A'), instance('B')]));
  const a = node(b.props.children);
  assert.equal(b.key, 'B'); assert.equal(a.key, 'A');
  assert.strictEqual(a.props.children, content);
  assert.strictEqual(a.props.context, context); assert.strictEqual(b.props.context, context);
  const reordered = node(compose(JSON.parse(JSON.stringify([instance('B'), instance('A')]))));
  assert.equal(reordered.key, 'A');
});

test('unknown output bounds keeps the named instance finding and known target geometry', () => {
  const findings: unknown[] = [];
  const rendered = renderToStaticMarkup(compose([instance('unknown-output')], {
    onFinding: (finding: unknown) => findings.push(finding),
  }));
  assert.match(rendered, /filterUnits="userSpaceOnUse"/);
  assert.match(rendered, /x="10" y="20" width="80" height="40"/);
  assert.deepEqual(findings, [{code: 'unknown-modifier-bounds', clipId: clip.id,
    instanceId: 'unknown-output', element, reason: 'Modifier declares unknown output bounds'}]);
});

test('actual shared evaluator keeps unknown output coverage from hiding lower content', () => {
  const lowerTrack = {id: 'lower', kind: 'visual', label: 'Content', fit: 'cover'};
  const upperTrack = {id: 'plate', kind: 'visual', label: 'Picture', fit: 'cover'};
  const assets = {assets: {photo: {file: 'photo.jpg', type: 'image/jpeg', resolution: '200x100'}}};
  const lower = {id: 'lower-content', track: 'lower', asset: 'photo', at: 0, hold: 2};
  const modified = {id: 'modified', track: 'plate', asset: 'photo', at: 0, hold: 2,
    modifiers: [instance('unknown-output')]};
  const prepared = prepareScene([
    {track: lowerTrack, clips: [lower], transitions: []},
    {track: upperTrack, clips: [modified], transitions: []},
  ], assets, 10, 200, 100, {modifiers: registry, roles: {'lower-content': 'content', modified: 'picture'}});
  const answer = querySceneFrame(prepared, 1);
  const lowerLayer = answer.layers.find((layer: any) => layer.clipId === lower.id)!;
  const modifiedLayer = answer.layers.find((layer: any) => layer.clipId === modified.id)!;
  assert.equal(answer.coverage, 'UNKNOWN');
  assert.equal(lowerLayer.visible, true);
  assert.equal(modifiedLayer.targetBounds.status, 'known');
  assert.deepEqual(modifiedLayer.targetBounds, {status: 'known', rect: {x: 0, y: 0, width: 200, height: 100}});
  assert.equal(modifiedLayer.bounds.status, 'unknown');
  assert.equal(modifiedLayer.coverage, 'unknown');
  assert.equal(modifiedLayer.fullFrame, false);
  assert.equal(modifiedLayer.opacity, null);
  assert.deepEqual(answer.findings, [{code: 'unknown-modifier-bounds', clipId: modified.id,
    instanceId: 'unknown-output', element, reason: 'Modifier declares unknown output bounds'}]);
});

test('zero strength is exact child markup, including existing source alpha', () => {
  assert.equal(renderToStaticMarkup(compose([instance('zero', 0)])), renderToStaticMarkup(content));
});

test('nonzero vignette has fixed radial attenuation and source-alpha-preserving arithmetic in target bounds', () => {
  const markup = renderToStaticMarkup(compose([instance('darken', 0.5)]));
  assert.match(markup, /filterUnits="userSpaceOnUse"/);
  assert.match(markup, /x="10" y="20" width="80" height="40"/);
  assert.match(markup, /in="SourceGraphic" in2="attenuation" operator="arithmetic" k1="1" k2="0" k3="0" k4="0"/);
  assert.match(markup, /color-interpolation-filters="sRGB"/);
  assert.equal((markup.match(/<img /g) ?? []).length, 1);
  const svg = decodeURIComponent(attenuationImage(0.5).split(',')[1]);
  assert.match(svg, /offset="50%" stop-color="white"/);
  assert.match(svg, /offset="100%" stop-color="rgb\(50%,50%,50%\)"/);
  assert.doesNotMatch(svg, /opacity|script|https:|http:.*href/);
});

test('real host resolves static params and rejects invalid or new animated carriers', () => {
  for (const strength of [-1, 2, NaN, '0.5', {from: 0, to: 1}]) {
    assert.throws(() => compose([{id: 'bad', element, params: {strength}}]), /finite static number/);
  }
  assert.throws(() => compose([{id: 'bad', element, params: {strength: 0.5, radius: 2}}]), /finite static number/);
});

test('real host emits named target-unknown finding and explicitly refuses unsupported host/identity', () => {
  const findings: unknown[] = [];
  refuses(() => compose([instance('A')], {context: {...context, bounds: {status: 'unknown', reason: 'No target declaration'}},
    onFinding: (finding: unknown) => findings.push(finding)}), 'unknown-target-bounds');
  assert.deepEqual(findings, [{code: 'unknown-target-bounds', clipId: clip.id, reason: 'No target declaration'}]);
  refuses(() => compose([instance('A')], {host: 'remote'}), 'unsupported-host');
  refuses(() => compose([instance('A')], {registry: {[element]: {...declaration, element: 'substituted'}}}), 'invalid-declaration');
  refuses(() => compose([instance('A'), instance('A')]), 'invalid-instance-id');
  for (const bounds of [undefined, null, 'expand', 'unknown']) {
    assert.throws(() => bindVignette(host, {...binding.descriptor, bounds} as any), /Invalid vignette bounds declaration/);
  }
  assert.throws(() => bindVignette(host, {...binding.descriptor, alpha: 'unknown'}), /Invalid vignette declaration/);
  assert.throws(() => bindVignette(host, {...binding.descriptor, support: 'unknown'}), /Invalid vignette declaration/);
});

test('both supported host labels use the same vignette implementation', () => {
  for (const label of ['composition', 'app']) {
    const tree = node(compose([instance(label)], {host: label}));
    assert.equal(tree.props.params.strength, 0.5);
  }
});

test('actual ClipLook keeps cropped child inside vignette and fade outside, without touching audio', () => {
  const target = {...clip, modifiers: [instance('A')], opacity: 0.4, effects: {fade_in: 1},
    cropLeft: 0.25, volume: 0.7};
  const source = JSON.stringify(target);
  const tree = node(composeClipLook({clip: target, fps: 20, kind: 'element', children: content,
    modifierRegistry: registry, modifierBounds: bounds}, 10, theme));
  const modifier = node(tree.props.children);
  assert.equal(modifier.key, 'A');
  assert.equal(tree.props.style.opacity, 0.2);
  assert.equal(modifier.props.context.frame, 10);
  assert.equal(modifier.props.context.keyframeFrame, 5);
  assert.equal(modifier.props.context.keyframeTime, 0.25);
  assert.strictEqual(modifier.props.context.bounds, bounds);
  assert.match(node(modifier.props.children).props.style.clipPath, /25%/);
  assert.equal(JSON.stringify(target), source);
});
