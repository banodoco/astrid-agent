import { existsSync, realpathSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { discoveryProbe } from './discoveryProbe.ts';
import { agentCommand, astridSignInLine, LOCAL_AGENT_TABLE, type LocalAgentId } from './localAgents.ts';

type ExternalAgent = Exclude<LocalAgentId, 'astrid'>;
type SkillStatus = { skillSynced: boolean; detail?: string };
const HARNESSES = { 'claude-code': 'claude', codex: 'codex', hermes: 'hermes' } as const;

/** One service-owned attempt per harness, including failed and timed-out attempts. */
export class LocalAgentSkills {
  private readonly attempts = new Map<ExternalAgent, Promise<SkillStatus>>();
  private readonly results = new Map<ExternalAgent, SkillStatus>();

  constructor(private readonly env: NodeJS.ProcessEnv = process.env, private readonly cwd?: string, private readonly timeoutMs = 15_000) {}

  private check(agent: ExternalAgent): SkillStatus {
    const harness = HARNESSES[agent];
    const home = (process.platform === 'win32' ? this.env.USERPROFILE : this.env.HOME) || homedir();
    const harnessHome = (agent === 'claude-code' ? this.env.CLAUDE_CONFIG_DIR : agent === 'codex' ? this.env.CODEX_HOME : this.env.HERMES_HOME) || join(home, `.${harness}`);
    const skills = join(harnessHome, 'skills');
    const view = join(this.env.ASTRID_STATE_HOME || this.env.XDG_STATE_HOME || join(home, '.local', 'state'), 'astrid', 'skills', harness);
    try {
      if (realpathSync(join(skills, 'astrid')) === realpathSync(view) && existsSync(join(view, 'SKILL.md'))) return { skillSynced: true };
    } catch { /* Missing or dangling links are unsynced. */ }
    return { skillSynced: false, detail: existsSync(skills)
      ? `${LOCAL_AGENT_TABLE[agent].name}'s Astrid skill isn't synced.`
      : `${LOCAL_AGENT_TABLE[agent].name}'s skills folder wasn't found.` };
  }

  terminal(agent: LocalAgentId): { command: string } & SkillStatus {
    if (agent === 'astrid') return { command: agentCommand(astridSignInLine(this.env)), skillSynced: true };
    return { command: agent === 'claude-code' ? 'claude' : agent, ...(this.results.get(agent) ?? this.check(agent)) };
  }

  sync(agent: ExternalAgent): Promise<SkillStatus> {
    const existing = this.attempts.get(agent);
    if (existing) return existing;
    const attempt = this.run(agent).then(result => { this.results.set(agent, result); return result; });
    this.attempts.set(agent, attempt);
    return attempt;
  }

  private async run(agent: ExternalAgent): Promise<SkillStatus> {
    // services.py supplies the interpreter that owns Astrid, independent of PATH.
    const python = this.env.ASTRID_SERVICE_PYTHON;
    if (!python) return { skillSynced: false, detail: "Astrid's Python wasn't found." };
    try {
      await discoveryProbe(python, ['-m', 'astrid.skills', 'sync', '--harness', HARNESSES[agent]], {
        env: this.env, cwd: this.cwd, timeout: this.timeoutMs, maxBuffer: 64 * 1024,
      });
      return this.check(agent);
    } catch (error) {
      return { skillSynced: false, detail: typeof error === 'object' && error !== null && Reflect.get(error, 'killed') === true
        ? `${LOCAL_AGENT_TABLE[agent].name}'s Astrid skill sync timed out.`
        : `${LOCAL_AGENT_TABLE[agent].name}'s Astrid skill couldn't be synced.` };
    }
  }
}
