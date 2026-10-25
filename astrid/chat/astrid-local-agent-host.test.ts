import { test } from 'bun:test';
import assert from 'node:assert/strict';
import { chmodSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { EXTERNAL_AGENT_IDS } from './localAgents.ts';
import { DSH_ACP_LAUNCHER, LOCAL_AGENT_ADAPTERS } from './astrid-local-agent-adapters.ts';
import { createAstridAcpHttpServer } from './astrid-acp-bridge.ts';
import { createLocalAgentAcpProcessHost, renderAgentProfile } from './astrid-local-agent-host.ts';
import { resolveAstridAgentContext } from './astridAcpLauncher.ts';

type Json = Record<string, unknown>;

test('external agents receive the profile with the package root filled in and frontmatter removed', () => {
  const profile = '---\nname: astrid\n---\nRead `{{ASTRID_PACKAGE_ROOT}}/packs/_core/docs/SKILL.md`, then `{{ASTRID_PACKAGE_ROOT}}/packs/<pack>/docs/SKILL.md`.\n';
  assert.equal(
    renderAgentProfile(profile, '/install/astrid/agent_profiles/astrid.md'),
    'Read `/install/astrid/packs/_core/docs/SKILL.md`, then `/install/astrid/packs/<pack>/docs/SKILL.md`.\n',
  );
  assert.equal(renderAgentProfile('No frontmatter.', '/a/b/c.md'), 'No frontmatter.');
  assert.throws(() => renderAgentProfile('---\nname: astrid\n', '/a/b/c.md'), /unterminated frontmatter/);
});

function executable(path: string, contents: string): void {
  writeFileSync(path, contents);
  chmodSync(path, 0o755);
}

function fixture(directory: string) {
  const bin = join(directory, 'bin');
  const cwd = join(directory, 'project');
  const logs = join(directory, 'adapter.jsonl');
  const profile = join(directory, 'astrid-profile.md');
  mkdirSync(bin);
  mkdirSync(cwd);
  writeFileSync(profile, '# Packaged Astrid profile\nPROFILE_SENTINEL');
  executable(join(bin, 'claude'), `#!${process.execPath}\nprocess.stdout.write('Logged in\\n');\n`);
  executable(join(bin, 'codex'), `#!${process.execPath}\nprocess.stderr.write('Logged in using ChatGPT\\n');\n`);
  executable(join(bin, 'dsh'), `#!${process.execPath}\nprocess.exit(0);\n`);
  executable(join(bin, 'node'), `#!/bin/sh\nprintf 'v22.1.0\\n'\n`);
  const dshHome = join(directory, 'dsh-home');
  mkdirSync(dshHome, { recursive: true });
  writeFileSync(join(dshHome, '.credentials.yaml'), 'DEEPSEEK_API_KEY: sk-test-fixture\n');
  const shell = join(directory, 'login-shell');
  executable(shell, `#!/bin/sh\n[ "$1" = "-lic" ] || exit 98\nprintf '\\n__ASTRID_LOGIN_PATH__%s\\n' "$ASTRID_TEST_LOGIN_PATH"\n`);
  const adapterProgram = `#!${process.execPath}
const fs = require('node:fs');
const readline = require('node:readline');
const log = ${JSON.stringify(logs)};
const native = process.argv[1].endsWith('/hermes');
if (native && process.argv[2] === 'acp' && process.argv[3] === '--check') process.exit(0);
const viaNpx = process.argv[1].endsWith('/npx');
const adapter = process.argv[4];
const expected = ${JSON.stringify(LOCAL_AGENT_ADAPTERS)};
const dshLauncher = ${JSON.stringify(DSH_ACP_LAUNCHER)};
// DeepSeek Harness runs the pinned launcher on a real Node, so it arrives as
// npx --yes <launcher> --profile acp rather than through the Bun adapters.
if (viaNpx && (process.argv[2] !== '--yes' || process.argv[3] !== dshLauncher
  || process.argv[4] !== '--profile' || process.argv[5] !== 'acp')) process.exit(97);
if (!native && !viaNpx && (process.argv[2] !== 'x' || process.argv[3] !== '--bun' || !Object.values(expected).includes(adapter))) process.exit(98);
if (native && process.argv[2] !== 'acp') process.exit(98);
const agent = native ? 'hermes' : viaNpx ? 'dsh-acp' : adapter === expected['claude-code'] ? 'claude-agent-acp' : 'codex-acp';
let model = 'model-a';
let effort = 'low';
const effortId = agent === 'codex-acp' || agent === 'dsh-acp' ? 'reasoning_effort' : 'effort';
const configOptions = () => native ? [] : [
  {id:'model',name:'Model',category:'model',type:'select',currentValue:model,options:[{value:'model-a',name:'A'},{value:'model-b',name:'B'}]},
  {id:effortId,name:'Effort',category:'thought_level',type:'select',currentValue:effort,options:[{value:'low',name:'Low'},{value:'high',name:'High'}],_meta:{jetbrains:{air:{version:1,recommendedValue:'high'}}}}
];
const write = value => process.stdout.write(JSON.stringify(value) + '\\n');
const record = value => fs.appendFileSync(log, JSON.stringify({agent, value}) + '\\n');
const waiting = new Map();
let nextPermission = 0;
record({startup: true, cliPath: process.env.CODEX_PATH ?? null, adapter});
const askPermission = params => new Promise(resolve => {
  const id = 'permission-' + (++nextPermission);
  waiting.set(id, resolve);
  write({jsonrpc:'2.0', id, method:'session/request_permission', params});
});
const rl = readline.createInterface({input: process.stdin, crlfDelay: Infinity});
rl.on('line', async line => {
  const message = JSON.parse(line);
  if (message.method === undefined && message.id !== undefined) {
    record({permissionResponse:message});
    waiting.get(message.id)?.(message.result);
    waiting.delete(message.id);
    return;
  }
  if (message.id === undefined) { record({notification:message}); return; }
  if (message.method === 'initialize') {
    record({method:message.method, params:message.params});
    write({jsonrpc:'2.0',id:message.id,result:{protocolVersion:1,agentCapabilities:{loadSession:true,sessionCapabilities:{list:{},resume:{},close:{},...(agent === 'claude-agent-acp' ? {systemPrompt:{mode:{override:{}}}} : {})}}}});
    return;
  }
  record({method:message.method, params:message.params});
  if (message.method === 'session/new') { write({jsonrpc:'2.0',id:message.id,result:{sessionId:'shared-native-id',configOptions:configOptions()}}); return; }
  if (message.method === 'session/set_config_option') {
    if (message.params.configId === 'model') model = message.params.value;
    else if (message.params.configId === effortId) effort = message.params.value;
    write({jsonrpc:'2.0',id:message.id,result:{configOptions:configOptions()}}); return;
  }
  if (message.method === 'session/list') { write({jsonrpc:'2.0',id:message.id,result:{sessions:[{sessionId:'shared-native-id'}]}}); return; }
  if (message.method === 'session/load' || message.method === 'session/resume') { write({jsonrpc:'2.0',id:message.id,result:{sessionId:message.params.sessionId}}); return; }
  if (message.method === 'session/fork') { write({jsonrpc:'2.0',id:message.id,result:{sessionId:'forked-native-id'}}); return; }
  if (message.method === 'session/prompt') {
    record({receivedPrompt:message.params.prompt});
    await askPermission({sessionId:'shared-native-id',toolCall:{title:'Read file'},options:[{optionId:'offered-allow-once',kind:'allow_once'},{optionId:'offered-allow-always',kind:'allow_always'}]});
    await askPermission({sessionId:'shared-native-id',toolCall:{title:'Delete file'},options:[{optionId:'offered-reject-once',kind:'reject_once'},{optionId:'offered-reject-always',kind:'reject_always'}]});
    write({jsonrpc:'2.0',method:'session/update',params:{sessionId:'shared-native-id',update:{sessionUpdate:'agent_message_chunk',content:{type:'text',text:'adapter notification'}}}});
    write({jsonrpc:'2.0',id:message.id,result:{stopReason:'end_turn'}});
    return;
  }
  write({jsonrpc:'2.0',id:message.id,result:{}});
});
rl.on('close', () => process.exit(0));
`;
  executable(join(bin, 'bun'), adapterProgram);
  executable(join(bin, 'npx'), adapterProgram);
  executable(join(bin, 'hermes'), adapterProgram);
  return { bin, cwd, logs, profile, shell, dshHome };
}

async function listen(server: import('node:http').Server): Promise<string> {
  await new Promise<void>(resolve => server.listen(0, '127.0.0.1', resolve));
  return `http://127.0.0.1:${(server.address() as { port: number }).port}`;
}

function records(path: string): Json[] {
  return readFileSync(path, 'utf8').trim().split('\n').filter(Boolean).map(line => JSON.parse(line) as Json);
}

async function waitForRecord(path: string, matches: (record: Json) => boolean): Promise<Json> {
  const deadline = Date.now() + 2_000;
  while (Date.now() < deadline) {
    const record = records(path).find(matches);
    if (record) return record;
    await new Promise(resolve => setTimeout(resolve, 5));
  }
  throw new Error('Expected adapter record was not written before the deadline.');
}

test('external ACP bridge namespaces sessions and routes context, permissions, and cancellation through its Bun launch shim', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'astrid-local-agent-host-'));
  const f = fixture(directory);
  let nextConnection = 0;
  const { server, bridge } = createAstridAcpHttpServer({
    config: { token: 'test-token', port: 0, cwd: f.cwd },
    idFactory: () => `connection-${++nextConnection}`,
    localAgentHostFactory: (agent, options) => createLocalAgentAcpProcessHost(agent, {
      ...options,
      env: { ...options.env, PATH: f.bin, SHELL: f.shell, ASTRID_TEST_LOGIN_PATH: f.bin, DSH_HOME: f.dshHome },
      profileFileForCommand: () => f.profile,
    }),
  });
  try {
    const base = await listen(server);
    const call = (path: string, body?: unknown, method = body === undefined ? 'GET' : 'POST') => fetch(base + path, {
      method, headers: { Authorization: 'Bearer test-token', 'X-Astrid-Bridge-Version': 'v1', 'Content-Type': 'application/json' },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    });
    const connected = await Promise.all([
      call('/connect', { agent: 'claude-code', defaultModelId: 'model-b' }), call('/connect', { agent: 'codex', defaultModelId: 'model-b' }),
    ]);
    assert.deepEqual(connected.map(response => response.status), [200, 200]);
    const claudeId = (await connected[0]!.json() as Json).connection_id as string;
    const codexId = (await connected[1]!.json() as Json).connection_id as string;
    const rpc = async (connection: string, method: string, params: unknown = {}) => call(`/${connection}/rpc`, { method, params });

    // Exercise real native load/resume calls with cold, distinct sessions.
    // Later operations on the shared session should come from bridge cache.
    for (const [connection, agent] of [[claudeId, 'claude-code'], [codexId, 'codex']] as const) {
      assert.equal((await rpc(connection, 'session/load', { sessionId: `${agent}:cold-load` })).status, 200);
      assert.equal((await rpc(connection, 'session/resume', { sessionId: `${agent}:cold-resume` })).status, 200);
    }

    const created = await Promise.all([rpc(claudeId, 'session/new'), rpc(codexId, 'session/new')]);
    const sessionIds = await Promise.all(created.map(async response => (await response.json() as Json).result as Json));
    assert.deepEqual(sessionIds.map(result => result.sessionId), ['claude-code:shared-native-id', 'codex:shared-native-id']);
    for (const [connection, agent, configId] of [[claudeId, 'claude-code', 'effort'], [codexId, 'codex', 'reasoning_effort']] as const) {
      const sessionId = `${agent}:shared-native-id`;
      const selected = await call(`/${connection}/model`, { sessionId, modelId: 'model-b' });
      assert.equal(selected.status, 404);
      assert.equal((await rpc(connection, 'session/set_config_option', { sessionId, configId, value: 'high' })).status, 200);
      const catalog = await (await call(`/${connection}/models?sessionId=${sessionId}`)).json();
      assert.equal(catalog.models.currentModelId, 'model-b');
      assert.equal(catalog.models.thoughtLevel.currentValue, 'high');
      assert.equal(catalog.models.thoughtLevel.recommendedValue, 'high');
      assert.equal((await rpc(connection, 'session/set_config_option', { sessionId, configId, value: 'invented' })).status, 400);
    }
    assert.equal((await rpc(claudeId, 'session/load', { sessionId: 'claude-code:shared-native-id' })).status, 200);
    assert.equal((await rpc(codexId, 'session/load', { sessionId: 'codex:shared-native-id' })).status, 200);
    assert.equal((await rpc(claudeId, 'session/resume', { sessionId: 'claude-code:shared-native-id' })).status, 200);
    assert.equal((await rpc(codexId, 'session/resume', { sessionId: 'codex:shared-native-id' })).status, 200);
    const claudeList = await rpc(claudeId, 'session/list');
    const codexList = await rpc(codexId, 'session/list');
    const claudeSessions = (((await claudeList.json() as Json).result as Json).sessions as Json[]);
    const codexSessions = (((await codexList.json() as Json).result as Json).sessions as Json[]);
    assert.equal(claudeSessions[0]!.sessionId, 'claude-code:shared-native-id');
    assert.equal(codexSessions[0]!.sessionId, 'codex:shared-native-id');

    const crossAgent = await rpc(codexId, 'session/load', { sessionId: 'claude-code:shared-native-id' });
    assert.equal(crossAgent.status, 409);
    assert.match((await crossAgent.json() as Json).detail as string, /different agent/);

    const prompt = await rpc(claudeId, 'session/prompt', {
      sessionId: 'claude-code:shared-native-id',
      prompt: [{ type: 'text', text: '<astrid_editor_context>EDITOR_CONTEXT_SENTINEL</astrid_editor_context>' }],
    });
    assert.equal(prompt.status, 200);
    const events = await call(`/${claudeId}/events`, undefined, 'GET');
    const notifications = (await events.json() as Json).notifications as Json[];
    assert.ok(notifications.some(notification => (notification.params as Json).sessionId === 'claude-code:shared-native-id'));

    const cancel = await call(`/${codexId}/cancel`, { sessionId: 'codex:shared-native-id' });
    assert.equal(cancel.status, 200);
    await waitForRecord(f.logs, row => row.agent === 'codex-acp' && (row.value as Json).notification !== undefined);
    assert.equal((await call(`/${claudeId}`, {}, 'DELETE')).status, 200);
    assert.equal((await call(`/${codexId}`, {}, 'DELETE')).status, 200);

    const logged = records(f.logs);
    const claudeRecords = logged.filter(row => row.agent === 'claude-agent-acp').map(row => row.value as Json);
    const codexRecords = logged.filter(row => row.agent === 'codex-acp').map(row => row.value as Json);
    for (const [rows, configId] of [[claudeRecords, 'effort'], [codexRecords, 'reasoning_effort']] as const) {
      assert.deepEqual(rows.filter(row => row.method === 'session/set_config_option').map(row => row.params), [
        { sessionId: 'shared-native-id', configId: 'model', value: 'model-b' },
        { sessionId: 'shared-native-id', configId, value: 'high' },
      ]);
      const capabilities = (rows.find(row => row.method === 'initialize')!.params as Json).clientCapabilities as Json;
      assert.deepEqual(((capabilities._meta as Json).jetbrains as Json).air, { version: 1, capabilities: ['recommendedValue'] });
    }
    assert.equal((claudeRecords.find(row => row.startup)!).adapter, LOCAL_AGENT_ADAPTERS['claude-code']);
    assert.equal((codexRecords.find(row => row.startup)!).adapter, LOCAL_AGENT_ADAPTERS.codex);
    const claudeNew = claudeRecords.find(row => row.method === 'session/new')!.params as Json;
    assert.equal((((claudeNew._meta as Json).claudeCode as Json).options as Json).pathToClaudeCodeExecutable, join(f.bin, 'claude'));
    const systemPrompt = claudeNew.systemPrompt as Json;
    assert.equal(systemPrompt.mode, 'append');
    const grounding = (systemPrompt.content as Json[]).map(part => part.text).join('\n');
    assert.match(grounding, /PROFILE_SENTINEL/);
    assert.match(grounding, /Load the relevant Astrid SKILL\.md/);
    const claudePrompt = claudeRecords.find(row => row.receivedPrompt)!.receivedPrompt as Json[];
    const sentText = claudePrompt.map(part => part.text).join('\n');
    assert.doesNotMatch(sentText, /PROFILE_SENTINEL|astrid_agent_context/);
    assert.match(sentText, /EDITOR_CONTEXT_SENTINEL/);
    assert.deepEqual(claudeRecords.filter(row => row.permissionResponse).map(row => ((((row.permissionResponse as Json).result as Json).outcome as Json).optionId)), ['offered-allow-always', 'offered-reject-once']);
    assert.equal((codexRecords.find(row => row.startup)!).cliPath, join(f.bin, 'codex'));
    assert.deepEqual(codexRecords.find(row => row.notification)!.notification, { jsonrpc: '2.0', method: 'session/cancel', params: { sessionId: 'shared-native-id' } });
    for (const method of ['session/load', 'session/resume', 'session/list']) {
      assert.ok(claudeRecords.some(row => row.method === method));
      assert.ok(codexRecords.some(row => row.method === method));
    }
    for (const agentRows of [claudeRecords, codexRecords]) {
      for (const method of ['session/load', 'session/resume']) {
        const nativeCalls = agentRows.filter(row => row.method === method);
        const coldCalls = nativeCalls.filter(row => String((row.params as Json).sessionId).includes('cold-'));
        const sharedCalls = nativeCalls.filter(row => (row.params as Json).sessionId === 'shared-native-id');
        assert.equal(coldCalls.length, 1);
        assert.equal(sharedCalls.length, 0); // shared cache never re-loads or resumes the native session
      }
    }
  } finally {
    await bridge.close();
    await new Promise<void>(resolve => server.close(() => resolve()));
    rmSync(directory, { recursive: true, force: true });
  }
});

