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

test('vendored payload matches the recorded package and retains source Git blob identities', () => {
  const files = (dir) => readdirSync(dir, {withFileTypes: true}).flatMap((entry) => {
    const file = path.join(dir, entry.name);
    if (entry.isDirectory()) return files(file);
    assert.ok(entry.isFile(), `unexpected non-regular payload: ${file}`);
    return [path.relative(vendor, file).split(path.sep).join('/')];
  });
  assert.deepEqual(files(vendor).sort(), source.files.map((file) => file.path).sort());
  assert.equal(source.files.length, source.archive_member_count);
  assert.equal(source.files.reduce((sum, file) => sum + file.bytes, 0), source.unpacked_bytes);
  for (const file of source.files) {
    assert.ok(!path.isAbsolute(file.path) && !file.path.split('/').includes('..'));
    const bytes = readFileSync(path.join(vendor, file.path));
    assert.equal(bytes.length, file.bytes, file.path);
    assert.equal(createHash('sha256').update(bytes).digest('hex'), file.sha256, file.path);
    if (file.source_git_blob) {
      assert.equal(createHash('sha1').update(`blob ${bytes.length}\0`).update(bytes).digest('hex'), file.source_git_blob, file.path);
      assert.equal(file.emitted_from, null, file.path);
    } else {
      assert.ok(file.path.startsWith('typescript/dist/src/') && file.path.endsWith('.js'), file.path);
      assert.ok(source.files.some((sourceFile) => sourceFile.path === file.emitted_from && sourceFile.source_git_blob), file.path);
    }
  }
  assert.equal(source.files.find((file) => file.path === 'package.json').sha256,
    '20b0f15eba3fce002314fb24bba9017c22fd6ab2b72f4931167412e7cec00a7e');
});

test('provenance identifies this run-local development archive without authenticating the tag', () => {
  assert.equal(source.source_commit, '0d748b52f460c2ac5b0865a7316c4c05b73e2555');
  assert.equal(source.source_tree, '28817f14126afb9d9dd6a1f8b619e906493b4f31');
  assert.equal(source.source_parent, 'df38db31a18ada18d3339959b930e7b6bbbf16ea');
  assert.equal(source.source_branch, 'codex/timeline-r4-A-COMPOSITION-UNKNOWN-BOUNDS-PACK-004');
  assert.equal(source.archive, 'projects/A-integration/out/A-Composition-unknown-bounds-pack-004/banodoco-timeline-composition-0.0.7.tgz');
  assert.equal(source.archive_sha256, '02500c55dcdc1883e027d89ab42e2426191fad3ab1fdcd08d3fce1ca51f11032');
  assert.equal(source.archive_sha512, '0f7dd333677fae6299d58a6324a6cafdf2b50ed1deb33bafd483e13a16707d553ba4a22c6d2874078bdab1b22df6d5a41a8766d260111e2589ce849d0657eed6');
  assert.equal(source.archive_bytes, 74276);
  assert.equal(source.archive_member_count, 86);
  assert.equal(source.unpacked_bytes, 265158);
  assert.equal(source.npm_integrity, 'sha512-D33TM2d/rmKZ1YpjJKbK/fK1DtHeszuv1IPhOhZwfVU7pKIsbSh0B4vasbIt9tWkGodm0mARHiWJzoSdBlfu1g==');
  assert.equal(source.producer_handback, 'projects/I-story-motion-audio/receipts/I-EXT-COMPONENT-UNKNOWN-BOUNDS-006-handback.md');
  assert.equal(source.producer_handback_sha256, 'ae569f046c193dc87f7b55684239e413e823a527e62877f5695f9a9b822013bd');
  assert.equal(source.required_ref, 'v0.0.7/e2ab21a7');
  assert.equal(source.required_ref_authenticated, false);
  assert.match(source.scope, /Run-local development package/);
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
