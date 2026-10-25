import { randomUUID } from 'node:crypto';
import { ApiError } from './runtime-client.ts';
import { ChatError, ProjectChatRegistry, chatClaimIdentity, chatStoreIdentity, projectChatClaimDirectory, runtimeChatClient, type QueuedChatMessage } from './astrid-project-chat.ts';
import { spawn, type ChildProcessWithoutNullStreams } from 'node:child_process';
import { createServer, type IncomingMessage, type ServerResponse } from 'node:http';
import { fileURLToPath } from 'node:url';
import { delimiter } from 'node:path';
import { isAbsolutePath, isPathWithin } from './paths.ts';
import { getAstridAgentModelCatalog } from './astrid-agent-models.ts';
import { setAstridAgentKey } from './astrid-agent-key.ts';
import { AgentModelState } from './agent-model-state.ts';
import { acpClientCapabilities } from './acp-client-capabilities.ts';
import { discoverLocalAgents } from './astrid-local-agents.ts';
import { LocalAgentSkills } from './astrid-local-agent-skills.ts';
import { createLocalAgentAcpProcessHost } from './astrid-local-agent-host.ts';
import { SharedAcpSessions, sharedSessionModels } from './astrid-shared-acp-sessions.ts';
import { LOCAL_AGENT_IDS, LOCAL_AGENT_TABLE, localAgentSteps, localAgentIdSchema, sessionAgentId, type LocalAgentId, type LocalAgentModels } from './localAgents.ts';

import {
  AcpProcessHost,
  AcpProcessHostError,
  type AcpProcessHostCallbacks,
} from './acpProcessHost.ts';
import {
  ASTRID_ACP_CONTEXT_SYSTEM_PROMPT,
  createAstridAcpProcessHost,
  type AstridAcpLauncherOptions,
} from './astridAcpLauncher.ts';
import {
  ASTRID_BRIDGE_PROTOCOL_HEADER,
  ASTRID_BRIDGE_PROTOCOL_VERSION,
} from './astridBridgeWire.ts';

const DEFAULT_PORT = 0;
const MAX_BODY_BYTES = 128 * 1024;
const MAX_NOTIFICATION_BYTES = 1024 * 1024;
const CLIENT_VERSION = 'astrid-r1-consumer-20260911';
const REQUEST_METHODS = new Set([
  'authenticate',
  'session/new',
  'session/load',
  'session/list',
  'session/resume',
  'session/fork',
  'session/close',
  'session/prompt',
  'session/set_config_option',
  'session/takeover',
]);

type JsonRecord = Record<string, unknown>;

class LocalMediaOnlyError extends Error {}

export type AstridAcpBridgeConfig = {
  readonly token: string;
  readonly port: number;
  readonly cwd: string;
  readonly profile?: string;
  /** Optional OMP override; omission uses OMP's cwd-derived canonical store. */
  readonly sessionDir?: string;
  readonly systemPromptFile?: string;
  readonly command?: string;
  /** Hosted same-machine mode refuses inline prompt attachments. */
  readonly localMediaOnly?: boolean;
  /** Service-owned preferences and excluded internal native sessions. */
  readonly modelStateFile?: string;
};

type Connection = {
  readonly id: string;
  readonly agent: LocalAgentId;
  readonly host: AcpProcessHost;
  readonly notifications: unknown[];
  readonly terminals: Map<string, TerminalRecord>;
  readonly sessions: Set<string>;
  readonly configOptions: Map<string, Map<string, Set<string>>>;
  readonly models: Map<string, LocalAgentModels>;
  defaultModelId?: string;
  disconnected: boolean;
  closed: boolean;
  notificationBytes: number;
  readonly localMediaOnly: boolean;
  lastSeenAt: number;
  inFlight: number;
};


type TerminalExitStatus = {
  readonly exitCode: number | null;
  readonly signal: string | null;
};

type TerminalRecord = {
  readonly process: ChildProcessWithoutNullStreams;
  readonly outputByteLimit: number;
  exit: Promise<TerminalExitStatus>;
  output: string;
  outputBytes: number;
  truncated: boolean;
  exited: TerminalExitStatus | null;
};

type BridgeOptions = {
  readonly chatRegistry?: ProjectChatRegistry;
  readonly config: AstridAcpBridgeConfig;
  readonly hostFactory?: (options: AstridAcpLauncherOptions) => AcpProcessHost;
  readonly localAgentHostFactory?: typeof createLocalAgentAcpProcessHost;
  readonly discoverAgents?: typeof discoverLocalAgents;
  readonly localAgentSkills?: LocalAgentSkills;
  readonly astridModelCatalog?: typeof getAstridAgentModelCatalog;
  readonly idFactory?: () => string;
  readonly now?: () => number;
};

// Browser event polls renew the tab's lease, including in background tabs.
// Allow a minute of background timer throttling plus a missed poll. A running
// RPC is server activity, not proof that its browser is still alive.
const CONNECTION_IDLE_MS = 90_000;
const REAP_SWEEP_MS = 15_000;
const MAX_REAPED_IDS = 256;
const DISCOVERY_REQUEST_TIMEOUT_MS = 5_000;

function asRecord(value: unknown): JsonRecord | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  return value as JsonRecord;
}

function parsePort(value: string | undefined): number {
  const candidate = value ?? String(DEFAULT_PORT);
  if (!/^\d{1,5}$/.test(candidate)) throw new Error('ASTRID_ACP_BRIDGE_PORT must be an integer from 0 to 65535');
  const port = Number(candidate);
  if (!Number.isSafeInteger(port) || port < 0 || port > 65_535) {
    throw new Error('ASTRID_ACP_BRIDGE_PORT must be an integer from 0 to 65535');
  }
  return port;
}

function requiredAbsolute(env: Readonly<Record<string, string | undefined>>, key: string): string {
  const value = env[key]?.trim();
  if (!value || !isAbsolutePath(value)) throw new Error(`${key} must be an explicit absolute path`);
  return value;
}

