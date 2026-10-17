// Source-bound Root execution only: no browser, frame, encode or capture.
import fs from 'node:fs';
import path from 'node:path';
import vm from 'node:vm';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';

const [repo, dependencyRoot, projectionPath, identity, phase] = process.argv.slice(2);
const dependencies = createRequire(path.join(dependencyRoot, 'package.json'));
const ts = dependencies('typescript');
const stage = JSON.parse(fs.readFileSync(projectionPath, 'utf8'));
const {componentAliases} = await import(pathToFileURL(path.join(repo, 'remotion/component-staging.mjs')));
const stageEnv = {ASTRID_REMOTION_COMPONENT_STAGE: JSON.stringify(stage)};
const remotionRoot = path.join(repo, 'remotion');
const config = componentAliases({}, remotionRoot, stageEnv);
// Remotion's real config loader transpiles ESM helpers into CommonJS. The
// explicit captured root must work there as well as in native ESM.
const transformed = dependencies('esbuild').transformSync(
  fs.readFileSync(path.join(remotionRoot, 'component-staging.mjs'), 'utf8'), {format: 'cjs', loader: 'js'}).code;
const commonjs = {exports: {}};
new Function('require', 'module', 'exports', transformed)(dependencies, commonjs, commonjs.exports);
assert.deepEqual(commonjs.exports.componentAliases({}, remotionRoot, stageEnv), config);
assert.throws(() => componentAliases({}, 'relative', stageEnv), /absolute/);
const checkedLoader = createRequire(import.meta.url)(path.join(repo, 'remotion/component-source-loader.cjs'));
if (phase === 'late-change') {
  const filename = Object.keys(stage.files).find(file => file.endsWith('.tsx'));
  fs.appendFileSync(filename, '\n// changed after alias setup\n');
}
const cache = new Map();
const composition = path.join(repo, 'remotion/vendor/timeline-composition/typescript/src');
const host = () => load(path.join(composition, 'lib/modifiers.tsx'));
const empty = () => null;
const compositionStub = {TimelineComposition: empty, getTimelineDurationInFrames: () => 1};
function load(filename) {
  filename = path.resolve(filename);
  if (cache.has(filename)) return cache.get(filename).exports;
  const module = {exports: {}};
  cache.set(filename, module);
  let source = fs.readFileSync(filename);
  if (Object.hasOwn(stage.files, filename)) {
    source = checkedLoader.call({resourcePath: filename, getOptions: () => ({root: stage.root, files: stage.files})}, source);
  }
  const js = ts.transpileModule(source.toString(), {fileName: filename, compilerOptions: {
    module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020,
    jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true,
  }}).outputText;
  const require = (id) => {
    if (id === '@astrid-component-bindings') return load(config.resolve.alias['@astrid-component-bindings$']);
    if (id === '@banodoco/timeline-composition/theme-api') return host();
    if (id === '@banodoco/timeline-composition') return compositionStub;
    if (id === 'remotion') return {Composition: empty, Sequence: empty, useVideoConfig: () => ({fps: 24})};
    if (id === '@remotion/media') return {Video: empty};
    if (filename.endsWith('/Root.tsx') && id.startsWith('./') && id !== './components') return new Proxy({}, {get: () => empty});
    if (id.startsWith('.') || path.isAbsolute(id)) {
      const target = path.resolve(path.dirname(filename), id);
      return load([target, target + '.ts', target + '.tsx'].find(f => fs.existsSync(f)));
    }
    return dependencies(id);
  };
  vm.runInThisContext(`(function(require,module,exports){${js}\n})`, {filename})(require, module, module.exports);
  return module.exports;
}
const {Root} = load(path.join(repo, 'remotion/src/Root.tsx'));
const root = Root();
const native = root.props.children.find(child => child.props.id === 'TimelineComposition');
const existing = {builtin: {element: 'builtin'}};
const rendered = native.props.component({timeline: {clips: []}, assets: {assets: {}}, modifierRegistry: existing});
const registry = rendered.props.children[0].props.modifierRegistry;
assert.equal(registry.builtin, existing.builtin);
assert.equal(registry[identity].element, identity);
assert.equal(typeof registry[identity].wrap, 'function');
const {componentBindings} = load(stage.module);
const binding = componentBindings[0];
const {bindComponentRegistry} = load(path.join(repo, 'remotion/src/components.ts'));
const React = dependencies('react');
const {renderToStaticMarkup} = dependencies('react-dom/server');
const context = {bounds: {status: 'known', rect: {x: 0, y: 0, width: 320, height: 180}}};
const instance = {id: 'native-source-probe', element: identity, params: {strength: 0.6}};
const content = React.createElement('span', null, 'original child');
const actual = registry[identity].wrap(instance, context, content);
assert.notEqual(actual.type, undefined);
assert.match(renderToStaticMarkup(actual), /original child/);
// J029's admitted binder uses the React-style Component spelling. Exercise
// that original shape even for the portable synthetic source fixture.
const Shade = ({children}) => React.createElement('div', null, children);
const makeBinding = (canonical) => ({...binding, module: {
  [binding.descriptor.implementation.export]: (api, descriptor) => api.declareModifier({
    kind: 'modifier', element: identity, hosts: descriptor.hosts,
    bounds: 'preserve', alpha: 'preserve', support: 'preserve',
    Component: canonical ? empty : Shade, ...(canonical ? {component: Shade} : {}),
    resolveParams: params => params,
  }),
}});
for (const canonical of [false, true]) {
  const declaration = bindComponentRegistry([makeBinding(canonical)])[identity];
  const wrapped = declaration.wrap(instance, context, content);
  assert.equal(wrapped.type, Shade);
  assert.equal(wrapped.props.children, content);
  assert.match(renderToStaticMarkup(wrapped), /original child/);
}
for (const invalid of [
  {...binding, identity: 'timeline.component/wrong.identity'},
  {...binding, implementation_digest: '0'.repeat(64)},
  {...binding, descriptor: {...binding.descriptor, implementation: {export: 'missing'}}},
  {...binding, module: {[binding.descriptor.implementation.export]: () => ({element: 'wrong'})}},
]) assert.throws(() => bindComponentRegistry([invalid]));
assert.throws(() => bindComponentRegistry([binding, binding]));
assert.deepEqual(bindComponentRegistry([]), {});
console.log(JSON.stringify({identity, key: binding.key, declaration: registry[identity].element,
  stagedModule: stage.module, importedSources: [...cache.keys()].filter(f => Object.hasOwn(stage.files, f))}));
