import { test } from 'bun:test';
import assert from 'node:assert/strict';
import { chmodSync, existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir, userInfo } from 'node:os';
import { join } from 'node:path';
import { formatAstridSignInLine, localAgentSteps, localAgentCatalogSchema, agentCommand, EXTERNAL_AGENT_IDS, LOCAL_AGENT_TABLE } from './localAgents.ts';
import { LOCAL_AGENT_ADAPTERS } from './astrid-local-agent-adapters.ts';
import { processHasExited } from './testProcess.ts';
import { defaultShellFromPasswd, discoverLocalAgents, executableNames, localAgentLaunch, windowsLoginPath } from './astrid-local-agents.ts';

function executable(directory: string, name: string, contents: string): string {
  const path = join(directory, name);
  writeFileSync(path, contents);
  chmodSync(path, 0o755);
  return path;
}

function fake(directory: string, name: string, output: string | { json: string; text: string }, exit = 0, stream: 'stdout' | 'stderr' = 'stdout'): string {
  const expectedArgs = name === 'claude' ? null : name === 'codex' ? 'login status' : null;
  const response = typeof output === 'string'
    ? `printf '%s' ${JSON.stringify(output)}`
    : `[ "$*" = "auth status" ] && printf '%s' ${JSON.stringify(output.json)} || { [ "$*" = "auth status --text" ] && printf '%s' ${JSON.stringify(output.text)} || exit 99; }`;
  return executable(directory, name, `#!/bin/sh\n${expectedArgs ? `[ "$*" = "${expectedArgs}" ] || exit 99\n` : ''}${response}${stream === 'stderr' ? ' >&2' : ''}\nexit ${exit}\n`);
}

function loginShell(directory: string): string {
  return executable(directory, 'login-shell', `#!/bin/sh\n[ "$1" = "-lic" ] || exit 98\nprintf '\\n__ASTRID_LOGIN_PATH__%s\\n' "$ASTRID_TEST_LOGIN_PATH"\n`);
}

function testEnv(shell: string, ownPath: string, loginPath = ownPath): NodeJS.ProcessEnv {
  return { ...process.env, PATH: ownPath, SHELL: shell, ASTRID_TEST_LOGIN_PATH: loginPath };
}

test('Astrid sign-in line uses absolute OS-specific launcher syntax and quotes custom roots', () => {
  assert.equal(formatAstridSignInLine("/tmp/Owner's $HOME", 'linux'),
    "Run `'/tmp/Owner'\"'\"'s $HOME/run-astrid'`, then enter `/login` in Astrid.");
  assert.equal(formatAstridSignInLine("C:\\Program Files\\Owner's Astrid", 'win32'),
    "Run `& 'C:\\Program Files\\Owner''s Astrid\\run-astrid.cmd'`, then enter `/login` in Astrid.");
});