function isRecord(value: unknown): value is JsonRecord {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

/** Reject inline binary media while retaining text and scoped resource metadata. */
export function assertLocalMediaOnly(value: unknown): void {
  if (Array.isArray(value)) {
    for (const item of value) assertLocalMediaOnly(item);
    return;
  }
  const record = asRecord(value);
  if (!record) return;
  const inlineUri = typeof record.uri === 'string' && /^(data|blob):/i.test(record.uri);
  if (['image', 'audio', 'video'].includes(String(record.type))
      || typeof record.blob === 'string' || inlineUri) {
    throw new LocalMediaOnlyError('Hosted local-only ACP accepts text and scoped resource metadata, not inline image, audio, video or binary resources. Import the media into the local Runtime and send its resource ID instead.');
  }
  for (const child of Object.values(record)) assertLocalMediaOnly(child);
}

function advertisedConfigOptions(value: unknown): Map<string, Set<string>> {
  const record = asRecord(value);
  if (!Array.isArray(record?.configOptions)) return new Map();
  const result = new Map<string, Set<string>>();
  for (const candidate of record.configOptions) {
    if (!isRecord(candidate) || typeof candidate.id !== 'string' || !Array.isArray(candidate.options)) continue;
    const values = new Set<string>();
    for (const option of candidate.options) {
      if (isRecord(option) && typeof option.value === 'string') values.add(option.value);
      else if (isRecord(option) && Array.isArray(option.options)) {
        for (const nested of option.options) if (isRecord(nested) && typeof nested.value === 'string') values.add(nested.value);
      }
    }
    if (values.size > 0) result.set(candidate.id, values);
  }
  return result;
}

function sessionIdFrom(value: unknown): string | null {
  const record = asRecord(value);
  const sessionId = record?.sessionId ?? record?.session_id ?? record?.id;
  return typeof sessionId === 'string' && sessionId ? sessionId : null;
}

function isMissingPersistedSession(error: unknown, sessionId: string): boolean {
  if (!(error instanceof AcpProcessHostError) || error.code !== 'acp_transport') return false;
  const prefix = 'ACP request "session/resume" failed: ';
  if (!error.message.startsWith(prefix)) return false;
  const detail = error.message.slice(prefix.length).trim().toLowerCase();
  return detail === `acp session not found: ${sessionId}`.toLowerCase()
    || /\bresource not found\b/.test(detail)
    || /\bno rollout found\b/.test(detail);
}

function terminalCwd(value: unknown, root: string): string {
  if (typeof value !== 'string' || !isAbsolutePath(value)) return root;
  return isPathWithin(root, value) ? value : root;
}

function terminalEnvironment(value: unknown): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = { ...process.env };
  if (!Array.isArray(value)) return env;
  for (const item of value) {
    if (!isRecord(item) || typeof item.name !== 'string' || typeof item.value !== 'string') continue;
    env[item.name] = item.value;
  }
  return env;
}

function terminalOutputLimit(value: unknown): number {
  if (typeof value !== 'number' || !Number.isFinite(value)) return 1_048_576;
  return Math.max(1, Math.min(Math.floor(value), 8 * 1_048_576));
}

function appendTerminalOutput(terminal: TerminalRecord, chunk: Buffer | string): void {
  const text = Buffer.isBuffer(chunk) ? chunk.toString('utf8') : chunk;
  const remaining = terminal.outputByteLimit - terminal.outputBytes;
  if (remaining <= 0) {
    terminal.truncated = true;
    return;
  }
  const bytes = Buffer.byteLength(text);
  if (bytes <= remaining) {
    terminal.output += text;
    terminal.outputBytes += bytes;
    return;
  }
  terminal.output += Buffer.from(text).subarray(0, remaining).toString('utf8');
  terminal.outputBytes = terminal.outputByteLimit;
  terminal.truncated = true;
}

function createTerminal(
  params: unknown,
  root: string,
  terminals: Map<string, TerminalRecord>,
): { terminalId: string } {
  if (!isRecord(params) || typeof params.command !== 'string' || !params.command) {
    throw new Error('terminal/create requires a command');
  }
  const args = Array.isArray(params.args) ? params.args.filter((value): value is string => typeof value === 'string') : [];
  const child = spawn(params.command, args, {
    cwd: terminalCwd(params.cwd, root),
    env: terminalEnvironment(params.env),
    shell: false,
    stdio: 'pipe',
  });
  const terminal: TerminalRecord = {
    process: child,
    outputByteLimit: terminalOutputLimit(params.outputByteLimit),
    exit: Promise.resolve({ exitCode: null, signal: null }),
    output: '',
    outputBytes: 0,
    truncated: false,
    exited: null,
  };
  terminal.exit = new Promise<TerminalExitStatus>((resolve) => {
    let settled = false;
    const finish = (status: TerminalExitStatus): void => {
      if (settled) return;
      settled = true;
      terminal.exited = status;
      resolve(status);
    };
    child.once('exit', (code, signal) => finish({
      exitCode: typeof code === 'number' ? code : null,
      signal: signal ?? null,
    }));
    child.once('error', () => finish({ exitCode: null, signal: null }));
  });
  child.stdout.on('data', (chunk) => appendTerminalOutput(terminal, chunk));
  child.stderr.on('data', (chunk) => appendTerminalOutput(terminal, chunk));
  const terminalId = randomUUID();
  terminals.set(terminalId, terminal);
  return { terminalId };
}

function getTerminal(params: unknown, terminals: Map<string, TerminalRecord>): TerminalRecord {
  const terminalId = isRecord(params) && typeof params.terminalId === 'string' ? params.terminalId : '';
  const terminal = terminals.get(terminalId);
  if (!terminal) throw new Error('terminal is not available');
  return terminal;
}

function closeTerminals(terminals: Map<string, TerminalRecord>): void {
  for (const terminal of terminals.values()) {
    if (!terminal.exited) terminal.process.kill('SIGTERM');
  }
  terminals.clear();
}

export function resolveAstridAcpBridgeConfig(
  env: Readonly<Record<string, string | undefined>> = process.env,
): AstridAcpBridgeConfig {
  const token = env.ASTRID_ACP_BRIDGE_TOKEN?.trim() || env.ASTRID_BRIDGE_TOKEN?.trim();
  if (!token) throw new Error('ASTRID_ACP_BRIDGE_TOKEN (or legacy ASTRID_BRIDGE_TOKEN) is required');
  const policy = env.ASTRID_ACP_LOCAL_MEDIA_ONLY?.trim();
  if (policy && policy !== '0' && policy !== '1') throw new Error('ASTRID_ACP_LOCAL_MEDIA_ONLY must be 0 or 1');
  const profile = env.ASTRID_ACP_PROFILE?.trim() || undefined;
  return {
    token,
    localMediaOnly: policy === '1',
    port: parsePort(env.ASTRID_ACP_BRIDGE_PORT),
    cwd: requiredAbsolute(env, 'ASTRID_ACP_CWD'),
    profile,
    ...(env.ASTRID_ACP_SESSION_DIR?.trim()
      ? { sessionDir: requiredAbsolute(env, 'ASTRID_ACP_SESSION_DIR') }
      : {}),
    ...(env.ASTRID_ACP_SYSTEM_PROMPT_FILE?.trim()
      ? { systemPromptFile: env.ASTRID_ACP_SYSTEM_PROMPT_FILE.trim() }
      : {}),
    ...(env.ASTRID_ACP_COMMAND?.trim() ? { command: env.ASTRID_ACP_COMMAND.trim() } : {}),
  };
}

