// Execute the generated dispatch and its revision-specific webpack imports.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import path from 'node:path';
import {createRequire} from 'node:module';
import {pathToFileURL} from 'node:url';

const [aliasModule, generatedFile, refsFile] = process.argv.slice(2);
const host = process.env.ASTRID_REMOTION_PROJECT_DIR ?? path.dirname(aliasModule);
const require = createRequire(path.join(host, 'package.json'));
const ts = require('typescript');
const {packElementAliases, remotionPrimitiveAliasesFor} = await import(pathToFileURL(aliasModule).href);
const aliases = packElementAliases();
const configuration = remotionPrimitiveAliasesFor(path.dirname(aliasModule))({});
const resolve = require('enhanced-resolve').create.sync({
  extensions: ['.ts', '.tsx', '.js'], alias: configuration.resolve.alias,
});
assert.equal(resolve(path.join(path.dirname(aliasModule), 'src'), './effects.generated'), generatedFile);
const compile = (file) => ts.transpileModule(fs.readFileSync(file, 'utf8'), {
  compilerOptions: {module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022},
}).outputText;
const moduleRequire = (specifier) => {
  if (specifier === 'react') return {createElement: (Component, props) => Component(props)};
  const alias = Object.keys(aliases).find((key) => specifier.startsWith(`${key}/`));
  assert.ok(alias, `missing webpack alias for ${specifier}`);
  const relative = specifier.slice(alias.length + 1).split('?')[0];
  const file = path.join(aliases[alias], `${relative}.tsx`);
  const module = {exports: {}};
  new Function('exports', 'require', compile(file))(module.exports, moduleRequire);
  return module.exports;
};
const module = {exports: {}};
new Function('exports', 'require', compile(generatedFile))(module.exports, moduleRequire);
const refs = JSON.parse(fs.readFileSync(refsFile, 'utf8'));
const results = refs.map((elementRef) => module.exports.EFFECT_REGISTRY.reveal({clip: {elementRef}}));
assert.deepEqual(refs.map((elementRef) => module.exports.EFFECT_REGISTRY.reveal.prepareSceneCapability({
  clip: {elementRef},
})), ['first', 'second']);
assert.throws(() => module.exports.EFFECT_REGISTRY.reveal({
  clip: {elementRef: {...refs[0], revision: `sha256:${'f'.repeat(64)}`}},
}), /Unknown effect owner/);
console.log(JSON.stringify(results));
