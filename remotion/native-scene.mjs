import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {build} from 'esbuild';
import {remotionPrimitiveAliasesFor} from './webpack-alias.mjs';

const loadHostSceneModule = async (project) => {
  const aliases = remotionPrimitiveAliasesFor(project)({}).resolve.alias;
  const result = await build({
    absWorkingDir: project, entryPoints: [path.join(project, 'src/host-scene.ts')],
    bundle: true, write: false, platform: 'node', format: 'esm',
    nodePaths: [path.join(project, 'node_modules')], logLevel: 'silent',
    plugins: [{name: 'renderer-aliases', setup(builder) {
      builder.onResolve({filter: /^[^./]/}, async (args) => {
        if (args.pluginData?.resolvingPackage) return;
        const alias = Object.keys(aliases).find(key => args.path === key || args.path.startsWith(`${key}/`));
        if (!alias) {
          const resolved = await builder.resolve(args.path, {kind: args.kind,
            resolveDir: args.resolveDir, pluginData: {resolvingPackage: true}});
          if (resolved.errors.length) return {errors: resolved.errors};
          return {path: path.isAbsolute(resolved.path) ? pathToFileURL(resolved.path).href : resolved.path,
            external: true};
        }
        const target = path.join(aliases[alias], args.path.slice(alias.length).split('?')[0]);
        return builder.resolve(target, {kind: args.kind, resolveDir: project});
      });
    }}],
  });
  // No executable output is written after launch binding. Sources, aliases,
  // esbuild and installed dependencies are the selected runtime's bound inputs.
  const entry = `data:text/javascript;base64,${Buffer.from(result.outputFiles[0].text).toString('base64')}`;
  return import(entry);
};

export const loadHostScene = async (project) => (await loadHostSceneModule(project)).prepareHostScene;
export const loadHostTransitionCompiler = async (project) => (await loadHostSceneModule(project)).compileHostTransitions;