function jsonResponse(response: ServerResponse, status: number, value: unknown): void {
  const body = JSON.stringify(value);
  response.statusCode = status;
  response.setHeader('Content-Type', 'application/json');
  response.setHeader('Cache-Control', 'no-store');
  response.setHeader(ASTRID_BRIDGE_PROTOCOL_HEADER, ASTRID_BRIDGE_PROTOCOL_VERSION);
  response.setHeader('Content-Length', Buffer.byteLength(body));
  response.end(body);
}

async function readJson(request: IncomingMessage): Promise<unknown> {
  const chunks: Buffer[] = [];
  let total = 0;
  for await (const chunk of request) {
    const value = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    total += value.byteLength;
    if (total > MAX_BODY_BYTES) throw new Error('request body exceeds 128 KiB');
    chunks.push(value);
  }
  if (total === 0) return {};
  try {
    return JSON.parse(Buffer.concat(chunks).toString('utf8'));
  } catch {
    throw new Error('request body is not valid JSON');
  }
}

function hostError(error: unknown): { status: number; body: JsonRecord } {
  if (error instanceof ChatError || error instanceof ApiError) {
    return { status: error.status, body: { error: error.code, detail: error.message } };
  }
  if (error instanceof AcpProcessHostError) {
    return { status: 502, body: { error: error.code, detail: error.message, ...(error.next_action ? { next_action: error.next_action } : {}) } };
  }
  return {
    status: 500,
    body: { error: 'acp_bridge_error', detail: error instanceof Error ? error.message : String(error) },
  };
}

export class AstridAcpBridge {
  private readonly config: AstridAcpBridgeConfig;
  private readonly hostFactory: (options: AstridAcpLauncherOptions) => AcpProcessHost;
  private readonly localAgentHostFactory: typeof createLocalAgentAcpProcessHost;
  private readonly idFactory: () => string;
  private readonly connections = new Map<string, Connection>();
  private readonly temporaryConnections = new Set<Connection>();
  private closing = false;
  private readonly sharedSessions = new SharedAcpSessions(
    (client, notification) => this.enqueueNotification(client as Connection, notification),
    (client, sessionId) => {
      const connection = client as Connection;
      for (let index = connection.notifications.length - 1; index >= 0; index--) {
        if (asRecord(asRecord(connection.notifications[index])?.params)?.sessionId === sessionId) connection.notifications.splice(index, 1);
      }
      connection.notificationBytes = connection.notifications.reduce<number>((total, item) => total + Buffer.byteLength(JSON.stringify(item)), 0);
    },
  );
  private readonly chatRegistry?: ProjectChatRegistry;
  private readonly now: () => number;
  // Ids we reaped, so a late poll gets the disconnected answer, not a bare 404.
  private readonly reaped = new Set<string>();
  private readonly modelState: AgentModelState;
  private readonly bootstraps = new Map<LocalAgentId, Promise<void>>();
  private readonly bootstrapped = new Set<LocalAgentId>();
  private readonly discoverAgents: typeof discoverLocalAgents;
  private readonly localAgentSkills: LocalAgentSkills;
  private readonly astridModelCatalog: typeof getAstridAgentModelCatalog;
  private readonly sweeper: ReturnType<typeof setInterval>;

  constructor(options: BridgeOptions) {
    this.config = options.config;
    this.localAgentSkills = options.localAgentSkills ?? new LocalAgentSkills(process.env, this.config.cwd);
    this.modelState = new AgentModelState(this.config.modelStateFile);
    this.discoverAgents = options.discoverAgents ?? discoverLocalAgents;
    this.astridModelCatalog = options.astridModelCatalog ?? getAstridAgentModelCatalog;
    this.hostFactory = options.hostFactory ?? createAstridAcpProcessHost;
    this.localAgentHostFactory = options.localAgentHostFactory ?? createLocalAgentAcpProcessHost;
    this.idFactory = options.idFactory ?? randomUUID;
    this.now = options.now ?? Date.now;
    this.sweeper = setInterval(() => { void this.reapIdle(); }, REAP_SWEEP_MS);
    this.sweeper.unref();
    const runtime = options.chatRegistry ? undefined : runtimeChatClient();
    this.chatRegistry = options.chatRegistry ?? (runtime ? new ProjectChatRegistry(runtime, chatStoreIdentity(this.config), Date.now, projectChatClaimDirectory(this.config), chatClaimIdentity(this.config)) : undefined);
  }

