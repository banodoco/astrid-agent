import { spyOn, test } from 'bun:test';
import assert from 'node:assert/strict';
import { chmodSync, existsSync, mkdtempSync, readFileSync, realpathSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { delimiter, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { getAstridAgentModelCatalog } from './astrid-agent-models.ts';
import { createAstridAcpHttpServer } from './astrid-acp-bridge.ts';
import { discoverLocalAgents } from './astrid-local-agents.ts';
import { AcpProcessHost } from './acpProcessHost.ts';
import { bridgeTestClient } from './bridge-test-client.ts';
import { localAgentCatalogSchema } from './localAgents.ts';
import { processHasExited } from './testProcess.ts';

const catalog = {
  schemaVersion: 1,
  currentModel: 'openai/retired-model',
  providers: [
    {
      id: 'openai', name: 'OpenAI', signedIn: true,
      usable: true, reason: null, login: 'oauth',
      models: [{ id: 'gpt-4.1', name: 'GPT 4.1', selector: 'openai/gpt-4.1' }],
      usage: [{ fetchedAt: 123, limits: [{
        label: 'Monthly', amount: { unit: 'tokens', used: 20, limit: 100, remaining: 80, usedFraction: 0.2, remainingFraction: 0.8 },
        window: { durationMs: 2_592_000_000, resetsAt: 456 }, status: 'warning',
      }] }],
    },
    {
      id: 'local', name: 'Local models', signedIn: false,
      usable: true, reason: null, login: 'local',
      models: [{ id: 'llama', name: 'Llama', selector: 'local/llama' }],
    },
    { id: 'openrouter', name: 'OpenRouter', signedIn: false, usable: false,
      reason: 'needs_sign_in', login: 'api-key', recommended: true, models: [] },
    { id: 'unknown', name: 'Unknown', signedIn: false, usable: false,
      reason: 'no_available_models', models: [] },
  ],
};

function fakeAstrid(directory: string): { command: string; calls: string; models: string; selection: string } {
  const command = join(directory, 'astrid');
  const calls = join(directory, 'calls.jsonl');
  const models = join(directory, 'models.json');
  const selection = join(directory, 'selection.json');
  writeFileSync(models, JSON.stringify(catalog));
  writeFileSync(selection, JSON.stringify(catalog));
  writeFileSync(command, `#!/usr/bin/env bun
const fs = require('node:fs');
fs.appendFileSync(${JSON.stringify(calls)}, JSON.stringify({ argv: process.argv.slice(2), cwd: process.cwd(), profile: process.env.OMP_PROFILE ?? null }) + '\\n');
if (process.env.ASTRID_FAKE_FAIL === '1') {
  process.stderr.write('credential-secret-private diagnostic');
  process.exit(9);
}
if (process.env.ASTRID_FAKE_BAD_JSON === '1') {
  process.stdout.write('{malformed');
} else if (process.argv[2] === 'agent' && process.argv[3] === 'models' && process.argv[4] === '--json') {
  process.stdout.write(fs.readFileSync(${JSON.stringify(models)}, 'utf8'));
} else {
  process.stderr.write('credential-secret-private diagnostic');
  process.exit(8);
}
`, { mode: 0o755 });
  chmodSync(command, 0o755);
  return { command, calls, models, selection };
}

function readCalls(path: string): unknown[] {
  return readFileSync(path, 'utf8').trim().split('\n').filter(Boolean).map(line => JSON.parse(line));
}

function writeModelCommand(directory: string, script: string): string {
  if (process.platform === 'win32') {
    const scriptPath = join(directory, 'astrid-model-cli.js');
    const command = join(directory, 'astrid.cmd');
    writeFileSync(scriptPath, script, 'utf8');
    writeFileSync(command, `@echo off\r\n"${process.execPath}" "${scriptPath}" %*\r\n`, 'utf8');
    return command;
  }
  const command = join(directory, 'astrid');
  writeFileSync(command, `#!${process.execPath}\n${script}\n`, { mode: 0o755 });
  chmodSync(command, 0o755);
  return command;
}

test('model discovery allows sequential OMP startup to exceed the old ten-second cutoff', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'astrid-slow-models-'));
  const token = 'slow-models-test';
  const { bridge, server } = createAstridAcpHttpServer({
    config: { token, port: 0, cwd: directory, command: writeModelCommand(directory,
      `setTimeout(() => process.stdout.write(${JSON.stringify(JSON.stringify(catalog))}), 11_000);`) },
    chatRegistry: {} as never,
  });
  try {
    await new Promise<void>((resolve, reject) => {
      server.once('error', reject);
      server.listen(0, '127.0.0.1', resolve);
    });
    const address = server.address();
    assert.ok(address && typeof address !== 'string');
    const response = await fetch(`http://127.0.0.1:${address.port}/models`, {
      headers: { authorization: `Bearer ${token}`, 'x-astrid-bridge-version': 'v1' },
    });
    assert.equal(response.status, 200);
    const result = await response.json() as typeof catalog;
    assert.equal(result.currentModel, catalog.currentModel);
    assert.equal(result.providers.length, catalog.providers.length);
  } finally {
    server.close();
    await bridge.close();
    rmSync(directory, { recursive: true, force: true });
  }
}, 16_000);

