import { readFileSync } from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'bun:test';
import { ASTRID_ACP_CONTEXT_SYSTEM_PROMPT } from './astridAcpLauncher.ts';

/**
 * Timeline verbs this prompt is allowed to use. The vendored cli-surface.json proves each verb exists;
 * this list is the prompt's own policy (a subset of what Astrid provides) and is not redundant with it.
 */
const ASTRID_TIMELINE_VERBS_IN_PROMPT = ['show', 'checkout', 'edit', 'publish'] as const;

/**
 * Flags the removed `retime-clip` route accepted. Some now exist on other verbs (`--hold` on
 * `timelines edit`, `--expected-version` on `timelines render`), so the per-verb flag check cannot catch
 * them; the substring ban is what keeps them out of the prompt.
 */
const REMOVED_RETIME_FLAGS = ['--preserve-duration', '--hold', '--expected-version'] as const;

/** Snapshot of the exported Astrid CLI surface, moved with the prompt contract tests. */
type CliSurfaceNode = {
  readonly flags: Readonly<Record<string, unknown>>;
  readonly verbs: Readonly<Record<string, CliSurfaceNode>>;
};
type CliSurface = { readonly binary: string; readonly families: Readonly<Record<string, CliSurfaceNode>> };

const CLI_SURFACE = JSON.parse(
  readFileSync(path.resolve(__dirname, 'cli-surface.fixture.json'), 'utf8'),
) as CliSurface;

/**
 * Each `astrid-tools ...` command in the prompt, up to the end of its sentence. Quoted arguments are
 * consumed whole (so flags after them still count) and their contents masked (so prose is not parsed as flags).
 */
function promptInvocations(prompt: string): string[] {
  return [...prompt.matchAll(/astrid-tools ((?:"[^"\n]*"|[^\n,;"'`().]|\.(?=\S))+)/g)].map((match) =>
    match[1].replace(/"[^"\n]*"/g, '"_"').trim(),
  );
}

/**
 * Walks family -> verb (-> nested verb)* against cli-surface.json, then checks the flags the command
 * passes against the leaf verb's flags. Positionals and flag values are not validated.
 */
function resolveInvocation(command: string): { command: string; verbPath: string; error: string | null; unknownFlags: string[] } {
  const tokens = command.split(/\s+/);
  const verbPath: string[] = [];
  let verbs: Readonly<Record<string, CliSurfaceNode>> = CLI_SURFACE.families;
  let leaf: CliSurfaceNode | undefined;
  let index = 0;
  while (Object.keys(verbs).length > 0) {
    const token = tokens[index];
    if (token === undefined || !Object.keys(verbs).includes(token)) {
      const expected = Object.keys(verbs).join(', ');
      const where = verbPath.length ? verbPath.join(' ') : 'astrid-tools';
      return { command, verbPath: verbPath.join(' '), error: `after "${where}" expected one of [${expected}], got "${token ?? '(end)'}"`, unknownFlags: [] };
    }
    leaf = verbs[token];
    verbPath.push(token);
    verbs = leaf.verbs;
    index += 1;
  }
  if (!leaf) throw new Error('cli-surface.json has no families');
  const defined = Object.keys(leaf.flags);
  const flags = tokens
    .slice(index)
    .map((token) => token.split('=')[0])
    .filter((token) => /^--?[A-Za-z]/.test(token));
  return {
    command,
    verbPath: verbPath.join(' '),
    error: null,
    unknownFlags: flags.filter((flag) => !defined.includes(flag)),
  };
}

const PROMPT_INVOCATIONS = promptInvocations(ASTRID_ACP_CONTEXT_SYSTEM_PROMPT).map(resolveInvocation);

describe('ASTRID_ACP_CONTEXT_SYSTEM_PROMPT runtime commands', () => {
  it('does not reference the removed retime-clip command', () => {
    expect(ASTRID_ACP_CONTEXT_SYSTEM_PROMPT).not.toContain('retime-clip');
  });

  it('only uses timeline verbs that the current Astrid CLI provides', () => {
    const verbs = [...ASTRID_ACP_CONTEXT_SYSTEM_PROMPT.matchAll(/astrid-tools timelines ([a-z-]+)/g)].map(
      (match) => match[1],
    );
    expect(verbs.length).toBeGreaterThan(0);
    for (const verb of verbs) {
      expect(ASTRID_TIMELINE_VERBS_IN_PROMPT).toContain(verb);
    }
  });

  it('does not mention flags that only the removed retime-clip route accepted', () => {
    for (const flag of REMOVED_RETIME_FLAGS) {
      expect(ASTRID_ACP_CONTEXT_SYSTEM_PROMPT).not.toContain(flag);
    }
  });

  it('invokes only family and verb paths that the vendored cli-surface.json defines', () => {
    expect(PROMPT_INVOCATIONS.length).toBeGreaterThan(0);
    const unresolved = PROMPT_INVOCATIONS.filter((invocation) => invocation.error !== null).map(({ command, error }) => ({ command, error }));
    expect(unresolved).toEqual([]);
  });

  it('passes only flags that the invoked verb defines in cli-surface.json', () => {
    const unknown = PROMPT_INVOCATIONS.filter((invocation) => invocation.unknownFlags.length > 0).map(
      ({ command, verbPath, unknownFlags }) => ({ command, verbPath, unknownFlags }),
    );
    expect(unknown).toEqual([]);
  });
});