  async handle(request: IncomingMessage, response: ServerResponse): Promise<void> {
    if (!this.isAuthorized(request)) {
      jsonResponse(response, this.config.token ? 401 : 503, {
        error: this.config.token ? 'unauthorized' : 'acp_auth_not_configured',
        detail: this.config.token ? 'A valid Astrid bridge bearer credential is required' : 'ASTRID_BRIDGE_TOKEN is required',
      });
      return;
    }
    if (request.headers[ASTRID_BRIDGE_PROTOCOL_HEADER.toLowerCase()] !== ASTRID_BRIDGE_PROTOCOL_VERSION) {
      jsonResponse(response, 400, {
        error: 'invalid_protocol',
        detail: `Expected ${ASTRID_BRIDGE_PROTOCOL_HEADER}: ${ASTRID_BRIDGE_PROTOCOL_VERSION}`,
      });
      return;
    }

    const url = new URL(request.url ?? '/', 'http://127.0.0.1');
    const held: Array<() => void> = [];
    try {
      if (request.method === 'GET' && url.pathname === '/health') {
        jsonResponse(response, 200, { status: 'ok', bridge: 'astrid-acp', protocol: ASTRID_BRIDGE_PROTOCOL_VERSION });
        return;
      }
      if (request.method === 'GET' && url.pathname === '/agents') {
        const [agents, astridModels] = await Promise.all([
          this.discoverAgents({ cwd: this.config.cwd }),
          this.astridModelCatalog({ command: this.config.command, cwd: this.config.cwd, profile: this.config.profile }).catch(() => null),
        ]);
        // A provider with available models can be used without a configured key.
        // A failed catalog lookup is unknown, and unknown sign-in counts as ready.
        const astridReady = astridModels?.providers.some(provider => provider.signedIn || provider.models.length > 0) ?? true;
        const choices = agents.map(agent => agent.id === 'astrid'
          ? { ...agent, state: astridReady ? agent.state : 'sign-in' as const,
            line: astridReady ? agent.line : LOCAL_AGENT_TABLE.astrid.signInLine,
            signIn: astridModels === null ? 'unknown' as const : astridReady ? 'signed-in' as const : 'signed-out' as const,
            steps: localAgentSteps('astrid', astridReady ? 'ready' : 'sign-in') }
          : agent);
        await Promise.all(choices.map(async agent => {
          if (agent.state === 'ready') await this.ensureAgentModels(agent.id, this.localMediaOnly(request));
        }));
        jsonResponse(response, 200, {
          defaultAgent: this.modelState.defaultAgent,
          agents: choices.map(agent => ({ ...agent,
            ...(agent.id === 'astrid' ? { signInLine: LOCAL_AGENT_TABLE.astrid.signInLine, creditLine: 'Built on oh-my-pi', creditUrl: 'https://github.com/can1357/oh-my-pi' } : {}),
            terminal: this.localAgentSkills.terminal(agent.id),
            models: this.modelState.catalogs.get(agent.id)?.availableModels ?? [],
            defaultModel: this.modelState.defaults.get(agent.id) ?? null,
          })),
        });
        return;
      }
      if (request.method === 'GET' && url.pathname === '/models') {
        const catalog = await this.astridModelCatalog({ command: this.config.command, cwd: this.config.cwd, profile: this.config.profile });
        jsonResponse(response, 200, catalog);
        return;
      }
      if (request.method === 'POST' && url.pathname === '/astrid/api-key') {
        const body = asRecord(await readJson(request));
        if (body?.provider !== 'openrouter' || typeof body.key !== 'string'
          || body.key.length < 20 || body.key.length > 512 || /\s/u.test(body.key)) {
          throw new ChatError(400, 'invalid_key', 'Enter an OpenRouter key with 20 to 512 characters and no whitespace.');
        }
        const result = await setAstridAgentKey({ command: this.config.command, cwd: this.config.cwd, profile: this.config.profile }, body.key);
        this.modelState.catalogs.delete('astrid');
        this.bootstrapped.delete('astrid');
        if (result.currentModel !== null) this.modelState.remember('astrid', result.currentModel);
        jsonResponse(response, 200, result);
        return;
      }
      if (request.method === 'POST' && url.pathname === '/model') {
        throw new ChatError(404, 'not_found', 'To use another model, start a new chat.');
      }
      if (request.method === 'POST' && url.pathname === '/connect') {
        const body = asRecord(await readJson(request));
        const agent = localAgentIdSchema.safeParse(body?.agent ?? this.modelState.defaultAgent);
        const defaultModelId = body?.defaultModelId;
        if (!agent.success || !body || Object.keys(body).some(key => key !== 'agent' && key !== 'defaultModelId') || (defaultModelId !== undefined && (typeof defaultModelId !== 'string' || !defaultModelId))) {
          jsonResponse(response, 400, { error: 'invalid_body', detail: `Choose ${LOCAL_AGENT_IDS.map(id => LOCAL_AGENT_TABLE[id].name).join(', ')}.` });
          return;
        }
        const { connection, initialize } = await this.createConnection(this.localMediaOnly(request), agent.data, defaultModelId as string | undefined);
        jsonResponse(response, 200, { connection_id: connection.id, initialize });
        return;
      }

      const projectChat = url.pathname.match(/^\/projects\/([^/]+)\/chat(?:\/(sessions|unassigned|associate|draft|prompt|main))?$/);
      if (projectChat) {
        await this.projectChat(decodeURIComponent(projectChat[1]), projectChat[2], request, response, this.localMediaOnly(request), held);
        return;
      }

      const match = url.pathname.match(/^\/([^/]+)(?:\/(rpc|cancel|events|reconnect|models|model))?$/);
      if (!match) {
        jsonResponse(response, 404, { error: 'not_found', detail: 'Unknown Astrid App ACP route' });
        return;
      }
      const connection = this.connections.get(match[1]);
      if (!connection || connection.closed) {
        if (this.reaped.has(match[1])) {
          if (match[2] === 'events' && request.method === 'GET') {
            jsonResponse(response, 200, { notifications: [], disconnected: true });
          } else {
            jsonResponse(response, 404, { error: 'connection_not_found', detail: 'ACP connection expired while idle; reconnect' });
          }
          return;
        }
        jsonResponse(response, 404, { error: 'not_found', detail: 'ACP connection is not available; reconnect' });
        return;
      }
      held.push(this.use(connection));
      if (this.localMediaOnly(request) && !connection.localMediaOnly) {
        jsonResponse(response, 409, { error: 'hosted_connection_required', detail: 'Create a new hosted ACP connection to enable local media protections in the installed agent' });
        return;
      }
      if (match[2] === 'models' && request.method === 'GET') {
        const sessionId = url.searchParams.get('sessionId');
        if (!sessionId || !connection.sessions.has(sessionId)) throw new ChatError(400, 'invalid_session', 'sessionId must belong to this ACP connection');
        const snapshot = this.sharedSessions.snapshot(connection, sessionId);
        this.rememberSession(connection, 'session/load', { sessionId }, snapshot);
        jsonResponse(response, 200, { models: sharedSessionModels(snapshot) });
        return;
      }
      if (match[2] === 'model' && request.method === 'POST') {
        throw new ChatError(404, 'not_found', 'To use another model, start a new chat.');
      }
      if (match[2] === 'events' && request.method === 'GET') {
        const notifications = connection.notifications.splice(0);
        connection.notificationBytes = 0;
        jsonResponse(response, 200, { notifications, disconnected: connection.disconnected });
        return;
      }
      if (match[2] === 'rpc' && request.method === 'POST') {
        await this.rpc(connection, request, response, connection.localMediaOnly || this.localMediaOnly(request));
        return;
      }
      if (match[2] === 'cancel' && request.method === 'POST') {
        await this.cancel(connection, request, response);
        return;
      }
      if (match[2] === 'reconnect' && request.method === 'POST') {
        jsonResponse(response, connection.disconnected ? 502 : 200, connection.disconnected
          ? { error: 'acp_process_exited', detail: 'The live ACP process exited; reconnect requires a new host process' }
          : { connection_id: match[1], reused: true });
        return;
      }
      if (!match[2] && (request.method === 'DELETE' || request.method === 'POST')) {
        await this.releaseConnection(match[1], connection);
        jsonResponse(response, 200, { connection_id: match[1], closed: true });
        return;
      }
      jsonResponse(response, 404, { error: 'not_found', detail: 'Unknown Astrid App ACP connection route' });
    } catch (error) {
      const result = error instanceof LocalMediaOnlyError
        ? { status: 415, body: { error: 'local_media_required', detail: error.message } }
        : error instanceof Error && error.message.includes('128 KiB')
        ? { status: 413, body: { error: 'payload_too_large', detail: error.message } }
      : error instanceof Error && (error.message.includes('request body') || error.message.includes('method') || error.message.includes('sessionId') || error.message.includes('config option'))
          ? { status: 400, body: { error: 'invalid_body', detail: error.message } }
          : hostError(error);
      jsonResponse(response, result.status, result.body);
    } finally {
      for (const release of held) release();
      await this.disposeUnusedHosts();
    }
  }

  /** Renew the browser lease on arrival; retain its host until the request finishes. */
  private use(connection: Connection): () => void {
    connection.inFlight++;
    connection.lastSeenAt = this.now();
    return () => { connection.inFlight--; };
  }

