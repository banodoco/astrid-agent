import { isAbsolutePath } from './paths.ts';
import {
  AcpProcessHost,
  AcpProcessHostError,
  commandForPlatform,
  type AcpProcessHostCallbacks,
  type AcpProcessSpawner,
} from './acpProcessHost.ts';

/** Resolve the real user-machine Astrid launcher; it selects the branded OMP build itself. */
export const ASTRID_ACP_COMMAND = 'astrid';
export const ASTRID_ACP_MODE_ARG = '--mode=acp';
export const ASTRID_ACP_PROFILE_FILE_ARG = '--agent-profile-file';
export const ASTRID_ACP_AGENT_CONTEXT_ARG = '--agent-context';
export const ASTRID_ACP_AGENT_IDENTITY = 'astrid';
/** Stable integration rules; the selected project itself is supplied per turn. */
export const ASTRID_ACP_CONTEXT_SYSTEM_PROMPT = [
  'When an Astrid App editor prompt contains <astrid_editor_context>, treat that structured block as the authoritative editor context for that turn.',
  'Use the Astrid runtime tools available in the session; do not infer project, timeline, or asset state from the filesystem. The context block is a compact scoped read, not a full asset registry.',
  'The context IDs are resource references, not authorization. Preserve the selected project/timeline scope and use the document version returned by the runtime when saving.',
  'The context can change between turns, so never assume a prior project or timeline remains selected.',
  'Only for a small timeline edit on the selected timeline, skip startup maintenance, skill discovery, ls/grep workspace scans, import-location checks, and exploratory one-off Python probes, and use the compact runtime reads below instead. Generation and local setup requests may read skills and probe the local machine as needed.',
  'Use the compact runtime read first: astrid-tools timelines show --summary --project <project-slug> <timeline-ref>. It returns the timeline version, tracks, clip ids, start/duration/end timing, and counts without dumping prompts, effect parameters, media URLs, or the full asset registry.',
  'Every runtime command must include explicit --project <project-slug> and the timeline reference; do not rely on implicit current-project selection for an edit.',
  'For a single clip timing change, use the working-copy route: astrid-tools timelines checkout --project <project-slug> <timeline-ref>, then astrid-tools timelines edit --project <project-slug> <timeline-ref> --clip <clip-id> --at <seconds>, then astrid-tools timelines publish --project <project-slug> <timeline-ref> -m "<what changed>".',
  'edit --at starts the clip at the given time and keeps its length, so the clip end moves with it. Say so in the reply when the user asked for a timing change.',
  'If the user says only "make it start at" and the intent is ambiguous, make the change with --at and say that the clip end moved with it.',
  'For more complex edits, do one bounded summary/full read, make one in-memory patch, perform one versioned save, and do at most one focused verification read. Avoid repeating equivalent inspections or saving one field at a time.',
  'Custom effect, animation and transition code lives only in ordinary V3 pack files. The selected project_authoring context gives lazy init/inspect/admit commands and canonical paths. Do not embed source in timeline JSON or assistant operation envelopes; embedded historical source is migration-only and is not executed.',
  'Inspect the existing element/placement, read and edit its ordinary files, validate/admit a revision, then update the SAME clip through the supported exact-head CLI/SDK publication and return its saved-head receipt. Preserve params and timing except explicit user changes. A new admitted revision never silently replaces old saved pins.',
  'Prefer project-specific Drafts for new experimental code; create that persistent pack lazily when needed. Project preference Markdown remains user-owned; generated pack discovery is separate. Do not invent source directories or replace user instructions.',
  'A generated Remotion element is not a live-scene placement. A Three.js live scene is a world: before creating or editing one, read the rendering skill\'s references/worlds-authoring.md. Worlds are always explorable (window.astridScene.explore is required) and shots live within them. If a command is unavailable, report its actual error. Completion requires an actual Runtime saved-head receipt; emitted JSON and local source edits are not successful publication.',
].join('\n');

type HostRuntime = {
  readonly env?: Readonly<Record<string, string | undefined>>;
  readonly getBuiltinModule?: (id: string) => unknown;
};

type HostChildProcess = {
  readonly spawnSync?: (
    command: string,
    args: readonly string[],
    options: {
      readonly encoding: 'utf8';
      readonly env: Record<string, string | undefined>;
      readonly stdio: readonly ['ignore', 'pipe', 'pipe'];
      readonly timeout: number;
      readonly killSignal: 'SIGKILL';
      readonly windowsVerbatimArguments?: boolean;
    },
  ) => {
    readonly status: number | null;
    readonly signal?: NodeJS.Signals | null;
    readonly stdout?: string | Uint8Array;
    readonly stderr?: string | Uint8Array;
    readonly error?: Error;
  };
};

