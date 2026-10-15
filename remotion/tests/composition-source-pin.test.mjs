import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {readFileSync, readdirSync} from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import test from 'node:test';

const root = fileURLToPath(new URL('../', import.meta.url));
const vendor = path.join(root, 'vendor/timeline-composition');
const json = (file) => JSON.parse(readFileSync(file, 'utf8'));
const source = json(path.join(root, 'vendor/timeline-composition.source.json'));
const manifest = json(path.join(root, 'package.json'));
const lock = json(path.join(root, 'package-lock.json'));
const packed = json(path.join(vendor, 'package.json'));

test('vendored payloads retain the exact archive inventory and source/output identities', () => {
  const files = (dir) => readdirSync(dir, {withFileTypes: true}).flatMap((entry) => {
    const file = path.join(dir, entry.name);
    if (entry.isDirectory()) return files(file);
    assert.ok(entry.isFile(), `unexpected non-regular payload: ${file}`);
    return [path.relative(vendor, file).split(path.sep).join('/')];
  });
  assert.deepEqual(files(vendor).sort(), source.files.map((file) => file.path).sort());
  assert.equal(source.archive_file_count, 89);
  assert.equal(source.archive_unpacked_bytes, 289845);
  assert.equal(source.source_file_count, 45);
  assert.equal(source.compiled_file_count, 44);
  assert.equal(source.files.length, 89);
  assert.equal(source.files.reduce((sum, file) => sum + file.bytes, 0), source.archive_unpacked_bytes);
  for (const file of source.files) {
    assert.ok(!path.isAbsolute(file.path) && !file.path.split('/').includes('..'));
    const bytes = readFileSync(path.join(vendor, file.path));
    const digest = createHash('sha256').update(bytes).digest('hex');
    assert.equal(bytes.length, file.bytes, file.path);
    assert.equal(digest, file.sha256, file.path);
    assert.notEqual(Boolean(file.source_git_blob), Boolean(file.emitted_output_sha256), file.path);
    if (file.source_git_blob) {
      assert.equal(createHash('sha1').update(`blob ${bytes.length}\0`).update(bytes).digest('hex'), file.source_git_blob, file.path);
    } else {
      assert.equal(file.emitted_output_sha256, digest, file.path);
    }
  }
  assert.equal(source.files.filter((file) => file.source_git_blob).length, 45);
  assert.equal(source.files.filter((file) => file.emitted_output_sha256).length, 44);
  assert.equal(source.files.find((file) => file.path === 'package.json').sha256,
    '20b0f15eba3fce002314fb24bba9017c22fd6ab2b72f4931167412e7cec00a7e');
});

test('provenance identifies the admitted run-local development archive without authenticating the tag', () => {
  assert.equal(source.source_commit, 'df38db31a18ada18d3339959b930e7b6bbbf16ea');
  assert.equal(source.source_tree, '07f80d208f314d73bfea89f6d18579d4e6626387');
  assert.equal(source.source_parent, '9cf3fce5fa72cd02eca4a0ec65da864351ca5bbb');
  assert.equal(source.archive, 'projects/A-integration/out/A-Composition-declared-transition-artifact-001/banodoco-timeline-composition-0.0.7.tgz');
  assert.equal(source.archive_sha256, '24d9c149410e88b08c5c42f103721fbc5ca2542267c86ef150ed8f108383f2f3');
  assert.equal(source.archive_bytes, 79831);
  assert.equal(source.archive_sha512_base64, 'qqv8UzBRUXmTWrtppSkrWSea3zAro+0Y+VmDi+BV0WCp22Y/D+y5fjsCnY5naU8oBVUjVA6HUb/Y7VB2sttN5Q==');
  assert.equal(source.npm_integrity, `sha512-${source.archive_sha512_base64}`);
  assert.equal(source.npm_shasum, 'ae57bafeb1eb61f8f1d5478708c57d058c747464');
  assert.equal(source.producer_handback, 'projects/A-integration/receipts/A-Composition-declared-transition-artifact-001-handback.md');
  assert.equal(source.producer_handback_sha256, 'c63e2c5859131e847fd7f620cec9d9bf2834aa8ce51704e352f83703bcd0a7e5');
  assert.equal(source.archive_audit, 'projects/A-integration/evidence/A-Composition-declared-transition-artifact-001/archive-audit.json');
  assert.equal(source.archive_audit_sha256, 'c24ad1104fc102c37f13bb8246bd864285c01d2d71f4e817ef027efb287a3939');
  assert.equal(source.emitted_output_manifest, 'projects/A-integration/evidence/A-Composition-declared-transition-artifact-001/emitted-output-manifest.json');
  assert.equal(source.emitted_output_manifest_sha256, '6ccfcd41f6bdea533f65267bdae5ad8acb2d160e5c3ad460c7cdd28e48fe7e97');
  assert.equal(source.required_ref, 'v0.0.7/e2ab21a7');
  assert.equal(source.required_ref_authenticated, false);
  assert.match(source.scope, /run-local development source only/);
  assert.equal(packed.name, source.package);
  assert.equal(packed.version, source.package_version);
});

test('manifest and lock consume the local package with matching linked metadata', () => {
  const name = '@banodoco/timeline-composition';
  assert.equal(lock.lockfileVersion, 3);
  assert.equal(manifest.dependencies[name], 'file:vendor/timeline-composition');
  assert.deepEqual(lock.packages[''].dependencies, manifest.dependencies);
  assert.deepEqual(lock.packages[''].devDependencies, manifest.devDependencies);
  assert.deepEqual(lock.packages[`node_modules/${name}`], {
    resolved: 'vendor/timeline-composition', link: true,
  });
  const metadata = Object.fromEntries(
    ['name', 'version', 'dependencies', 'devDependencies', 'peerDependencies']
      .filter((key) => key in packed).map((key) => [key, packed[key]]),
  );
  assert.deepEqual(lock.packages['vendor/timeline-composition'], metadata);
  // The admitted package's own historical devDependencies remain byte-exact;
  // this assertion covers the host dependency closure, not that dev subtree.
  for (const group of ['dependencies', 'devDependencies']) {
    for (const [name, version] of Object.entries(manifest[group])) {
      if (name === 'remotion' || name.startsWith('@remotion/')) {
        assert.equal(version, '4.0.509', name);
        assert.equal(lock.packages[`node_modules/${name}`].version, version, name);
      }
    }
  }
});

test('public evaluation export resolves to an inventoried pure source file', () => {
  for (const target of Object.values(packed.exports['./evaluation'])) {
    assert.equal(target, './typescript/src/evaluation.ts');
    assert.ok(source.files.some((file) => `./${file.path}` === target));
  }
});
