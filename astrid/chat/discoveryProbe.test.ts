import { test } from 'bun:test';
import assert from 'node:assert/strict';
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { discoveryProbe, discoveryProbeInvocation } from './discoveryProbe.ts';
import { processHasExited } from './testProcess.ts';

test('Windows discovery probes route npm command shims through cmd.exe', () => {
  assert.deepEqual(discoveryProbeInvocation('C:\\Program Files\\Astrid\\astrid.cmd', ['agent', 'models', '--json'], 'win32'), {
    command: 'cmd.exe',
    args: ['/d', '/s', '/c', '""C:\\Program Files\\Astrid\\astrid.cmd" "agent" "models" "--json""'],
    windowsVerbatimArguments: true,
  });
  assert.deepEqual(discoveryProbeInvocation('C:\\Program Files\\Astrid\\astrid.exe', ['agent', 'models'], 'win32'), {
    command: 'C:\\Program Files\\Astrid\\astrid.exe', args: ['agent', 'models'],
  });
  assert.deepEqual(discoveryProbeInvocation('/usr/bin/astrid', ['agent', 'models'], 'linux'), {
    command: '/usr/bin/astrid', args: ['agent', 'models'],
  });
});

test.skipIf(process.platform !== 'win32')('native Windows discovery launches a harmless .cmd probe through cmd.exe', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'astrid-windows-probe-'));
  const shim = join(directory, 'probe.cmd');
  writeFileSync(shim, '@echo off\r\necho windows-probe-ok\r\n', 'utf8');
  try {
    const result = await discoveryProbe(shim, [], { timeout: 5_000 });
    assert.equal(result.stdout.trim(), 'windows-probe-ok');
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
});

test('a discovery deadline kills a wrapper and its hanging CLI child', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'astrid-probe-tree-'));
  const pids = join(directory, 'pids');
  const wrapper = join(directory, 'wrapper.js');
  writeFileSync(wrapper, `
const fs = require('node:fs');
fs.appendFileSync(${JSON.stringify(pids)}, process.pid + '\\n');
require('node:child_process').spawn(process.execPath, ['-e', ${JSON.stringify(`
  require('node:fs').appendFileSync(${JSON.stringify(pids)}, process.pid + '\\n');
  process.on('SIGTERM', () => {});
  setInterval(() => {}, 1000);
`)}], { stdio: 'inherit' });
process.on('SIGTERM', () => {});
setInterval(() => {}, 1000);
`);
  try {
    await assert.rejects(discoveryProbe(process.execPath, [wrapper], { timeout: 1_000 }), { killed: true });
    const children = readFileSync(pids, 'utf8').trim().split('\n').map(Number);
    assert.equal(children.length, 2, 'the probe must actually start its nested CLI');
    for (let attempt = 0; attempt < 100; attempt++) {
      if (children.every(processHasExited)) break;
      await new Promise(resolve => setTimeout(resolve, 10));
    }
    for (const pid of children) assert.equal(processHasExited(pid), true, `process ${pid} must have exited`);
  } finally {
    try { for (const pid of readFileSync(pids, 'utf8').trim().split('\n').map(Number)) process.kill(pid, 'SIGKILL'); } catch { /* Already reaped. */ }
    rmSync(directory, { recursive: true, force: true });
  }
}, 5_000);

test('discovery preserves nonzero status diagnostics for explicit signed-out detection', async () => {
  await assert.rejects(discoveryProbe(process.execPath, ['-e', `
process.stdout.write('status output');
process.stderr.write('Not logged in.');
process.exit(7);
`]), { code: 7, status: 7, signal: null, stdout: 'status output', stderr: 'Not logged in.' });
});

test('an unavailable discovery command rejects promptly', async () => {
  await assert.rejects(discoveryProbe(join(tmpdir(), 'astrid-missing-probe-command'), []), { code: 'ENOENT' });
});