  /** Release abandoned writers even during a turn; retain any host still in use. */
  async reapIdle(): Promise<void> {
    const cutoff = this.now() - CONNECTION_IDLE_MS;
    const abandoned = [...this.connections].filter(([, c]) => !c.closed && c.lastSeenAt <= cutoff);
    await Promise.all(abandoned.map(([id, connection]) => this.releaseConnection(id, connection)));
    await this.disposeUnusedHosts();
  }

  private async releaseConnection(id: string, connection: Connection): Promise<void> {
    connection.closed = true;
    this.sharedSessions.detach(connection);
    this.reaped.add(id);
    if (this.reaped.size > MAX_REAPED_IDS) this.reaped.delete(this.reaped.values().next().value as string);
    await this.disposeUnusedHosts();
  }

  private async disposeUnusedHosts(): Promise<void> {
    for (const [id, connection] of this.connections) {
      if (!connection.closed || connection.inFlight > 0 || this.sharedSessions.retainsHost(connection)) continue;
      this.connections.delete(id);
      this.sharedSessions.forgetHost(connection);
      closeTerminals(connection.terminals);
      await connection.host.dispose();
    }
  }

  async close(): Promise<void> {
    this.closing = true;
    clearInterval(this.sweeper);
    this.sharedSessions.close();
    await Promise.all([...this.connections.values(), ...this.temporaryConnections].map(async connection => {
      await connection.host.dispose(this.temporaryConnections.has(connection) ? 'SIGKILL' : 'SIGTERM');
      closeTerminals(connection.terminals);
    }));
    this.connections.clear();
    this.temporaryConnections.clear();
  }

  private isAuthorized(request: IncomingMessage): boolean {
    return Boolean(this.config.token) && request.headers.authorization === `Bearer ${this.config.token}`;
  }

  private localMediaOnly(request: IncomingMessage): boolean {
    return this.config.localMediaOnly === true || request.headers['x-astrid-local-media-only'] === '1';
  }

  private enqueueNotification(connection: Connection, notification: unknown): void {
    if (connection.closed || connection.disconnected) return;
    const bytes = Buffer.byteLength(JSON.stringify(notification));
    if (connection.notificationBytes + bytes > MAX_NOTIFICATION_BYTES) {
      connection.disconnected = true;
      connection.notifications.splice(0, connection.notifications.length, {
        jsonrpc: '2.0', method: 'astrid/acp/disconnected',
        params: { code: 'acp_event_overflow', detail: 'ACP event queue exceeded 1 MiB; reconnect and resume the installed session' },
      });
      connection.notificationBytes = 0;
      this.sharedSessions.detach(connection);
      // A slow follower must not terminate another tab's active agent.
      if (!this.sharedSessions.retainsHost(connection)) {
        closeTerminals(connection.terminals);
        void connection.host.dispose();
      }
      return;
    }
    connection.notificationBytes += bytes;
    connection.notifications.push(notification);
    if (asRecord(notification)?.method === 'astrid/acp/disconnected') connection.disconnected = true;
  }

  private async createConnection(localMediaOnly: boolean, agent: LocalAgentId, defaultModelId = this.modelState.defaults.get(agent), temporary = false): Promise<{ connection: Connection; initialize: unknown }> {
    if (this.closing) throw new ChatError(503, 'chat_stopping', 'The chat service is stopping.');
    const connectionId = temporary ? randomUUID() : this.idFactory();
    const notifications: unknown[] = [];
    const terminals = new Map<string, TerminalRecord>();
    let connection: Connection | null = null;
    const callbacks: AcpProcessHostCallbacks = {
      requestPermission: (params) => {
        // Astrid App's local editor is the user-facing ACP client. It has no separate
        // permission modal, so approve normal agent work once for the session;
        // keep destructive delete/move requests fail-closed until the UI grows a
        // real approval surface.
        const toolCall = isRecord(params) && isRecord(params.toolCall) ? params.toolCall : null;
        const title = typeof toolCall?.title === 'string' ? toolCall.title.toLowerCase() : '';
        const destructive = title.startsWith('delete ') || title.startsWith('move ');
        const options = isRecord(params) && Array.isArray(params.options) ? params.options.filter(isRecord) : [];
        const option = options.find(item => item.kind === (destructive ? 'reject_once' : 'allow_always'))
          ?? options.find(item => item.kind === (destructive ? 'reject_always' : 'allow_once'));
        return option && typeof option.optionId === 'string'
          ? { outcome: { outcome: 'selected', optionId: option.optionId } }
          : { outcome: { outcome: 'cancelled' } };
      },
      createTerminal: (params) => createTerminal(params, this.config.cwd, terminals),
      terminalOutput: (params) => {
        const terminal = getTerminal(params, terminals);
        return {
          output: terminal.output,
          truncated: terminal.truncated,
          exitStatus: terminal.exited,
        };
      },
      waitForTerminalExit: async (params) => getTerminal(params, terminals).exit,
      killTerminal: (params) => {
        const terminal = getTerminal(params, terminals);
        if (!terminal.exited) terminal.process.kill('SIGTERM');
        return {};
      },
      releaseTerminal: (params) => {
        const terminalId = isRecord(params) && typeof params.terminalId === 'string' ? params.terminalId : '';
        const terminal = terminals.get(terminalId);
        if (terminal && !terminal.exited) terminal.process.kill('SIGTERM');
        terminals.delete(terminalId);
        return {};
      },
      onNotification: (notification) => {
        if (connection) this.sharedSessions.notification(connection, notification);
      },
      onDisconnect: (error) => {
        if (connection?.disconnected) return;
        if (connection) connection.disconnected = true;
        if (connection) this.sharedSessions.forgetHost(connection);
        closeTerminals(terminals);
        notifications.push({
          jsonrpc: '2.0',
          method: 'astrid/acp/disconnected',
          params: { code: error.code },
        });
      },
    };
    const launcherOptions: AstridAcpLauncherOptions = {
      cwd: this.config.cwd,
      profile: this.config.profile,
      sessionDir: this.config.sessionDir,
      systemPromptFile: this.config.systemPromptFile,
      command: this.config.command,
      ...(temporary ? { requestTimeoutMs: DISCOVERY_REQUEST_TIMEOUT_MS } : {}),
      callbacks,
      ...(localMediaOnly ? { env: {
        PI_CONFIG_FILES: [process.env.PI_CONFIG_FILES, fileURLToPath(new URL('./astrid-acp-local-only.yml', import.meta.url))].filter(Boolean).join(delimiter),
        PI_BASH_NO_LOGIN: '1',
        ASTRID_SYSTEM_PROMPT: `${ASTRID_ACP_CONTEXT_SYSTEM_PROMPT}\nHosted same-machine mode: keep all media bytes on this computer. Use Runtime IDs, text and scoped metadata for CPU edits. Do not send media to model/provider tools or cloud media executors, including through shell commands. ${agent === 'astrid' ? 'Native image submission and inspection are disabled for this session.' : 'Do not submit or inspect images with native agent tools.'}`,
      } } : {}),
    };
    const host = agent === 'astrid' ? this.hostFactory(launcherOptions) : await this.localAgentHostFactory(agent, launcherOptions);
    if (this.closing) {
      await host.dispose(temporary ? 'SIGKILL' : 'SIGTERM');
      throw new ChatError(503, 'chat_stopping', 'The chat service is stopping.');
    }
    connection = { id: connectionId, agent, host, notifications, terminals, sessions: new Set(), configOptions: new Map(), models: new Map(), ...(defaultModelId ? { defaultModelId } : {}), disconnected: false, closed: false, notificationBytes: 0, localMediaOnly, lastSeenAt: this.now(), inFlight: 0 };
    if (temporary) this.temporaryConnections.add(connection);
    else this.connections.set(connectionId, connection);
    try {
      if (!temporary && agent !== 'astrid') await this.localAgentSkills.sync(agent);
      if (this.closing) throw new ChatError(503, 'chat_stopping', 'The chat service is stopping.');
      const initialize = await host.initialize({
        protocolVersion: 1,
        clientCapabilities: acpClientCapabilities(agent),
        clientInfo: { name: 'astrid-acp-bridge', version: CLIENT_VERSION },
      });
      if (this.closing) throw new ChatError(503, 'chat_stopping', 'The chat service is stopping.');
      return { connection, initialize };
    } catch (error) {
      this.connections.delete(connectionId);
      this.temporaryConnections.delete(connection);
      closeTerminals(terminals);
      await host.dispose(temporary ? 'SIGKILL' : 'SIGTERM');
      throw error;
    }
  }

