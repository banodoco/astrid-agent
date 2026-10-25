import { ACP_SESSION_CONTROL_FIELD, ACP_SESSION_CONTROL_UPDATE } from './acpSessionControl.ts';
import { sessionAgentId, type LocalAgentId } from './localAgents.ts';
import { sessionConfigModels } from './sessionConfigOptions.ts';
import { ChatError } from './astrid-project-chat.ts';
import type { AcpProcessHost, AcpRpcNotification } from './acpProcessHost.ts';
import { randomUUID } from 'node:crypto';

export type SharedAcpSessionClient = {
  readonly id: string;
  readonly agent: LocalAgentId;
  readonly localMediaOnly: boolean;
  readonly host: AcpProcessHost;
};

type Subscriber = SharedAcpSessionClient;
type JournalItem = AcpRpcNotification;
type Dedupe = { payload: string; result: Promise<unknown> };
type Session = {
  readonly id: string;
  readonly agent: LocalAgentId;
  readonly host: AcpProcessHost;
  readonly hostClient: SharedAcpSessionClient;
  readonly subscribers: Set<Subscriber>;
  readonly journalCursor: Map<Subscriber, number>;
  readonly journal: JournalItem[];
  readonly dedupe: Map<string, Dedupe>;
  writer: SharedAcpSessionClient | null;
  activeRequestId: string | null;
  loading: Promise<unknown> | null;
  loadedResult: unknown;
  loaded: boolean;
  changing: boolean;
};

function object(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

export const sharedSessionModels = sessionConfigModels;

function sessionId(value: unknown): string | null {
  const id = object(value)?.sessionId;
  return typeof id === 'string' && id.length ? id : null;
}

function control(session: Session, client: SharedAcpSessionClient) {
  return { role: session.writer === client ? 'writer' as const : 'follower' as const, active: session.activeRequestId !== null, activeRequestId: session.activeRequestId };
}

function withControl(result: unknown, session: Session, client: SharedAcpSessionClient): unknown {
  const response = object(result);
  if (!response) return result;
  return { ...response, [ACP_SESSION_CONTROL_FIELD]: control(session, client) };
}

function normalizeContext(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(normalizeContext);
  const input = object(value);
  if (!input) return value;
  return Object.fromEntries(Object.entries(input)
    .filter(([key]) => key !== 'captured_at' && key !== 'context_revision')
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([key, child]) => [key, normalizeContext(child)]));
}

function stableValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(stableValue);
  const input = object(value);
  if (!input) return value;
  return Object.fromEntries(Object.entries(input).sort(([a], [b]) => a.localeCompare(b)).map(([key, child]) => [key, stableValue(child)]));
}

function payloadKey(params: unknown): string {
  const input = object(params);
  if (!input) return JSON.stringify(params) ?? 'undefined';
  const prompt = Array.isArray(input.prompt) ? input.prompt.map(part => {
    const item = object(part);
    if (typeof item?.text !== 'string') return stableValue(part);
    const text = item.text.replace(/(<astrid_editor_context\b[^>]*>)([\s\S]*?)(<\/astrid_editor_context>)/g, (_whole, open: string, body: string, close: string) => {
      try { return `${open}${JSON.stringify(normalizeContext(JSON.parse(body)))}${close}`; }
      catch { return `${open}${body.trim()}${close}`; }
    });
    return { ...stableValue(item) as JsonRecord, text };
  }) : input.prompt;
  return JSON.stringify({ ...stableValue(input) as JsonRecord, prompt }) ?? 'undefined';
}

type JsonRecord = Record<string, unknown>;
type ConfigChange = { method: 'session/set_model' | 'session/set_config_option'; params?: unknown };

