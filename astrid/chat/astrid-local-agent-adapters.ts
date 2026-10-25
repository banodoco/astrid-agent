/** Current adapter releases used by both launches and the CI initialize smoke. */
export const LOCAL_AGENT_ADAPTERS = {
  'claude-code': '@agentclientprotocol/claude-agent-acp@0.93.0',
  codex: '@agentclientprotocol/codex-acp@2.2.2',
} as const;

/**
 * DeepSeek Harness serves ACP itself: `dsh --profile acp` boots the bundled
 * `dsh-acp-app` bundle, and the profile auto-initializes from the template the
 * launcher ships, so the launcher and its ACP bundle can never drift apart.
 *
 * It is deliberately not in LOCAL_AGENT_ADAPTERS: those are pure JavaScript
 * adapter packages that run under Bun, while the harness reaches for V8-only
 * native bindings and must run under a real Node runtime instead.
 */
export const DSH_ACP_LAUNCHER = '@deepseek-ai/dsh@0.2.1-rc.3';

/** The dsh profile the launcher boots to serve ACP over stdio. */
export const DSH_ACP_PROFILE = 'acp';

/**
 * The harness refuses to boot on older runtimes (it exits silently), so the
 * launcher is only used with a Node that meets this floor.
 */
export const DSH_ACP_MIN_NODE_MAJOR = 22;