  private async ensureAgentModels(agent: LocalAgentId, localMediaOnly: boolean): Promise<void> {
    if (this.modelState.catalogs.get(agent)?.availableModels.length || this.bootstrapped.has(agent)) return;
    const pending = this.bootstraps.get(agent);
    if (pending) return pending;
    const bootstrap = (async () => {
      const { connection } = await this.createConnection(localMediaOnly, agent, undefined, true);
      try {
        const result = await connection.host.request('session/new', { cwd: this.config.cwd, mcpServers: [] });
        const id = sessionIdFrom(result);
        if (!id) throw new ChatError(502, 'invalid_session', 'The agent did not return a bootstrap session ID.');
        this.modelState.hide(id);
        this.modelState.refresh(agent, sharedSessionModels(result));
        this.bootstrapped.add(agent);
        // Native close releases resources; the durable exclusion also covers
        // adapters that keep closed sessions in their transcript list.
        await connection.host.request('session/close', { sessionId: id }).catch(() => {});
      } finally {
        this.temporaryConnections.delete(connection);
        connection.closed = true;
        closeTerminals(connection.terminals);
        await connection.host.dispose('SIGKILL');
      }
    })()
      // Discovery must still show recovery rows if an adapter cannot start.
      // The next explicit menu refresh can retry a failed bootstrap.
      .catch(() => { /* A normal chat start reports the actionable adapter error. */ })
      .finally(() => { this.bootstraps.delete(agent); });
    this.bootstraps.set(agent, bootstrap);
    await bootstrap;
  }

  private assertVisibleSession(id: string): void {
    if (this.modelState.hiddenSessions.has(id)) throw new ChatError(404, 'session_not_found', 'This internal session is unavailable.');
  }

  private visibleSessions(value: unknown): unknown {
    const visible = (item: unknown) => !this.modelState.hiddenSessions.has(sessionIdFrom(item) ?? '');
    if (Array.isArray(value)) return value.filter(visible);
    const root = asRecord(value);
    if (!root) return value;
    if (Array.isArray(root.sessions)) return { ...root, sessions: root.sessions.filter(visible) };
    if (Array.isArray(root.items)) return { ...root, items: root.items.filter(visible) };
    return value;
  }