function editorRequestId(params: unknown): string | null {
  const input = object(params);
  if (typeof input?._astridRequestId === 'string' && input._astridRequestId) return input._astridRequestId;
  const prompt = Array.isArray(input?.prompt) ? input.prompt : [];
  for (const part of prompt) {
    const text = object(part)?.text;
    if (typeof text !== 'string') continue;
    const match = text.match(/<astrid_editor_context\b[^>]*>[\s\S]*?"request_id"\s*:\s*"([^"]+)"[\s\S]*?<\/astrid_editor_context>/);
    if (match?.[1]) return match[1];
  }
  return null;
}

function stripRequestId(params: unknown): unknown {
  const input = object(params);
  if (!input || !('_astridRequestId' in input)) return params;
  const { _astridRequestId: _ignored, ...rest } = input;
  return rest;
}

function payloadText(params: unknown): string {
  const prompt = object(params)?.prompt;
  if (!Array.isArray(prompt)) return '';
  return prompt.map(item => object(item)?.text).filter((part): part is string => typeof part === 'string').join('\n');
}

export class SharedAcpSessions {
  private readonly sessions = new Map<string, Session>();
  private readonly pendingRequests = new WeakMap<AcpProcessHost, Promise<void>>();

  constructor(
    private readonly emit: (client: SharedAcpSessionClient, notification: AcpRpcNotification) => void,
    private readonly beforeReplay?: (client: SharedAcpSessionClient, sessionId: string) => void,
  ) {}

  async request(client: SharedAcpSessionClient, method: string, params?: unknown): Promise<unknown> {
    const id = sessionId(params);
    const session = id ? this.sessions.get(id) : undefined;
    if (method === 'session/set_model' || method === 'session/set_config_option') {
      return this.changeConfig(client, id ?? '', () => ({ method, params }));
    }
    // Prompts keep their own session reservation; cancellation and cached replay
    // must remain available during a turn. All native non-prompt RPCs share the
    // physical connection's queue, including writes forwarded by another tab.
    if (method === 'session/prompt' || ((method === 'session/load' || method === 'session/resume') && session)) {
      return this.dispatch(client, method, params);
    }
    return this.enqueue(session?.host ?? client.host, () => this.dispatch(client, method, params));
  }

  async changeConfig(client: SharedAcpSessionClient, id: string, resolve: (snapshot: unknown) => ConfigChange): Promise<unknown> {
    const target = this.requireSession(client, id);
    this.requireWriter(target, client);
    this.requireNoPrompt(target);
    return this.enqueue(target.host, async () => {
      const session = this.requireSession(client, id);
      this.requireWriter(session, client);
      this.requireNoPrompt(session);
      // Resolve advertised IDs/values only after pending loads and setters have
      // updated the snapshot, rather than validating against stale options.
      const change = resolve(session.loadedResult);
      return this.dispatch(client, change.method, change.params);
    });
  }

  private enqueue(host: AcpProcessHost, run: () => Promise<unknown>): Promise<unknown> {
    const prior = this.pendingRequests.get(host);
    const result = prior ? prior.then(run) : run();
    // A failed read or setter must not poison the rest of the queue.
    const settled = result.then(() => {}, () => {});
    this.pendingRequests.set(host, settled);
    void settled.then(() => {
      if (this.pendingRequests.get(host) === settled) this.pendingRequests.delete(host);
    });
    return result;
  }

