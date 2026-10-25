import { describe, expect, it } from 'bun:test';
import type { JsonRpcProcessLike } from './process-types.ts';
import {
  AcpProcessHost,
  type AcpProcessSpawnOptions,
} from './acpProcessHost.ts';
import {
  ASTRID_ACP_AGENT_IDENTITY,
  ASTRID_ACP_COMMAND,
  ASTRID_ACP_CONTEXT_SYSTEM_PROMPT,
  createAstridAcpProcessHost,
} from './astridAcpLauncher.ts';

type Listener = (...args: unknown[]) => void;

class FakeReadable {
  private readonly listeners = new Map<string, Listener[]>();

  on(event: string, listener: Listener): this {
    this.listeners.set(event, [...(this.listeners.get(event) ?? []), listener]);
    return this;
  }

  emit(event: string, ...args: unknown[]): void {
    for (const listener of this.listeners.get(event) ?? []) listener(...args);
  }
}

class FakeWritable {
  readonly writes: string[] = [];
  ended = false;

  write(chunk: string): boolean {
    this.writes.push(chunk);
    return true;
  }

  end(): void {
    this.ended = true;
  }
}

class FakeProcess {
  readonly stdin = new FakeWritable();
  readonly stdout = new FakeReadable();
  private readonly listeners = new Map<string, Listener[]>();

  on(event: string, listener: Listener): this {
    this.listeners.set(event, [...(this.listeners.get(event) ?? []), listener]);
    return this;
  }

  emit(event: string, ...args: unknown[]): void {
    for (const listener of this.listeners.get(event) ?? []) listener(...args);
  }

  kill(): boolean {
    return true;
  }

  asProcess(): JsonRpcProcessLike {
    return this as unknown as JsonRpcProcessLike;
  }
}

function lastRequest(process: FakeProcess): Record<string, unknown> {
  const line = process.stdin.writes.at(-1);
  if (!line) throw new Error('expected an ACP request');
  return JSON.parse(line) as Record<string, unknown>;
}

function respond(process: FakeProcess, id: unknown, result: unknown): void {
  process.stdout.emit('data', `${JSON.stringify({ jsonrpc: '2.0', id, result })}\n`);
}

async function flush(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
}