  private async projectChat(projectId: string, action: string | undefined, request: IncomingMessage, response: ServerResponse, localMediaOnly: boolean, held: Array<() => void>): Promise<void> {
    const registry = this.chatRegistry;
    if (!registry) throw new ChatError(503, 'chat_storage_unavailable', 'Project chat requires the configured Workspace Runtime endpoint and credential');
    if (!action && request.method === 'GET') {
      jsonResponse(response, 200, await registry.get(projectId));
      return;
    }
    if (action === 'unassigned' && request.method === 'GET') {
      const connectionId = new URL(request.url ?? '/', 'http://localhost').searchParams.get('connection_id');
      const connection = connectionId ? this.connections.get(connectionId) : undefined;
      if (!connection || connection.closed || connection.disconnected) throw new ChatError(404, 'connection_not_found', 'ACP connection is unavailable; reconnect');
      held.push(this.use(connection));
      if (localMediaOnly && !connection.localMediaOnly) throw new ChatError(409, 'hosted_connection_required', 'Create a hosted ACP connection to enable installed-agent media protections');
      const result = this.visibleSessions(await this.sharedSessions.request(connection, 'session/list', { cwd: this.config.cwd }));
      const root = asRecord(result) ?? {};
      const items = Array.isArray(result) ? result : Array.isArray(root.sessions) ? root.sessions : Array.isArray(root.items) ? root.items : [];
      const candidates = items.flatMap(item => {
        const session = asRecord(item);
        const id = sessionIdFrom(item);
        if (!id) return [];
        const title = typeof session?.title === 'string' ? session.title : undefined;
        return [{ id, ...(title ? { title } : {}) }];
      });
      jsonResponse(response, 200, { sessions: await registry.unassigned(projectId, candidates) });
      return;
    }
    const body = asRecord(await readJson(request));
    if (!body) throw new ChatError(400, 'invalid_body', 'Expected a JSON object');
    if (localMediaOnly) assertLocalMediaOnly(body);
    if (request.method === 'PATCH' && (!action || action === 'draft' || action === 'main')) {
      if (!Number.isInteger(body.expected_revision) || (body.expected_revision as number) < 0) throw new ChatError(400, 'invalid_body', 'expected_revision must be a nonnegative integer');
      if (action === 'draft') {
        if (typeof body.text !== 'string' || body.text.length > 100_000) throw new ChatError(400, 'invalid_body', 'Draft text must be a string of at most 100000 characters');
        if (body.queued_messages !== undefined && (!Array.isArray(body.queued_messages) || body.queued_messages.length > 100 || !body.queued_messages.every(item => isRecord(item) && typeof item.id === 'string' && typeof item.text === 'string' && typeof item.session_id === 'string' && (item.attachments === undefined || Array.isArray(item.attachments))))) throw new ChatError(400, 'invalid_body', 'queued_messages must contain scoped message records');
        jsonResponse(response, 200, await registry.draft(projectId, body.expected_revision as number, body.text, body.queued_messages as QueuedChatMessage[] | undefined));
      } else if (action === 'main') {
        if (typeof body.main_session_id !== 'string' || !body.main_session_id) throw new ChatError(400, 'invalid_body', 'main_session_id is required');
        jsonResponse(response, 200, await registry.setMain(projectId, body.expected_revision as number, body.main_session_id));
      } else if ('session_id' in body || 'title' in body) {
        if (typeof body.session_id !== 'string' || !body.session_id || typeof body.title !== 'string') throw new ChatError(400, 'invalid_body', 'session_id and title are required');
        jsonResponse(response, 200, await registry.rename(projectId, body.expected_revision as number, body.session_id, body.title));
      } else {
        if (body.selected_session_id !== null && typeof body.selected_session_id !== 'string') throw new ChatError(400, 'invalid_body', 'selected_session_id is required');
        jsonResponse(response, 200, await registry.select(projectId, body.expected_revision as number, body.selected_session_id as string | null));
      }
      return;
    }
    if (request.method === 'POST' && (action === 'sessions' || action === 'prompt' || action === 'associate')) {
      const connection = typeof body.connection_id === 'string' ? this.connections.get(body.connection_id) : undefined;
      if (!connection || connection.closed || connection.disconnected) throw new ChatError(404, 'connection_not_found', 'ACP connection is unavailable; reconnect');
      held.push(this.use(connection));
      if (localMediaOnly && !connection.localMediaOnly) throw new ChatError(409, 'hosted_connection_required', 'Create a hosted ACP connection to enable installed-agent media protections');
      // Follow-up requests inherit the connection's sticky hosted policy even
      // when a direct caller omits the per-request header. Check before registry
      // mutations, session creation/resume or agent forwarding.
      if (connection.localMediaOnly || localMediaOnly) assertLocalMediaOnly(body);
      if (action === 'sessions') {
        if ((body.mode !== 'ensure' && body.mode !== 'ensure_main' && body.mode !== 'new') || typeof body.operation_id !== 'string' || !body.operation_id || body.operation_id.length > 200) throw new ChatError(400, 'invalid_body', 'mode and operation_id are required');
        if (body.recover_session_id !== undefined && (typeof body.recover_session_id !== 'string' || !body.recover_session_id || body.mode === 'new')) throw new ChatError(400, 'invalid_body', 'recover_session_id is invalid');
        if (typeof body.recover_session_id === 'string' && sessionAgentId(body.recover_session_id) !== connection.agent) throw new ChatError(403, 'session_agent_mismatch', 'Recovery must use the chat’s original agent');
        const recoveryTarget = typeof body.recover_session_id === 'string'
          ? (await registry.get(projectId)).sessions.find(session => session.id === body.recover_session_id)
          : undefined;
        if (body.recover_session_id && (!recoveryTarget || recoveryTarget.pending_first_message === false)) throw new ChatError(409, 'session_not_recoverable', 'Only an empty or legacy local-agent chat can be recovered automatically');
        const state = await registry.create(projectId, body.mode, body.operation_id, async () => {
          return await this.createSession(connection, { cwd: this.config.cwd, mcpServers: [] }, recoveryTarget?.model_id);
        }, async sessionId => {
          // A different agent's chat remains available through its original
          // connection. Changing the default must never mark that chat missing.
          if (sessionAgentId(sessionId) !== connection.agent) return true;
          try {
            const result = await this.sharedSessions.request(connection, 'session/resume', { sessionId, cwd: this.config.cwd, mcpServers: [] });
            this.rememberSession(connection, 'session/resume', { sessionId }, result);
            return true;
          } catch (error) {
            if (isMissingPersistedSession(error, sessionId)) return false;
            throw error;
          }
        }, body.recover_session_id as string | undefined);
        if (state.operation_session_id) connection.sessions.add(state.operation_session_id);
        jsonResponse(response, 200, state);
      } else if (action === 'associate') {
        if (typeof body.session_id !== 'string' || !Number.isInteger(body.expected_revision)) throw new ChatError(400, 'invalid_body', 'session_id and expected_revision are required');
        this.assertVisibleSession(body.session_id);
        const state = await registry.associate(projectId, body.expected_revision as number, body.session_id, async sessionId => {
          try {
            const result = await this.sharedSessions.request(connection, 'session/resume', { sessionId, cwd: this.config.cwd, mcpServers: [] });
            this.rememberSession(connection, 'session/resume', { sessionId }, result);
            return true;
          } catch (error) {
            if (isMissingPersistedSession(error, sessionId)) return false;
            throw error;
          }
        });
        jsonResponse(response, 200, state);
      } else {
        if (typeof body.session_id !== 'string' || !Array.isArray(body.prompt)) throw new ChatError(400, 'invalid_body', 'session_id and prompt are required');
        await registry.assertOwned(projectId, body.session_id);
        if (body.request_id !== undefined && (typeof body.request_id !== 'string' || !/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$/.test(body.request_id))) throw new ChatError(400, 'invalid_body', 'request_id must be a submission identifier');
        await registry.markSessionStarted(projectId, body.session_id);
        const result = await this.sharedSessions.request(connection, 'session/prompt', { sessionId: body.session_id, prompt: body.prompt, ...(body.request_id ? { _astridRequestId: body.request_id } : {}) });
        jsonResponse(response, 200, { result });
      }
      return;
    }
    throw new ChatError(404, 'not_found', 'Unknown project chat operation');
  }

  private rememberSession(connection: Connection, method: string, params: unknown, result: unknown): void {
    if (method !== 'session/new' && method !== 'session/load' && method !== 'session/resume' && method !== 'session/fork') return;
    const input = asRecord(params);
    const sessionId = sessionIdFrom(result) ?? (method === 'session/load' || method === 'session/resume' ? sessionIdFrom(input) : null);
    if (!sessionId) return;
    connection.sessions.add(sessionId);
    const options = advertisedConfigOptions(result);
    if (Array.isArray(asRecord(result)?.configOptions)) connection.configOptions.set(sessionId, options);
    const models = sharedSessionModels(result);
    if (models) {
      connection.models.set(sessionId, models);
      this.modelState.refresh(connection.agent, models);
    }
    else connection.models.delete(sessionId);
  }

  private async createSession(connection: Connection, params: unknown, requestedModelId = connection.defaultModelId): Promise<unknown> {
    const result = await this.sharedSessions.request(connection, 'session/new', params);
    const sessionId = sessionIdFrom(result);
    if (!sessionId) throw new ChatError(502, 'invalid_session', 'The agent did not return a new session ID.');
    this.rememberSession(connection, 'session/new', params, result);
    try { return await this.applyNewSessionModel(connection, sessionId, result, requestedModelId); }
    catch (error) {
      // A failed selection must not leave a usable chat on a different model.
      try { this.modelState.hide(sessionId); }
      finally {
        await this.sharedSessions.request(connection, 'session/close', { sessionId }).catch(() => {});
        connection.sessions.delete(sessionId);
        connection.models.delete(sessionId);
        connection.configOptions.delete(sessionId);
      }
      throw error;
    }
  }