  private async dispatch(client: SharedAcpSessionClient, method: string, params?: unknown): Promise<unknown> {
    if (method === 'session/new') {
      const result = await client.host.request(method, params);
      const id = sessionId(result);
      if (!id) return result;
      this.assertAgent(client, id);
      const session = this.create(id, client, result);
      return withControl(result, session, client);
    }
    if (method === 'session/load' || method === 'session/resume') return this.load(client, method, params);
    if (method === 'session/list') return client.host.request(method, params);

    const id = sessionId(params);
    if (!id) return client.host.request(method, params);
    const session = this.requireSession(client, id);
    if (method === 'session/prompt') return this.prompt(client, session, params);
    if (method === 'session/close' || method === 'session/fork' || method === 'session/set_config_option' || method === 'session/set_model') {
      this.requireWriter(session, client);
      if (method === 'session/set_config_option' || method === 'session/set_model') this.requireNoPrompt(session);
      this.requireIdle(session);
      session.changing = true;
      try {
        const result = await session.host.request(method, params);
        if (method === 'session/close') this.delete(session);
        if (method === 'session/set_config_option' || method === 'session/set_model') this.updateCachedConfig(session, method, params, result);
        if (method === 'session/fork') {
          const forkId = sessionId(result);
          if (forkId) {
            this.assertAgent(client, forkId);
            const forked = this.create(forkId, session.hostClient, result, client);
            return withControl(result, forked, client);
          }
        }
        return result;
      } finally { session.changing = false; }
    }
    return session.host.request(method, params);
  }

  snapshot(client: SharedAcpSessionClient, id: string): unknown {
    return this.requireSession(client, id).loadedResult;
  }

  async cancel(client: SharedAcpSessionClient, id: string): Promise<void> {
    const session = this.requireSession(client, id);
    this.requireWriter(session, client);
    await session.host.cancel({ sessionId: session.id });
  }

  async takeover(client: SharedAcpSessionClient, id: string): Promise<unknown> {
    const session = this.requireSession(client, id);
    session.writer = client;
    this.broadcastControl(session);
    return { [ACP_SESSION_CONTROL_FIELD]: control(session, client) };
  }

  notification(sourceClient: SharedAcpSessionClient, notification: AcpRpcNotification): void {
    const params = object(notification.params);
    const id = typeof params?.sessionId === 'string' ? params.sessionId : null;
    const session = id ? this.sessions.get(id) : undefined;
    if (!session || session.hostClient !== sourceClient || notification.method !== 'session/update') {
      this.emit(sourceClient, notification);
      return;
    }
    const update = object(params?.update);
    if (update?.sessionUpdate === 'config_option_update' || update?.sessionUpdate === 'current_model_update') this.mergeMetadata(session, update);
    const marked: AcpRpcNotification = session.activeRequestId && update
      ? { ...notification, params: { ...params, update: { ...update, live: true, requestId: session.activeRequestId } } }
      : notification;
    session.journal.push(marked);
    for (const subscriber of session.subscribers) session.journalCursor.set(subscriber, session.journal.length);
    this.broadcast(session, marked);
  }

  detach(client: SharedAcpSessionClient): void {
    for (const session of this.sessions.values()) {
      if (!session.subscribers.delete(client)) continue;
      session.journalCursor.delete(client);
      // The bridge is the authority: promote exactly one surviving subscriber.
      if (session.writer === client) session.writer = session.subscribers.values().next().value ?? null;
      this.broadcastControl(session);
    }
  }

  retainsHost(client: SharedAcpSessionClient): boolean {
    for (const session of this.sessions.values()) {
      if (session.hostClient === client && (session.subscribers.size > 0 || session.activeRequestId !== null || session.loading !== null || session.changing)) return true;
    }
    return false;
  }

  forgetHost(client: SharedAcpSessionClient): void {
    const subscribers = new Set<SharedAcpSessionClient>();
    for (const session of [...this.sessions.values()]) {
      if (session.hostClient !== client) continue;
      for (const subscriber of session.subscribers) subscribers.add(subscriber);
      this.delete(session);
    }
    const disconnect: AcpRpcNotification = { jsonrpc: '2.0', method: 'astrid/acp/disconnected', params: { code: 'acp_process_exited' } };
    for (const subscriber of subscribers) this.emit(subscriber, disconnect);
  }

  close(): void {
    this.sessions.clear();
  }

