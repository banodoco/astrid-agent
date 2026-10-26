import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { LOCAL_AGENT_ADAPTERS } from './astrid-local-agent-adapters.ts';

type AdapterId = keyof typeof LOCAL_AGENT_ADAPTERS;
type JsonRecord = Record<string, unknown>;
const INITIALIZE_TIMEOUT_MS = 90_000;

async function initialize(adapterId: AdapterId, packageName: string): Promise<void> {
  const child = spawn('bun', ['x', '--bun', packageName], { stdio: ['pipe', 'pipe', 'pipe'] });
  const lines = createInterface({ input: child.stdout });
  let stderr = '';
  let timer: ReturnType<typeof setTimeout> | undefined;
  const result = new Promise<void>((resolve, reject) => {
    let settled = false;
    const finish = (error?: Error) => {
      if (settled) return;
      settled = true;
      if (error) reject(error);
      else resolve();
    };
    child.stderr.setEncoding('utf8');
    child.stderr.on('data', chunk => { stderr = `${stderr}${String(chunk)}`.slice(-4_000); });
    child.stdin.on('error', error => finish(error));
    child.on('error', error => finish(error));
    child.on('close', (code, signal) => finish(new Error(`${packageName} exited before ACP initialize completed (${signal ?? code}).${stderr ? `\n${stderr}` : ''}`)));
    lines.on('line', line => {
      let message: JsonRecord;
      try {
        const parsed: unknown = JSON.parse(line);
        if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) throw new Error('Expected a JSON-RPC object.');
        message = parsed as JsonRecord;
      } catch (error) {
        finish(new Error(`${packageName} wrote invalid ACP output: ${error instanceof Error ? error.message : String(error)}`));
        return;
      }
      if (message.id !== 1) return;
      if (message.error) {
        finish(new Error(`${packageName} rejected ACP initialize: ${JSON.stringify(message.error)}`));
        return;
      }
      const response = message.result;
      if (typeof response !== 'object' || response === null || Reflect.get(response, 'protocolVersion') !== 1) {
        finish(new Error(`${packageName} returned an invalid ACP initialize result.`));
        return;
      }
      finish();
    });
    timer = setTimeout(() => finish(new Error(`${packageName} did not complete ACP initialize within ${INITIALIZE_TIMEOUT_MS} ms.${stderr ? `\n${stderr}` : ''}`)), INITIALIZE_TIMEOUT_MS);
  });

  child.stdin.write(`${JSON.stringify({
    jsonrpc: '2.0',
    id: 1,
    method: 'initialize',
    params: {
      protocolVersion: 1,
      clientCapabilities: {},
      clientInfo: { name: 'Astrid adapter CI smoke', version: '1' },
    },
  })}\n`);
  try {
    await result;
  } finally {
    if (timer) clearTimeout(timer);
    lines.close();
    child.kill('SIGTERM');
  }
  console.log(`${adapterId}: ACP initialize passed under Bun (${packageName}).`);
}

// Hermes and Octop speak ACP from their own CLIs, so they deliberately have no pinned Bun adapter.
for (const [adapterId, packageName] of Object.entries(LOCAL_AGENT_ADAPTERS) as Array<[AdapterId, string]>) {
  await initialize(adapterId, packageName);
}