  private async applyNewSessionModel(connection: Connection, sessionId: string, result: unknown, requestedModelId?: string): Promise<unknown> {
    const models = sessionId ? connection.models.get(sessionId) : undefined;
    this.modelState.refresh(connection.agent, models ?? null);
    if (!models) {
      if (requestedModelId) throw new ChatError(409, 'model_unavailable', 'The selected model was not advertised by this new ACP session.');
      this.modelState.remember(connection.agent);
      return result;
    }
    if (requestedModelId) {
      if (!models.availableModels.some(model => model.modelId === requestedModelId)) {
        throw new ChatError(409, 'model_unavailable', 'The selected default model is unavailable in this new ACP session.');
      }
      const configId = models.modelConfigId;
      const usesConfigOption = Boolean(configId);
      await this.sharedSessions.request(connection, usesConfigOption ? 'session/set_config_option' : 'session/set_model', usesConfigOption
        ? { sessionId, configId, value: requestedModelId }
        : { sessionId, modelId: requestedModelId });
      this.updateLocalModelState(connection, sessionId, requestedModelId);
    }
    this.modelState.remember(connection.agent, connection.models.get(sessionId)?.currentModelId);
    return { ...(asRecord(result) ?? {}), ...(asRecord(this.sharedSessions.snapshot(connection, sessionId)) ?? {}), models: connection.models.get(sessionId) };
  }

  private updateLocalModelState(connection: Connection, sessionId: string, requestedModelId: string): void {
    this.rememberSession(connection, 'session/load', { sessionId }, this.sharedSessions.snapshot(connection, sessionId));
    if (connection.models.get(sessionId)?.currentModelId !== requestedModelId) {
      throw new ChatError(409, 'model_selection_rejected', 'The installed agent selected a different model than requested.');
    }
  }

  private async rpc(connection: Connection, request: IncomingMessage, response: ServerResponse, localMediaOnly: boolean): Promise<void> {
    const body = asRecord(await readJson(request));
    const method = body?.method;
    if (typeof method !== 'string' || !REQUEST_METHODS.has(method)) {
      throw new Error(`method must be one of: ${[...REQUEST_METHODS].join(', ')}`);
    }
    let params = body?.params;
    if (localMediaOnly) assertLocalMediaOnly(params);
    if (method === 'session/new' || method === 'session/load' || method === 'session/resume') {
      const input = asRecord(params) ?? {};
      if (method !== 'session/new' && typeof input.sessionId === 'string') this.assertVisibleSession(input.sessionId);
      // The host owns the process cwd; the browser cannot redirect the ACP session.
      params = { ...input, cwd: this.config.cwd, mcpServers: input.mcpServers ?? [] };
    }
    if (method === 'session/set_config_option') {
      const input = asRecord(params);
      if (!input || Object.keys(input).some((key) => !['sessionId', 'configId', 'value'].includes(key))) {
        throw new Error('config option request accepts only sessionId, configId, and value');
      }
      if (typeof input.sessionId !== 'string' || !input.sessionId) throw new Error('sessionId is required for config option updates');
      if (!connection.sessions.has(input.sessionId)) throw new Error('config option sessionId is not owned by this ACP connection');
      if (typeof input.configId !== 'string' || !input.configId) throw new Error('config option id is required');
      const modelConfigId = sharedSessionModels(this.sharedSessions.snapshot(connection, input.sessionId))?.modelConfigId;
      if (input.configId === 'model' || input.configId === modelConfigId) throw new ChatError(409, 'session_model_fixed', 'To use another model, start a new chat.');
      if (typeof input.value !== 'string' || !input.value) throw new Error('config option value is required');
    }
    let result = method === 'session/new'
      ? await this.createSession(connection, params)
      : method === 'session/takeover'
      ? await this.sharedSessions.takeover(connection, String(asRecord(params)?.sessionId ?? ''))
      : method === 'session/set_config_option'
      ? await this.sharedSessions.changeConfig(connection, String(asRecord(params)?.sessionId), snapshot => {
        const input = asRecord(params)!;
        const modelConfigId = sharedSessionModels(snapshot)?.modelConfigId;
        if (input.configId === 'model' || input.configId === modelConfigId) throw new ChatError(409, 'session_model_fixed', 'To use another model, start a new chat.');
        this.rememberSession(connection, 'session/load', input, snapshot);
        const allowedValues = connection.configOptions.get(input.sessionId as string)?.get(input.configId as string);
        if (!allowedValues?.has(input.value as string)) throw new Error('config option value was not advertised for this session');
        return { method: 'session/set_config_option', params };
      })
      : await this.sharedSessions.request(connection, method, params);
    if (method === 'session/new' || method === 'session/load' || method === 'session/resume' || method === 'session/fork') {
      this.rememberSession(connection, method, params, result);
    } else if (method === 'session/list') {
      result = this.visibleSessions(result);
      const items = Array.isArray(result) ? result : isRecord(result) && Array.isArray(result.sessions) ? result.sessions : [];
      for (const item of items) {
        const sessionId = sessionIdFrom(item);
        if (sessionId) connection.sessions.add(sessionId);
      }
    } else if (method === 'session/set_config_option') {
      const input = asRecord(params)!;
      this.rememberSession(connection, 'session/load', input, this.sharedSessions.snapshot(connection, input.sessionId as string));
    } else if (method === 'session/close') {
      const sessionId = sessionIdFrom(params);
      if (sessionId) {
        connection.sessions.delete(sessionId);
        connection.configOptions.delete(sessionId);
        connection.models.delete(sessionId);
      }
    }
    jsonResponse(response, 200, { result });
  }

  private async cancel(connection: Connection, request: IncomingMessage, response: ServerResponse): Promise<void> {
    const body = asRecord(await readJson(request));
    const sessionId = body?.sessionId;
    if (typeof sessionId !== 'string' || !sessionId) throw new Error('sessionId is required');
    await this.sharedSessions.cancel(connection, sessionId);
    jsonResponse(response, 200, { cancelled: true, session_id: sessionId });
  }
}

export function createAstridAcpHttpServer(options: BridgeOptions): {
  readonly bridge: AstridAcpBridge;
  readonly server: ReturnType<typeof createServer>;
} {
  const bridge = new AstridAcpBridge(options);
  const server = createServer((request, response) => {
    void bridge.handle(request, response);
  });
  server.once('close', () => { void bridge.close(); });
  return { bridge, server };
}

export function startAcpBridge(config: AstridAcpBridgeConfig): ReturnType<typeof createServer> {
  const { server } = createAstridAcpHttpServer({ config });
  server.listen(config.port, '127.0.0.1');
  return server;
}

