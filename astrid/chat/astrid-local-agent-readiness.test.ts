import { test } from 'bun:test';
import assert from 'node:assert/strict';
import { chmodSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { createAstridAcpHttpServer } from './astrid-acp-bridge.ts';
import { bridgeTestClient } from './bridge-test-client.ts';
import { createLocalAgentAcpProcessHost } from './astrid-local-agent-host.ts';
import { EXTERNAL_AGENT_IDS, LOCAL_AGENT_TABLE, localAgentSteps } from './localAgents.ts';

function executable(path: string, contents: string): void {
  writeFileSync(path, contents);
  chmodSync(path, 0o755);
}

for (const agent of EXTERNAL_AGENT_IDS) {
  test(`${agent} host and bridge return specific readiness errors with recovery commands`, async () => {
    const directory = mkdtempSync(join(process.cwd(), '.agent-host-test-'));
    const bin = join(directory, 'bin');
    mkdirSync(bin);
    const shell = join(directory, 'login-shell');
    executable(shell, `#!/bin/sh\nprintf '\\n__ASTRID_LOGIN_PATH__%s\\n' "$ASTRID_TEST_LOGIN_PATH"\n`);
    const dshHome = join(directory, 'dsh-home');
    mkdirSync(dshHome, { recursive: true });
    const options = { cwd: directory, env: { PATH: bin, SHELL: shell, ASTRID_TEST_LOGIN_PATH: bin, DSH_HOME: dshHome, ASTRID_STATE_HOME: join(directory, 'state') } };
    const { bridge } = createAstridAcpHttpServer({
      config: { token: 'readiness', cwd: directory, port: 0 },
      localAgentHostFactory: (id, launchOptions) => createLocalAgentAcpProcessHost(id, { ...launchOptions, ...options }),
    });
    const call = bridgeTestClient(bridge, 'readiness');
    const check = async (error: string, detail: string, next_action: string) => {
      await assert.rejects(createLocalAgentAcpProcessHost(agent, options), { code: error, next_action });
      const response = await call('/connect', { agent });
      assert.equal(response.status, 502);
      assert.deepEqual(await response.json(), { error, detail, next_action });
    };
    try {
      await check('agent_not_installed', `${LOCAL_AGENT_TABLE[agent].name} is not installed on PATH.`, localAgentSteps(agent, 'not-installed')[0]!.command);
      const cli = join(bin, agent === 'claude-code' ? 'claude' : agent);
      // DeepSeek Harness has no auth subcommand: its empty credential store is the signed-out state.
      executable(cli, agent === 'hermes' ? '#!/bin/sh\nexit 7\n' : agent === 'dsh'
        ? '#!/bin/sh\nexit 0\n'
        : agent === 'codex'
        ? '#!/bin/sh\nprintf "Not logged in" >&2\nexit 1\n'
        : '#!/bin/sh\nprintf \'{"loggedIn":false}\'\n');
      await check('agent_sign_in_required', `${LOCAL_AGENT_TABLE[agent].name} sign-in is required. ${LOCAL_AGENT_TABLE[agent].signInLine}`, localAgentSteps(agent, 'sign-in')[0]!.command);
      if (agent === 'dsh') {
        // Signed in through the store DSH itself writes, but the harness needs a
        // modern Node and the fixture PATH only has Astrid's bundled one.
        writeFileSync(join(dshHome, '.credentials.yaml'), 'DEEPSEEK_API_KEY: sk-test-fixture\n');
        await check('agent_runtime_missing', 'Node 22 or newer is not installed on PATH.', process.platform === 'win32' ? 'winget install OpenJS.NodeJS.LTS' : 'brew install node');
      } else if (agent !== 'hermes') {
        executable(cli, agent === 'codex' ? '#!/bin/sh\nprintf "Logged in using ChatGPT" >&2\n' : '#!/bin/sh\nprintf \'{"loggedIn":true}\'\n');
        await check('agent_runtime_missing', 'Bun is not installed on PATH.', 'curl -fsSL https://astrid.haus/install | sh');
      }
    } finally { await bridge.close(); rmSync(directory, { recursive: true, force: true }); }
  });
}
