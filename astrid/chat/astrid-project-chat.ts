import { createHash, randomUUID } from 'node:crypto';
import { isDeepStrictEqual } from 'node:util';
import { closeSync, mkdirSync, openSync, readFileSync, realpathSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';
import { ApiError, WorkspaceClient, type Project } from './runtime-client.ts';
import { currentRuntime } from './runtime-discovery.ts';
import { isExternalAgentSession } from './localAgents.ts';

const KEY = 'astrid_agent_chat_v1';
export class ChatError extends Error {
  constructor(public status: number, public code: string, message: string) { super(message); }
}
export interface QueuedChatMessage { id: string; text: string; session_id: string; attachments?: unknown[] }
export interface ChatState {
  project_id: string;
  scope_key: string;
  revision: number;
  selected_session_id: string | null;
  main_session_id: string | null;
  sessions: Array<{ id: string; title?: string; missing?: boolean; pending_first_message?: boolean; model_id?: string }>;
  draft: { text: string; revision: number; queued_messages: QueuedChatMessage[] };
}
interface StoredChat extends Omit<ChatState, 'project_id' | 'scope_key'> {
  creating?: { operation_id: string; owner: string; expires_at: number; main_session_id?: string | null };
  operations?: Record<string, string>;
}
type ProjectStore = Pick<WorkspaceClient, 'getProject' | 'updateProject' | 'listProjects'>;
function record(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
function isRecord(value: unknown): value is Record<string, unknown> { return Boolean(value && typeof value === 'object' && !Array.isArray(value)); }
function canonicalPath(path: string): string { try { return realpathSync(path); } catch { return resolve(path); } }
export function chatStoreIdentity(config: { cwd: string; profile?: string; sessionDir?: string; command?: string }): string {
  return createHash('sha256').update(JSON.stringify({ home: canonicalPath(homedir()), cwd: canonicalPath(config.cwd), profile: config.profile ?? '', sessionDir: config.sessionDir ? canonicalPath(config.sessionDir) : '', command: config.command ?? 'astrid' })).digest('hex');
}
export function chatClaimIdentity(config: { cwd: string; sessionDir?: string }): string {
  return createHash('sha256').update(JSON.stringify(config.sessionDir
    ? { home: canonicalPath(homedir()), sessionDir: canonicalPath(config.sessionDir) }
    : { home: canonicalPath(homedir()), cwd: canonicalPath(config.cwd) })).digest('hex');
}
export function projectChatClaimDirectory(config: { cwd: string; sessionDir?: string }): string {
  return join(homedir(), '.omp', 'agent', 'project-chat-claims', chatClaimIdentity(config));
}
export function runtimeChatClient(env: NodeJS.ProcessEnv = process.env): ProjectStore | undefined {
  const discoveryPath = env.ASTRID_WORKSPACE_DISCOVERY?.trim();
  if (discoveryPath) {
    // Resolve discovery and credentials for each operation. Runtime can move to
    // another loopback port or rotate its product token while this agent process
    // stays alive; the proxy helper validates loopback endpoints and rereads both.
    const clientForOperation = () => {
      const runtime = currentRuntime(discoveryPath);
      return new WorkspaceClient(runtime.endpoint.origin, runtime.token);
    };
    return {
      getProject: (...args) => clientForOperation().getProject(...args),
      updateProject: (...args) => clientForOperation().updateProject(...args),
      listProjects: (...args) => clientForOperation().listProjects(...args),
    };
  }

  // Preserve the older explicit endpoint/token-file configuration for callers
  // that have not adopted the discovery file yet.
  const endpoint = env.VITE_WORKSPACE_RUNTIME_URL?.trim();
  const tokenFile = env.WORKSPACE_RUNTIME_TOKEN_FILE?.trim();
  if (!endpoint || !tokenFile) return undefined;
  const url = new URL(endpoint);
  if (url.protocol !== 'http:' || !['localhost', '127.0.0.1', '[::1]'].includes(url.hostname)) throw new Error('Chat runtime endpoint must be loopback HTTP');
  const raw = readFileSync(tokenFile, 'utf8').trim();
  const token = raw.startsWith('{') ? record(JSON.parse(raw)).token : raw;
  if (typeof token !== 'string' || !token) throw new Error('Chat runtime credential is empty');
  const client = new WorkspaceClient(url.origin, token);
  return {
    getProject: (...args) => client.getProject(...args),
    updateProject: (...args) => client.updateProject(...args),
    listProjects: (...args) => client.listProjects(...args),
  };
}

/** Associations and drafts only. OMP remains the sole transcript authority. */
export class ProjectChatRegistry {
  constructor(private readonly store: ProjectStore, private readonly identity: string, private readonly now = Date.now, private readonly claimDirectory = join(homedir(), '.omp', 'agent', 'project-chat-claims', identity), private readonly claimIdentity = identity) {}
  private claimPath(sessionId: string): string { return join(this.claimDirectory, `${createHash('sha256').update(sessionId).digest('hex')}.json`); }
  private claimOwner(sessionId: string): string | null {
    try { const value = record(JSON.parse(readFileSync(this.claimPath(sessionId), 'utf8'))); return typeof value.owner === 'string' ? value.owner : null; }
    catch { return null; }
  }
  private claim(sessionId: string, owner: string): void {
    mkdirSync(this.claimDirectory, { recursive: true, mode: 0o700 });
    const path = this.claimPath(sessionId);
    try {
      const fd = openSync(path, 'wx', 0o600);
      try { writeFileSync(fd, JSON.stringify({ owner, identity: this.claimIdentity })); }
      finally { closeSync(fd); }
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code !== 'EEXIST') throw error;
      if (this.claimOwner(sessionId) !== owner) throw new ChatError(409, 'session_already_assigned', 'Session already belongs to another project');
    }
  }
  private read(project: Project): StoredChat {
    const value = record(record(project.metadata[KEY])[this.identity]);
    if (!Object.keys(value).length) return { revision: 0, selected_session_id: null, main_session_id: null, sessions: [], draft: { text: '', revision: 0, queued_messages: [] } };
    if (!Number.isInteger(value.revision) || !Array.isArray(value.sessions) || !Number.isInteger(record(value.draft).revision) || typeof record(value.draft).text !== 'string') throw new ChatError(500, 'invalid_chat_registry', 'Stored chat registry is invalid');
    const state = structuredClone(value) as unknown as StoredChat;
    state.draft.queued_messages ??= [];
    state.main_session_id ??= null;
    if (typeof state.main_session_id !== 'string' && state.main_session_id !== null) throw new ChatError(500, 'invalid_chat_registry', 'Stored Main chat is invalid');
    return state;
  }
  private view(project: Project, state = this.read(project)): ChatState {
    return { project_id: project.project_id, scope_key: `${project.realm_id}:${this.identity}:${project.project_id}`, revision: state.revision, selected_session_id: state.selected_session_id, main_session_id: state.main_session_id, sessions: state.sessions, draft: state.draft };
  }
  async get(projectId: string): Promise<ChatState> { return this.view(await this.store.getProject(projectId)); }
  private async projects(): Promise<Project[]> {
    const all: Project[] = [];
    let cursor: string | undefined;
    do {
      const page = await this.store.listProjects(cursor, 100);
      all.push(...page.items);
      cursor = page.next_cursor ?? undefined;
    } while (cursor);
    return all;
  }
  async unassigned(projectId: string, candidates: Array<{ id: string; title?: string }>): Promise<Array<{ id: string; title?: string }>> {
    const current = await this.store.getProject(projectId);
    const owner = `${current.realm_id}:${projectId}`;
    const owned = new Set<string>();
    for (const project of await this.projects()) {
      const associations = record(record(project.metadata[KEY])[this.identity]);
      if (Array.isArray(associations.sessions)) {
        for (const session of associations.sessions) if (isRecord(session) && typeof session.id === 'string') owned.add(session.id);
      }
    }
    return candidates.filter(session => !owned.has(session.id) && (!this.claimOwner(session.id) || this.claimOwner(session.id) === owner));
  }
  async associate(projectId: string, expected: number, sessionId: string, verify: (sessionId: string) => Promise<boolean>): Promise<ChatState> {
    const current = await this.store.getProject(projectId);
    for (const project of await this.projects()) {
      const associations = record(record(project.metadata[KEY])[this.identity]);
      const sessions = Array.isArray(associations.sessions) ? associations.sessions : [];
      if (sessions.some(session => isRecord(session) && session.id === sessionId) && project.project_id !== projectId) throw new ChatError(409, 'session_already_assigned', 'Session already belongs to another project');
    }
    const owner = `${current.realm_id}:${projectId}`;
    if (this.claimOwner(sessionId) && this.claimOwner(sessionId) !== owner) throw new ChatError(409, 'session_already_assigned', 'Session already belongs to another project');
    if (!(await verify(sessionId))) throw new ChatError(404, 'session_not_found', 'OMP session no longer exists');
    this.claim(sessionId, owner);
    return this.change(projectId, state => {
      if (state.sessions.some(session => session.id === sessionId)) return false;
      if (state.revision !== expected) throw new ChatError(409, 'chat_conflict', 'Project chat changed; reload before attaching a session');
      state.sessions.push({ id: sessionId });
      state.selected_session_id = sessionId;
      state.revision++;
    });
  }
  private async change(projectId: string, edit: (state: StoredChat) => boolean | void): Promise<ChatState> {
    for (let attempt = 0; attempt < 12; attempt++) {
      const project = await this.store.getProject(projectId);
      const state = this.read(project);
      if (edit(state) === false) return this.view(project, state);
      const metadata = { ...project.metadata, [KEY]: { ...record(project.metadata[KEY]), [this.identity]: state } };
      try {
        return this.view(await this.store.updateProject(projectId, randomUUID(), project.version, undefined, metadata));
      } catch (error) {
        if (!(error instanceof ApiError && error.status === 409)) throw error;
      }
    }
    throw new ChatError(409, 'chat_conflict', 'Project changed repeatedly; reload and retry');
  }
  async select(projectId: string, expected: number, sessionId: string | null): Promise<ChatState> {
    return this.change(projectId, state => {
      if (state.revision !== expected) throw new ChatError(409, 'chat_conflict', 'Chat selection changed; reload before selecting');
      if (sessionId !== null && !state.sessions.some(s => s.id === sessionId)) throw new ChatError(403, 'session_not_owned', 'Session does not belong to this project');
      state.selected_session_id = sessionId;
      state.revision++;
    });
  }
  async setMain(projectId: string, expected: number, sessionId: string): Promise<ChatState> {
    return this.change(projectId, state => {
      if (state.revision !== expected) throw new ChatError(409, 'chat_conflict', 'Main chat changed; reload before choosing Main');
      if (!state.sessions.some(s => s.id === sessionId && !s.missing)) throw new ChatError(403, 'session_not_owned', 'Choose an available chat belonging to this project');
      state.main_session_id = sessionId;
      state.selected_session_id = sessionId;
      state.revision++;
    });
  }
  async rename(projectId: string, expected: number, sessionId: string, title: string): Promise<ChatState> {
    if (typeof title !== 'string' || !title.trim() || title.trim().length > 120) throw new ChatError(400, 'invalid_title', 'Chat title must contain 1 to 120 characters');
    const normalizedTitle = title.trim();
    return this.change(projectId, state => {
      if (state.revision !== expected) throw new ChatError(409, 'chat_conflict', 'Project chat changed; reload before renaming');
      const session = state.sessions.find(s => s.id === sessionId);
      if (!session) throw new ChatError(403, 'session_not_owned', 'Session does not belong to this project');
      if (session.title === normalizedTitle) return false;
      session.title = normalizedTitle;
      state.revision++;
    });
  }
  async draft(projectId: string, expected: number, text: string, queued?: QueuedChatMessage[]): Promise<ChatState> {
    return this.change(projectId, state => {
      const queuedMessages = queued ?? state.draft.queued_messages;
      // A duplicate save (including a lost acknowledgement) is already durable.
      // Acknowledge it without advancing the revision or weakening divergent CAS.
      if (state.draft.text === text && isDeepStrictEqual(state.draft.queued_messages, queuedMessages)) return false;
      if (state.draft.revision !== expected) throw new ChatError(409, 'draft_conflict', 'Draft changed in another client; retain local text and reload');
      state.draft = { text, revision: expected + 1, queued_messages: queuedMessages };
    });
  }
  async assertOwned(projectId: string, sessionId: string): Promise<void> {
    const state = await this.get(projectId);
    if (!state.sessions.some(s => s.id === sessionId)) throw new ChatError(403, 'session_not_owned', 'Session does not belong to this project');
  }
  async markSessionStarted(projectId: string, sessionId: string): Promise<void> {
    if (!isExternalAgentSession(sessionId)) return;
    await this.change(projectId, state => {
      const session = state.sessions.find(item => item.id === sessionId);
      if (!session || session.pending_first_message === false) return false;
      session.pending_first_message = false;
    });
  }
  async setSessionModel(projectId: string, sessionId: string, modelId: string): Promise<ChatState> {
    return this.change(projectId, state => {
      const session = state.sessions.find(item => item.id === sessionId && !item.missing);
      if (!session) throw new ChatError(403, 'session_not_owned', 'Session does not belong to this project');
      if (session.model_id === modelId) return false;
      session.model_id = modelId;
    });
  }
  async create(projectId: string, mode: 'ensure' | 'ensure_main' | 'new', operationId: string, create: () => Promise<unknown>, verify?: (sessionId: string) => Promise<boolean>, recoverSessionId?: string): Promise<ChatState & { operation_session_id?: string }> {
    let recoverableSession: { id: string } | null = null;
    if (mode !== 'new' && verify) {
      let current = await this.get(projectId);
      if (recoverSessionId) {
        const target = current.sessions.find(session => session.id === recoverSessionId);
        if (!target || target.pending_first_message === false) throw new ChatError(409, 'session_not_recoverable', 'Only an empty local-agent chat can be recovered automatically');
      }
      const candidates = (recoverSessionId
        ? current.sessions.filter(s => s.id === recoverSessionId)
        : mode === 'ensure_main' ? current.sessions.filter(s => s.id === current.main_session_id) : [...current.sessions])
        .sort((a, b) => Number(b.id === current.selected_session_id) - Number(a.id === current.selected_session_id));
      for (const candidate of candidates) {
        const preferred = mode === 'ensure_main' ? current.main_session_id === candidate.id : current.selected_session_id === candidate.id;
        if (candidate.missing) {
          if (candidate.id === recoverSessionId || (preferred && candidate.pending_first_message)) recoverableSession = { id: candidate.id };
          if (recoverableSession) break;
          continue;
        }
        if (await verify(candidate.id)) { recoverableSession = null; break; }
        if ((candidate.id === recoverSessionId || (preferred && candidate.pending_first_message)) && !recoverableSession) recoverableSession = { id: candidate.id };
        current = await this.change(projectId, state => {
          const session = state.sessions.find(s => s.id === candidate.id);
          if (session && !session.missing) { session.missing = true; state.revision++; }
        });
        if (recoverableSession) break;
      }
    }
    const owner = randomUUID();
    let claimed = false;
    let operationSessionId: string | null = null;
    const existing = await this.change(projectId, state => {
      claimed = false;
      const replay = state.operations?.[operationId];
      if (replay) { operationSessionId = replay; return false; }
      if (mode !== 'new' && !recoverableSession) {
        const existing = recoverSessionId
          ? state.sessions.find(s => !s.missing && s.id === recoverSessionId)
          : mode === 'ensure_main'
          ? state.sessions.find(s => !s.missing && s.id === (recoverSessionId ?? state.main_session_id))
          : state.sessions.find(s => !s.missing && s.id === state.selected_session_id) ?? state.sessions.find(s => !s.missing);
        if (existing) {
          operationSessionId = existing.id;
          state.operations = { ...state.operations, [operationId]: existing.id };
          if (!recoverSessionId) state.selected_session_id = existing.id;
          state.revision++;
          return;
        }
      }
      if (state.creating && state.creating.expires_at > this.now()) throw new ChatError(409, 'chat_creation_pending', 'A chat session is being created; retry shortly');
      state.creating = { operation_id: operationId, owner, expires_at: this.now() + 120_000, main_session_id: state.main_session_id };
      claimed = true;
    });
    if (!claimed) return { ...existing, operation_session_id: operationSessionId ?? existing.selected_session_id ?? undefined };
    try {
      // A lost reply may mean OMP created a session. Never infer or delete that session.
      const result = record(await create());
      const sessionId = result.sessionId;
      if (typeof sessionId !== 'string' || !sessionId) throw new ChatError(502, 'invalid_session', 'OMP did not return a session ID');
      const modelId = typeof record(result.models)?.currentModelId === 'string' ? record(result.models)!.currentModelId as string : undefined;
      const targetProject = await this.store.getProject(projectId);
      this.claim(sessionId, `${targetProject.realm_id}:${projectId}`);
      const completed = await this.change(projectId, state => {
        if (state.creating?.owner !== owner) throw new ChatError(409, 'chat_claim_expired', 'Session creation claim expired; the unassigned OMP session is preserved');
        const recoveryIndex = recoverableSession
          ? state.sessions.findIndex(session => session.id === recoverableSession?.id && session.missing)
          : -1;
        if (recoveryIndex >= 0 && recoverableSession) {
          const entry = { ...state.sessions[recoveryIndex]! };
          delete entry.missing;
          state.sessions[recoveryIndex] = { ...entry, id: sessionId, pending_first_message: true, ...(modelId ? { model_id: modelId } : {}) };
          state.operations = Object.fromEntries(Object.entries(state.operations ?? {}).map(([id, previousSessionId]) => [
            id,
            previousSessionId === recoverableSession.id ? sessionId : previousSessionId,
          ]));
          state.draft.queued_messages = state.draft.queued_messages.map(message =>
            message.session_id === recoverableSession?.id ? { ...message, session_id: sessionId } : message,
          );
          if (state.selected_session_id === recoverableSession.id) state.selected_session_id = sessionId;
          if (state.main_session_id === recoverableSession.id) state.main_session_id = sessionId;
        } else {
          state.sessions.push({ id: sessionId, ...(modelId ? { model_id: modelId } : {}), ...(isExternalAgentSession(sessionId) ? { pending_first_message: true } : {}) });
        }
        // New promotes itself unless the user chose a different Main after this claim began.
        if (mode === 'new') {
          state.selected_session_id = sessionId;
          if (state.creating.main_session_id === state.main_session_id) state.main_session_id = sessionId;
        } else if (!recoverSessionId && (mode !== 'ensure_main' || !state.main_session_id)) {
          state.selected_session_id = sessionId;
        }
        state.operations = { ...state.operations, [operationId]: sessionId };
        delete state.creating;
        state.revision++;
      });
      return { ...completed, operation_session_id: sessionId };
    } finally {
      await this.change(projectId, state => {
        if (state.creating?.operation_id !== operationId) return false;
        delete state.creating;
      });
    }
  }
}
