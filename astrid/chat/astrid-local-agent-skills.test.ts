import { test } from 'bun:test';
import { which } from 'bun';
import assert from 'node:assert/strict';
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, symlinkSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { LocalAgentSkills } from './astrid-local-agent-skills.ts';
import { createAstridAcpHttpServer } from './astrid-acp-bridge.ts';
import { bridgeTestClient } from './bridge-test-client.ts';
import { agentCommand, astridSignInLine, EXTERNAL_AGENT_IDS, formatAstridSignInLine, LOCAL_AGENT_TABLE, type LocalAgentId } from './localAgents.ts';
import { processHasExited } from './testProcess.ts';

const harnesses = { 'claude-code': 'claude', codex: 'codex', hermes: 'hermes' } as const;
function fixture(mode: 'success' | 'failure' | 'timeout' = 'success') {
  const home = mkdtempSync(join(process.cwd(), '.agent-skills-test-'));
  writeFileSync(join(home, 'package.json'), '{"type":"commonjs"}');
  const python = join(home, 'python');
  const log = join(home, 'calls.jsonl');
  writeFileSync(python, `#!${which('node')!}
const fs = require('node:fs');
const path = require('node:path');
const args = process.argv.slice(2);
fs.appendFileSync(${JSON.stringify(log)}, JSON.stringify({ args, pid: process.pid }) + '\\n');
if (args.slice(0, 4).join(' ') !== '-m astrid.skills sync --harness' || args.length !== 5) process.exit(98);
if (${JSON.stringify(mode)} === 'failure') process.exit(9);
if (${JSON.stringify(mode)} === 'timeout') {
  process.on('SIGTERM', () => {});
  setInterval(() => {}, 1000);
} else {
  const harness = args[4];
  const view = path.join(process.env.HOME, '.local', 'state', 'astrid', 'skills', harness);
  const skills = path.join(process.env.HOME, '.' + harness, 'skills');
  fs.mkdirSync(view, { recursive: true });
  fs.mkdirSync(skills, { recursive: true });
  fs.writeFileSync(path.join(view, 'SKILL.md'), '# Astrid');
  fs.symlinkSync(view, path.join(skills, 'astrid'));
}
`);
  chmodSync(python, 0o755);
  const env = { HOME: home, USERPROFILE: home, PATH: '', ASTRID_SERVICE_PYTHON: python, ASTRID_INSTALL_PROGRAM_ROOT: home };
  const skills = new LocalAgentSkills(env, home, mode === 'timeout' ? 300 : 2_000);
  let connection = 0;
  const host = () => ({ initialize: async () => ({}), request: async () => ({}), dispose: async () => {} }) as never;
  const { bridge } = createAstridAcpHttpServer({
    config: { cwd: home, token: 'skills', port: 0 },
    localAgentSkills: skills,
    hostFactory: host, localAgentHostFactory: async () => host(),
    idFactory: () => `c${++connection}`,
    discoverAgents: async () => (['astrid', ...EXTERNAL_AGENT_IDS] as LocalAgentId[]).map(id => ({ id, name: LOCAL_AGENT_TABLE[id].name, state: 'ready', line: null, signIn: 'signed-in', steps: [] })),
    astridModelCatalog: async () => ({ providers: [], currentModel: null }),
  });
  return { home, skills, env, bridge, call: bridgeTestClient(bridge, 'skills'),
    calls: () => existsSync(log) ? readFileSync(log, 'utf8').trim().split('\n').map(line => JSON.parse(line) as { args: string[]; pid: number }) : [],
    close: async () => { await bridge.close(); rmSync(home, { recursive: true, force: true }); },
  };
}

for (const mode of ['success', 'failure', 'timeout'] as const) {
  test(`first connect syncs once per harness and caches ${mode} without failing connections`, async () => {
    const f = fixture(mode);
    try {
      // GET /agents may create temporary model probes, but must never run sync.
      const before = await (await f.call('/agents')).json();
      assert.equal(f.calls().length, 0);
      for (const row of before.agents.filter((row: { id: string }) => row.id !== 'astrid')) {
        assert.equal(row.terminal.skillSynced, false);
        assert.equal(row.terminal.detail, `${row.name}'s skills folder wasn't found.`);
      }
      assert.equal((await f.call('/connect', { agent: 'astrid' })).status, 200);
      assert.equal(f.calls().length, 0);
      const started = Date.now();
      const responses = await Promise.all(EXTERNAL_AGENT_IDS.flatMap(agent => [f.call('/connect', { agent }), f.call('/connect', { agent })]));
      assert.ok(responses.every(response => response.status === 200));
      assert.ok(Date.now() - started < 3_000);
      assert.equal(f.calls().length, 3);
      for (const agent of EXTERNAL_AGENT_IDS) {
        assert.deepEqual(f.calls().filter(call => call.args[4] === harnesses[agent]).map(call => call.args), [['-m', 'astrid.skills', 'sync', '--harness', harnesses[agent]]]);
        assert.equal((await f.call('/connect', { agent })).status, 200);
        const result = f.skills.terminal(agent);
        assert.equal(result.command, agent === 'claude-code' ? 'claude' : agent);
        assert.equal(result.skillSynced, mode === 'success');
        if (mode === 'success') assert.equal(result.detail, undefined);
        else assert.equal(result.detail, `${LOCAL_AGENT_TABLE[agent].name}'s Astrid skill ${mode === 'timeout' ? 'sync timed out.' : "couldn't be synced."}`);
      }
      const after = await (await f.call('/agents')).json();
      assert.ok(after.agents.filter((row: { id: string }) => row.id !== 'astrid').every((row: { terminal: { skillSynced: boolean } }) => row.terminal.skillSynced === (mode === 'success')));
      assert.equal(f.calls().length, 3);
      if (mode === 'timeout') for (const call of f.calls()) assert.equal(processHasExited(call.pid), true);
      if (mode === 'success') {
        rmSync(join(f.home, '.codex', 'skills', 'astrid'));
        assert.equal(f.skills.terminal('codex').skillSynced, true, 'the service caches the sync result');
      }
    } finally { await f.close(); }
  });
}

