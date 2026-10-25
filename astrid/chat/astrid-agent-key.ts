import { discoveryProbe } from './discoveryProbe.ts';
import { ASTRID_ACP_COMMAND } from './astridAcpLauncher.ts';
import { ChatError } from './astrid-project-chat.ts';

export async function setAstridAgentKey(options: { command?: string; cwd: string; profile?: string }, key: string): Promise<{
  provider: 'openrouter'; signedIn: true; currentModel: string | null;
}> {
  try {
    const result = await discoveryProbe(options.command ?? ASTRID_ACP_COMMAND, ['agent', 'key', 'set', 'openrouter'], {
      cwd: options.cwd,
      env: { ...process.env, ...(options.profile ? { OMP_PROFILE: options.profile } : {}) },
      input: key,
      timeout: 60_000,
      maxBuffer: 1024 * 1024,
    });
    const value = JSON.parse(result.stdout);
    if (value.provider !== 'openrouter' || value.signedIn !== true
      || !(value.currentModel === null || (typeof value.currentModel === 'string' && !value.currentModel.includes(key)))) {
      throw new Error('Invalid key response');
    }
    return { provider: 'openrouter', signedIn: true, currentModel: value.currentModel };
  } catch (error) {
    const code = (error as { code?: unknown })?.code;
    if (code === 3) throw new ChatError(400, 'invalid_key', "OpenRouter didn't accept that key.");
    if (code === 4) throw new ChatError(502, 'provider_unreachable', 'Could not reach OpenRouter. Try again.');
    // Child output and errors may contain the key, including on successful exit.
    throw new ChatError(502, 'acp_bridge_error', 'Could not save the OpenRouter key. Try again.');
  }
}