describe('AcpProcessHost', () => {
  it('forwards initialize/new/close and preserves the opaque OMP session id', async () => {
    const process = new FakeProcess();
    const host = new AcpProcessHost({ process: process.asProcess(), requestTimeoutMs: 1_000 });

    const initializePromise = host.initialize<{ agentCapabilities: { loadSession: boolean } }>({
      protocolVersion: 1,
      clientCapabilities: {},
    });
    await flush();
    const initializeRequest = lastRequest(process);
    expect(initializeRequest).toMatchObject({ jsonrpc: '2.0', id: 1, method: 'initialize' });
    respond(process, initializeRequest.id, { agentCapabilities: { loadSession: true } });
    await expect(initializePromise).resolves.toEqual({ agentCapabilities: { loadSession: true } });

    const newSessionPromise = host.newSession<{ sessionId: string }>({ cwd: '/tmp/astrid', mcpServers: [] });
    await flush();
    const newSessionRequest = lastRequest(process);
    expect(newSessionRequest).toMatchObject({ method: 'session/new', params: { cwd: '/tmp/astrid' } });
    respond(process, newSessionRequest.id, { sessionId: 'omp-owned-session-opaque' });
    await expect(newSessionPromise).resolves.toEqual({ sessionId: 'omp-owned-session-opaque' });

    const closePromise = host.closeSession({ sessionId: 'omp-owned-session-opaque' });
    await flush();
    const closeRequest = lastRequest(process);
    expect(closeRequest).toMatchObject({ method: 'session/close', params: { sessionId: 'omp-owned-session-opaque' } });
    respond(process, closeRequest.id, {});
    await expect(closePromise).resolves.toEqual({});
    await host.dispose();
    expect(process.stdin.ended).toBe(true);
  });

  it('surfaces the installed agent session-not-found detail instead of generic Internal error', async () => {
    const process = new FakeProcess();
    const host = new AcpProcessHost({ process: process.asProcess() });
    const loading = host.loadSession({ sessionId: 'saved-chat' });
    await flush();
    const request = lastRequest(process);
    process.stdout.emit('data', `${JSON.stringify({
      jsonrpc: '2.0', id: request.id,
      error: { code: -32603, message: 'Internal error', data: { details: 'ACP session not found: saved-chat' } },
    })}\n`);
    await expect(loading).rejects.toMatchObject({ code: 'acp_transport', message: 'ACP request "session/load" failed: ACP session not found: saved-chat' });
    await host.dispose();
  });

  for (const method of ['session/load', 'session/resume']) {
    it(`explains an unavailable saved model on ${method} and permits a new chat`, async () => {
      const process = new FakeProcess();
      const host = new AcpProcessHost({ process: process.asProcess() });
      const loading = host.request(method, { sessionId: 'old-chat' });
      await flush();
      process.stdout.emit('data', `${JSON.stringify({
        jsonrpc: '2.0', id: lastRequest(process).id,
        error: { code: -32603, message: 'Internal error', data: { details: 'Could not restore model retired/model' } },
      })}\n`);
      await expect(loading).rejects.toMatchObject({
        code: 'acp_transport', message: expect.stringContaining("This chat's saved model is unavailable. Start a new chat."),
      });
      const creating = host.newSession({ cwd: '/chat', mcpServers: [] });
      await flush();
      respond(process, lastRequest(process).id, { sessionId: 'new-chat' });
      await expect(creating).resolves.toEqual({ sessionId: 'new-chat' });
      await host.dispose();
    });
  }

  it('forwards notifications and answers explicit permission callbacks without ambient authority', async () => {
    const notifications: Record<string, unknown>[] = [];
    const process = new FakeProcess();
    const host = new AcpProcessHost({
      process: process.asProcess(),
      callbacks: {
        onNotification: (notification) => notifications.push(notification),
        requestPermission: async (params) => ({ outcome: 'selected', optionId: 'allow-once', params }),
      },
    });
    await host.connect();

    process.stdout.emit('data', `${JSON.stringify({
      jsonrpc: '2.0',
      method: 'session/update',
      params: { sessionId: 'omp-owned-session-opaque', update: { kind: 'agent_message_chunk' } },
    })}\n`);
    process.stdout.emit('data', `${JSON.stringify({
      jsonrpc: '2.0',
      id: 77,
      method: 'session/request_permission',
      params: { sessionId: 'omp-owned-session-opaque', options: [{ optionId: 'allow-once' }] },
    })}\n`);
    await flush();

    expect(notifications).toEqual([expect.objectContaining({ method: 'session/update' })]);
    expect(lastRequest(process)).toMatchObject({
      jsonrpc: '2.0',
      id: 77,
      result: { outcome: 'selected', optionId: 'allow-once' },
    });
    await host.dispose();
  });

  it('fails closed when a required filesystem callback is absent', async () => {
    const process = new FakeProcess();
    const host = new AcpProcessHost({ process: process.asProcess() });
    await host.connect();
    process.stdout.emit('data', `${JSON.stringify({
      jsonrpc: '2.0',
      id: 9,
      method: 'fs/read_text_file',
      params: { path: '/tmp/no-read-authority' },
    })}\n`);
    await flush();
    expect(lastRequest(process)).toMatchObject({
      id: 9,
      error: { code: -32001, message: expect.stringContaining('unavailable') },
    });
    await host.dispose();
  });

  it('rejects pending calls when the owned process disconnects and builds the explicit omp acp launch', async () => {
    const process = new FakeProcess();
    let spawnOptions: AcpProcessSpawnOptions | undefined;
    const host = new AcpProcessHost({
      command: '/Users/hannahomalley/.bun/bin/omp',
      cwd: '/tmp/astrid-project',
      profile: 'astrid-profile',
      sessionDir: '/tmp/astrid-sessions',
      spawnProcess: (options) => {
        spawnOptions = options;
        return process.asProcess();
      },
      requestTimeoutMs: 1_000,
    });
    const pending = host.listSessions();
    await flush();
    expect(spawnOptions).toEqual({
      command: '/Users/hannahomalley/.bun/bin/omp',
      args: ['acp', '--profile', 'astrid-profile', '--session-dir', '/tmp/astrid-sessions'],
      cwd: '/tmp/astrid-project',
      env: undefined,
    });
    process.emit('exit', 1, null);
    await expect(pending).rejects.toMatchObject({ code: 'acp_process_exited' });
    await host.dispose();
  });

  it('builds the real Astrid launcher command with identity, cwd, and session custody', async () => {
    const process = new FakeProcess();
    let spawnOptions: AcpProcessSpawnOptions | undefined;
    const host = createAstridAcpProcessHost({
      cwd: '/tmp/astrid-project',
      profile: 'astrid',
      sessionDir: '/tmp/astrid-sessions',
      systemPromptFile: '/tmp/astrid.md',
      profileFileForCommand:()=>'/tmp/astrid.md',
      fileIsRegularFile: () => true,
      spawnProcess: (options) => {
        spawnOptions = options;
        return process.asProcess();
      },
      requestTimeoutMs: 1_000,
    });

    const pending = host.listSessions();
    await flush();
    expect(spawnOptions).toEqual({
      command: ASTRID_ACP_COMMAND,
      args: [
        '--mode=acp',
        '--profile', 'astrid',
        '--session-dir', '/tmp/astrid-sessions',
      ],
      cwd: '/tmp/astrid-project',
      env: {
        ASTRID_HOST_EXECUTION_PROFILE: 'installed',
        OMP_AGENT_IDENTITY: ASTRID_ACP_AGENT_IDENTITY,
        ASTRID_AGENT_IDENTITY: ASTRID_ACP_AGENT_IDENTITY,
        ASTRID_SYSTEM_PROMPT: ASTRID_ACP_CONTEXT_SYSTEM_PROMPT,
      },
    });
    const request = lastRequest(process);
    respond(process, request.id, { sessions: [] });
    await expect(pending).resolves.toEqual({ sessions: [] });
    await host.dispose();
  });

  it('resolves the packaged Astrid profile through the installed command without a user-home seed', async () => {
    const process = new FakeProcess();
    let lookedUp: { command: string; env?: Record<string, string | undefined> } | undefined;
    let checkedPath = '';
    const host = createAstridAcpProcessHost({
      cwd: '/tmp/clean-project',
      profile: 'astrid',
      profileFileForCommand: (command, env) => {
        lookedUp = { command, env };
        return '/opt/astrid/astrid/agent_profiles/astrid.md';
      },
      fileIsRegularFile: (path) => {
        checkedPath = path;
        return true;
      },
      spawnProcess: () => process.asProcess(),
      requestTimeoutMs: 1_000,
    });

    const pending = host.listSessions();
    await flush();
    expect(lookedUp?.command).toBe(ASTRID_ACP_COMMAND);
    expect(lookedUp?.env?.ASTRID_AGENT_IDENTITY).toBeUndefined();
    expect(checkedPath).toBe('/opt/astrid/astrid/agent_profiles/astrid.md');
    const request = lastRequest(process);
    respond(process, request.id, { sessions: [] });
    await expect(pending).resolves.toEqual({ sessions: [] });
    await host.dispose();
  });

  it('omits the session override so OMP owns canonical cwd-derived storage', async () => {
    const process = new FakeProcess();
    let spawnOptions: AcpProcessSpawnOptions | undefined;
    const host = createAstridAcpProcessHost({
      cwd: '/tmp/astrid-canonical-project',
      profile: 'astrid',
      systemPromptFile: '/tmp/astrid.md',
      profileFileForCommand:()=>'/tmp/astrid.md',
      fileIsRegularFile: () => true,
      spawnProcess: (options) => {
        spawnOptions = options;
        return process.asProcess();
      },
      requestTimeoutMs: 1_000,
    });

    const pending = host.listSessions();
    await flush();
    expect(spawnOptions).toEqual({
      command: ASTRID_ACP_COMMAND,
      args: ['--mode=acp', '--profile', 'astrid'],
      cwd: '/tmp/astrid-canonical-project',
      env: {
        ASTRID_HOST_EXECUTION_PROFILE: 'installed',
        OMP_AGENT_IDENTITY: ASTRID_ACP_AGENT_IDENTITY,
        ASTRID_AGENT_IDENTITY: ASTRID_ACP_AGENT_IDENTITY,
        ASTRID_SYSTEM_PROMPT: ASTRID_ACP_CONTEXT_SYSTEM_PROMPT,
      },
    });
    const request = lastRequest(process);
    respond(process, request.id, { sessions: [] });
    await expect(pending).resolves.toEqual({ sessions: [] });
    await host.dispose();
  });

  it('pins the hosted installed package profile despite ambient named mode while preserving appended instructions and OMP custody',async()=>{
    const process=new FakeProcess();let spawned:AcpProcessSpawnOptions|undefined;
    const host=createAstridAcpProcessHost({cwd:'/tmp/project-with-existing-user-profile',profile:'user-omp-profile',sessionDir:'/tmp/preserved-sessions',
      env:{ASTRID_HOST_EXECUTION_PROFILE:'named',ASTRID_SYSTEM_PROMPT:'Preserved custom appended instructions',ASTRID_SYSTEM_PROMPT_FILE:'/tmp/user-extra.md'},
      profileFileForCommand:()=>'/selected/astrid/agent_profiles/astrid.md',fileIsRegularFile:()=>true,spawnProcess:options=>{spawned=options;return process.asProcess();}});
    const pending=host.listSessions();await flush();
    expect(spawned?.env).toMatchObject({ASTRID_HOST_EXECUTION_PROFILE:'installed',ASTRID_SYSTEM_PROMPT:'Preserved custom appended instructions',ASTRID_SYSTEM_PROMPT_FILE:'/tmp/user-extra.md'});
    expect(spawned?.args).toEqual(['--mode=acp','--profile','user-omp-profile','--session-dir','/tmp/preserved-sessions']);
    const request=lastRequest(process);respond(process,request.id,{sessions:[]});await pending;await host.dispose();
  });
  it('rejects a validation-only custom base override rather than pretending to execute it',()=>{
    expect(()=>createAstridAcpProcessHost({cwd:'/tmp/project',systemPromptFile:'/custom/base.md',profileFileForCommand:()=>'/installed/astrid.md',fileIsRegularFile:()=>true})).toThrow('Custom base-profile overrides are unsupported');
  });

  it('fails closed before spawn when the canonical prompt file is unavailable', () => {
    let spawned = false;
    expect(() => createAstridAcpProcessHost({
      cwd: '/tmp/astrid-project',
      profile: 'astrid',
      sessionDir: '/tmp/astrid-sessions',
      systemPromptFile: '/tmp/missing-astrid.md',
      profileFileForCommand:()=>'/tmp/missing-astrid.md',
      fileIsRegularFile: () => false,
      spawnProcess: () => {
        spawned = true;
        return new FakeProcess().asProcess();
      },
    })).toThrow('system prompt is unavailable');
    expect(spawned).toBe(false);
  });
});