test('cheap check requires a live entry pointing at the generated view and honors harness and state roots', async () => {
  const f = fixture();
  try {
    for (const agent of EXTERNAL_AGENT_IDS) {
      const harness = harnesses[agent];
      const harnessHome = join(f.home, 'custom', harness);
      const stateHome = join(f.home, 'custom-state');
      const env = { ...f.env, ASTRID_STATE_HOME: stateHome,
        [agent === 'claude-code' ? 'CLAUDE_CONFIG_DIR' : agent === 'codex' ? 'CODEX_HOME' : 'HERMES_HOME']: harnessHome };
      const skills = new LocalAgentSkills(env);
      const view = join(stateHome, 'astrid', 'skills', harness);
      const entry = join(harnessHome, 'skills', 'astrid');
      assert.equal(skills.terminal(agent).skillSynced, false);
      mkdirSync(join(harnessHome, 'skills'), { recursive: true });
      assert.match(skills.terminal(agent).detail!, /isn't synced/);
      symlinkSync(view, entry);
      assert.equal(skills.terminal(agent).skillSynced, false, 'dangling link');
      mkdirSync(view, { recursive: true });
      assert.equal(skills.terminal(agent).skillSynced, false, 'missing SKILL.md');
      writeFileSync(join(view, 'SKILL.md'), '# Astrid');
      assert.deepEqual(skills.terminal(agent), { command: agent === 'claude-code' ? 'claude' : agent, skillSynced: true });
      rmSync(entry);
      symlinkSync(f.home, entry);
      assert.equal(skills.terminal(agent).skillSynced, false, 'foreign target');
      rmSync(entry);
      mkdirSync(entry);
      writeFileSync(join(entry, 'SKILL.md'), '# User skill');
      assert.equal(skills.terminal(agent).skillSynced, false, 'foreign directory');
      rmSync(entry, { recursive: true });
      symlinkSync(view, entry);
      const xdg = new LocalAgentSkills({ ...env, ASTRID_STATE_HOME: '', XDG_STATE_HOME: stateHome });
      assert.equal(xdg.terminal(agent).skillSynced, true);
    }
    assert.equal(f.calls().length, 0);
  } finally { await f.close(); }
});

test('missing Astrid interpreter is a cached best-effort failure', async () => {
  const skills = new LocalAgentSkills({ HOME: process.cwd() });
  assert.deepEqual(await skills.sync('codex'), { skillSynced: false, detail: "Astrid's Python wasn't found." });
  assert.equal(skills.sync('codex'), skills.sync('codex'));
});

test('Astrid agents response reads the shim record for sign-in copy, steps and terminal command', async () => {
  const f = fixture();
  const previous = process.env.ASTRID_INSTALL_PROGRAM_ROOT;
  process.env.ASTRID_INSTALL_PROGRAM_ROOT = f.home;
  try {
    const record = join(f.home, 'install.json');
    const fallback = formatAstridSignInLine(f.home);
    for (const contents of [null, '{', '{}', '{"command_shim":null}', JSON.stringify({ command_shim: join(f.home, '.local/bin/astrid') }), '{"command_shim":null}']) {
      if (contents !== null) writeFileSync(record, contents);
      const written = contents?.includes('.local') ?? false;
      const line = written ? 'Run `astrid`, then enter `/login` in Astrid.' : fallback;
      const command = written ? 'astrid' : agentCommand(fallback);
      const response = await f.call('/agents');
      assert.equal(response.status, 200);
      const row = (await response.json()).agents.find((agent: { id: string }) => agent.id === 'astrid');
      assert.equal(row.line, line);
      assert.equal(row.signInLine, line);
      assert.deepEqual(row.steps, [
        { kind: 'sign-in', command, label: 'Start Astrid in a terminal' },
        { kind: 'sign-in', command: '/login', label: 'Then sign in' },
      ]);
      assert.deepEqual(row.terminal, { command, skillSynced: true });
    }
    writeFileSync(record, '{"command_shim":"managed"}');
    assert.equal(astridSignInLine({ ASTRID_SERVICE_PYTHON: join(f.home, 'versions/v1/py/bin/python') }), 'Run `astrid`, then enter `/login` in Astrid.');
    for (const platform of ['linux', 'darwin', 'win32'] as const) {
      assert.equal(formatAstridSignInLine(f.home, platform, true), 'Run `astrid`, then enter `/login` in Astrid.');
    }
  } finally {
    if (previous === undefined) delete process.env.ASTRID_INSTALL_PROGRAM_ROOT;
    else process.env.ASTRID_INSTALL_PROGRAM_ROOT = previous;
    await f.close();
  }
});
