// Candidate file evidence for the T-11 renderer observation. This module does
// not observe browser execution and must never itself emit a success event.
import {createHash} from 'node:crypto';
import {lstatSync, readFileSync, readdirSync, realpathSync} from 'node:fs';
import {isAbsolute, join, relative, resolve, sep} from 'node:path';

const LIMIT_FILES = 8192;
const LIMIT_BYTES = 256 * 1024 * 1024;
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const canonical = value => JSON.stringify(value);

function confined(root, path) {
  const rel = relative(root, path);
  return rel !== '' && rel !== '..' && !rel.startsWith(`..${sep}`) && !isAbsolute(rel);
}

function regular(path) {
  const st = lstatSync(path);
  if (!st.isFile() || st.isSymbolicLink()) throw new Error('evidence input is not a regular file');
  return st;
}

// Paths in the inventory are UTF-8, slash-separated and sorted by UTF-8 bytes;
// symlinks and non-regular entries are refused instead of followed.
export function inventory(root) {
  if (!isAbsolute(root) || realpathSync(root) !== root || !lstatSync(root).isDirectory()) {
    throw new Error('evidence root must be an absolute canonical directory');
  }
  const entries = [];
  let visited = 0;
  let size = 0;
  function visit(dir, prefix = '') {
    for (const name of readdirSync(dir).sort((a, b) => Buffer.compare(Buffer.from(a), Buffer.from(b)))) {
      if (++visited > LIMIT_FILES) throw new Error('evidence inventory exceeds limit');
      const path = join(dir, name);
      const rel = prefix ? `${prefix}/${name}` : name;
      const st = lstatSync(path);
      if (st.isSymbolicLink()) throw new Error('evidence symlink refused');
      if (st.isDirectory()) visit(path, rel);
      else if (st.isFile()) {
        size += st.size;
        if (size > LIMIT_BYTES) throw new Error('evidence inventory exceeds limit');
        const bytes = readFileSync(path);
        if (bytes.length !== st.size) throw new Error('evidence file changed during read');
        entries.push({path: rel, size: bytes.length, sha256: hash(bytes)});
      } else throw new Error('evidence special file refused');
    }
  }
  visit(root);
  if (!entries.length) throw new Error('empty evidence inventory');
  entries.sort((a, b) => Buffer.compare(Buffer.from(a.path), Buffer.from(b.path)));
  return entries;
}

// These hashes describe disk inputs only, not the bundler graph or bytes
// executed by Chromium. Callers must treat them as candidates until independently
// bound to the exact execution and authenticated run.
export function compositionEvidence(project, bundle) {
  if (!isAbsolute(project) || resolve(project) !== project || realpathSync(project) !== project) {
    throw new Error('project must be canonical');
  }
  const manifest = JSON.parse(readFileSync(join(project, 'package.json'), 'utf8'));
  const spec = manifest.dependencies?.['@banodoco/timeline-composition'];
  if (typeof spec !== 'string' || !spec.startsWith('file:')) throw new Error('local composition archive required');
  const archive = resolve(project, spec.slice(5));
  if (!confined(project, archive) || realpathSync(archive) !== archive) throw new Error('archive outside project');
  const archiveStat = regular(archive);
  if (archiveStat.size <= 0 || archiveStat.size > LIMIT_BYTES) throw new Error('archive exceeds bounds');
  const archiveBytes = readFileSync(archive);
  if (archiveBytes.length !== archiveStat.size) throw new Error('archive changed during read');
  const packageRoot = join(project, 'node_modules/@banodoco/timeline-composition');
  if (realpathSync(packageRoot) !== packageRoot) throw new Error('package root is not canonical');
  const packageManifest = JSON.parse(readFileSync(join(packageRoot, 'package.json'), 'utf8'));
  if (packageManifest.name !== '@banodoco/timeline-composition' || typeof packageManifest.version !== 'string' || !packageManifest.version) {
    throw new Error('composition package identity invalid');
  }
  const inputs = inventory(packageRoot);
  const bundleFiles = inventory(bundle);
  const identity = {version: 1, files: bundleFiles};
  return {
    composition_archive_sha256: hash(archiveBytes),
    resolved_input_inventory: inputs,
    resolved_input_inventory_sha256: hash(Buffer.from(canonical(inputs))),
    bundle_candidate: {identity, sha256: hash(Buffer.from(canonical(identity)))},
  };
}
