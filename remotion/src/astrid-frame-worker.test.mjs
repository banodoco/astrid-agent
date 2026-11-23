import assert from 'node:assert/strict';
import test from 'node:test';
// The worker's standalone mode reads stdin; end it before importing in a test.
process.stdin.push(null);
const {_test} = await import('./astrid-frame-worker.mjs');

test('request roots replace prior roots and an ordinary request clears both projections', () => {
  const target = {
    PATH: '/owner/bin',
    KEEP_OWNER_VALUE: 'owner',
    ASTRID_PACKS_PATH: '/owner/packs',
    ASTRID_RENDER_PACK_ROOTS: '[{"root":"/stale"}]',
    ASTRID_RENDER_REGISTRY_ROOT: '/stale/registry',
  };
  const roots = JSON.stringify([{root: '/host/selected', scope: 'pack-selected-elements', kind_roots: {}}]);
  _test.applyFrameRequestEnvironment(target, {
    ASTRID_RENDER_PACK_ROOTS: roots,
    ASTRID_RENDER_REGISTRY_ROOT: '/host/selected-registry',
    TMPDIR: '/task/one',
  });
  assert.equal(target.ASTRID_RENDER_PACK_ROOTS, roots);
  assert.equal(target.ASTRID_RENDER_REGISTRY_ROOT, '/host/selected-registry');
  assert.equal(target.TMPDIR, '/task/one');

  _test.applyFrameRequestEnvironment(target, {TMPDIR: '/task/two'});
  assert.equal('ASTRID_RENDER_PACK_ROOTS' in target, false);
  assert.equal('ASTRID_RENDER_REGISTRY_ROOT' in target, false);
  assert.equal(target.TMPDIR, '/task/two');
  assert.equal('ASTRID_PACKS_PATH' in target, false);
  assert.equal(target.PATH, '/owner/bin');
  assert.equal(target.KEEP_OWNER_VALUE, 'owner');
});

test('PNG frames disable audio registration while keeping video rendering enabled', () => {
  const options = _test.framesOptions({port: 1234, frames: [12], outputDir: '/tmp/frames'}, {}, {id: 'TimelineComposition'});
  assert.equal(options.muted, true);
  assert.equal(options.imageFormat, 'png');
  assert.equal(options.frames[0], 12);
  assert.equal(options.videoEnabled, undefined);
});

test('diagnostics are opt in and retain the bounds browser log', () => {
  const captured = [];
  const bounds = {onBrowserLog: (log) => captured.push(log.text)};
  const disabled = _test.frameDiagnostics({}, {timeline: {}}, bounds);
  assert.equal(disabled.result, null);
  assert.equal(disabled.props.astridFrameDiagnostics, undefined);

  const enabled = _test.frameDiagnostics({diagnostics: true}, {timeline: {}}, bounds);
  assert.equal(enabled.props.astridFrameDiagnostics, true);
  enabled.onBrowserLog({text: 'ASTRID_FRAME_DIAG {"ready":false}'});
  assert.deepEqual(captured, ['ASTRID_FRAME_DIAG {"ready":false}']);
  assert.equal(enabled.result.browser.length, 1);
  assert.equal(enabled.result.browser[0].data, '{"ready":false}');

  const options = _test.framesOptions({port: 1, frames: [4], outputDir: '/tmp/frames'},
    enabled.props, {id: 'TimelineComposition'}, enabled.onBrowserLog, enabled.result);
  options.onStart();
  options.onFrameUpdate(1, 4, 8);
  assert.deepEqual(enabled.result.frameUpdates[0].frame, 4);
});
