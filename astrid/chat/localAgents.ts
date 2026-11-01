import { z } from 'zod';
import { readFileSync } from 'node:fs';
import { dirname, isAbsolute, join, win32 } from 'node:path';

export const localAgentIdSchema = z.enum(['astrid', 'claude-code', 'codex', 'hermes']);
export type LocalAgentId = z.infer<typeof localAgentIdSchema>;
export const LOCAL_AGENT_IDS = localAgentIdSchema.options;
export const EXTERNAL_AGENT_IDS = LOCAL_AGENT_IDS.filter((id): id is Exclude<LocalAgentId, 'astrid'> => id !== 'astrid');

export function formatAstridSignInLine(root: string, platform = process.platform, shimWritten = false): string {
  if (shimWritten) return 'Run `astrid`, then enter `/login` in Astrid.';
  if (platform === 'win32') {
    const launcher = win32.join(root, 'run-astrid.cmd').replaceAll("'", "''");
    return `Run \`& '${launcher}'\`, then enter \`/login\` in Astrid.`;
  }
  const launcher = join(root, 'run-astrid').replaceAll("'", "'\"'\"'");
  return `Run \`'${launcher}'\`, then enter \`/login\` in Astrid.`;
}

export function astridSignInLine(env: NodeJS.ProcessEnv = process.env, platform = process.platform): string {
  const paths = platform === 'win32' ? win32 : { dirname, isAbsolute, join };
  const configuredRoot = env.ASTRID_INSTALL_PROGRAM_ROOT;
  const servicePython = env.ASTRID_SERVICE_PYTHON;
  const fromService = servicePython && paths.isAbsolute(servicePython)
    ? [1, 2, 3, 4, 5].reduce(path => paths.dirname(path), servicePython)
    : undefined;
  const home = env.USERPROFILE || env.HOME || '.';
  const defaultRoot = platform === 'win32'
    ? paths.join(env.LOCALAPPDATA || paths.join(home, 'AppData', 'Local'), 'Astrid')
    : paths.join(env.XDG_DATA_HOME || paths.join(home, '.local', 'share'), 'astrid');
  const root = configuredRoot && paths.isAbsolute(configuredRoot) ? configuredRoot
    : fromService || defaultRoot;
  let shimWritten = false;
  try {
    shimWritten = typeof JSON.parse(readFileSync(paths.join(root, 'install.json'), 'utf8')).command_shim === 'string';
  } catch { /* Older installs and clashes retain the absolute launcher command. */ }
  return formatAstridSignInLine(root, platform, shimWritten);
}

/** One shared table for the service's agent names and the exact recovery lines shown in the menu. */
export const LOCAL_AGENT_TABLE: Record<LocalAgentId, {
  name: string;
  signInLine: string;
  notInstalledLine?: string;
  windowsNotInstalledLine?: string;
}> = {
  astrid: { name: 'Astrid', get signInLine() { return astridSignInLine(); } },
  'claude-code': {
    name: 'Claude Code',
    signInLine: 'Run `claude auth login`, then come back.',
    notInstalledLine: 'Install with `curl -fsSL https://claude.ai/install.sh | bash`',
    windowsNotInstalledLine: 'In PowerShell, run `irm https://claude.ai/install.ps1 | iex`',
  },
  hermes: {
    name: 'Hermes',
    signInLine: 'Run `hermes setup`, then come back.',
    notInstalledLine: 'Install with `curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash`',
    windowsNotInstalledLine: 'Install with `iex (irm https://hermes-agent.nousresearch.com/install.ps1)`',
  },
  codex: {
    name: 'Codex',
    signInLine: 'Run `codex login`, then come back.',
    notInstalledLine: 'Install with `curl -fsSL https://chatgpt.com/codex/install.sh | sh`',
    windowsNotInstalledLine: 'In PowerShell, run `irm https://chatgpt.com/codex/install.ps1 | iex`',
  },
};