/** Ask the installed Astrid command for its immutable packaged profile resource. */
export function resolveAstridAcpSystemPromptFile(
  command = ASTRID_ACP_COMMAND,
  env: Record<string, string | undefined> = {},
): string {
  const runtime = (globalThis as { readonly process?: HostRuntime }).process;
  const childProcess = runtime?.getBuiltinModule?.('node:child_process') as HostChildProcess | undefined;
  if (typeof childProcess?.spawnSync !== 'function') {
    throw new AcpProcessHostError(
      'Astrid ACP profile lookup is available only in the user-machine Node host.',
      { code: 'acp_unavailable' },
    );
  }
  const invocation = commandForPlatform(command, [ASTRID_ACP_PROFILE_FILE_ARG], process.platform);
  const result = childProcess.spawnSync(invocation.command, invocation.args, {
    encoding: 'utf8',
    env: { ...(runtime?.env ?? {}), ...env },
    stdio: ['ignore', 'pipe', 'pipe'],
    timeout: 5_000,
    killSignal: 'SIGKILL',
    windowsVerbatimArguments: invocation.windowsVerbatimArguments,
  });
  const path = typeof result.stdout === 'string'
    ? result.stdout.trim()
    : result.stdout ? new TextDecoder().decode(result.stdout).trim() : '';
  if (result.error || result.status !== 0 || !path) {
    const errorCode = result.error && 'code' in result.error ? Reflect.get(result.error, 'code') : undefined;
    const diagnostic = [
      result.status !== null ? `exit=${result.status}` : undefined,
      result.signal ? `signal=${result.signal}` : undefined,
      typeof errorCode === 'string' || typeof errorCode === 'number' ? `error=${errorCode}` : undefined,
      result.error && Reflect.get(result.error, 'killed') === true ? 'killed=true' : undefined,
    ].filter(Boolean).join(', ');
    throw new AcpProcessHostError(
      `Astrid ACP packaged profile lookup failed${diagnostic ? ` (${diagnostic})` : ''}.`,
      { code: 'acp_unavailable', cause: result.error },
    );
  }
  return requireAbsolute(path, 'system prompt file');
}

const AGENT_CONTEXT_TIMEOUT_MS = 5_000;
const AGENT_CONTEXT_MAX_BYTES = 64 * 1024;

/**
 * Ask the installed Astrid command for the user's rendered saved preferences.
 * The command always exits 0 and prints nothing when there is nothing to add.
 * Any failure, timeout or oversized output yields an empty string; this never throws.
 */
export function resolveAstridAgentContext(
  command = ASTRID_ACP_COMMAND,
  env: Record<string, string | undefined> = {},
): Promise<string> {
  return new Promise<string>(resolve => {
    try {
      const runtime = (globalThis as { readonly process?: HostRuntime }).process;
      const childProcess = runtime?.getBuiltinModule?.('node:child_process') as typeof import('node:child_process') | undefined;
      if (typeof childProcess?.spawn !== 'function') return resolve('');
      const chunks: Buffer[] = [];
      let size = 0;
      let settled = false;
      const invocation = commandForPlatform(command, [ASTRID_ACP_AGENT_CONTEXT_ARG], process.platform);
      const child = childProcess.spawn(invocation.command, invocation.args, {
        env: { ...(runtime?.env ?? {}), ...env } as NodeJS.ProcessEnv,
        windowsVerbatimArguments: invocation.windowsVerbatimArguments,
        stdio: ['ignore', 'pipe', 'ignore'],
      });
      const finish = (value: string): void => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve(value);
      };
      const timer = setTimeout(() => {
        try { child.kill('SIGKILL'); } catch { /* Already gone. */ }
        finish('');
      }, AGENT_CONTEXT_TIMEOUT_MS);
      child.stdout?.on('data', (chunk: Buffer) => {
        size += chunk.length;
        if (size > AGENT_CONTEXT_MAX_BYTES) {
          try { child.kill('SIGKILL'); } catch { /* Already gone. */ }
          finish('');
          return;
        }
        chunks.push(chunk);
      });
      child.on('error', () => finish(''));
      child.on('close', code => finish(code === 0 ? Buffer.concat(chunks).toString('utf8').trim() : ''));
    } catch {
      resolve('');
    }
  });
}

type HostFileSystem = {
  readonly statSync?: (path: string) => { isFile: () => boolean };
};

function isRegularFile(path: string): boolean {
  const runtime = (globalThis as { readonly process?: HostRuntime }).process;
  if (typeof runtime?.getBuiltinModule !== 'function') return false;
  const fs = runtime.getBuiltinModule('node:fs') as HostFileSystem | undefined;
  try {
    return fs?.statSync?.(path).isFile() === true;
  } catch {
    return false;
  }
}

