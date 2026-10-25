import { randomBytes } from 'node:crypto';
import { mkdirSync } from 'node:fs';
import { dirname, isAbsolute, join } from 'node:path';
import type { Readable } from 'node:stream';
import { chatStoreIdentity } from './astrid-project-chat.ts';
import { createAstridAcpHttpServer, resolveAstridAcpBridgeConfig } from './astrid-acp-bridge.ts';
import { chatRegistration, chatRegistrationPath, removeChatRegistration, writeChatRegistration } from './chat-registration.ts';

export async function runChatService(env = process.env, createServer = createAstridAcpHttpServer, stdin: Readable = process.stdin): Promise<void> {
  const discovery = env.ASTRID_WORKSPACE_DISCOVERY?.trim();
  if (!discovery || !isAbsolute(discovery)) throw new Error('ASTRID_WORKSPACE_DISCOVERY must be an explicit absolute path');
  const config = resolveAstridAcpBridgeConfig({
    ...env,
    ASTRID_ACP_BRIDGE_TOKEN: env.ASTRID_ACP_BRIDGE_TOKEN?.trim() || randomBytes(32).toString('base64url'),
    ASTRID_ACP_LOCAL_MEDIA_ONLY: '1',
  });
  mkdirSync(config.cwd, { recursive: true });
  const { server, bridge } = createServer({ config: {
    ...config,
    modelStateFile: join(dirname(discovery), `chat-models-${chatStoreIdentity(config)}.json`),
  } });
  const path = chatRegistrationPath(discovery);
  let registration: ReturnType<typeof chatRegistration> | undefined;
  const release = () => {
    if (registration) removeChatRegistration(path, value => value?.endpoint === registration?.endpoint && value?.token === registration?.token);
  };
  let stopping: Promise<void> | undefined;
  const stop = () => stopping ??= (async () => {
    release();
    server.close();
    server.closeAllConnections();
    await bridge.close();
  })();
  try {
    await new Promise<void>((resolve, reject) => {
      server.once('error', reject);
      server.listen(config.port, '127.0.0.1', resolve);
    });
    const address = server.address();
    if (!address || typeof address === 'string') throw new Error('chat bridge did not bind a TCP port');
    registration = chatRegistration(address.port, config.token);
    const closed = new Promise<void>((resolve, reject) => {
      server.once('close', resolve);
      server.once('error', reject);
    });
    if (stdin.readableEnded) await stop();
    else {
      // Publish only after our own listener bound successfully.
      writeChatRegistration(path, registration);
      process.once('exit', release);
      process.once('SIGINT', stop);
      process.once('SIGTERM', stop);
      // Runtime owns this pipe. EOF requests the same cleanup as SIGTERM,
      // including native ACP hosts and terminals in the service's group.
      stdin.once('end', stop);
      stdin.resume();
      console.log(`Astrid chat listening on 127.0.0.1:${address.port}`);
    }
    await closed;
  } finally {
    await stop();
    stdin.removeListener('end', stop);
    stdin.pause();
    process.removeListener('exit', release);
    process.removeListener('SIGINT', stop);
    process.removeListener('SIGTERM', stop);
  }
}

if (import.meta.main) await runChatService();
