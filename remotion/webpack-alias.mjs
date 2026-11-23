import path from 'node:path';
import fs from 'node:fs';
import {fileURLToPath} from 'node:url';
import {createRequire} from 'node:module';
const MANIFEST_NAMES = ['pack.yaml', 'pack.yml', 'pack.json'];
const ELEMENT_KINDS = new Set(['effect', 'effects', 'animation', 'animations', 'transition', 'transitions']);
const CANONICAL_KIND = {
  effect: 'effects',
  effects: 'effects',
  animation: 'animations',
  animations: 'animations',
  transition: 'transitions',
  transitions: 'transitions',
};

const nativeModuleDir = () => {
  if (typeof import.meta.url !== 'string') {
    throw new Error('native ESM only; pass remotionRoot through remotionPrimitiveAliasesFor');
  }
  return path.dirname(fileURLToPath(import.meta.url));
};
const projectDirOf = (remotionRoot = nativeModuleDir()) => path.resolve(remotionRoot, '..');
const repoRootOf = projectDirOf;

const readText = (file) => {
  try {
    return fs.readFileSync(file, 'utf8');
  } catch (error) {
    if (error.code === 'ENOENT') return null;
    throw new Error(`cannot read manifest file ${file}`, {cause: error});
  }
};

// The original source-only reader handles a narrow block-mapping subset.
// Reject representations it cannot interpret instead of losing an alias.
const parseSimpleYamlManifest = (text, file) => {
  const fail = (form) => { throw new Error(`unsupported YAML ${form} in ${file}`); };
  const rendering = {};
  let id;
  let inRendering = false;
  let sawRendering = false;
  let entry = null;
  let extraField = false;
  const scalar = (raw, form) => {
    const quoted = /^\s*(?:'([^'\\#]*)'|"([^"\\#]*)")\s*(?:#.*)?$/.exec(raw);
    if (quoted) return quoted[1] ?? quoted[2];
    const plain = /^\s*([^#\[\]{}&*!|>'"%`\\]+?)(?:\s+#.*)?\s*$/.exec(raw);
    if (!plain || /:\s/.test(plain[1]) || /^[?@-]\s/.test(plain[1])) fail(form);
    const value = plain[1].trim();
    if (form !== 'id scalar' && /^(?:null|~|true|false|[-+]?[0-9]+(?:\.[0-9]+)?)$/i.test(value)) fail(form);
    return value;
  };
  const finish = () => {
    if (!entry) return;
    const rawKind = entry.key.split('/', 1)[0];
    if (ELEMENT_KINDS.has(rawKind) && (!entry.type || !entry.path)) fail('incomplete rendering declaration');
  };
  for (const line of text.split(/\r?\n/)) {
    if (/^\s*(?:#.*)?$/.test(line)) continue;
    if (/^\s*\t/.test(line) || /^\uFEFF/.test(line)) fail('tab indentation or byte-order mark');
    if (/^\s*(?:---|\.\.\.)\s*(?:#.*)?$/.test(line)) fail('document marker');
    if (/^\S/.test(line)) {
      if (inRendering) finish();
      inRendering = false;
      entry = null;
      if (/^<<\s*:/.test(line)) fail('root merge');
      if (/^[!&*]/.test(line)) fail('root tag or alias');
      if (/^(?:id|['"]id['"])\s*:/.test(line)) {
        if (id !== undefined || !/^id\s*:/.test(line)) fail('id declaration');
        id = scalar(line.replace(/^id\s*:/, ''), 'id scalar');
      }
      if (/^(?:rendering|['"]rendering['"])\s*:/.test(line)) {
        if (sawRendering || !/^rendering\s*:/.test(line)) fail('rendering declaration');
        sawRendering = true;
        const rest = line.replace(/^rendering\s*:/, '').trim();
        if (/^\{\}\s*(?:#.*)?$/.test(rest)) continue;
        if (rest && !rest.startsWith('#')) fail('rendering flow or scalar form');
        inRendering = true;
      } else if (/^[{[]/.test(line)) {
        fail('root flow form');
      }
      continue;
    }
    if (!inRendering) {
      if (/^\s+(?:rendering|['"]rendering['"])\s*:/.test(line)) fail('rendering indentation');
      continue;
    }
    if (/\t/.test(line) || /(?:^|\s)(?:[&*!]|<<:)/.test(line)) fail('rendering alias, tag or indentation');
    const entryMatch = /^  ([^\s:#][^:#]*):\s*(?:#.*)?$/.exec(line);
    if (entryMatch) {
      finish();
      const key = entryMatch[1].trim();
      if (/['"\[\]{}&*!|>%`\\]/.test(key)) fail('rendering entry key');
      if (Object.hasOwn(rendering, key)) fail('duplicate rendering entry');
      entry = {key};
      rendering[key] = entry;
      extraField = false;
      continue;
    }
    if (/^  \S/.test(line)) fail('rendering entry or flow form');
    if (!entry) fail('rendering mapping');
    const field = /^    ([A-Za-z0-9_-]+):\s*(.*)$/.exec(line);
    if (field) {
      const [, key, raw] = field;
      if (key === 'type' || key === 'path') {
        if (Object.hasOwn(entry, key)) fail(`duplicate ${key}`);
        entry[key] = scalar(raw, `${key} scalar or flow form`);
        extraField = false;
      } else {
        if (/^[\[{|>]/.test(raw)) fail(`${key} flow or multiline form`);
        extraField = true;
      }
      continue;
    }
    if (!extraField || !/^    (?:-|\s)/.test(line)) fail('rendering field or indentation');
  }
  if (inRendering) finish();
  return {id, rendering};
};

const readPackManifest = (packRoot, astridDir, remotionRoot) => {
  for (const name of MANIFEST_NAMES) {
    const text = readText(path.join(packRoot, name));
    if (text === null) continue;
    let manifest;
    try {
      manifest = JSON.parse(text);
    } catch {
      if (name.endsWith('.json')) throw new Error(`invalid JSON form in ${path.join(packRoot, name)}`);
      let moduleRoot = remotionRoot;
      if (moduleRoot === undefined) {
        try { moduleRoot = nativeModuleDir(); }
        catch { moduleRoot = path.join(astridDir, 'remotion'); }
      }
      const require = createRequire(path.join(moduleRoot, 'package.json'));
      const installed = path.join(moduleRoot, 'node_modules/js-yaml');
      const file = path.join(packRoot, name);
      if (fs.existsSync(installed)) {
        let yaml;
        try { yaml = require(installed); }
        catch (error) { throw new Error(`cannot load project YAML parser for ${file}`, {cause: error}); }
        try { manifest = yaml.load(text); }
        catch (error) { throw new Error(`invalid YAML form in ${file}`, {cause: error}); }
      } else {
        manifest = parseSimpleYamlManifest(text, file);
      }
    }
    if (!manifest || typeof manifest !== 'object' || Array.isArray(manifest)) {
      throw new Error(`invalid manifest mapping form in ${path.join(packRoot, name)}`);
    }
    if (manifest.rendering !== undefined && (!manifest.rendering || typeof manifest.rendering !== 'object' || Array.isArray(manifest.rendering))) {
      throw new Error(`invalid rendering form in ${path.join(packRoot, name)}`);
    }
    return {id: manifest.id, rendering: manifest.rendering ?? {}};
  }
  return {};
};

const isDirectory = (file) => {
  try {
    return fs.statSync(file).isDirectory();
  } catch {
    return false;
  }
};

const hasManifest = (dir) => MANIFEST_NAMES.some((name) => fs.existsSync(path.join(dir, name)));

const packRootsIn = (root, {skipLocal = false} = {}) => {
  const resolvedRoot = path.resolve(root);
  if (hasManifest(resolvedRoot)) return [resolvedRoot];
  let names = [];
  try {
    names = fs.readdirSync(resolvedRoot).sort();
  } catch {
    return [];
  }
  return names
    .filter((name) => !name.startsWith('.') && !(skipLocal && name === 'local'))
    .map((name) => path.join(resolvedRoot, name))
    .filter((dir) => isDirectory(dir) && hasManifest(dir));
};

const isWithin = (root, candidate) => {
  const relative = path.relative(path.resolve(root), path.resolve(candidate));
  return relative === '' || (relative !== '..' && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative));
};

/** Resolve a manifest-declared resource without allowing pack-root escape. */
export const resolveDeclaredResource = (packRoot, declaredPath) => {
  if (typeof declaredPath !== 'string' || !declaredPath || path.isAbsolute(declaredPath)) {
    throw new Error(`V3 resource path must be a non-empty relative path: ${declaredPath}`);
  }
  const root = path.resolve(packRoot);
  const resolved = path.resolve(root, declaredPath);
  if (!isWithin(root, resolved)) {
    throw new Error(`V3 resource escapes pack root: ${declaredPath}`);
  }
  return resolved;
};

/** Resolve a public asset while keeping lookup inside the owned public root. */
export const resolvePublicResource = (publicRoot, resourcePath) => {
  if (typeof resourcePath !== 'string' || !resourcePath || path.isAbsolute(resourcePath)) {
    throw new Error(`public resource path must be a non-empty relative path: ${resourcePath}`);
  }
  const root = path.resolve(publicRoot);
  const resolved = path.resolve(root, resourcePath);
  if (!isWithin(root, resolved)) {
    throw new Error(`public resource escapes public root: ${resourcePath}`);
  }
  return resolved;
};

/** Refuse renderer scratch/staging paths beneath public/ to prevent recursion. */
export const assertOutsidePublicRoot = (publicRoot, candidate) => {
  const root = path.resolve(publicRoot);
  const resolved = path.resolve(candidate);
  if (isWithin(root, resolved)) {
    throw new Error(`renderer staging path is inside public and would recurse: ${resolved}`);
  }
  return resolved;
};

const collectDeclaredPackElementAliases = (packRoots, scope, {strict = false, astridDir = repoRootOf(), remotionRoot} = {}) => {
  const aliases = {};
  for (const packRoot of packRoots) {
    const manifest = readPackManifest(packRoot, astridDir, remotionRoot);
    const packId = manifest.id || path.basename(packRoot);
    for (const [rawKey, declaration] of Object.entries(manifest.rendering ?? {})) {
      if (!declaration || declaration.type !== 'element' || typeof declaration.path !== 'string') continue;
      const rawKind = rawKey.split('/', 1)[0];
      if (!ELEMENT_KINDS.has(rawKind)) continue;
      const kind = CANONICAL_KIND[rawKind];
      let elementManifest;
      try {
        elementManifest = resolveDeclaredResource(packRoot, declaration.path);
      } catch {
        continue;
      }
      const elementRoot = path.dirname(elementManifest);
      const kindRoot = path.dirname(elementRoot);
      if (path.basename(kindRoot) !== kind || !isDirectory(kindRoot)) continue;
      const key = `@${scope ?? `pack-${packId}-elements`}-${kind}`;
      if (strict && Object.hasOwn(aliases, key) && aliases[key] !== kindRoot) {
        throw new Error(`conflicting render pack alias: ${key}`);
      }
      aliases[key] = kindRoot;
    }
  }
  return aliases;
};

// V3 aliases come only from declared rendering entries. There is deliberately
// no fallback scan of legacy `elements/` locations.
export const packElementAliases = ({env = process.env, astridDir = repoRootOf(), remotionRoot} = {}) => {
  // A managed render supplies exactly the roots used by its Python registry.
  // Do not broaden that selection with this process's checkout or environment.
  if (Object.hasOwn(env, 'ASTRID_RENDER_PACK_ROOTS')) {
    const roots = JSON.parse(env.ASTRID_RENDER_PACK_ROOTS);
    if (!Array.isArray(roots)) throw new Error('invalid render pack roots');
    const aliases = {};
    for (const entry of roots) {
      if (!entry || typeof entry.root !== 'string' || !path.isAbsolute(entry.root) ||
          typeof entry.scope !== 'string' || !/^[A-Za-z0-9_-]+$/.test(entry.scope) ||
          !isDirectory(entry.root) || !hasManifest(entry.root)) {
        throw new Error('invalid render pack root');
      }
      let selected;
      if (entry.kind_roots === undefined) {
        // Compatibility for standalone callers which still send the original
        // staged-pack carrier. Managed renders always send kind_roots.
        selected = collectDeclaredPackElementAliases([entry.root], entry.scope, {strict: true, astridDir, remotionRoot});
      } else {
        if (!entry.kind_roots || typeof entry.kind_roots !== 'object' || Array.isArray(entry.kind_roots)) {
          throw new Error('invalid render kind roots');
        }
        selected = {};
        const realRoot = fs.realpathSync(entry.root);
        for (const [kind, directory] of Object.entries(entry.kind_roots)) {
          if (!['effects', 'animations', 'transitions'].includes(kind) ||
              typeof directory !== 'string' || !path.isAbsolute(directory) ||
              !isDirectory(directory) || path.basename(directory) !== kind ||
              !isWithin(realRoot, fs.realpathSync(directory))) {
            throw new Error('invalid render kind root');
          }
          selected[`@${entry.scope}-${kind}`] = fs.realpathSync(directory);
        }
      }
      if (entry.scope === 'pack-rendering-elements') selected['@astrid-rendering'] = entry.root;
      if (entry.legacy_schema !== undefined) {
        if (entry.scope !== 'pack-rendering-elements' || typeof entry.legacy_schema !== 'string' ||
            !path.isAbsolute(entry.legacy_schema) || path.basename(entry.legacy_schema) !== 'r13-legacy-elements.json' ||
            !fs.existsSync(entry.legacy_schema)) throw new Error('invalid legacy element schema');
      }
      for (const [key, value] of Object.entries(selected)) {
        if (Object.hasOwn(aliases, key) && aliases[key] !== value) {
          throw new Error(`conflicting render pack alias: ${key}`);
        }
        aliases[key] = value;
      }
    }
    return aliases;
  }
  const inTree = packRootsIn(path.resolve(astridDir, 'astrid/packs'));
  const external = [];
  for (const rawRoot of (env.ASTRID_PACKS_PATH ?? '').split(path.delimiter)) {
    if (rawRoot) external.push(...packRootsIn(rawRoot, {skipLocal: true}));
  }
  const selected = {};
  for (const {root, scope} of JSON.parse(env.ASTRID_RENDER_PACK_ROOTS ?? '[]')) {
    Object.assign(selected, collectDeclaredPackElementAliases([root], scope, {astridDir, remotionRoot}));
  }
  return {
    ...collectDeclaredPackElementAliases(inTree, undefined, {astridDir, remotionRoot}),
    ...collectDeclaredPackElementAliases(external, undefined, {astridDir, remotionRoot}),
    ...selected,
    ...(inTree.some((root) => path.basename(root) === 'rendering')
      ? {'@astrid-rendering': path.resolve(astridDir, 'astrid/packs/rendering')} : {}),
  };
};

// These imports belong to the host itself, not to arbitrary packs which may
// happen to use the same relative spelling. Both webpack and esbuild use this
// exact issuer-bound lookup.
export const selectedHostResource = ({remotionRoot, issuer, request, env = process.env}) => {
  if (typeof issuer !== 'string' || typeof request !== 'string') return undefined;
  const owner = path.resolve(issuer);
  const host = path.resolve(remotionRoot, 'src');
  if (owner === path.join(host, 'host-registries.ts') && env.ASTRID_RENDER_REGISTRY_ROOT) {
    const match = /^\.\/(effects|animations|transitions)\.generated$/.exec(request);
    if (match) return path.resolve(env.ASTRID_RENDER_REGISTRY_ROOT, 'src', `${match[1]}.generated.ts`);
  }
  if (owner === path.join(host, 'r13-clip-presentation.tsx') &&
      request === '../../astrid/core/timeline/schema/r13-legacy-elements.json' &&
      Object.hasOwn(env, 'ASTRID_RENDER_PACK_ROOTS')) {
    // Validate the complete carrier and refuse competing schema bindings.
    packElementAliases({env, astridDir: projectDirOf(remotionRoot), remotionRoot});
    const schemas = JSON.parse(env.ASTRID_RENDER_PACK_ROOTS)
      .filter(entry => entry.legacy_schema !== undefined).map(entry => entry.legacy_schema);
    if (new Set(schemas).size > 1) throw new Error('conflicting legacy element schemas');
    if (schemas.length) return schemas[0];
  }
  return undefined;
};

const primitiveAliases = (projectDir) => {
  const activeThemeDir = path.resolve(projectDir, '_active_theme');
  const aliases = {
    '@theme-elements-effects': path.resolve(activeThemeDir, 'elements/effects'),
    '@theme-effects': path.resolve(activeThemeDir, 'effects'),
    '@theme-elements-animations': path.resolve(activeThemeDir, 'elements/animations'),
    '@theme-animations': path.resolve(activeThemeDir, 'animations'),
    '@theme-elements-transitions': path.resolve(activeThemeDir, 'elements/transitions'),
    '@theme-transitions': path.resolve(activeThemeDir, 'transitions'),
  };
  return aliases;
};

const hostResourcePlugins = new Map();
const usesLegacyCarrier = (rawRoots) => {
  const roots = rawRoots === undefined ? null : JSON.parse(rawRoots);
  return Array.isArray(roots) && roots.length > 0 &&
    roots.every(entry => entry && entry.kind_roots === undefined);
};
const hostResourcePlugin = (remotionRoot) => {
  if (!hostResourcePlugins.has(remotionRoot)) {
    hostResourcePlugins.set(remotionRoot, {apply(compiler) {
      compiler.hooks.normalModuleFactory.tap('AstridHostResources', (factory) => {
        factory.hooks.beforeResolve.tap('AstridHostResources', (data) => {
          if (!data) return;
          const issuer = data.contextInfo?.issuer;
          const selected = selectedHostResource({remotionRoot, issuer, request: data.request});
          if (selected) data.request = selected;
          else if (process.env.ASTRID_RENDER_REGISTRY_ROOT && usesLegacyCarrier(process.env.ASTRID_RENDER_PACK_ROOTS) &&
                   /^\.\/(effects|animations|transitions)\.generated$/.test(data.request)) {
            // The legacy public alias map remains usable by standalone
            // resolvers. In webpack it must not redirect another issuer.
            if (typeof issuer !== 'string' || !path.isAbsolute(issuer)) {
              throw new Error('generated registry import requires an absolute issuer');
            }
            data.request = path.resolve(path.dirname(issuer), data.request);
          }
        });
      });
    }});
  }
  return hostResourcePlugins.get(remotionRoot);
};

const applyAliasesForRoot = (currentConfiguration, remotionRoot) => {
  const repoRoot = projectDirOf(remotionRoot);
  const packAliases = packElementAliases({astridDir: repoRoot, remotionRoot});
  const selectedRegistries = {};
  const rawRoots = process.env.ASTRID_RENDER_PACK_ROOTS;
  // Older standalone integrations use only {root, scope}. Keep their public
  // alias map stable; managed callers always supply kind_roots, even if empty.
  const legacyCarrier = usesLegacyCarrier(rawRoots);
  if (legacyCarrier && process.env.ASTRID_RENDER_REGISTRY_ROOT) {
    for (const kind of ['effects', 'animations', 'transitions']) {
      selectedRegistries[`./${kind}.generated$`] = path.resolve(
        process.env.ASTRID_RENDER_REGISTRY_ROOT, 'src', `${kind}.generated.ts`,
      );
    }
  }
  return {
    ...currentConfiguration,
    plugins: [...(currentConfiguration.plugins ?? []), hostResourcePlugin(remotionRoot)],
    module: {
      ...currentConfiguration.module,
      rules: [
        ...(currentConfiguration.module?.rules ?? []),
        {
          enforce: 'pre',
          include: [path.join(remotionRoot, 'node_modules/@banodoco/timeline-composition/dist/index.js')],
          use: [{loader: path.join(remotionRoot, 'timeline-composition-legacy-loader.cjs')}],
        },
      ],
    },
    resolve: {
      ...currentConfiguration.resolve,
      alias: {
        ...currentConfiguration.resolve?.alias,
        ...packAliases,
        ...selectedRegistries,
        ...primitiveAliases(repoRoot),
      },
      modules: [
        ...(currentConfiguration.resolve?.modules ?? ['node_modules']),
        path.resolve(remotionRoot, 'node_modules'),
      ],
    },
  };
};

export const remotionPrimitiveAliasesFor = (remotionRoot) => {
  if (typeof remotionRoot !== 'string' || !path.isAbsolute(remotionRoot)) {
    throw new Error('Remotion alias binding requires an explicit absolute remotionRoot');
  }
  const capturedRoot = path.resolve(remotionRoot);
  return (configuration) => applyAliasesForRoot(configuration, capturedRoot);
};

export const applyRemotionPrimitiveAliases = (configuration) =>
  applyAliasesForRoot(configuration, nativeModuleDir());