test('agent model catalog maps the CLI response to the safe ACP projection and preserves CLI state', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'astrid-agent-models-'));
  try {
    const fake = fakeAstrid(directory);
    const result = await getAstridAgentModelCatalog({ command: fake.command, cwd: directory, profile: 'fixture-profile' });
    assert.deepEqual(result, {
      providers: [
        {
          id: 'openai', name: 'OpenAI', signedIn: true, models: [{ id: 'gpt-4.1', name: 'GPT 4.1' }],
          usable: true, reason: null, login: 'oauth',
          usage: { limits: [{ unit: 'tokens', used: 20, limit: 100, remaining: 80, usedFraction: 0.2, remainingFraction: 0.8, durationMs: 2_592_000_000, resetsAt: 456, status: 'warning' }] },
        },
        { id: 'local', name: 'Local models', signedIn: false, usable: true, reason: null,
          login: 'local', models: [{ id: 'llama', name: 'Llama' }] },
        { id: 'openrouter', name: 'OpenRouter', signedIn: false, usable: false,
          reason: 'needs_sign_in', login: 'api-key', recommended: true, models: [] },
        { id: 'unknown', name: 'Unknown', signedIn: false, usable: false,
          reason: 'no_available_models', models: [] },
      ],
      currentModel: 'openai/retired-model',
    });
    assert.deepEqual(readCalls(fake.calls), [{
      argv: ['agent', 'models', '--json'], cwd: realpathSync(directory), profile: 'fixture-profile',
    }]);
    const serialized = JSON.stringify(result);
    for (const privateValue of ['selector', 'fetchedAt', 'Monthly']) assert.equal(serialized.includes(privateValue), false);
  } finally {
    delete process.env.ASTRID_FAKE_FAIL;
    delete process.env.ASTRID_FAKE_BAD_JSON;
    rmSync(directory, { recursive: true, force: true });
  }
});

