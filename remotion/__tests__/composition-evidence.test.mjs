import test from 'node:test';
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {mkdtempSync, mkdirSync, writeFileSync, symlinkSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import {compositionEvidence, inventory} from '../src/composition-evidence.mjs';

const sha = value => createHash('sha256').update(value).digest('hex');
function fixture(t) {
  const root = mkdtempSync(join(tmpdir(), 'composition-evidence-'));
  t.after(() => rmSync(root, {recursive: true, force: true}));
  const project = join(root, 'project');
  const pkg = join(project, 'node_modules/@banodoco/timeline-composition');
  const bundle = join(root, 'bundle');
  mkdirSync(pkg, {recursive: true});
  mkdirSync(join(project, 'artifacts'), {recursive: true});
  mkdirSync(bundle);
  writeFileSync(join(project, 'package.json'), JSON.stringify({dependencies: {'@banodoco/timeline-composition': 'file:artifacts/c.tgz'}}));
  writeFileSync(join(project, 'artifacts/c.tgz'), 'archive');
  writeFileSync(join(pkg, 'package.json'), JSON.stringify({name: '@banodoco/timeline-composition', version: '1'}));
  writeFileSync(join(pkg, 'index.js'), 'composition');
  writeFileSync(join(bundle, 'index.js'), 'bundle');
  return {root, project, pkg, bundle};
}

test('records deterministic candidates, never an execution event', t => {
  const f = fixture(t);
  const result = compositionEvidence(f.project, f.bundle);
  assert.equal(result.composition_archive_sha256, sha('archive'));
  assert.equal(result.resolved_input_inventory_sha256, sha(JSON.stringify(result.resolved_input_inventory)));
  assert.equal(result.bundle_candidate.sha256, sha(JSON.stringify(result.bundle_candidate.identity)));
  assert.deepEqual(result, compositionEvidence(f.project, f.bundle));
  assert.equal(result.event, undefined);
  writeFileSync(join(f.bundle, 'index.js'), 'different bundle');
  assert.notEqual(compositionEvidence(f.project, f.bundle).bundle_candidate.sha256, result.bundle_candidate.sha256);
  writeFileSync(join(f.pkg, 'index.js'), 'changed composition');
  assert.notEqual(compositionEvidence(f.project, f.bundle).resolved_input_inventory_sha256, result.resolved_input_inventory_sha256);
  writeFileSync(join(f.project, 'artifacts/c.tgz'), 'changed archive');
  assert.notEqual(compositionEvidence(f.project, f.bundle).composition_archive_sha256, result.composition_archive_sha256);
});

test('refuses absent archive, symlinks, empty bundle and wrong package identity', t => {
  const f = fixture(t);
  const manifest = join(f.project, 'package.json');
  writeFileSync(manifest, JSON.stringify({dependencies: {'@banodoco/timeline-composition': 'file:../escape.tgz'}}));
  assert.throws(() => compositionEvidence(f.project, f.bundle));
  writeFileSync(manifest, JSON.stringify({dependencies: {'@banodoco/timeline-composition': 'file:artifacts/c.tgz'}}));
  symlinkSync(join(f.pkg, 'index.js'), join(f.pkg, 'link'));
  assert.throws(() => compositionEvidence(f.project, f.bundle), /symlink/);
  rmSync(join(f.pkg, 'link'));
  writeFileSync(join(f.pkg, 'package.json'), '{"name":"wrong","version":"1"}');
  assert.throws(() => compositionEvidence(f.project, f.bundle), /identity/);
  rmSync(join(f.bundle, 'index.js'));
  assert.throws(() => inventory(f.bundle), /empty/);
});