test('external agent readiness fails closed for missing CLIs and sign-in, but does not require a local adapter install', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'astrid-local-agent-readiness-'));
  try {
    const bin = join(directory, 'bin');
    const cwd = join(directory, 'project');
    mkdirSync(directory, { recursive: true });
    mkdirSync(bin); mkdirSync(cwd);
    writeFileSync(join(directory, 'unused-profile.md'), 'profile');
    const shell = join(directory, 'login-shell');
    executable(shell, `#!/bin/sh\n[ "$1" = "-lic" ] || exit 98\nprintf '\\n__ASTRID_LOGIN_PATH__%s\\n' "$ASTRID_TEST_LOGIN_PATH"\n`);
    const options = { cwd, env: { PATH: bin, SHELL: shell, ASTRID_TEST_LOGIN_PATH: bin }, profileFileForCommand: () => join(directory, 'unused-profile.md') };
    await assert.rejects(createLocalAgentAcpProcessHost('claude-code', options), /not installed on PATH/);
    executable(join(bin, 'claude'), `#!${process.execPath}\nprocess.stdout.write('Not logged in. Run claude auth login to authenticate.');\n`);
    await assert.rejects(createLocalAgentAcpProcessHost('claude-code', options), /sign-in is required/);
    executable(join(bin, 'claude'), `#!${process.execPath}\nprocess.stdout.write('Logged in');\n`);
    executable(join(bin, 'bun'), `#!${process.execPath}\nprocess.exit(99);\n`);
    await createLocalAgentAcpProcessHost('claude-code', options);
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

for (const agent of EXTERNAL_AGENT_IDS) {
  test(`${agent} delivers grounding through its capability and preserves it across session operations`, async () => {
    const directory = mkdtempSync(join(tmpdir(), 'astrid-agent-grounding-'));
    const f = fixture(directory);
    const host = await createLocalAgentAcpProcessHost(agent, {
      cwd: f.cwd,
      env: { PATH: f.bin, SHELL: f.shell, ASTRID_TEST_LOGIN_PATH: f.bin, DSH_HOME: f.dshHome },
      profileFileForCommand: () => f.profile,
      agentContextForCommand: async () => '<astrid_agent_context>\nPREFS_SENTINEL\n</astrid_agent_context>\n',
      callbacks: { requestPermission: () => ({ outcome: { outcome: 'cancelled' } }) },
    });
    try {
      await host.initialize({ protocolVersion: 1, clientCapabilities: {} });
      const meta = { caller: 'preserve-me', claudeCode: { caller: 'nested', options: { customOption: true } } };
      const created = await host.newSession<Json>({ cwd: f.cwd, mcpServers: [], _meta: meta });
      assert.equal(created.sessionId, `${agent}:shared-native-id`);
      assert.deepEqual(await host.listSessions(), { sessions: [{ sessionId: created.sessionId }] });
      await assert.rejects(host.loadSession({ sessionId: 'another-agents-chat' }), /different agent/);
      const send = (sessionId: string, text: string) => host.prompt({
        sessionId, prompt: [{ type: 'text', text: `<astrid_editor_context>${text}</astrid_editor_context>` }],
      });
      await send(created.sessionId as string, 'first');
      await host.resumeSession({ sessionId: created.sessionId, cwd: f.cwd, _meta: meta });
      await send(created.sessionId as string, 'second');
      await host.loadSession({ sessionId: `${agent}:older-chat`, cwd: f.cwd, _meta: meta });
      await send(`${agent}:older-chat`, 'loaded');
      await host.resumeSession({ sessionId: `${agent}:other-old-chat`, cwd: f.cwd, _meta: meta });
      await send(`${agent}:other-old-chat`, 'resumed');
      const forked = await host.forkSession<Json>({ sessionId: created.sessionId, cwd: f.cwd, _meta: meta });
      assert.equal(forked.sessionId, `${agent}:forked-native-id`);
      await send(forked.sessionId as string, 'forked');
      const rows = records(f.logs).map(row => row.value as Json);
      const opening = rows.filter(row => ['session/new', 'session/load', 'session/resume', 'session/fork'].includes(row.method as string));
      const firstSystemPrompt = (opening[0]!.params as Json).systemPrompt as Json | undefined;
      if (agent === 'claude-code') {
        assert.equal(firstSystemPrompt?.mode, 'append');
        const appended = (firstSystemPrompt?.content as Json[])[0]!.text as string;
        assert.match(appended, /PROFILE_SENTINEL/);
        assert.ok(appended.indexOf('PREFS_SENTINEL') > appended.indexOf('PROFILE_SENTINEL'), 'preferences follow the profile');
        assert.ok(appended.endsWith('</astrid_agent_context>'));
      }
      const prompts = rows.filter(row => row.receivedPrompt).map(row => row.receivedPrompt as Json[]);
      assert.equal(prompts.length, 5);
      for (const [index, prompt] of prompts.entries()) {
        const text = prompt.map(part => part.text).join('\n');
        assert.match(text, /astrid_editor_context/);
        if (agent !== 'claude-code' && index === 0) {
          assert.match(text, /<astrid_agent_context>[\s\S]*PROFILE_SENTINEL[\s\S]*PREFS_SENTINEL/);
          assert.match(text, /Load the relevant Astrid SKILL\.md/);
        } else {
          assert.doesNotMatch(text, /astrid_agent_context|PROFILE_SENTINEL|PREFS_SENTINEL/);
          assert.equal(prompt.length, 1);
        }
      }
      for (const row of opening) {
        const params = row.params as Json;
        if (agent === 'claude-code') {
          assert.deepEqual(params.systemPrompt, firstSystemPrompt);
          const forwardedMeta = params._meta as Json;
          assert.equal(forwardedMeta.caller, 'preserve-me');
          const claudeMeta = forwardedMeta.claudeCode as Json;
          assert.equal(claudeMeta.caller, 'nested');
          assert.deepEqual(claudeMeta.options, { customOption: true, pathToClaudeCodeExecutable: join(f.bin, 'claude') });
        } else {
          assert.equal(params.systemPrompt, undefined);
          assert.deepEqual(params._meta, meta);
        }
      }
    } finally {
      await host.dispose();
      rmSync(directory, { recursive: true, force: true });
    }
  });
}

for (const agent of ['codex', 'hermes'] as const) {
  test(`${agent} preserves an empty chat, thinking level and first grounding during browser handoff`, async () => {
    const directory = mkdtempSync(join(tmpdir(), 'astrid-grounding-handoff-'));
    const f = fixture(directory);
    let nextConnection = 0;
    const sessionId = `${agent}:shared-native-id`;
    const { server, bridge } = createAstridAcpHttpServer({
      config: { token: 'handoff', port: 0, cwd: f.cwd },
      idFactory: () => `c${++nextConnection}`,
      localAgentHostFactory: async (selected, options) => {
        const host = await createLocalAgentAcpProcessHost(selected, {
          ...options, env: { ...options.env, PATH: f.bin, SHELL: f.shell, ASTRID_TEST_LOGIN_PATH: f.bin, DSH_HOME: f.dshHome },
          profileFileForCommand: () => f.profile,
        });
        const nativeRequest = host.request.bind(host);
        host.request = (async (method, params) => {
          // Empty native chats may exist only in their creating process. A
          // second host cannot load it until a prompt persists a transcript.
          if (method === 'session/load' && (params as Json)?.sessionId === sessionId) throw new Error('empty native session is not persisted');
          return nativeRequest(method, params);
        }) as typeof host.request;
        return host;
      },
    });
    const { bridgeTestClient } = await import('./bridge-test-client.ts');
    const call = bridgeTestClient(bridge, 'handoff');
    const rpc = (connection: string, method: string, params: Json) => call(`/${connection}/rpc`, { method, params });
    try {
      assert.equal((await call('/connect', { agent })).status, 200);
      const oldSession = `${agent}:older-chat`;
      assert.equal((await rpc('c1', 'session/load', { sessionId: oldSession })).status, 200);
      assert.equal((await call('/connect', { agent, ...(agent === 'codex' ? { defaultModelId: 'model-b' } : {}) })).status, 200);
      assert.equal((await rpc('c2', 'session/new', {})).status, 200);
      const advertised = await (await call(`/c2/models?sessionId=${encodeURIComponent(sessionId)}`)).json();
      const thoughtId = advertised.models?.thoughtLevel?.configId;
      if (agent === 'codex') {
        assert.equal(thoughtId, 'reasoning_effort');
        assert.equal((await rpc('c2', 'session/set_config_option', { sessionId, configId: thoughtId, value: 'high' })).status, 200);
      } else assert.equal(advertised.models, null);
      assert.equal((await rpc('c1', 'session/load', { sessionId })).status, 200);
      assert.equal((await call('/c2', undefined, { method: 'DELETE' })).status, 200);
      const models = await (await call(`/c1/models?sessionId=${encodeURIComponent(sessionId)}`)).json();
      if (agent === 'codex') {
        assert.equal(models.models.currentModelId, 'model-b');
        assert.equal(models.models.thoughtLevel.currentValue, 'high');
      } else assert.equal(models.models, null);
      for (const id of [sessionId, sessionId, oldSession]) {
        assert.equal((await rpc('c1', 'session/prompt', { sessionId: id, prompt: [{ type: 'text', text: 'user' }] })).status, 200);
      }
      const rows = records(f.logs).map(row => row.value as Json);
      const prompts = rows.filter(row => row.receivedPrompt).map(row => JSON.stringify(row.receivedPrompt));
      assert.equal(prompts.length, 3);
      assert.match(prompts[0]!, /astrid_agent_context.*PROFILE_SENTINEL/);
      assert.doesNotMatch(prompts[1]!, /astrid_agent_context|PROFILE_SENTINEL/);
      assert.doesNotMatch(prompts[2]!, /astrid_agent_context|PROFILE_SENTINEL/);
      assert.equal(rows.filter(row => row.method === 'session/load' && (row.params as Json).sessionId === 'shared-native-id').length, 0);
      assert.equal(rows.filter(row => row.method === 'session/new').length, 1);
    } finally {
      await bridge.close(); server.close();
      rmSync(directory, { recursive: true, force: true });
    }
  });
}

for (const [label, agentContextForCommand] of [
  ['empty output', async () => ''],
] as const) {
  for (const agent of EXTERNAL_AGENT_IDS) {
    test(`${agent} starts without a preferences block after ${label} from the context command`, async () => {
      const directory = mkdtempSync(join(tmpdir(), 'astrid-agent-no-prefs-'));
      const f = fixture(directory);
      const host = await createLocalAgentAcpProcessHost(agent, {
        cwd: f.cwd,
        env: { PATH: f.bin, SHELL: f.shell, ASTRID_TEST_LOGIN_PATH: f.bin, DSH_HOME: f.dshHome },
        profileFileForCommand: () => f.profile,
        agentContextForCommand,
        callbacks: { requestPermission: () => ({ outcome: { outcome: 'cancelled' } }) },
      });
      try {
        await host.initialize({ protocolVersion: 1, clientCapabilities: {} });
        const created = await host.newSession<Json>({ cwd: f.cwd, mcpServers: [] });
        await host.prompt({ sessionId: created.sessionId, prompt: [{ type: 'text', text: 'hello' }] });
        const rows = records(f.logs).map(row => row.value as Json);
        const opening = rows.find(row => row.method === 'session/new')!.params as Json;
        const prompt = JSON.stringify(rows.find(row => row.receivedPrompt)!.receivedPrompt);
        if (agent === 'claude-code') {
          const appended = JSON.stringify(opening.systemPrompt);
          assert.match(appended, /PROFILE_SENTINEL/);
          assert.doesNotMatch(appended, /PREFS_SENTINEL/);
          assert.doesNotMatch(prompt, /astrid_agent_context/);
        } else {
          assert.match(prompt, /astrid_agent_context.*PROFILE_SENTINEL/);
          assert.doesNotMatch(prompt, /PREFS_SENTINEL/);
        }
      } finally {
        await host.dispose();
        rmSync(directory, { recursive: true, force: true });
      }
    });
  }
}

for (const agent of EXTERNAL_AGENT_IDS) {
  test(`${agent} starts without preferences and records a failed context lookup`, async () => {
    const directory = mkdtempSync(join(tmpdir(), 'astrid-agent-context-error-'));
    const f = fixture(directory);
    const warnings: string[] = [];
    const warn = console.warn;
    console.warn = (...values: unknown[]) => warnings.push(values.map(String).join(' '));
    try {
      const host = await createLocalAgentAcpProcessHost(agent, {
        cwd: f.cwd,
        env: { PATH: f.bin, SHELL: f.shell, ASTRID_TEST_LOGIN_PATH: f.bin },
        profileFileForCommand: () => f.profile,
        agentContextForCommand: async () => { throw new Error('astrid --agent-context failed'); },
      });
      try {
        await host.initialize({ protocolVersion: 1, clientCapabilities: {} });
        const created = await host.newSession<Json>({ cwd: f.cwd, mcpServers: [] });
        await host.prompt({ sessionId: created.sessionId, prompt: [{ type: 'text', text: 'hello' }] });
        const rows = records(f.logs).map(row => row.value as Json);
        const opening = rows.find(row => row.method === 'session/new')!.params as Json;
        const prompt = JSON.stringify(rows.find(row => row.receivedPrompt)!.receivedPrompt);
        if (agent === 'claude-code') {
          assert.match(JSON.stringify(opening.systemPrompt), /PROFILE_SENTINEL/);
          assert.doesNotMatch(JSON.stringify(opening.systemPrompt), /PREFS_SENTINEL/);
          assert.doesNotMatch(prompt, /PREFS_SENTINEL/);
        } else {
          assert.match(prompt, /astrid_agent_context.*PROFILE_SENTINEL/);
          assert.doesNotMatch(prompt, /PREFS_SENTINEL/);
        }
        assert.match(warnings.join('\n'), /Astrid agent preferences lookup failed: astrid --agent-context failed/);
      } finally {
        await host.dispose();
      }
    } finally {
      console.warn = warn;
      rmSync(directory, { recursive: true, force: true });
    }
  });
}

test('resolveAstridAgentContext runs `astrid --agent-context` and never throws', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'astrid-agent-context-'));
  try {
    const script = (name: string, body: string) => {
      const path = join(directory, name);
      executable(path, `#!${process.execPath}\n${body}\n`);
      return path;
    };
    const ok = script('ok', `if (process.argv[2] !== '--agent-context') process.exit(9);
process.stdout.write('  <astrid_agent_context>' + process.env.CONTEXT_MARK + '</astrid_agent_context>\\n');`);
    assert.equal(
      await resolveAstridAgentContext(ok, { CONTEXT_MARK: 'USER_PREFS' }),
      '<astrid_agent_context>USER_PREFS</astrid_agent_context>',
    );
    assert.equal(await resolveAstridAgentContext(script('empty', ''), {}), '');
    assert.equal(await resolveAstridAgentContext(script('fails', `process.stdout.write('partial'); process.exit(3);`), {}), '');
    assert.equal(await resolveAstridAgentContext(join(directory, 'does-not-exist'), {}), '');
  } finally { rmSync(directory, { recursive: true, force: true }); }
});
