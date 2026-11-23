import path from 'node:path';
import fs from 'node:fs';
import {fileURLToPath} from 'node:url';
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
  } catch {
    return null;
  }
};

const unquote = (value) => value.trim().replace(/^(['"])(.*)\1$/, '$2');

const parseYamlManifest = (text) => {
  const id = text.match(/^id:\s*(['"]?)([A-Za-z0-9_-]+)\1\s*$/m)?.[2];
  const rendering = {};
  let inRendering = false;
  let current = null;
  for (const line of text.split(/\r?\n/)) {
    if (/^\S/.test(line)) {
      inRendering = /^rendering:\s*$/.test(line);
      current = null;
      continue;
    }
    if (!inRendering) continue;
    const key = line.match(/^  ([^\s:#][^:#]*):\s*$/)?.[1]?.trim();
    if (key) {
      current = {key, type: undefined, path: undefined};
      rendering[key] = current;
      continue;
    }
    if (!current) continue;
    const type = line.match(/^    type:\s*([^#]+?)\s*(?:#.*)?$/)?.[1];
    if (type) current.type = unquote(type);
    const declaredPath = line.match(/^    path:\s*([^#]+?)\s*(?:#.*)?$/)?.[1];
    if (declaredPath) current.path = unquote(declaredPath);
  }
  return {id, rendering};
};

const readPackManifest = (packRoot) => {
  for (const name of MANIFEST_NAMES) {
    const text = readText(path.join(packRoot, name));
    if (text === null) continue;
    try {
      const json = JSON.parse(text);
      return {id: json.id, rendering: json.rendering ?? {}};
    } catch {
      return parseYamlManifest(text);
    }
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

const collectDeclaredPackElementAliases = (packRoots, scope, {strict = false} = {}) => {
  const aliases = {};
  for (const packRoot of packRoots) {
    const manifest = readPackManifest(packRoot);
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
export const packElementAliases = ({env = process.env, astridDir = repoRootOf()} = {}) => {
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
      const selected = collectDeclaredPackElementAliases([entry.root], entry.scope, {strict: true});
      if (entry.scope === 'pack-rendering-elements') selected['@astrid-rendering'] = entry.root;
      if (entry.legacy_schema !== undefined) {
        if (entry.scope !== 'pack-rendering-elements' || typeof entry.legacy_schema !== 'string' ||
            !path.isAbsolute(entry.legacy_schema) || path.basename(entry.legacy_schema) !== 'r13-legacy-elements.json' ||
            !fs.existsSync(entry.legacy_schema)) throw new Error('invalid legacy element schema');
        selected['../../astrid/core/timeline/schema/r13-legacy-elements.json$'] = entry.legacy_schema;
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
    Object.assign(selected, collectDeclaredPackElementAliases([root], scope));
  }
  return {
    ...collectDeclaredPackElementAliases(inTree),
    ...collectDeclaredPackElementAliases(external),
    ...selected,
    ...(inTree.some((root) => path.basename(root) === 'rendering')
      ? {'@astrid-rendering': path.resolve(astridDir, 'astrid/packs/rendering')} : {}),
  };
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

const applyAliasesForRoot = (currentConfiguration, remotionRoot) => {
  const repoRoot = projectDirOf(remotionRoot);
  const packAliases = packElementAliases({astridDir: repoRoot});
  const selectedRegistries = {};
  if (process.env.ASTRID_RENDER_REGISTRY_ROOT) {
    for (const kind of ['effects', 'animations', 'transitions']) {
      selectedRegistries[`./${kind}.generated$`] = path.resolve(
        process.env.ASTRID_RENDER_REGISTRY_ROOT, 'src', `${kind}.generated.ts`,
      );
    }
  }
  return {
    ...currentConfiguration,
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
