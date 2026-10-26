import { test } from 'bun:test';
import assert from 'node:assert/strict';
import { acpClientCapabilities } from './acp-client-capabilities.ts';
import type { LocalAgentId } from './localAgents.ts';

const recommendation = { jetbrains: { air: { version: 1, capabilities: ['recommendedValue'] } } };

test('Windows OMP uses its embedded shell and other ACP routes retain terminals', () => {
  assert.deepEqual(acpClientCapabilities('astrid', 'win32'), { _meta: recommendation });
  assert.equal('terminal' in acpClientCapabilities('astrid', 'win32'), false);
  for (const agent of ['claude-code', 'codex', 'hermes', 'octop'] as LocalAgentId[]) {
    assert.deepEqual(acpClientCapabilities(agent, 'win32'), { terminal: true, _meta: recommendation });
  }
  for (const platform of ['darwin', 'linux'] as const) {
    for (const agent of ['astrid', 'claude-code', 'codex', 'hermes', 'octop'] as LocalAgentId[]) {
      assert.deepEqual(acpClientCapabilities(agent, platform), { terminal: true, _meta: recommendation });
    }
  }
});
