import { readFileSync } from 'node:fs';
import { dirname } from 'node:path';
import { AcpProcessHost, AcpProcessHostError, type AcpProcessHostOptions } from './acpProcessHost.ts';
import { ASTRID_ACP_COMMAND, ASTRID_ACP_CONTEXT_SYSTEM_PROMPT, resolveAstridAcpSystemPromptFile, resolveAstridAgentContext, type AstridAcpLauncherOptions } from './astridAcpLauncher.ts';
import { localAgentSteps, sessionAgentId, type LocalAgentId } from './localAgents.ts';
import { discoverLocalAgents, localAgentLaunch } from './astrid-local-agents.ts';

type ExternalAgent = Exclude<LocalAgentId, 'astrid'>;
function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null;
}

/** Render the packaged profile as the terminal launcher does: package root filled in, OMP frontmatter dropped. */
export function renderAgentProfile(text: string, profilePath: string): string {
  const rendered = text.replaceAll('{{ASTRID_PACKAGE_ROOT}}', dirname(dirname(profilePath)));
  if (!rendered.startsWith('---\n')) return rendered;
  const end = rendered.indexOf('\n---\n', 4);
  if (end < 0) throw new AcpProcessHostError(`Astrid agent profile has an unterminated frontmatter block: ${profilePath}`, { code: 'acp_unavailable' });
  return rendered.slice(end + 5);
}

/** Namespace only the wire ID. The adapter retains its own opaque session ID and transcript. */
class LocalAgentAcpHost extends AcpProcessHost {
  private supportsSystemPrompt = false;
  private readonly needsGrounding = new Set<string>();
  /**
   * Harness chats that had to be replaced because their session was gone: the
   * chat keeps its identity while requests follow the replacement session.
   */
  private readonly replacedSessions = new Map<string, string>();

  constructor(private readonly agent: ExternalAgent, private readonly context: string, private readonly cliPath: string, options: AcpProcessHostOptions) {
    const callbacks = options.callbacks;
    super({ ...options, callbacks: { ...callbacks, onNotification: value => {
      const params = record(value.params);
      const served = params && typeof params.sessionId === 'string' ? params.sessionId : null;
      const original = served !== null
        ? [...this.replacedSessions.entries()].find(([, actual]) => actual === served)?.[0] ?? served
        : null;
      callbacks?.onNotification?.(params && original !== null
        ? { ...value, params: { ...params, sessionId: `${agent}:${original}` } }
        : value);
    } } });
  }

  private nativeParams(params: unknown): unknown {
    const input = record(params);
    if (!input || typeof input.sessionId !== 'string') return params;
    if (sessionAgentId(input.sessionId) !== this.agent) {
      throw new AcpProcessHostError('This saved chat belongs to a different agent.', { code: 'acp_unavailable' });
    }
    const native = input.sessionId.slice(this.agent.length + 1);
    return { ...input, sessionId: this.replacedSessions.get(native) ?? native };
  }