export function agentCommand(line: string): string {
  return line.match(/`([^`]+)`/)![1]!;
}

export function astridInstallCommand(platform = process.platform): string {
  return platform === 'win32' ? 'irm https://astrid.haus/install.ps1 | iex' : 'curl -fsSL https://astrid.haus/install | sh';
}

export function localAgentSteps(id: LocalAgentId, state: LocalAgentChoice['state'], platform = process.platform): Array<{ kind: 'install' | 'sign-in'; command: string; label: string }> {
  if (state === 'ready') return [];
  const definition = LOCAL_AGENT_TABLE[id];
  if (id === 'astrid') return [
    { kind: 'sign-in', command: agentCommand(definition.signInLine), label: 'Start Astrid in a terminal' },
    { kind: 'sign-in', command: '/login', label: 'Then sign in' },
  ];
  const installLine = platform === 'win32' ? definition.windowsNotInstalledLine ?? definition.notInstalledLine! : definition.notInstalledLine!;
  return [
    ...(state === 'not-installed' ? [{ kind: 'install' as const, command: agentCommand(installLine), label: `Install ${definition.name}` }] : []),
    { kind: 'sign-in', command: agentCommand(definition.signInLine), label: 'Sign in' },
  ];
}

export const thoughtLevelSchema = z.object({
  configId: z.string().min(1),
  name: z.string(),
  currentValue: z.string(),
  options: z.array(z.object({ value: z.string().min(1), name: z.string(), description: z.string().nullable().optional() })),
  recommendedValue: z.string().optional(),
});
export type AgentThoughtLevel = z.infer<typeof thoughtLevelSchema>;

export function defaultThoughtLevel(level: AgentThoughtLevel): string {
  return level.recommendedValue ?? level.options.find(option => option.value === 'default')?.value ?? level.currentValue;
}

const agentModelSchema = z.object({
  modelId: z.string().min(1),
  name: z.string().min(1),
  description: z.string().nullable().optional(),
  thoughtLevel: thoughtLevelSchema.optional(),
});

export const localAgentCatalogSchema = z.object({
  defaultAgent: localAgentIdSchema,
  agents: z.array(z.object({
    id: localAgentIdSchema,
    name: z.string(),
    state: z.enum(['ready', 'sign-in', 'not-installed']),
    line: z.string().nullable(),
    signInLine: z.string().optional(),
    signIn: z.enum(['signed-in', 'signed-out', 'unknown']).optional(),
    steps: z.array(z.object({ kind: z.enum(['install', 'sign-in']), command: z.string(), label: z.string() })).optional(),
    creditLine: z.string().optional(),
    creditUrl: z.string().optional(),
    terminal: z.object({ command: z.string(), skillSynced: z.boolean(), detail: z.string().optional() }).optional(),
    models: z.array(agentModelSchema),
    defaultModel: z.string().nullable(),
  })),
});
export type LocalAgentChoice = Pick<z.infer<typeof localAgentCatalogSchema>['agents'][number], 'id' | 'name' | 'state' | 'line' | 'signIn' | 'steps'>;

export const localAgentModelsSchema = z.object({
  availableModels: z.array(agentModelSchema),
  currentModelId: z.string(),
  modelConfigId: z.string().optional(),
  thoughtLevel: thoughtLevelSchema.optional(),
});
export type LocalAgentModels = z.infer<typeof localAgentModelsSchema>;

/** Astrid's existing opaque IDs remain unchanged; other agents have distinct stores. */
export function sessionAgentId(sessionId: string): LocalAgentId {
  if (sessionId.startsWith('claude-code:')) return 'claude-code';
  if (sessionId.startsWith('codex:')) return 'codex';
  if (sessionId.startsWith('hermes:')) return 'hermes';
  return 'astrid';
}

export function isExternalAgentSession(sessionId: string, agent?: typeof EXTERNAL_AGENT_IDS[number]): boolean {
  return agent ? sessionId.startsWith(`${agent}:`) : EXTERNAL_AGENT_IDS.some(id => sessionId.startsWith(`${id}:`));
}

