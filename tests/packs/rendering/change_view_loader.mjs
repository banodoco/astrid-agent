// Test-only aliases: run against the explicitly selected immutable EVAL source.
import {readFile} from 'node:fs/promises';
import {createRequire} from 'node:module';
import {fileURLToPath, pathToFileURL} from 'node:url';
const root = process.env.ASTRID_CHANGE_VIEW_EVAL_ROOT;
const dependencies = process.env.ASTRID_CHANGE_VIEW_NODE_MODULES;
if (!root || !dependencies) throw new Error('Explicit EVAL source and existing dependencies required');
const require = createRequire(pathToFileURL(`${dependencies}/../package.json`));
const ts = require(`${dependencies}/typescript`);
export async function resolve(specifier, context, next) {
  const aliases = {
    '@workspace-effects/': `${process.cwd()}/astrid/packs/rendering/rendering/elements/effects/`,
    '@workspace-transitions/': `${process.cwd()}/astrid/packs/rendering/rendering/elements/transitions/`,
    '@workspace-animations/': `${dependencies}/../vendor/banodoco-animations/`,
    '@banodoco/timeline-theme-2rp/': `${dependencies}/../vendor/timeline-theme-2rp/`,
  };
  for (const [prefix, folder] of Object.entries(aliases)) if (specifier.startsWith(prefix)) {
    return {url: pathToFileURL(folder + specifier.slice(prefix.length) + '.tsx').href, shortCircuit: true};
  }
  const source = specifier === '@banodoco/timeline-composition/evaluation' ? 'evaluation'
    : specifier === '@banodoco/timeline-composition' ? 'index' : null;
  if (source) return {url: pathToFileURL(`${root}/typescript/src/${source}.ts`).href, shortCircuit: true};
  try { return await next(specifier, context); } catch (error) {
    if (error.code === 'ERR_MODULE_NOT_FOUND' && /^\.{1,2}\//.test(specifier)) {
      for (const suffix of ['.ts', '.tsx']) {
        try { return await next(specifier.replace(/\.js$/, '') + suffix, context); } catch {}
      }
    }
    if (['ERR_MODULE_NOT_FOUND', 'ERR_PACKAGE_PATH_NOT_EXPORTED'].includes(error.code)
        && !specifier.startsWith('.') && !specifier.startsWith('/') && !specifier.includes(':')) {
      return next(specifier, {...context, parentURL: pathToFileURL(`${dependencies}/../package.json`).href});
    }
    throw error;
  }
}
export async function load(url, context, next) {
  if (/\.[cm]?tsx?$/.test(url)) return {format: 'module', shortCircuit: true,
    source: ts.transpileModule(await readFile(new URL(url), 'utf8'), {fileName: fileURLToPath(url),
      compilerOptions: {module: ts.ModuleKind.ESNext, target: ts.ScriptTarget.ES2022,
        jsx: ts.JsxEmit.ReactJSX}}).outputText};
  return next(url, context);
}