test('agent catalog discovery never invokes the global default mutation', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'astrid-agent-readonly-'));
  try {
    const fake = fakeAstrid(directory);
    const before = readFileSync(fake.selection, 'utf8');
    await getAstridAgentModelCatalog({ command: fake.command, cwd: directory, profile: 'fixture-profile' });
    await getAstridAgentModelCatalog({ command: fake.command, cwd: directory, profile: 'fixture-profile' });
    assert.equal(readFileSync(fake.selection, 'utf8'), before);
    assert.deepEqual(readCalls(fake.calls), Array.from({ length: 2 }, () => ({
      argv: ['agent', 'models', '--json'], cwd: realpathSync(directory), profile: 'fixture-profile',
    })));
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

test('agent model adapter rejects unsupported schemas and malformed JSON, and redacts CLI diagnostics', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'astrid-agent-invalid-'));
  const warning = spyOn(console, 'warn').mockImplementation(() => {});
  try {
    const fake = fakeAstrid(directory);
    writeFileSync(fake.models, JSON.stringify({ ...catalog, schemaVersion: 2 }));
    await assert.rejects(getAstridAgentModelCatalog({ command: fake.command, cwd: directory }));

    writeFileSync(fake.models, JSON.stringify(catalog));
    process.env.ASTRID_FAKE_BAD_JSON = '1';
    await assert.rejects(getAstridAgentModelCatalog({ command: fake.command, cwd: directory }));
    delete process.env.ASTRID_FAKE_BAD_JSON;

    process.env.ASTRID_FAKE_FAIL = '1';
    await assert.rejects(getAstridAgentModelCatalog({ command: fake.command, cwd: directory }), error => {
      assert.equal((error as Error).message.includes('credential-secret-private'), false);
      return true;
    });
    const diagnostics = warning.mock.calls.map(call => call.join(' ')).join('\n');
    assert.match(diagnostics, /exit=9/);
    assert.doesNotMatch(diagnostics, /credential-secret-private/);
  } finally {
    warning.mockRestore();
    delete process.env.ASTRID_FAKE_FAIL;
    delete process.env.ASTRID_FAKE_BAD_JSON;
    rmSync(directory, { recursive: true, force: true });
  }
});

test('agent model launch logs ENOENT without exposing child output', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'astrid-agent-missing-'));
  const warning = spyOn(console, 'warn').mockImplementation(() => {});
  try {
    await assert.rejects(getAstridAgentModelCatalog({ command: join(directory, 'missing-astrid'), cwd: directory }));
    const diagnostics = warning.mock.calls.map(call => call.join(' ')).join('\n');
    assert.match(diagnostics, /error=ENOENT/);
    assert.doesNotMatch(diagnostics, /credential|secret|stdout|stderr/i);
  } finally {
    warning.mockRestore();
    rmSync(directory, { recursive: true, force: true });
  }
});

test('agent catalog discovery kills a hanging CLI and rejects its partial output', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'astrid-agent-hanging-'));
  const pidFile = join(directory, 'child.pid');
  const warning = spyOn(console, 'warn').mockImplementation(() => {});
  try {
    const command = join(directory, 'astrid');
    writeFileSync(command, `#!${process.execPath}
require('node:fs').writeFileSync(${JSON.stringify(pidFile)}, String(process.pid));
process.on('SIGTERM', () => {});
process.stdout.write(${JSON.stringify(JSON.stringify(catalog))});
process.stderr.write('credential-secret-private diagnostic');
setInterval(() => {}, 1000);
`, { mode: 0o755 });
    const started = Date.now();
    await assert.rejects(getAstridAgentModelCatalog({ command, cwd: directory }), error => {
      assert.equal((error as Error).message, 'Astrid model operation failed. Try again.');
      return true;
    });
    const diagnostics = warning.mock.calls.map(call => call.join(' ')).join('\n');
    assert.match(diagnostics, /signal=SIGKILL/);
    assert.match(diagnostics, /timeout=true/);
    assert.doesNotMatch(diagnostics, /credential-secret-private/);
    assert.ok(Date.now() - started < 35_000, 'a hanging catalog must finish before the bounded probe deadline');
    const pid = Number(readFileSync(pidFile, 'utf8'));
    assert.equal(processHasExited(pid), true, `process ${pid} must have exited`);
  } finally {
    warning.mockRestore();
    if (existsSync(pidFile)) {
      try { process.kill(Number(readFileSync(pidFile, 'utf8')), 'SIGKILL'); } catch { /* Already reaped. */ }
    }
    rmSync(directory, { recursive: true, force: true });
  }
}, 40_000);

