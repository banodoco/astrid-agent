import { test } from 'bun:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { spawn } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { AgentModelState } from './agent-model-state.ts';
import { createAstridAcpHttpServer } from './astrid-acp-bridge.ts';
import { bridgeTestClient } from './bridge-test-client.ts';

const key = 'sk-or-test-private-key-for-local-fixtures';

function fakeAstrid(directory: string, mode: { exit?: number; model?: string | null; badJson?: boolean; leak?: boolean } = {}) {
  const command = join(directory, 'astrid');
  const calls = join(directory, 'calls.jsonl');
  const signed = join(directory, 'signed');
  writeFileSync(command, `#!${process.execPath}
const fs = require('node:fs');
const args = process.argv.slice(2);
if (args[0] === 'agent' && args[1] === 'models') {
  process.stdout.write(JSON.stringify({ schemaVersion: 1, currentModel: null, providers: [
    { id: 'openrouter', name: 'OpenRouter', signedIn: fs.existsSync(${JSON.stringify(signed)}), usable: false, models: [] },
    { id: 'local', name: 'Local', signedIn: true, usable: true, models: [] },
  ] }));
} else {
  (async () => {
    let input = '';
    for await (const chunk of process.stdin) input += chunk;
    fs.appendFileSync(${JSON.stringify(calls)}, JSON.stringify({ args, profile: process.env.OMP_PROFILE ?? null,
      inputMatches: require('node:crypto').createHash('sha256').update(input).digest('hex') === ${JSON.stringify(createHash('sha256').update(key).digest('hex'))},
      envHasKey: Object.values(process.env).some(value => value?.includes(input)),
    }) + '\\n');
    process.stderr.write(input);
    if (${mode.exit ?? 0}) { process.stdout.write(input); process.exit(${mode.exit ?? 0}); }
    fs.writeFileSync(${JSON.stringify(signed)}, 'true');
    process.stdout.write(${mode.badJson ? "'malformed ' + input" : `JSON.stringify({ provider: 'openrouter', signedIn: true, currentModel: ${mode.leak ? 'input' : JSON.stringify(mode.model === undefined ? 'openrouter/chosen' : mode.model)}, ignored: input })`});
  })();
}
`, { mode: 0o755 });
  return { command, calls, signed };
}

function assertNoKey(directory: string, output: string) {
  assert.equal(output.includes(key), false);
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) assertNoKey(path, '');
    else assert.equal(readFileSync(path, 'utf8').includes(key), false, path);
  }
}

test('key route updates the remembered model, clears model discovery and refreshes provider sign-in', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'astrid-key-success-'));
  const fake = fakeAstrid(directory);
  const modelStateFile = join(directory, 'state.json');
  new AgentModelState(modelStateFile).remember('astrid', 'local/old');
  let bootstraps = 0;
  const host = {
    initialize: async () => ({}), dispose: async () => {},
    request: async (method: string) => {
      if (method !== 'session/new') return {};
      bootstraps++;
      const model = existsSync(fake.signed) ? 'openrouter/chosen' : 'local/old';
      return { sessionId: `bootstrap-${bootstraps}`, models: { currentModelId: model, availableModels: [{ modelId: model, name: model }] } };
    },
  };
  const { server, bridge } = createAstridAcpHttpServer({
    config: { token: 'test', port: 0, cwd: directory, command: fake.command, profile: 'app-profile', modelStateFile },
    discoverAgents: async () => [{ id: 'astrid', name: 'Astrid', state: 'ready', line: 'Ready' }],
    hostFactory: () => host as never,
  });
  const invoke = bridgeTestClient(bridge, 'test');
  try {
    await invoke('/agents');
    assert.equal(bootstraps, 1);
    const before = await (await invoke('/models')).json();
    assert.equal(before.providers[0].signedIn, false);
    const response = await invoke('/astrid/api-key', { provider: 'openrouter', key });
    assert.equal(response.status, 200);
    assert.equal(response.headers.get('x-astrid-bridge-version'), 'v1');
    assert.deepEqual(await response.json(), { provider: 'openrouter', signedIn: true, currentModel: 'openrouter/chosen' });
    assert.equal(new AgentModelState(modelStateFile).defaults.get('astrid'), 'openrouter/chosen');
    const after = await (await invoke('/models')).json();
    assert.equal(after.providers[0].signedIn, true);
    const agents = await (await invoke('/agents')).json();
    assert.equal(bootstraps, 2);
    assert.equal(agents.agents[0].defaultModel, 'openrouter/chosen');
    assert.deepEqual(JSON.parse(readFileSync(fake.calls, 'utf8')), {
      args: ['agent', 'key', 'set', 'openrouter'], profile: 'app-profile', inputMatches: true, envHasKey: false,
    });
    assertNoKey(directory, JSON.stringify({ before, after, agents }));
  } finally { await bridge.close(); server.close(); rmSync(directory, { recursive: true, force: true }); }
});