  override async request<T = unknown>(method: string, params?: unknown): Promise<T> {
    let input = this.nativeParams(params);
    const opensSession = ['session/new', 'session/load', 'session/resume', 'session/fork'].includes(method);
    if (opensSession) {
      if (this.agent === 'claude-code') {
        // Use the same installed Claude CLI that supplied its sign-in status.
        const meta = record(record(input)?._meta);
        const claudeCode = record(meta?.claudeCode);
        input = { ...record(input), _meta: { ...meta, claudeCode: { ...claudeCode, options: {
          ...record(claudeCode?.options), pathToClaudeCodeExecutable: this.cliPath,
        } } } };
      }
      if (this.supportsSystemPrompt) {
        // Claude's adapter also applies this when loading, resuming, or forking a session.
        input = { ...record(input), systemPrompt: { mode: 'append', content: [{ type: 'text', text: this.context }] } };
      }
    }
    if (method === 'session/prompt') {
      const prompt = record(input);
      if (!Array.isArray(prompt?.prompt)) throw new Error('session/prompt requires a prompt array');
      if (typeof prompt.sessionId === 'string' && this.needsGrounding.delete(prompt.sessionId)) {
        input = { ...prompt, prompt: [...prompt.prompt, { type: 'text', text: `\n<astrid_agent_context>\n${this.context}\n</astrid_agent_context>` }] };
      }
    }
    // The harness serves resume/list/close but not load, answering -32601 to it,
    // so opening a saved chat resumes that session instead.
    const nativeMethod = method === 'session/load' && this.agent === 'dsh' ? 'session/resume' : method;
    let result: unknown;
    try {
      result = await super.request<unknown>(nativeMethod, input);
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      const native = record(input)?.sessionId;
      if (nativeMethod === 'session/resume' && typeof native === 'string') {
        // Resuming the session this process already owns is the same open request,
        // so report it as opened rather than failing the person's saved chat.
        if (/already active/i.test(message)) return { sessionId: `${this.agent}:${native}` } as T;
        // The harness also refuses chats its store no longer holds (a rebuilt
        // instance home, or a chat another harness install started). A saved chat
        // must still open, so this conversation continues in a fresh session.
        if (/(not resumable|not found|unknown session|no such session)/i.test(message)) {
          const { sessionId: _stale, ...opened } = record(input) ?? {};
          const fresh = await super.request<unknown>('session/new', { ...opened, mcpServers: Array.isArray(opened.mcpServers) ? opened.mcpServers : [] });
          const freshId = record(fresh)?.sessionId;
          if (typeof freshId !== 'string') return fresh as T;
          // A brand-new session still needs the first-message grounding block.
          if (!this.supportsSystemPrompt) this.needsGrounding.add(freshId);
          // The saved chat keeps its own id; its traffic follows the new session.
          this.replacedSessions.set(native, freshId);
          return { ...record(fresh), sessionId: `${this.agent}:${native}` } as T;
        }
      }
      throw error;
    }
    if (method === 'initialize') {
      const capabilities = record(record(result)?.agentCapabilities);
      this.supportsSystemPrompt = Boolean(record(capabilities?.sessionCapabilities)?.systemPrompt);
    }
    if (method === 'session/new' && !this.supportsSystemPrompt) {
      const sessionId = record(result)?.sessionId;
      // Only chats created by this host need a first-message block; loaded/resumed
      // chats (including older chats) already retain grounding in their transcript.
      if (typeof sessionId === 'string') this.needsGrounding.add(sessionId);
    }
    const namespaced = (value: unknown): unknown => {
      const item = record(value);
      if (!item) return value;
      if (typeof item.sessionId === 'string') return { ...item, sessionId: `${this.agent}:${item.sessionId}` };
      if (typeof item.id === 'string') return { ...item, id: `${this.agent}:${item.id}` };
      return value;
    };
    if (method === 'session/list') {
      if (Array.isArray(result)) return result.map(namespaced) as T;
      const root = record(result);
      if (Array.isArray(root?.sessions)) return { ...root, sessions: root.sessions.map(namespaced) } as T;
    }
    return (opensSession ? namespaced(result) : result) as T;
  }

  override async cancel(params: unknown): Promise<void> {
    return super.cancel(this.nativeParams(params));
  }
}

export async function createLocalAgentAcpProcessHost(agent: ExternalAgent, options: AstridAcpLauncherOptions): Promise<AcpProcessHost> {
  const env = { ...process.env, ...options.env };
  const choice = (await discoverLocalAgents({ env, cwd: options.cwd })).find(item => item.id === agent)!;
  if (choice.state === 'not-installed') throw new AcpProcessHostError(`${choice.name} is not installed on PATH.`, { code: 'agent_not_installed', next_action: localAgentSteps(agent, 'not-installed')[0]!.command });
  if (choice.state === 'sign-in') throw new AcpProcessHostError(`${choice.name} sign-in is required. ${choice.line}`, { code: 'agent_sign_in_required', next_action: localAgentSteps(agent, 'sign-in')[0]!.command });
  const launch = await localAgentLaunch(agent, env);
  const profilePath = (options.profileFileForCommand ?? resolveAstridAcpSystemPromptFile)(options.command ?? ASTRID_ACP_COMMAND, env);
  const profile = renderAgentProfile(readFileSync(profilePath, 'utf8'), profilePath);
  // A fresh snapshot per host; a failed lookup means no preferences block.
  let preferences = '';
  try {
    preferences = (await (options.agentContextForCommand ?? resolveAstridAgentContext)(options.command ?? ASTRID_ACP_COMMAND, env)).trim();
  } catch (error) {
    console.warn(`Astrid agent preferences lookup failed: ${error instanceof Error ? error.message : String(error)}`);
  }
  return new LocalAgentAcpHost(agent, [
    profile,
    'You are the selected local agent in the Astrid App. Use Astrid skills and its installed CLI tools for creative work. Load the relevant Astrid SKILL.md when needed, as installed by `python3 -m astrid.skills.cli sync`. Do not run skill sync or change global agent settings during a chat.',
    options.env?.ASTRID_SYSTEM_PROMPT ?? ASTRID_ACP_CONTEXT_SYSTEM_PROMPT,
    ...(preferences ? [preferences] : []),
  ].join('\n\n'), launch.cliPath, {
    command: launch.command, args: launch.args, cwd: options.cwd,
    env: { ...env, ...launch.env }, callbacks: options.callbacks,
    spawnProcess: options.spawnProcess, requestTimeoutMs: options.requestTimeoutMs,
  });
}