function requireAbsolute(value: string, name: string): string {
  if (!value.trim() || !isAbsolutePath(value)) {
    throw new AcpProcessHostError(
      `Astrid ACP ${name} must be an explicit absolute path.`,
      { code: 'acp_unavailable' },
    );
  }
  return value;
}

export type AstridAcpLauncherOptions = {
  /** Caller-owned project cwd; never inferred from the browser. */
  readonly cwd: string;
  /** Optional isolated Astrid/OMP profile; omitted uses the machine's normal profile. */
  readonly profile?: string;
  /**
   * Optional caller-owned OMP session directory override. When omitted, OMP
   * uses its canonical cwd-derived session directory, which ACP can discover
   * again in a fresh process.
   */
  readonly sessionDir?: string;
  /** Optional assertion of the installed profile path; custom base-profile overrides are unsupported. */
  readonly systemPromptFile?: string;
  /** Defaults to the installed Astrid launcher selected on this machine. */
  readonly command?: string;
  readonly env?: Record<string, string | undefined>;
  readonly requestTimeoutMs?: number;
  readonly callbacks?: AcpProcessHostCallbacks;
  /** Test seam for prompt custody; production uses the host filesystem. */
  readonly fileIsRegularFile?: (path: string) => boolean;
  /** Test seam for the read-only installed-profile lookup. */
  readonly profileFileForCommand?: (command: string, env?: Record<string, string | undefined>) => string;
  /** Test seam for the user's saved-preferences block (`astrid --agent-context`); resolves to '' when absent. */
  readonly agentContextForCommand?: (command: string, env?: Record<string, string | undefined>) => Promise<string>;
  /** Test seam; production uses AcpProcessHost's explicit Node spawner. */
  readonly spawnProcess?: AcpProcessSpawner;
};

/**
 * Build the one canonical Astrid ACP host. This is intentionally a host-only
 * factory: browser code cannot resolve the prompt or spawn a process. Astrid
 * owns named-agent discovery, its system prompt, branded OMP selection, and
 * identity; Astrid App only asks that launcher to expose ACP over stdio. The
 * optional sessionDir/profile are forwarded as explicit OMP launch flags.
 */
export function createAstridAcpProcessHost(options: AstridAcpLauncherOptions): AcpProcessHost {
  const cwd = requireAbsolute(options.cwd, 'cwd');
  const command = options.command ?? ASTRID_ACP_COMMAND;
  const profile = options.profile?.trim() || undefined;
  const sessionDir = options.sessionDir === undefined
    ? undefined
    : requireAbsolute(options.sessionDir, 'session directory');
  const systemPromptFile = requireAbsolute(
    (options.profileFileForCommand ?? resolveAstridAcpSystemPromptFile)(command, options.env),
    'system prompt file',
  );
  if(options.systemPromptFile!==undefined&&requireAbsolute(options.systemPromptFile,'system prompt file')!==systemPromptFile){
    throw new AcpProcessHostError('Custom base-profile overrides are unsupported by hosted Astrid ACP. Use the selected package profile and appended ASTRID_SYSTEM_PROMPT or ASTRID_SYSTEM_PROMPT_FILE instructions.',{code:'acp_unavailable'});
  }
  const promptCheck = options.fileIsRegularFile ?? isRegularFile;
  if (!promptCheck(systemPromptFile)) {
    throw new AcpProcessHostError(
      `Astrid ACP system prompt is unavailable: ${systemPromptFile}`,
      { code: 'acp_unavailable' },
    );
  }
  const args = [
    ASTRID_ACP_MODE_ARG,
    ...(profile ? ['--profile', profile] : []),
    ...(sessionDir ? ['--session-dir', sessionDir] : []),
  ];
  const env = {
    ...(options.env ?? {}),
    ASTRID_HOST_EXECUTION_PROFILE: 'installed',
    OMP_AGENT_IDENTITY: ASTRID_ACP_AGENT_IDENTITY,
    ASTRID_AGENT_IDENTITY: ASTRID_ACP_AGENT_IDENTITY,
    ASTRID_SYSTEM_PROMPT: options.env?.ASTRID_SYSTEM_PROMPT ?? ASTRID_ACP_CONTEXT_SYSTEM_PROMPT,
  };
  return new AcpProcessHost({
    command,
    args,
    cwd,
    env,
    requestTimeoutMs: options.requestTimeoutMs,
    callbacks: options.callbacks,
    spawnProcess: options.spawnProcess,
  });
}