for (const [mode, status, error] of [
  [{ exit: 3 }, 400, 'invalid_key'],
  [{ exit: 4 }, 502, 'provider_unreachable'],
  [{ exit: 7 }, 502, 'acp_bridge_error'],
  [{ badJson: true }, 502, 'acp_bridge_error'],
  [{ leak: true }, 502, 'acp_bridge_error'],
] as const) {
  test(`key route redacts child output for ${JSON.stringify(mode)}`, async () => {
    const directory = mkdtempSync(join(tmpdir(), 'astrid-key-error-'));
    const fake = fakeAstrid(directory, mode);
    const { server, bridge } = createAstridAcpHttpServer({ config: { token: 'test', port: 0, cwd: directory, command: fake.command } });
    try {
      const response = await bridgeTestClient(bridge, 'test')('/astrid/api-key', { provider: 'openrouter', key });
      assert.equal(response.status, status);
      const body = await response.json();
      assert.equal(body.error, error);
      assert.equal(typeof body.detail, 'string');
      assertNoKey(directory, JSON.stringify(body));
    } finally { await bridge.close(); server.close(); rmSync(directory, { recursive: true, force: true }); }
  });
}

test('null currentModel preserves the bridge default', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'astrid-key-null-'));
  const fake = fakeAstrid(directory, { model: null });
  const modelStateFile = join(directory, 'state.json');
  new AgentModelState(modelStateFile).remember('astrid', 'other/existing');
  const { server, bridge } = createAstridAcpHttpServer({ config: { token: 'test', port: 0, cwd: directory, command: fake.command, modelStateFile } });
  try {
    const response = await bridgeTestClient(bridge, 'test')('/astrid/api-key', { provider: 'openrouter', key });
    assert.equal(response.status, 200);
    assert.equal((await response.json()).currentModel, null);
    assert.equal(new AgentModelState(modelStateFile).defaults.get('astrid'), 'other/existing');
  } finally { await bridge.close(); server.close(); rmSync(directory, { recursive: true, force: true }); }
});

test('key route validates auth, protocol, provider, shape, whitespace, length and body cap before spawning', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'astrid-key-invalid-'));
  const fake = fakeAstrid(directory);
  const { server, bridge } = createAstridAcpHttpServer({ config: { token: 'test', port: 0, cwd: directory, command: fake.command } });
  const invoke = bridgeTestClient(bridge, 'test');
  try {
    assert.equal((await invoke('/astrid/api-key', { provider: 'openrouter', key }, { auth: false })).status, 401);
    assert.equal((await invoke('/astrid/api-key', { provider: 'openrouter', key }, { protocol: 'v0' })).status, 400);
    for (const body of [null, [], {}, { provider: 'openai', key }, ...[null, 123, 'x'.repeat(19), 'x'.repeat(513), key + ' ', key + '\n', key + '\t'].map(key => ({ provider: 'openrouter', key }))]) {
      const response = await invoke('/astrid/api-key', body);
      assert.equal(response.status, 400);
      assert.equal((await response.json()).error, 'invalid_key');
    }
    const tooBig = await invoke('/astrid/api-key', { provider: 'openrouter', key: key.repeat(4_000) });
    assert.equal(tooBig.status, 413);
    assert.equal((await tooBig.json()).error, 'payload_too_large');
    assert.equal(existsSync(fake.calls), false);
  } finally { await bridge.close(); server.close(); rmSync(directory, { recursive: true, force: true }); }
});

test('shipped chat bundle serves the key route without logging keys or child diagnostics', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'astrid-key-bundle-'));
  const fake = fakeAstrid(directory, { exit: 4 });
  const child = spawn(process.execPath, [fileURLToPath(new URL('./service.mjs', import.meta.url))], {
    cwd: directory,
    env: { ...process.env, ASTRID_WORKSPACE_DISCOVERY: join(directory, 'workspace.json'), ASTRID_ACP_CWD: directory,
      ASTRID_ACP_COMMAND: fake.command, ASTRID_ACP_BRIDGE_TOKEN: 'bundle-test', ASTRID_ACP_BRIDGE_PORT: '0' },
    stdio: ['pipe', 'pipe', 'pipe'],
  });
  let logs = '';
  child.stdout.on('data', chunk => { logs += chunk; });
  child.stderr.on('data', chunk => { logs += chunk; });
  const exited = new Promise<void>(resolve => child.once('exit', () => resolve()));
  try {
    for (let attempt = 0; attempt < 200 && !existsSync(join(directory, 'chat.json')); attempt++) {
      if (child.exitCode !== null) throw new Error('Bundle exited before registering');
      await new Promise(resolve => setTimeout(resolve, 10));
    }
    const registration = JSON.parse(readFileSync(join(directory, 'chat.json'), 'utf8'));
    const response = await fetch(registration.endpoint + '/astrid/api-key', {
      method: 'POST', headers: { authorization: 'Bearer bundle-test', 'x-astrid-bridge-version': 'v1', 'content-type': 'application/json' },
      body: JSON.stringify({ provider: 'openrouter', key }),
    });
    assert.equal(response.status, 502);
    const body = await response.text();
    assert.equal(JSON.parse(body).error, 'provider_unreachable');
    child.stdin.end();
    await exited;
    assert.match(logs, /Astrid chat listening/);
    assertNoKey(directory, logs + body);
  } finally { child.kill('SIGKILL'); await exited; rmSync(directory, { recursive: true, force: true }); }
}, 10_000);
