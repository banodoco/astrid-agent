import path from 'node:path';
import fs from 'node:fs';
import os from 'node:os';
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
  let lineNumber = 0;
  const fail = (form) => { throw new Error(`unsupported YAML ${form} in ${file}:${lineNumber}`); };
  const rendering = {};
  let id;
  let inRendering = false;
  let sawRendering = false;
  let sawRoot = false;
  let sawDocumentStart = false;
  let endedDocument = false;
  let entry = null;
  let extraField = false;
  let blockIndent = null;
  let continuationIndent = null;
  let quotedContinuation = null;
  let rootSequence = false;
  let afterId = false;
  const mappings = [];
  const recordKey = (indent, key, sequenceItem = false) => {
    const depth = indent + (sequenceItem ? 2 : 0);
    while (mappings.length && mappings[mappings.length - 1].indent > (sequenceItem ? indent : depth)) mappings.pop();
    let frame = mappings[mappings.length - 1];
    if (!frame || frame.indent !== depth) {
      frame = {indent: depth, keys: new Set()};
      mappings.push(frame);
    }
    if (frame.keys.has(key)) fail('duplicate mapping key');
    frame.keys.add(key);
  };
  const scalar = (raw, form) => {
    const quoted = /^\s*(?:'([^'\\#]*)'|"([^"\\#]*)")\s*(?:#.*)?$/.exec(raw);
    if (quoted) return quoted[1] ?? quoted[2];
    const plain = /^\s*([^#\[\]{}|>'"`\\]+?)(?:\s+#.*)?\s*$/.exec(raw);
    if (!plain || /:\s/.test(plain[1]) || /^[&*!%@`]/.test(plain[1]) || /^[?-]\s/.test(plain[1])) fail(form);
    const value = plain[1].trim();
    if (/^(?:null|~|true|false|[-+]?(?:[0-9]|\.[0-9]|\.inf|\.nan))/i.test(value)) fail(form);
    return value;
  };
  const finish = () => {
    if (!entry) return;
    const rawKind = entry.key.split('/', 1)[0];
    if (ELEMENT_KINDS.has(rawKind) && (!entry.type || !entry.path)) fail('incomplete rendering declaration');
  };
  const finishRendering = () => {
    finish();
    if (Object.keys(rendering).length === 0) fail('empty rendering block');
  };
  // Outside rendering, the base reader ignored ordinary block mappings,
  // sequences and their scalar continuations. Recognize those shapes without
  // interpreting a nested key as a top-level rendering declaration.
  const quoteEnd = (value, quote, start) => {
    for (let index = start; index < value.length; index++) {
      if (quote === '"' && value[index] === '\\') { index++; continue; }
      if (value[index] === quote) {
        if (quote === "'" && value[index + 1] === "'") { index++; continue; }
        if (!/^\s*(?:#.*)?$/.test(value.slice(index + 1))) fail('quoted scalar form');
        return true;
      }
    }
    return false;
  };
  const validateIgnoredFlow = (raw) => {
    let position = 0;
    const space = () => { while (/\s/.test(raw[position] ?? '')) position++; };
    const token = (key) => {
      space();
      const quote = raw[position];
      if (quote === "'" || quote === '"') {
        if (key) fail('document flow mapping key form');
        position++;
        while (position < raw.length) {
          const char = raw[position++];
          if (quote === '"' && char === '\\') {
            const escape = raw[position++];
            if ('0abtnvfre /"\\N_LP'.includes(escape ?? '')) continue;
            const digits = {x: 2, u: 4, U: 8}[escape];
            if (!digits || !new RegExp(`^[0-9a-fA-F]{${digits}}$`).test(raw.slice(position, position + digits))) {
              fail('document flow escape form');
            }
            position += digits;
          } else if (char === quote) {
            if (quote === "'" && raw[position] === "'") { position++; continue; }
            return;
          }
        }
        fail('document flow quoted scalar form');
      }
      const start = position;
      while (position < raw.length && !/[,\[\]{}]/.test(raw[position]) && !(key && raw[position] === ':')) position++;
      const plain = raw.slice(start, position).trim();
      if (!plain || /^[&*!%@`]/.test(plain) || /#/.test(plain)) fail('document flow scalar form');
      if (key && !/^[A-Za-z_][A-Za-z0-9_./-]*$/.test(plain)) fail('document flow mapping key form');
      return plain;
    };
    const value = () => {
      space();
      if (raw[position] !== '[' && raw[position] !== '{') { token(false); return; }
      const mapping = raw[position++] === '{';
      const close = mapping ? '}' : ']';
      const keys = new Set();
      space();
      if (raw[position] === close) { position++; return; }
      while (position < raw.length) {
        if (mapping) {
          const key = token(true);
          if (keys.has(key)) fail('duplicate flow mapping key');
          keys.add(key);
          space();
          if (raw[position++] !== ':') fail('document flow mapping form');
          if (raw[position] && !/[\s\[\]{},]/.test(raw[position])) fail('document flow mapping separator');
        }
        value();
        space();
        if (raw[position] === close) { position++; return; }
        if (raw[position++] !== ',') fail('document flow separator');
        space();
        if (raw[position] === close) { position++; return; }
      }
      fail('document flow form');
    };
    value();
    if (!/^\s*(?:#.*)?$/.test(raw.slice(position))) fail('document flow form');
  };
  const validateIgnoredValue = (raw, indent) => {
    if (!raw || /^\s*#/.test(raw)) return;
    if (/^[&*!%@`]/.test(raw)) fail('document scalar, alias or tag form');
    if (raw[0] === "'" || raw[0] === '"') {
      if (!quoteEnd(raw, raw[0], 1)) quotedContinuation = {quote: raw[0], indent};
      return;
    }
    if (raw[0] === '[' || raw[0] === '{') {
      validateIgnoredFlow(raw);
    }
  };
  const ignoredLine = (line, allowContinuation = true) => {
    const indent = /^ */.exec(line)[0].length;
    if (blockIndent !== null) {
      if (indent > blockIndent) return;
      blockIndent = null;
    }
    const mapping = /^( *)(-\s+)?([A-Za-z_][A-Za-z0-9_./-]*):(?:\s+(.*))?$/.exec(line);
    if (mapping) {
      const raw = mapping[4] ?? '';
      recordKey(indent, mapping[3], Boolean(mapping[2]));
      if (indent === 0 && line.startsWith('-') && !rootSequence) fail('root sequence form');
      if (indent === 0 && !line.startsWith('-')) rootSequence = !raw;
      if (/^[|>][+-]?(?:\s+#.*)?$/.test(raw)) blockIndent = indent;
      else validateIgnoredValue(raw, indent);
      continuationIndent = raw && !/^\s*#/.test(raw) ? indent : null;
      return;
    }
    const sequence = /^( *)-(?:\s+(.*))?$/.exec(line);
    if (sequence) {
      if (indent === 0 && !rootSequence) fail('root sequence form');
      while (mappings.length && mappings[mappings.length - 1].indent > indent) mappings.pop();
      validateIgnoredValue(sequence[2] ?? '', indent);
      continuationIndent = sequence[2] ? indent : null;
      return;
    }
    if (allowContinuation && continuationIndent !== null && indent > continuationIndent &&
        !/^[\s]*[\[\]{}!&*?|>@%]/.test(line) && !/:\s/.test(line)) return;
    fail('document line form');
  };
  for (const line of text.split(/\r?\n/)) {
    lineNumber++;
    if (line.includes('\r')) fail('line break form');
    if (/^\s*(?:#.*)?$/.test(line)) continue;
    if (/^\s*\t/.test(line) || /^\uFEFF/.test(line)) fail('tab indentation or byte-order mark');
    if (afterId) {
      if (/^\s/.test(line)) fail('id continuation');
      afterId = false;
    }
    if (quotedContinuation) {
      if (/^ */.exec(line)[0].length <= quotedContinuation.indent) fail('quoted scalar form');
      if (quoteEnd(line, quotedContinuation.quote, 0)) quotedContinuation = null;
      continue;
    }
    if (endedDocument) fail('multiple documents');
    if (/^---\s*(?:#.*)?$/.test(line)) {
      if (sawRoot || sawDocumentStart) fail('multiple documents');
      sawDocumentStart = true;
      continue;
    }
    if (/^\.\.\.\s*(?:#.*)?$/.test(line)) {
      if (inRendering) finishRendering();
      inRendering = false;
      endedDocument = true;
      continue;
    }
    if (/^\S/.test(line)) {
      if (inRendering) finishRendering();
      inRendering = false;
      entry = null;
      sawRoot = true;
      if (/^<<\s*:/.test(line)) fail('root merge');
      if (/^['"]rendering['"]\s*:/.test(line)) fail('rendering declaration');
      if (/^[!&*?'"`%]/.test(line)) fail('root key, tag or alias');
      if (/^(?:id|['"]id['"])\s*:/.test(line)) {
        if (id !== undefined || !/^id\s*:/.test(line)) fail('id declaration');
        const raw = line.replace(/^id\s*:/, '');
        if (raw && !/^\s/.test(raw)) fail('id separator');
        id = scalar(raw, 'id scalar');
        afterId = true;
      }
      if (/^(?:rendering|['"]rendering['"])\s*:/.test(line)) {
        if (sawRendering || !/^rendering\s*:/.test(line)) fail('rendering declaration');
        sawRendering = true;
        recordKey(0, 'rendering');
        const raw = line.replace(/^rendering\s*:/, '');
        if (raw && !/^\s/.test(raw)) fail('rendering separator');
        const rest = raw.trim();
        if (/^\{\}\s*(?:#.*)?$/.test(rest)) continue;
        if (rest && !rest.startsWith('#')) fail('rendering flow or scalar form');
        inRendering = true;
      } else if (/^[{[]/.test(line)) {
        fail('root flow form');
      } else {
        ignoredLine(line);
      }
      continue;
    }
    if (!sawRoot) fail('root indentation');
    if (!inRendering) {
      ignoredLine(line);
      continue;
    }
    if (/\t/.test(line) || /^\s*<<\s*:/.test(line)) fail('rendering alias, tag or indentation');
    if (/^\s*(?:-\s*)?[A-Za-z0-9_-]+:\s+[&*!]/.test(line) || /^\s*-\s*[&*!]/.test(line)) {
      fail('rendering alias or tag');
    }
    const entryMatch = /^  ([^\s:#][^:#]*):(?:\s+#.*|\s*)$/.exec(line);
    if (entryMatch) {
      finish();
      const key = entryMatch[1].trim();
      if (/^[-?&*!%@`]/.test(key) || /['"\[\]{}|>`\\]/.test(key)) fail('rendering entry key');
      if (Object.hasOwn(rendering, key)) fail('duplicate rendering entry');
      recordKey(2, key);
      entry = {key};
      rendering[key] = entry;
      extraField = false;
      continue;
    }
    if (/^  \S/.test(line)) fail('rendering entry or flow form');
    if (!entry) fail('rendering mapping');
    const field = /^    ([A-Za-z0-9_-]+):(?:\s+(.*))?$/.exec(line);
    if (field) {
      const key = field[1];
      const raw = field[2] ?? '';
      recordKey(4, key);
      if (key === 'type' || key === 'path') {
        if (Object.hasOwn(entry, key)) fail(`duplicate ${key}`);
        entry[key] = scalar(raw, `${key} scalar or flow form`);
        extraField = false;
      } else {
        if (raw && !/^\s*#/.test(raw)) scalar(raw, `${key} scalar or flow form`);
        extraField = true;
      }
      continue;
    }
    if (!extraField || !/^    (?:-|\s)/.test(line)) fail('rendering field or indentation');
    ignoredLine(line, false);
  }
  if (inRendering) finishRendering();
  if (quotedContinuation) fail('quoted scalar form');
  if (!sawRoot) fail('empty document');
  return {id, rendering};
};

// JSON is also valid YAML and was accepted by the original reader. Check
// object keys separately because JSON.parse silently accepts duplicates.
const rejectDuplicateJsonKeys = (text, file) => {
  let position = 0;
  const whitespace = () => { while (/\s/.test(text[position] ?? '')) position++; };
  const string = () => {
    const start = position++;
    while (position < text.length) {
      if (text[position] === '\\') { position += 2; continue; }
      if (text[position++] === '"') return JSON.parse(text.slice(start, position));
    }
    throw new Error(`invalid JSON string form in ${file}`);
  };
  const value = (depth) => {
    if (depth > 512) throw new Error(`unsupported JSON nesting form in ${file}`);
    whitespace();
    if (text[position] === '{') {
      position++;
      const keys = new Set();
      whitespace();
      while (text[position] !== '}') {
        const key = string();
        if (keys.has(key)) throw new Error(`duplicate JSON key form in ${file}`);
        keys.add(key);
        whitespace();
        position++; // colon; syntax was already validated by JSON.parse
        value(depth + 1);
        whitespace();
        if (text[position] === ',') { position++; whitespace(); }
      }
      position++;
    } else if (text[position] === '[') {
      position++;
      whitespace();
      while (text[position] !== ']') {
        value(depth + 1);
        whitespace();
        if (text[position] === ',') { position++; whitespace(); }
      }
      position++;
    } else if (text[position] === '"') {
      string();
    } else {
      while (position < text.length && !/[\s,}\]]/.test(text[position])) position++;
    }
  };
  value(0);
};

const readPackManifest = (packRoot, astridDir, remotionRoot) => {
  for (const name of MANIFEST_NAMES) {
    const text = readText(path.join(packRoot, name));
    if (text === null) continue;
    let manifest;
    const file = path.join(packRoot, name);
    if (name.endsWith('.json')) {
      try { manifest = JSON.parse(text); }
      catch { throw new Error(`invalid JSON form in ${file}`); }
      rejectDuplicateJsonKeys(text, file);
    } else {
      let moduleRoot = remotionRoot;
      if (moduleRoot === undefined) {
        try { moduleRoot = nativeModuleDir(); }
        catch { moduleRoot = path.join(astridDir, 'remotion'); }
      }
      const require = createRequire(path.join(moduleRoot, 'package.json'));
      const installed = path.join(moduleRoot, 'node_modules/js-yaml');
      let installedExists;
      try { fs.lstatSync(installed); installedExists = true; }
      catch (error) {
        if (error.code !== 'ENOENT') throw new Error(`cannot inspect project YAML parser for ${file}`, {cause: error});
        installedExists = false;
      }
      if (installedExists) {
        let yaml;
        try {
          const projectRoot = fs.realpathSync(moduleRoot);
          if (!isWithin(projectRoot, fs.realpathSync(installed))) {
            throw new Error('YAML parser escapes the Remotion project');
          }
          const parserEntry = fs.realpathSync(require.resolve(installed));
          if (!isWithin(projectRoot, parserEntry)) throw new Error('YAML parser entry escapes the Remotion project');
          yaml = require(parserEntry);
        }
        catch (error) { throw new Error(`cannot load project YAML parser for ${file}`, {cause: error}); }
        try { manifest = yaml.load(text); }
        catch (error) { throw new Error(`invalid YAML form in ${file}`, {cause: error}); }
      } else {
        try { manifest = JSON.parse(text); }
        catch { manifest = undefined; }
        if (manifest === undefined) manifest = parseSimpleYamlManifest(text, file);
        else rejectDuplicateJsonKeys(text, file);
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

// Installed packs (for example the manifest-pinned astrid_motion pack under
// ~/Astrid/packs) import the built-in contracts by a relative path which only
// resolves inside the Agent tree. Redirect exactly that request, from issuers
// inside an installed pack tree, to the built-in rendering pack. Nothing else.
const ELEMENT_CONTRACTS_REQUEST = /^(?:\.\.\/)+rendering\/shared\/element_contracts(?:\.(?:tsx?|jsx?|mjs))?$/;
const RESOLVABLE_SUFFIXES = ['', '.ts', '.tsx', '.js', '.jsx', '.mjs', '.json'];
const isFileWithSuffix = (base) => RESOLVABLE_SUFFIXES.some(
  (suffix) => fs.existsSync(base + suffix) && fs.statSync(base + suffix).isFile());
const installedPackRoots = (env) => {
  const roots = [];
  if (env.ASTRID_PACKS_PATH) {
    for (const raw of env.ASTRID_PACKS_PATH.split(path.delimiter)) if (raw) roots.push(path.resolve(raw));
  }
  if (env.ASTRID_RENDER_PACK_ROOTS) {
    const entries = JSON.parse(env.ASTRID_RENDER_PACK_ROOTS);
    if (Array.isArray(entries)) {
      for (const entry of entries) if (entry && typeof entry.root === 'string') roots.push(path.resolve(entry.root));
    }
  }
  roots.push(path.join(os.homedir(), 'Astrid', 'packs'));
  return roots;
};
export const installedPackElementContracts = ({remotionRoot, issuer, request, env = process.env}) => {
  if (typeof issuer !== 'string' || typeof request !== 'string') return undefined;
  if (!ELEMENT_CONTRACTS_REQUEST.test(request) || !path.isAbsolute(issuer)) return undefined;
  const owner = path.resolve(issuer);
  const builtIn = path.resolve(projectDirOf(remotionRoot), 'astrid/packs/rendering');
  if (isWithin(builtIn, owner)) return undefined;
  if (!installedPackRoots(env).some((root) => isWithin(root, owner))) return undefined;
  if (isFileWithSuffix(path.resolve(path.dirname(owner), request))) return undefined;
  const target = path.join(builtIn, 'shared', 'element_contracts');
  if (!isFileWithSuffix(target)) throw new Error(`built-in element_contracts module is missing: ${target}`);
  return target;
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
          const selected = selectedHostResource({remotionRoot, issuer, request: data.request}) ??
            installedPackElementContracts({remotionRoot, issuer, request: data.request});
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