  private async load(client: SharedAcpSessionClient, method: string, params: unknown): Promise<unknown> {
    const id = sessionId(params);
    if (!id) throw new ChatError(400, 'invalid_body', 'sessionId is required');
    this.assertAgent(client, id);
    let session = this.sessions.get(id);
    if (!session) {
      session = this.create(id, client);
      const nativeParams = { ...(object(params) ?? {}), sessionId: id };
      session.loading = session.host.request(method, nativeParams).then(result => { session!.loadedResult = { ...(object(result) ?? {}), ...(object(session!.loadedResult) ?? {}) }; session!.loaded = true; return session!.loadedResult; }, error => {
        this.delete(session!);
        throw error;
      });
    }
    const cacheHit = session.loaded && session.loading === null;
    this.assertAttachAllowed(client, session);
    session.subscribers.add(client);
    if (!session.writer) session.writer = client;
    if (cacheHit) {
      this.beforeReplay?.(client, id);
      session.journalCursor.set(client, 0);
    }
    const result = session.loaded ? session.loadedResult : await (session.loading ?? Promise.resolve({ sessionId: id }));
    session.loading = null;
    const cursor = session.journalCursor.get(client) ?? 0;
    for (const entry of session.journal.slice(cursor)) this.emit(client, entry);
    session.journalCursor.set(client, session.journal.length);
    this.emitControl(session, client);
    return withControl(result, session, client);
  }

  private async prompt(client: SharedAcpSessionClient, session: Session, params: unknown): Promise<unknown> {
    const requestId = editorRequestId(params) ?? `astrid-${randomUUID()}`;
    const forwarded = stripRequestId(params);
    const key = requestId ? `${session.id}\0${requestId}` : null;
    const serialized = payloadKey(forwarded);
    if (key) {
      const prior = session.dedupe.get(key);
      if (prior) {
        if (prior.payload !== serialized) throw new ChatError(409, 'request_id_conflict', 'This request ID was already used for a different prompt.');
        return prior.result;
      }
    }
    this.requireWriter(session, client);
    this.requireIdle(session);
    session.activeRequestId = requestId;
    this.broadcastControl(session);
    if (requestId) {
      const live: AcpRpcNotification = { jsonrpc: '2.0', method: 'session/update', params: { sessionId: session.id, update: { sessionUpdate: 'user_message_chunk', messageId: `astrid-live:${requestId}`, content: { type: 'text', text: payloadText(params) }, live: true, requestId } } };
      for (const subscriber of session.subscribers) if (subscriber !== client) this.emit(subscriber, live);
      session.journal.push(live);
    }
    const operation = session.host.request('session/prompt', forwarded).finally(() => {
      session.activeRequestId = null;
      this.broadcastControl(session);
    });
    if (key) {
      const entry = { payload: serialized, result: operation };
      session.dedupe.set(key, entry);
      void operation.catch(() => {
        if (session.dedupe.get(key) === entry) session.dedupe.delete(key);
      });
    }
    return operation;
  }

  private create(id: string, client: SharedAcpSessionClient, loadedResult?: unknown, writer = client): Session {
    if (this.sessions.has(id)) throw new ChatError(409, 'session_already_attached', 'This native session is already attached to the bridge.');
    this.assertAgent(writer, id);
    const session: Session = { id, agent: client.agent, host: client.host, hostClient: client, subscribers: new Set([writer]), journalCursor: new Map([[writer, 0]]), journal: [], dedupe: new Map(), writer, activeRequestId: null, loading: null, loadedResult, loaded: loadedResult !== undefined, changing: false };
    this.sessions.set(id, session);
    return session;
  }

  private mergeMetadata(session: Session, update: JsonRecord): void {
    const current = object(session.loadedResult) ?? {};
    const next = { ...current };
    if (Array.isArray(update.configOptions)) next.configOptions = update.configOptions;
    if (object(update.models)) next.models = update.models;
    if (typeof update.currentModelId === 'string') {
      const models = sharedSessionModels(next);
      if (models) next.models = { ...models, currentModelId: update.currentModelId };
      if (Array.isArray(next.configOptions)) next.configOptions = next.configOptions.map(option => {
        const item = object(option);
        return item?.category === 'model' || item?.id === 'model' ? { ...item, currentValue: update.currentModelId } : option;
      });
    }
    session.loadedResult = next;
  }

