const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');

const root = path.resolve(__dirname, '..');
const packageIndex = path.join(root, 'node_modules/@banodoco/timeline-composition/dist/index.js');
const loader = require('../timeline-composition-legacy-loader.cjs');

test('adapts only the pinned composition entry point and retains package sequencing', () => {
  const source = fs.readFileSync(packageIndex, 'utf8');
  const output = loader.call({rootContext: root, resourcePath: packageIndex}, source);
  assert.match(output, /incomingTransitions: sorted\.map/);
  assert.match(output, /return \{ T: legacyFrames \}/);
  assert.match(output, /legacyTransitionStyle\(clip, frame, fps\)/);
  assert.match(output, /wrapLegacyClipPhase\("continuous"/);
  assert.match(output, /wrapLegacyClipPhase\("entrance"/);
  assert.match(output, /wrapLegacyClipPhase\("exit"/);
  assert.match(output, /compileCanonicalTransitions\(\[\{ track, clips: sorted \}\]/);
  assert.match(output, /style: legacyStyle \? \{ overflow: "hidden", \.\.\.legacyStyle \} : \{ opacity: evaluated\.opacity \}/);
  assert.throws(() => loader.call({rootContext: root, resourcePath: `${packageIndex}.copy`}, source), /unexpected module/);
  assert.throws(() => loader.call({rootContext: root, resourcePath: packageIndex}, source + '\n'), /pinned 0\.0\.19 entry/);
});