test('the Python Astrid model wrapper and its nested OMP CLI exit on discovery timeout', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'astrid-python-probe-tree-'));
  const pids = join(directory, 'pids.jsonl');
  const previous = { OMP_BIN: process.env.OMP_BIN, PYTHONPATH: process.env.PYTHONPATH };
  try {
    const command = process.env.ASTRID_TEST_ASTRID ?? join(directory, 'astrid');
    if (!process.env.ASTRID_TEST_ASTRID) {
      writeFileSync(command, '#!/usr/bin/env python3\nfrom astrid.omp_agent import main\nraise SystemExit(main())\n', { mode: 0o755 });
    }
    const omp = join(directory, 'omp');
    writeFileSync(omp, `#!${process.execPath}
const fs = require('node:fs');
fs.appendFileSync(${JSON.stringify(pids)}, JSON.stringify({ pid: process.ppid, role: 'wrapper' }) + '\\n');
fs.appendFileSync(${JSON.stringify(pids)}, JSON.stringify({ pid: process.pid, role: 'omp', args: process.argv.slice(2) }) + '\\n');
require('node:child_process').spawn(process.execPath, ['-e', ${JSON.stringify(`
require('node:fs').appendFileSync(${JSON.stringify(pids)}, JSON.stringify({ pid: process.pid, role: 'nested-cli' }) + '\\n');
process.on('SIGTERM', () => {});
setInterval(() => {}, 1000);
`)}], { stdio: 'inherit' });
process.on('SIGTERM', () => {});
setInterval(() => {}, 1000);
`, { mode: 0o755 });
    process.env.OMP_BIN = omp;
    process.env.PYTHONPATH = [fileURLToPath(new URL('../../', import.meta.url)), previous.PYTHONPATH].filter(Boolean).join(delimiter);
    await assert.rejects(getAstridAgentModelCatalog({ command, cwd: directory }), {
      message: 'Astrid model operation failed. Try again.',
    });
    const children = readFileSync(pids, 'utf8').trim().split('\n').map(line => JSON.parse(line) as { pid: number; role: string; args?: string[] });
    assert.deepEqual(children.map(child => child.role), ['wrapper', 'omp', 'nested-cli']);
    assert.deepEqual(children[1]!.args?.slice(0, 3), ['models', 'ls', '--json']);
    for (let attempt = 0; attempt < 100; attempt++) {
      if (children.every(child => processHasExited(child.pid))) break;
      await new Promise(resolve => setTimeout(resolve, 10));
    }
    for (const child of children) assert.equal(processHasExited(child.pid), true, `process ${child.pid} must have exited`);
  } finally {
    for (const [name, value] of Object.entries(previous)) {
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    }
    if (existsSync(pids)) for (const line of readFileSync(pids, 'utf8').trim().split('\n')) {
      try { process.kill((JSON.parse(line) as { pid: number }).pid, 'SIGKILL'); } catch { /* Already reaped. */ }
    }
    rmSync(directory, { recursive: true, force: true });
  }
}, 40_000);