  private updateCachedConfig(session: Session, method: string, params: unknown, result: unknown): void {
    const output = object(result) ?? {};
    const input = object(params) ?? {};
    this.mergeMetadata(session, output);
    const current = object(session.loadedResult) ?? {};
    if (method === 'session/set_config_option' && !Array.isArray(output.configOptions)) {
      const configOptions = Array.isArray(current.configOptions) ? current.configOptions : [];
      this.mergeMetadata(session, { configOptions: configOptions.map(option => {
        const item = object(option);
        return item?.id === input.configId ? { ...item, currentValue: input.value } : option;
      }) });
    } else if (method === 'session/set_model' && !object(output.models)) {
      this.mergeMetadata(session, { currentModelId: input.modelId });
    }
    const metadata = object(session.loadedResult) ?? {};
    const models = sharedSessionModels(metadata);
    // Adapters may acknowledge a setter with an empty response and no event.
    // Publish the accepted state so every attached tab sees the same selection.
    this.notification(session.hostClient, { jsonrpc: '2.0', method: 'session/update', params: { sessionId: session.id, update: {
      sessionUpdate: method === 'session/set_config_option' ? 'config_option_update' : 'current_model_update',
      ...(Array.isArray(metadata.configOptions) ? { configOptions: metadata.configOptions } : {}),
      ...(models ? { models, ...(method === 'session/set_model' ? { currentModelId: models.currentModelId } : {}) } : {}),
    } } });
  }

  private requireSession(client: SharedAcpSessionClient, id: string): Session {
    this.assertAgent(client, id);
    const session = this.sessions.get(id);
    if (!session) throw new ChatError(404, 'session_not_found', 'The shared session is not attached to this bridge. Load it first.');
    this.assertAttachAllowed(client, session);
    if (!session.subscribers.has(client)) throw new ChatError(409, 'session_not_attached', 'Load this session before using it.');
    return session;
  }

  private assertAgent(client: SharedAcpSessionClient, id: string): void {
    if (sessionAgentId(id) !== client.agent) throw new ChatError(409, 'session_agent_mismatch', 'This saved chat belongs to a different agent.');
  }

  private assertAttachAllowed(client: SharedAcpSessionClient, session: Session): void {
    if (session.agent !== client.agent) throw new ChatError(409, 'session_agent_mismatch', 'This saved chat belongs to a different agent.');
    if (client.localMediaOnly !== session.hostClient.localMediaOnly) throw new ChatError(409, 'hosted_connection_required', 'A client cannot attach to a session with a different local media policy.');
  }

  private requireWriter(session: Session, client: SharedAcpSessionClient): void {
    if (session.writer !== client) throw new ChatError(409, 'session_read_only', 'Take over this session before sending changes.');
  }

  private requireIdle(session: Session): void {
    if (session.activeRequestId !== null || session.changing) throw new ChatError(409, 'session_busy', 'This session has an active request.');
  }

  private requireNoPrompt(session: Session): void {
    if (session.activeRequestId !== null) throw new ChatError(409, 'acp_request_in_progress', 'Wait for the current ACP request to finish before changing models or settings');
  }

  private broadcast(session: Session, notification: AcpRpcNotification): void {
    for (const subscriber of session.subscribers) this.emit(subscriber, notification);
  }

  private emitControl(session: Session, client: SharedAcpSessionClient): void {
    this.emit(client, { jsonrpc: '2.0', method: 'session/update', params: { sessionId: session.id, update: { sessionUpdate: ACP_SESSION_CONTROL_UPDATE, ...control(session, client) } } });
  }

  private broadcastControl(session: Session): void {
    for (const subscriber of session.subscribers) this.emitControl(session, subscriber);
  }

  private delete(session: Session): void {
    this.sessions.delete(session.id);
  }
}
