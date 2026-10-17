// Real source transpilation + React server layout; no browser or resource stub.
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {createRequire} = require('node:module');
const [repo, dependencyRoot, input] = process.argv.slice(2);
const dependencies = createRequire(path.join(dependencyRoot, 'package.json'));
const ts = dependencies('typescript');
const React = dependencies('react');
const {renderToStaticMarkup} = dependencies('react-dom/server');
const remotion = dependencies('remotion');
const cache = new Map();
function load(filename) {
  filename = path.resolve(filename);
  if (cache.has(filename)) return cache.get(filename).exports;
  const module = {exports: {}};
  cache.set(filename, module);
  const compiled = ts.transpileModule(fs.readFileSync(filename, 'utf8'), {
    fileName: filename, compilerOptions: {
      module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2020,
      jsx: ts.JsxEmit.ReactJSX, esModuleInterop: true,
    },
  });
  function boundRequire(id) {
    if (id === 'remotion') return {...remotion, useCurrentFrame: () => 0};
    if (id.startsWith('.')) {
      const target = path.resolve(path.dirname(filename), id);
      return load([target, target + '.ts', target + '.tsx'].find(f => fs.existsSync(f)));
    }
    return dependencies(id);
  }
  vm.runInThisContext(`(function(require,module,exports){${compiled.outputText}\n})`, {filename})(
    boundRequire, module, module.exports);
  return module.exports;
}
const AmType = load(path.join(repo,
  'astrid/packs/astrid_motion/rendering/elements/effects/am-type/component.tsx')).default;
const {clipOpacity} = load(path.join(repo,
  'remotion/vendor/timeline-composition/typescript/src/lib/clip-look.ts'));
const clips = JSON.parse(fs.readFileSync(input, 'utf8'));
const results = clips.map(clip => {
  const element = AmType({clip, params: clip.params, fps: 30});
  return {html: renderToStaticMarkup(element), style: element.props.style,
    opacity: clipOpacity(clip, 0, 30)};
});
console.log(JSON.stringify(results));