for (const phase of ['profile', 'initialize', 'session/new', 'session/close']) {
  test(`agents endpoint survives a hanging catalog and ${phase} bootstrap, and reaps discovery children`, async () => {
    const directory = mkdtempSync(join(tmpdir(), 'astrid-agents-deadline-'));
    const calls = join(directory, 'children.jsonl');
    const profile = join(directory, 'profile.md');
    let close: (() => Promise<void>) | undefined;
    try {
      writeFileSync(profile, 'Fixture profile.');
      const shell = join(directory, 'login-shell');
      writeFileSync(shell, `#!/bin/sh\nprintf '\\n__ASTRID_LOGIN_PATH__%s\\n' "$PATH"\n`, { mode: 0o755 });
      for (const name of ['astrid', 'codex']) {
        writeFileSync(join(directory, name), `#!${process.execPath}
const fs = require('node:fs');
const args = process.argv.slice(2);
const agent = ${JSON.stringify(name)};
if (args[0] === '--agent-profile-file') {
  if (${JSON.stringify(phase)} === 'profile') {
    fs.appendFileSync(${JSON.stringify(calls)}, JSON.stringify({ pid: process.pid, agent, args }) + '\\n');
    process.on('SIGTERM', () => {});
    setInterval(() => {}, 1000);
  } else {
    process.stdout.write(${JSON.stringify(profile)});
  }
} else if (args[0] === '--agent-context') {
  // Saved-preferences block: nothing to add.
} else if (args[0] === 'login') {
  process.stdout.write('Logged in using ChatGPT');
} else {
  fs.appendFileSync(${JSON.stringify(calls)}, JSON.stringify({ pid: process.pid, agent, args }) + '\\n');
  process.on('SIGTERM', () => {});
  if (args[0] === 'agent') {
    setInterval(() => {}, 1000);
  } else {
    require('node:readline').createInterface({ input: process.stdin }).on('line', line => {
      const request = JSON.parse(line);
      if (agent === 'astrid' && request.method === ${JSON.stringify(phase)}) return;
      const result = request.method === 'session/new'
        ? { sessionId: agent + '-discovery', models: { currentModelId: 'fixture', availableModels: [{ modelId: 'fixture', name: 'Fixture' }] } }
        : request.method === 'initialize' ? { protocolVersion: 1 } : {};
      process.stdout.write(JSON.stringify({ jsonrpc: '2.0', id: request.id, result }) + '\\n');
    });
    setInterval(() => {}, 1000);
  }
}
`, { mode: 0o755 });
      }
      const env = { ...process.env, PATH: directory, SHELL: shell };
      const { bridge } = createAstridAcpHttpServer({
        config: { token: 'deadline-test', port: 0, cwd: directory, command: join(directory, 'astrid') },
        discoverAgents: options => discoverLocalAgents({ ...options, env }),
        // Exercise real ACP subprocesses without fetching the optional adapter.
        localAgentHostFactory: async (_agent, options) => new AcpProcessHost({
          command: join(directory, 'codex'), args: ['--mode=acp'], cwd: directory,
          requestTimeoutMs: options.requestTimeoutMs, callbacks: options.callbacks,
        }),
        chatRegistry: {} as never,
      });
      close = () => bridge.close();
      const started = Date.now();
      const response = await bridgeTestClient(bridge, 'deadline-test')('/agents');
      assert.equal(response.status, 200);
      assert.ok(Date.now() - started < 16_000, 'discovery and bootstrap must stay below the browser deadline');
      const agents = localAgentCatalogSchema.parse(await response.json()).agents;
      assert.match(agents.find(agent => agent.id === 'astrid')!.signInLine!, /run-astrid.*\/login/);
      assert.equal(agents.find(agent => agent.id === 'astrid')?.state, 'ready', 'unknown catalog preserves the existing classification');
      assert.deepEqual(agents.find(agent => agent.id === 'codex')?.models, [{ modelId: 'fixture', name: 'Fixture' }]);
      const children = readFileSync(calls, 'utf8').trim().split('\n').map(line => JSON.parse(line) as { pid: number; agent: string; args: string[] });
      assert.equal(children.filter(child => child.agent === 'astrid').length, 2, 'both the hanging catalog and bootstrap discovery ran');
      assert.equal(children.filter(child => child.agent === 'codex').length, 1);
      // Allow child exit events to report termination after SIGKILL.
      for (let attempt = 0; attempt < 100; attempt++) {
        if (children.every(child => processHasExited(child.pid))) break;
        await new Promise(resolve => setTimeout(resolve, 10));
      }
      for (const child of children) assert.equal(processHasExited(child.pid), true, `process ${child.pid} must have exited`);
    } finally {
      await close?.();
      if (existsSync(calls)) for (const line of readFileSync(calls, 'utf8').trim().split('\n')) {
        try { process.kill((JSON.parse(line) as { pid: number }).pid, 'SIGKILL'); } catch { /* Already reaped. */ }
      }
      rmSync(directory, { recursive: true, force: true });
    }
  }, 20_000);
}