test('reports Not installed when external CLIs are absent and has no adapter fields', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'astrid-local-agents-'));
  try {
    const shell = loginShell(directory);
    const agents = await discoverLocalAgents({ env: testEnv(shell, directory), cwd: directory });
    assert.deepEqual(agents.map(({ id, name, state, line }) => ({ id, name, state, line })), [
      { id: 'astrid', name: LOCAL_AGENT_TABLE.astrid.name, state: 'ready', line: null },
      { id: 'claude-code', name: LOCAL_AGENT_TABLE['claude-code'].name, state: 'not-installed', line: LOCAL_AGENT_TABLE['claude-code'].notInstalledLine },
      { id: 'codex', name: LOCAL_AGENT_TABLE.codex.name, state: 'not-installed', line: LOCAL_AGENT_TABLE.codex.notInstalledLine },
      { id: 'hermes', name: LOCAL_AGENT_TABLE.hermes.name, state: 'not-installed', line: LOCAL_AGENT_TABLE.hermes.notInstalledLine },
    ]);
    assert.equal(LOCAL_AGENT_TABLE['claude-code'].windowsNotInstalledLine, 'In PowerShell, run `irm https://claude.ai/install.ps1 | iex`');
    assert.equal(LOCAL_AGENT_TABLE.codex.notInstalledLine, 'Install with `curl -fsSL https://chatgpt.com/codex/install.sh | sh`');
    assert.equal(LOCAL_AGENT_TABLE.codex.windowsNotInstalledLine, 'In PowerShell, run `irm https://chatgpt.com/codex/install.ps1 | iex`');
    assert.doesNotMatch(JSON.stringify(LOCAL_AGENT_TABLE), /npm install -g/);
    assert.deepEqual(agents.map(agent => agent.signIn), ['unknown', 'unknown', 'unknown', 'unknown']);
    assert.equal('adapterInstalled' in agents[1]!, false);
    assert.equal('installCommand' in agents[1]!, false);
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

test('Windows PATH uses expanded user and machine registry values without starting a shell', async () => {
  const calls: string[] = [];
  const env = { USERPROFILE: 'C:\\Users\\Astrid', SystemRoot: 'C:\\Windows' };
  const registryReader = async (key: string) => {
    calls.push(key);
    return key.startsWith('HKCU')
      ? 'HKEY_CURRENT_USER\\Environment\r\n    LocalBin    REG_EXPAND_SZ    %USERPROFILE%\\bin\r\n    Path    REG_EXPAND_SZ    %LOCALBIN%;%SystemRoot%\\UserTools\r\n'
      : 'HKEY_LOCAL_MACHINE\\Environment\r\n    Path    REG_EXPAND_SZ    %SystemRoot%\\System32\r\n';
  };
  assert.equal(await windowsLoginPath(env, registryReader), 'C:\\Users\\Astrid\\bin;C:\\Windows\\UserTools;C:\\Windows\\System32');
  assert.deepEqual(calls.sort(), [
    'HKCU\\Environment',
    'HKLM\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Environment',
  ]);
  assert.deepEqual(executableNames('claude', 'win32', '.CMD;.EXE;.BAT'), ['claude.CMD', 'claude.EXE', 'claude.BAT', 'claude']);
  const missing = await discoverLocalAgents({ env: { ...process.env, PATH: '' }, platform: 'win32', registryReader: async () => '' });
  assert.equal(missing.find(agent => agent.id === 'claude-code')?.line, 'In PowerShell, run `irm https://claude.ai/install.ps1 | iex`');
  assert.equal(missing.find(agent => agent.id === 'codex')?.line, 'In PowerShell, run `irm https://chatgpt.com/codex/install.ps1 | iex`');
});

test('Windows discovery finds a user CLI .cmd shim from the registry PATH', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'astrid-windows-path-'));
  try {
    executable(directory, 'claude.CMD', '@echo off\r\n');
    executable(directory, 'codex.EXE', '');
    const claudePackage = join(directory, 'node_modules', '@anthropic-ai', 'claude-code');
    mkdirSync(claudePackage, { recursive: true });
    writeFileSync(join(claudePackage, 'cli.js'), `require('node:fs').writeSync(1, JSON.stringify({ loggedIn: true }));\n`);
    const env = { ...process.env, PATH: '', PATHEXT: '.EXE;.CMD' };
    const registryReader = async (key: string) => key.startsWith('HKCU')
      ? `Path    REG_SZ    ${directory}\r\n`
      : '';
    const agents = await discoverLocalAgents({ env, platform: 'win32', registryReader, cwd: directory });
    assert.equal(agents.find(agent => agent.id === 'claude-code')?.state, 'ready');
    assert.equal(agents.find(agent => agent.id === 'claude-code')?.line, null);
    assert.equal(agents.find(agent => agent.id === 'codex')?.state, 'ready');
    assert.equal(agents.find(agent => agent.id === 'claude-code')?.state === 'not-installed', false);
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

test('Windows Claude npm shims resolve to the JavaScript entrypoint used by auth checks and ACP launch', async () => {
  const prefix = mkdtempSync(join(tmpdir(), 'astrid-windows-npm-'));
  const packageDirectory = join(prefix, 'node_modules', '@anthropic-ai', 'claude-code');
  try {
    mkdirSync(packageDirectory, { recursive: true });
    executable(prefix, 'claude.CMD', '@echo off\r\n');
    executable(prefix, 'bun.EXE', '');
    const entryPoint = join(packageDirectory, 'cli.js');
    writeFileSync(entryPoint, `require('node:fs').writeSync(1, JSON.stringify({ loggedIn: false }));\n`);
    const env = { ...process.env, PATH: '', PATHEXT: '.CMD;.EXE' };
    const registryReader = async (key: string) => key.startsWith('HKCU') ? `Path    REG_SZ    ${prefix}\r\n` : '';

    const agents = await discoverLocalAgents({ env, platform: 'win32', registryReader });
    assert.equal(agents.find(agent => agent.id === 'claude-code')?.state, 'sign-in');
    assert.equal(agents.find(agent => agent.id === 'claude-code')?.signIn, 'signed-out');
    assert.deepEqual(agents.find(agent => agent.id === 'claude-code')?.steps, [{ kind: 'sign-in', command: 'claude auth login', label: 'Sign in' }]);
    const launch = await localAgentLaunch('claude-code', env, { platform: 'win32', registryReader });
    assert.deepEqual(launch, {
      command: join(prefix, 'bun.EXE'),
      args: ['x', '--bun', LOCAL_AGENT_ADAPTERS['claude-code']],
      env: { PATH: prefix },
      cliPath: entryPoint,
    });
    assert.equal(launch.cliPath.endsWith('.js'), true);
  } finally { rmSync(prefix, { recursive: true, force: true }); }
});

test('Windows Codex npm shims run the package entrypoint for auth status and retain the shim for ACP launch', async () => {
  const prefix = mkdtempSync(join(tmpdir(), 'astrid-windows-codex-'));
  const packageDirectory = join(prefix, 'node_modules', '@openai', 'codex', 'bin');
  try {
    mkdirSync(packageDirectory, { recursive: true });
    const shim = executable(prefix, 'codex.CMD', '@echo off\r\n');
    executable(prefix, 'bun.EXE', '');
    const entryPoint = join(packageDirectory, 'codex.js');
    writeFileSync(entryPoint, `require('node:fs').writeSync(1, process.env.ASTRID_TEST_CODEX_SIGNED_IN === '1' ? 'Logged in using ChatGPT' : 'Not logged in. Run codex login to authenticate.');\n`);
    const env = { ...process.env, PATH: '', PATHEXT: '.CMD;.EXE', ASTRID_TEST_CODEX_SIGNED_IN: '0' };
    const registryReader = async (key: string) => key.startsWith('HKCU') ? `Path    REG_SZ    ${prefix}\r\n` : '';

    let agents = await discoverLocalAgents({ env, platform: 'win32', registryReader });
    assert.equal(agents.find(agent => agent.id === 'codex')?.state, 'sign-in');
    env.ASTRID_TEST_CODEX_SIGNED_IN = '1';
    agents = await discoverLocalAgents({ env, platform: 'win32', registryReader });
    assert.equal(agents.find(agent => agent.id === 'codex')?.state, 'ready');
    assert.deepEqual(await localAgentLaunch('codex', env, { platform: 'win32', registryReader }), {
      command: join(prefix, 'bun.EXE'),
      args: ['x', '--bun', LOCAL_AGENT_ADAPTERS.codex],
      env: { PATH: prefix, CODEX_PATH: shim },
      cliPath: shim,
    });
  } finally { rmSync(prefix, { recursive: true, force: true }); }
});

test('falls back to the default passwd shell when SHELL is unset', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'astrid-passwd-shell-'));
  try {
    const shell = loginShell(directory);
    assert.equal(defaultShellFromPasswd(`astrid:x:${process.getuid?.() ?? 1234}:100:User:/home/astrid:${shell}\n`, 'astrid', String(process.getuid?.() ?? 1234)), shell);
    const env = { ...process.env, SHELL: undefined, PATH: directory, ASTRID_TEST_LOGIN_PATH: directory };
    const agents = await discoverLocalAgents({ env, platform: 'linux', passwdReader: () => `astrid:x:${process.getuid?.() ?? 1234}:100:User:/home/astrid:${shell}\n`, cwd: directory });
    assert.equal(agents.find(agent => agent.id === 'claude-code')?.state, 'not-installed');
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

test('macOS falls back to the account shell from Directory Service when passwd lacks the user', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'astrid-directory-shell-'));
  try {
    const loginBin = join(directory, 'login-bin');
    mkdirSync(loginBin);
    const shell = loginShell(directory);
    fake(loginBin, 'claude', { json: '{"loggedIn":true}', text: '' });
    const env = { ...process.env, SHELL: undefined, PATH: directory, ASTRID_TEST_LOGIN_PATH: loginBin };
    let lookedUp: string | undefined;
    const agents = await discoverLocalAgents({
      env,
      platform: 'darwin',
      passwdReader: () => '',
      systemShellReader: async username => { lookedUp = username; return shell; },
    });
    assert.equal(lookedUp, userInfo().username);
    assert.equal(agents.find(agent => agent.id === 'claude-code')?.state, 'ready');
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

test('signed-in CLIs are Ready with no adapters installed and launch uses each pinned Bun command', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'astrid-local-launch-'));
  try {
    const shell = loginShell(directory);
    const claude = fake(directory, 'claude', { json: '{"loggedIn":true}', text: '' });
    const codex = fake(directory, 'codex', 'Logged in using ChatGPT');
    const bun = executable(directory, 'bun', '#!/bin/sh\nexit 0\n');
    const env = testEnv(shell, directory);
    const agents = await discoverLocalAgents({ env });
    assert.deepEqual(agents.slice(1, 3).map(({ id, state, line }) => ({ id, state, line })), [
      { id: 'claude-code', state: 'ready', line: null },
      { id: 'codex', state: 'ready', line: null },
    ]);
    assert.equal(existsSync(join(directory, 'claude-agent-acp')), false);
    assert.equal(existsSync(join(directory, 'codex-acp')), false);
    assert.deepEqual(await localAgentLaunch('claude-code', env), {
      command: bun, args: ['x', '--bun', LOCAL_AGENT_ADAPTERS['claude-code']], env: { PATH: directory }, cliPath: claude,
    });
    assert.deepEqual(await localAgentLaunch('codex', env), {
      command: bun, args: ['x', '--bun', LOCAL_AGENT_ADAPTERS.codex], env: { PATH: directory, CODEX_PATH: codex }, cliPath: codex,
    });
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

test('prefers the login-shell CLI and service Bun, with login Bun as fallback', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'astrid-login-path-'));
  try {
    const servicePath = join(directory, 'service-bin');
    const loginPath = join(directory, 'login-bin');
    const shellDir = join(directory, 'shell-bin');
    for (const path of [servicePath, loginPath, shellDir]) mkdirSync(path);
    const shell = loginShell(shellDir);
    const claude = fake(loginPath, 'claude', { json: '{"loggedIn":true}', text: '' });
    fake(loginPath, 'codex', 'Logged in using ChatGPT');
    const bun = executable(servicePath, 'bun', '#!/bin/sh\nexit 0\n');
    executable(loginPath, 'bun', '#!/bin/sh\nexit 99\n');
    const env = testEnv(shell, servicePath, loginPath);
    const agents = await discoverLocalAgents({ env });
    assert.equal(agents[1]?.state, 'ready');
    assert.equal(agents[2]?.state, 'ready');
    assert.deepEqual(await localAgentLaunch('claude-code', env), {
      command: bun,
      args: ['x', '--bun', LOCAL_AGENT_ADAPTERS['claude-code']],
      env: { PATH: `${loginPath}:${servicePath}` },
      cliPath: claude,
    });
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

test('falls back to login-shell Bun when service PATH has no Bun', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'astrid-login-bun-fallback-'));
  try {
    const servicePath = join(directory, 'service-bin');
    const loginPath = join(directory, 'login-bin');
    const shellDir = join(directory, 'shell-bin');
    for (const path of [servicePath, loginPath, shellDir]) mkdirSync(path);
    const shell = loginShell(shellDir);
    const claude = fake(loginPath, 'claude', { json: '{"loggedIn":true}', text: '' });
    const loginBun = executable(loginPath, 'bun', '#!/bin/sh\nexit 0\n');
    const env = testEnv(shell, servicePath, loginPath);

    assert.deepEqual(await localAgentLaunch('claude-code', env), {
      command: loginBun,
      args: ['x', '--bun', LOCAL_AGENT_ADAPTERS['claude-code']],
      env: { PATH: `${loginPath}:${servicePath}` },
      cliPath: claude,
    });
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

test('reads Claude JSON login status and falls back to text only when JSON is invalid', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'astrid-local-agents-'));
  try {
    const shell = loginShell(directory);
    fake(directory, 'claude', { json: '{"loggedIn":false}', text: 'Not logged in. Run claude auth login to authenticate.' });
    let agents = await discoverLocalAgents({ env: testEnv(shell, directory) });
    assert.equal(agents[1]?.state, 'sign-in');
    assert.equal(agents[1]?.signIn, 'signed-out');
    assert.equal(agents[1]?.line, LOCAL_AGENT_TABLE['claude-code'].signInLine);

    fake(directory, 'claude', { json: '{invalid json', text: 'Authenticated with Claude Pro account' });
    agents = await discoverLocalAgents({ env: testEnv(shell, directory) });
    assert.equal(agents[1]?.state, 'ready');
    assert.equal(agents[1]?.signIn, 'signed-in');
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

test('reads successful Codex status from stderr and does not trust failed diagnostics', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'astrid-local-agents-'));
  try {
    const shell = loginShell(directory);
    fake(directory, 'codex', 'Logged in using ChatGPT', 0, 'stderr');
    let agents = await discoverLocalAgents({ env: testEnv(shell, directory) });
    assert.equal(agents[2]?.state, 'ready');

    fake(directory, 'codex', 'Not logged in. Run codex login to authenticate.', 1, 'stderr');
    agents = await discoverLocalAgents({ env: testEnv(shell, directory) });
    assert.equal(agents[2]?.state, 'sign-in');

    fake(directory, 'codex', 'Diagnostic: Logged in using ChatGPT', 1, 'stderr');
    agents = await discoverLocalAgents({ env: testEnv(shell, directory) });
    assert.equal(agents[2]?.state, 'ready');
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

for (const probe of ['claude-json', 'claude-text', 'codex'] as const) {
  test(`${probe} discovery times out, kills its child, and preserves other agent choices`, async () => {
    const directory = mkdtempSync(join(tmpdir(), 'astrid-hanging-auth-'));
    const calls = join(directory, 'hanging.jsonl');
    try {
      const shell = loginShell(directory);
      const name = probe === 'codex' ? 'codex' : 'claude';
      fake(directory, name === 'codex' ? 'claude' : 'codex', name === 'codex'
        ? { json: '{"loggedIn":false}', text: '' }
        : 'Not logged in. Run codex login to authenticate.');
      executable(directory, name, `#!${process.execPath}
const fs = require('node:fs');
const args = process.argv.slice(2);
if (${JSON.stringify(probe)} === 'claude-text' && args.length === 2) {
  require('node:fs').writeSync(1, '{invalid json');
} else {
  fs.appendFileSync(${JSON.stringify(calls)}, JSON.stringify({ pid: process.pid, args }) + '\\n');
  process.on('SIGTERM', () => {});
  require('node:fs').writeSync(1, ${JSON.stringify(probe === 'claude-json' ? '{"loggedIn":false}' : 'Not logged in. Run codex login to authenticate.')});
  setInterval(() => {}, 1000);
}
`);
      const started = Date.now();
      const agents = await discoverLocalAgents({ env: testEnv(shell, directory), cwd: directory });
      assert.ok(Date.now() - started < 9_000, 'one hanging probe must finish before the browser deadline');
      assert.equal(agents[0]?.state, 'ready');
      assert.equal(agents.find(agent => agent.id === (name === 'codex' ? 'codex' : 'claude-code'))?.state, 'ready', 'timed-out status remains unknown');
      assert.equal(agents.find(agent => agent.id === (name === 'codex' ? 'codex' : 'claude-code'))?.signIn, 'unknown');
      assert.equal(agents.find(agent => agent.id === (name === 'codex' ? 'claude-code' : 'codex'))?.state, 'sign-in');
      const children = readFileSync(calls, 'utf8').trim().split('\n').map(line => JSON.parse(line) as { pid: number; args: string[] });
      assert.equal(children.length, 1, 'a timed-out Claude JSON probe must not start a text probe');
      assert.deepEqual(children[0]!.args, probe === 'codex' ? ['login', 'status'] : probe === 'claude-text' ? ['auth', 'status', '--text'] : ['auth', 'status']);
      for (const child of children) assert.equal(processHasExited(child.pid), true);
    } finally {
      if (existsSync(calls)) for (const line of readFileSync(calls, 'utf8').trim().split('\n')) {
        const pid = (JSON.parse(line) as { pid: number }).pid;
        if (!processHasExited(pid)) {
          try { process.kill(pid, 'SIGKILL'); } catch { /* Already reaped. */ }
        }
      }
      rmSync(directory, { recursive: true, force: true });
    }
  }, 12_000);
}
test('discovers Hermes with hermes acp --check and launches its native ACP command', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'astrid-hermes-'));
  try {
    const shell = loginShell(directory);
    const hermes = executable(directory, 'hermes', '#!/bin/sh\n[ "$*" = "acp --check" ] || exit 91\nexit "${HERMES_CHECK_EXIT:-0}"\n');
    const env = testEnv(shell, directory);
    assert.equal((await discoverLocalAgents({ env }))[3]?.state, 'ready');
    assert.deepEqual(await localAgentLaunch('hermes', env), { command: hermes, args: ['acp'], env: { PATH: directory }, cliPath: hermes });
    assert.equal((await discoverLocalAgents({ env: { ...env, HERMES_CHECK_EXIT: '7' } }))[3]?.state, 'sign-in');
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

test('keeps Hermes ready when ACP check is inconclusive or times out', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'astrid-hermes-check-'));
  try {
    const shell = loginShell(directory);
    executable(directory, 'hermes', '#!/bin/sh\n[ "$*" = "acp --check" ] || exit 91\ncase "$HERMES_CHECK_MODE" in unknown) kill -TERM $$ ;; timeout) /bin/sleep 10 ;; esac\n');
    for (const mode of ['unknown', 'timeout']) {
      const agents = await discoverLocalAgents({ env: { ...testEnv(shell, directory), HERMES_CHECK_MODE: mode } });
      assert.equal(agents[3]?.state, 'ready', `${mode} check should remain usable`);
    }
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

test('mixed external-agent authentication states stay associated with their agent', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'astrid-mixed-auth-'));
  try {
    const shell = loginShell(directory);
    fake(directory, 'claude', { json: '{"loggedIn":true}', text: 'Logged in' });
    fake(directory, 'codex', 'Logged in using ChatGPT', 0, 'stderr');
    executable(directory, 'hermes', '#!/bin/sh\nexit 7\n');
    const agents = await discoverLocalAgents({ env: testEnv(shell, directory) });
    assert.deepEqual(agents.map(agent => [agent.id, agent.state]), [
      ['astrid', 'ready'], ['claude-code', 'ready'], ['codex', 'ready'], ['hermes', 'sign-in'],
    ]);
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

for (const platform of ['linux', 'win32'] as const) {
  test(`${platform} setup steps use the table's install and sign-in commands in order`, async () => {
    const missing = await discoverLocalAgents({ env: { PATH: '', SHELL: '/missing' }, platform, registryReader: async () => '', passwdReader: () => '' });
    for (const id of EXTERNAL_AGENT_IDS) {
      const definition = LOCAL_AGENT_TABLE[id];
      const install = platform === 'win32' ? definition.windowsNotInstalledLine! : definition.notInstalledLine!;
      const signIn = { kind: 'sign-in', command: agentCommand(definition.signInLine), label: 'Sign in' };
      assert.deepEqual(missing.find(agent => agent.id === id)?.steps, [
        { kind: 'install', command: agentCommand(install), label: `Install ${definition.name}` }, signIn,
      ]);
      assert.deepEqual(localAgentSteps(id, 'sign-in', platform), [signIn]);
      assert.deepEqual(localAgentSteps(id, 'ready', platform), []);
    }
    const env = { PATH: '', SHELL: '/missing' };
    const options = { platform, registryReader: async () => '', passwdReader: () => '' };
    await assert.rejects(localAgentLaunch('codex', env, options), {
      code: 'agent_not_installed', next_action: localAgentSteps('codex', 'not-installed', platform)[0]!.command,
    });
  });

  test(`${platform} missing Bun names the Astrid installer as the recovery command`, async () => {
    const directory = mkdtempSync(join(process.cwd(), '.agent-runtime-test-'));
    try {
      executable(directory, platform === 'win32' ? 'codex.EXE' : 'codex', '#!/bin/sh\nexit 0\n');
      await assert.rejects(localAgentLaunch('codex', { PATH: directory, PATHEXT: '.EXE', SHELL: '/missing' }, { platform, registryReader: async () => '', passwdReader: () => '' }), {
        code: 'agent_runtime_missing', next_action: platform === 'win32'
          ? 'irm https://astrid.haus/install.ps1 | iex' : 'curl -fsSL https://astrid.haus/install | sh',
      });
    } finally { rmSync(directory, { recursive: true, force: true }); }
  });
}

test('discovery retains each probe sign-in result without changing legacy readiness', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'astrid-auth-values-'));
  try {
    const shell = loginShell(directory);
    const env = testEnv(shell, directory);
    for (const signIn of ['signed-in', 'signed-out', 'unknown'] as const) {
      fake(directory, 'claude', { json: signIn === 'unknown' ? '{}' : JSON.stringify({ loggedIn: signIn === 'signed-in' }), text: '' });
      fake(directory, 'codex', signIn === 'signed-in' ? 'Logged in using ChatGPT' : signIn === 'signed-out' ? 'Not logged in' : 'Unrecognized');
      executable(directory, 'hermes', signIn === 'unknown' ? '#!/bin/sh\nkill -TERM $$\n' : `#!/bin/sh\nexit ${signIn === 'signed-in' ? 0 : 7}\n`);
      for (const agent of (await discoverLocalAgents({ env })).slice(1)) {
        assert.equal(agent.signIn, signIn, agent.id);
        assert.equal(agent.state, signIn === 'signed-out' ? 'sign-in' : 'ready');
        assert.deepEqual(agent.steps, signIn === 'signed-out' ? localAgentSteps(agent.id, 'sign-in') : []);
      }
    }
  } finally { rmSync(directory, { recursive: true, force: true }); }
});

test('agent schema accepts old rows and the optional contract fields, dropping future fields', () => {
  const row = { id: 'astrid', name: 'Astrid', state: 'ready', line: null, models: [], defaultModel: null };
  assert.deepEqual(localAgentCatalogSchema.parse({ defaultAgent: 'astrid', agents: [row] }).agents[0], row);
  const extended = { ...row, signIn: 'unknown', steps: [], creditLine: 'Built on oh-my-pi', creditUrl: 'https://github.com/can1357/oh-my-pi', terminal: { command: 'astrid', skillSynced: false, detail: 'A plain sentence.' } };
  assert.deepEqual(localAgentCatalogSchema.parse({ defaultAgent: 'astrid', agents: [{ ...extended, future: true }] }).agents[0], extended);
});
