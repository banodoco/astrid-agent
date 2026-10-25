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

  constructor(private readonly agent: ExternalAgent, private readonly context: string, private readonly cliPath: string, options: AcpProcessHostOptions) {
    const callbacks = options.callbacks;
    super({ ...options, callbacks: { ...callbacks, onNotification: value => {
      const params = record(value.params);
      callbacks?.onNotification?.(params && typeof params.sessionId === 'string'
        ? { ...value, params: { ...params, sessionId: `${agent}:${params.sessionId}` } }
        : value);
    } } });
  }

  private nativeParams(params: unknown): unknown {
    const input = record(params);
    if (!input || typeof input.sessionId !== 'string') return params;
    if (sessionAgentId(input.sessionId) !== this.agent) {
      throw new AcpProcessHostError('This saved chat belongs to a different agent.', { code: 'acp_unavailable' });
    }
    return { ...input, sessionId: input.sessionId.slice(this.agent.length + 1) };
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
    const result = await super.request<unknown>(method, input);
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
  // A fresh snapshot per host; failure or timeout means no preferences block, never a failed launch.
  const preferences = (await (options.agentContextForCommand ?? resolveAstridAgentContext)(options.command ?? ASTRID_ACP_COMMAND, env).catch(() => '')).trim();
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
