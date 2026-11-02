"""Pinned Node build program, packaged with the Python module."""

BUILDER_SOURCE = r'''
'use strict';
// The Python wrapper authenticates this esbuild API and its native binary.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const os = require('node:os');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const root = input.stage_root;
const sourceDigest = input.source_digest;
const shared = input.toolchain.shared_modules;
const esbuild = require(input.esbuild_api);
const hash = b => crypto.createHash('sha256').update(b).digest('hex');
const canonical = value => {
  if (Array.isArray(value)) return '[' + value.map(canonical).join(',') + ']';
  if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map(k => JSON.stringify(k) + ':' + canonical(value[k])).join(',') + '}';
  return JSON.stringify(value);
};
function hasComputedImport(code) {
  const re = /\bimport\s*\(/g;
  for (const m of code.matchAll(re)) {
    // Only a single quoted literal is allowed. Check the closing parenthesis,
    // too: a literal prefix followed by concatenation is still computed.
    if (!/^\s*(?:"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')\s*\)/.test(code.slice(m.index + m[0].length))) return true;
  }
  return false;
}
const fail = (code, reason, fix) => ({version: 1, ok: false, pack_id: input.pack_id, code, reason, fix, diagnostics: []});
const inside = p => p !== root && p.startsWith(root + path.sep);
function confined(p) {
  const absolute = path.resolve(p);
  if (!inside(absolute) || absolute !== fs.realpathSync(absolute)) throw Error('unsafe path');
  if (absolute.split(path.sep).includes('node_modules') || absolute.split(path.sep).includes('.build')) throw Error('unsafe path');
  return absolute;
}
const plugin = {
  name: 'confined-pack-imports',
  setup(build) {
    // Parse and lower each source before bundling: esbuild otherwise expands
    // template imports as globs and reads them before they can be refused.
    build.onLoad({filter: /\.(?:[cm]?[jt]s|[jt]sx)$/}, async args => {
      try {
        const source = fs.readFileSync(confined(args.path), 'utf8');
        const ext = path.extname(args.path).slice(1);
        const loader = ext === 'tsx' || ext === 'jsx' ? ext : ext.endsWith('ts') ? 'ts' : 'js';
        const parsed = await esbuild.transform(source, {loader, format: 'esm', jsx: 'automatic', target: 'es2020', logLevel: 'silent'});
        if (hasComputedImport(parsed.code))
          return {errors: [{text:'Computed module import is forbidden', detail:'unsafe_import'}]};
        return {contents: source, loader, resolveDir: path.dirname(args.path)};
      } catch (_) { return {errors: [{text:'Could not parse pack source'}]}; }
    });
    build.onResolve({filter: /.*/}, async args => {
      if (args.pluginData && args.pluginData.checked) return;
      try {
        if (args.kind === 'entry-point') return {path: confined(args.path)};
        if (Object.hasOwn(shared, args.path)) return {path: args.path, external: true};
        const cssRelative = (args.kind === 'url-token' || args.kind === 'import-rule') && !args.path.startsWith('/') && !args.path.includes(':');
        if (!args.path.startsWith('./') && !args.path.startsWith('../') && !cssRelative) throw Error('unknown module');
        if (/[?#\\*]/.test(args.path)) throw Error('unsafe specifier');
        const specifier = cssRelative && !args.path.startsWith('.') ? './' + args.path : args.path;
        const result = await build.resolve(specifier, {resolveDir: args.resolveDir, kind: args.kind, pluginData: {checked: true}});
        if (result.errors.length || result.external || result.namespace !== 'file') throw Error('unresolved path');
        return {path: confined(result.path)};
      } catch (_) {
        return {errors: [{text: 'Unsafe or unresolved pack import', detail: 'unsafe_import'}]};
      }
    });
  },
};
function mime(name) {
  const ext = path.extname(name).toLowerCase();
  return ({'.js':'text/javascript','.css':'text/css','.png':'image/png','.jpg':'image/jpeg','.jpeg':'image/jpeg','.svg':'image/svg+xml','.webp':'image/webp','.gif':'image/gif','.woff':'font/woff','.woff2':'font/woff2'})[ext];
}
async function main() {
  const virtualOut = path.join(root, '.astrid-build-output');
  let result;
  try {
    result = await esbuild.build({entryPoints: [path.join(root, input.entry)], outdir: virtualOut,
      bundle: true, format: 'esm', platform: 'browser', target: 'es2020', splitting: true,
      jsx: 'automatic', preserveSymlinks: true, entryNames: 'index', chunkNames: 'chunks/[name]-[hash]', assetNames: 'assets/[name]-[hash]',
      loader: {'.png':'file','.jpg':'file','.jpeg':'file','.svg':'file','.webp':'file','.gif':'file','.woff':'file','.woff2':'file'},
      write: false, metafile: true, logLevel: 'silent', plugins: [plugin]});
  } catch (e) {
    const unsafe = e.errors && e.errors.some(x => x.detail === 'unsafe_import');
    return fail(unsafe ? 'unsafe_import' : 'compile_failed', unsafe ? 'Unsafe or unresolved pack import.' : 'Pack compilation failed.', 'Check the pack imports and source.');
  }
  // Warnings include unresolved computed import() expressions. Fail closed.
  if (result.warnings.length) return fail('unsafe_import', 'Pack contains unsupported imports.', 'Use confined literal imports.');
  const files = new Map();
  for (const f of result.outputFiles) {
    const logical = path.relative(virtualOut, f.path).split(path.sep).join('/');
    if (!logical || logical.startsWith('../') || logical.includes('\\') || logical.includes('?') || logical.includes('#') || logical.normalize('NFC') !== logical || /[\x00-\x1f\x7f]/.test(logical) || logical.split('/').some(x => !x || x === '.' || x === '..') || !mime(logical)) return fail('unsafe_output', 'Builder emitted an unsafe output path.', 'Use supported output paths and assets.');
    if ([...files.keys()].some(x => x.toLowerCase() === logical.toLowerCase())) return fail('unsafe_output', 'Builder emitted colliding output paths.', 'Rename the conflicting files.');
    if (logical.endsWith('.js') && hasComputedImport(f.text)) return fail('unsafe_import', 'Computed module import is forbidden.', 'Use literal import paths.');
    files.set(logical, Buffer.from(f.contents));
  }
  if (!files.has('index.js')) return fail('incomplete_artifacts', 'Builder did not emit an entry.', 'Check the pack entry.');
  // esbuild metadata enumerates every emitted import and URL. No unresolved or
  // external output may remain except an exact locked singleton specifier.
  let usesSdk = false;
  for (const [out, info] of Object.entries(result.metafile.outputs)) {
    const current = path.relative(virtualOut, path.resolve(root, out)).split(path.sep).join('/');
    if (!files.has(current)) return fail('unsafe_output', 'Output metadata is inconsistent.', 'Check the pinned builder.');
    if (current === 'index.js' && !info.exports.includes('default')) return fail('invalid_request', 'Entry has no default export.', 'Export the SDK extension as default.');
    for (const imp of info.imports) {
      if (imp.external) {
        if (imp.path === '@astrid/editor-sdk') usesSdk = true;
        if (!Object.hasOwn(shared, imp.path)) return fail('unsafe_import', 'Unapproved external import.', 'Use a locked singleton.');
      } else {
        // Metafile paths are relative to the working directory, unlike JS URLs.
        const target = path.relative(virtualOut, path.resolve(root, imp.path)).split(path.sep).join('/');
        if (!files.has(target)) return fail('incomplete_artifacts', 'Referenced output is missing.', 'Check the pack build.');
      }
    }
  }
  if (!usesSdk) return fail('invalid_request', 'Entry does not import the editor SDK.', 'Export an extension defined by the editor SDK.');
  const graph = [...files].filter(([n]) => n !== 'index.js').sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0).map(([logical_path, bytes]) => ({logical_path, sha256: hash(bytes), size: bytes.length, mime: mime(logical_path)}));
  const graphDigest = hash(Buffer.from(canonical(graph)));
  const entry = Buffer.concat([Buffer.from(`/* astrid-graph-index-sha256:${graphDigest} */\n`), files.get('index.js')]);
  files.set('index.js', entry);
  const digest = hash(entry);
  const buildRoot = path.join(root, '.build');
  if (fs.existsSync(buildRoot) && (fs.lstatSync(buildRoot).isSymbolicLink() || !fs.statSync(buildRoot).isDirectory())) return fail('unsafe_output', 'Build directory is unsafe.', 'Use a private stage.');
  fs.mkdirSync(buildRoot, {recursive:true});
  const tmp = fs.mkdtempSync(path.join(buildRoot, '.tmp-'));
  try {
    for (const [name, bytes] of files) {
      const dest = path.join(tmp, name);
      fs.mkdirSync(path.dirname(dest), {recursive:true});
      fs.writeFileSync(dest, bytes, {flag:'wx'});
    }
    const entryDest = path.join(buildRoot, sourceDigest + '.js');
    const graphDest = path.join(buildRoot, sourceDigest);
    for (const dest of [entryDest, graphDest]) if (fs.existsSync(dest) && fs.lstatSync(dest).isSymbolicLink()) return fail('unsafe_output', 'Existing output is unsafe.', 'Use a clean private stage.');
    fs.rmSync(entryDest, {force:true});
    fs.rmSync(graphDest, {force:true, recursive:true});
    fs.renameSync(path.join(tmp, 'index.js'), entryDest);
    fs.renameSync(tmp, graphDest);
  } finally { fs.rmSync(tmp, {force:true, recursive:true}); }
  const manifestFiles = [...files].sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0).map(([name, bytes]) => ({path:name, disk:name === 'index.js' ? `.build/${sourceDigest}.js` : `.build/${sourceDigest}/${name}`, sha256:hash(bytes), size:bytes.length, mime:mime(name)}));
  return {version:1, ok:true, pack_id:input.pack_id, source_digest:sourceDigest, artifact_manifest:{version:1, packId:input.pack_id, sourceDigest, digest, graphIndexDigest:graphDigest, toolchainDigest:input.toolchain.lock_sha256, entry:'index.js', extensionManifest:input.extension_manifest, sharedModules:shared, files:manifestFiles}};
}
main().then(r => process.stdout.write(JSON.stringify(r))).catch(() => process.stdout.write(JSON.stringify(fail('io_failed', 'Build output could not be written.', 'Check the private stage.'))));

'''
