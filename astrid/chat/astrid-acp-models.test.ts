import { test, expect } from 'bun:test';
import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { createAstridAcpHttpServer } from './astrid-acp-bridge.ts';
import { bridgeTestClient } from './bridge-test-client.ts';
import { LOCAL_AGENT_TABLE, agentCommand, localAgentCatalogSchema, type LocalAgentChoice, type LocalAgentId } from './localAgents.ts';
import type { ProjectChatRegistry } from './astrid-project-chat.ts';

const ids: LocalAgentId[] = ['astrid', 'claude-code', 'codex'];
const names = ['Alpha', 'Beta'];
const modelIds = (agent: LocalAgentId) => agent === 'astrid' ? ['provider/alpha', 'provider/beta'] : ['alpha', 'beta'];
const choices = (ready: LocalAgentId[] = ids): LocalAgentChoice[] => ids.map(id => ({
  id, name: LOCAL_AGENT_TABLE[id].name, state: ready.includes(id) ? 'ready' : 'not-installed',
  line: ready.includes(id) ? null : LOCAL_AGENT_TABLE[id].notInstalledLine ?? LOCAL_AGENT_TABLE[id].signInLine,
}));
function metadata(agent: LocalAgentId, current = modelIds(agent)[0]!, available = modelIds(agent)) {
  return { configOptions: [{ id: 'model', currentValue: current,
    options: [{ group: 'models', options: available.map((value, index) => ({ value, name: names[index] ?? value })) }],
  }, { id: 'thinking', currentValue: 'high', options: [{ value: 'high', name: 'High' }, { value: 'low', name: 'Low' }] }] };
}
type HostCall = { method: string; params: Record<string, unknown> };
type Host = { agent: LocalAgentId; calls: HostCall[]; disposed: number };
function fixture(options: { ready?: LocalAgentId[]; stateFile?: string; nativeSessions?: string[]; noModels?: boolean; astridSignedIn?: boolean; astridModelsWithoutAuth?: boolean; astridCatalogUnavailable?: boolean; legacy?: boolean } = {}) {
  const sessionMetadata = (agent: LocalAgentId, current = modelIds(agent)[0]!) => options.legacy
    ? { models: { currentModelId: current, availableModels: modelIds(agent).map((modelId, index) => ({ modelId, name: names[index]! })) } }
    : metadata(agent, current);
  const hosts: Host[] = [];
  const nativeSessions = options.nativeSessions ?? [];
  let newSession = nativeSessions.length;
  let connection = 0;
  let newResult: ((agent: LocalAgentId) => Promise<unknown>) | undefined;
  let setter: ((params: Record<string, unknown>) => unknown) | undefined;
  let discovered = 0;
  const hostFactory = (agent: LocalAgentId) => {
    const host = { agent, calls: [] as HostCall[], disposed: 0 };
    hosts.push(host);
    return {
      initialize: async () => ({ protocolVersion: 1 }),
      request: async (method: string, raw: unknown) => {
        const params = (raw ?? {}) as Record<string, unknown>;
        host.calls.push({ method, params });
        if (method === 'session/new') {
          if (newResult) return newResult(agent);
          const sessionId = `${agent === 'astrid' ? '' : `${agent}:`}new-${++newSession}`;
          nativeSessions.push(sessionId);
          return { sessionId, ...(options.noModels ? {} : sessionMetadata(agent)) };
        }
        if (method === 'session/list') return { sessions: [...nativeSessions, `${agent === 'astrid' ? '' : `${agent}:`}saved`].map(sessionId => ({ sessionId })) };
        if (method === 'session/load' || method === 'session/resume') return { sessionId: params.sessionId, ...metadata(agent) };
        if (method === 'session/set_model' || method === 'session/set_config_option') return method === 'session/set_model' || params.configId === 'model' ? (setter ? setter(params) : sessionMetadata(agent, String(params.value ?? params.modelId))) : {};
        return {};
      },
      cancel: async () => {},
      dispose: async () => { host.disposed++; },
    } as never;
  };
  const { bridge } = createAstridAcpHttpServer({
    config: { token: 'models-token', cwd: '/tmp/model-service', port: 0, ...(options.stateFile ? { modelStateFile: options.stateFile } : {}) },
    hostFactory: () => hostFactory('astrid'),
    localAgentHostFactory: async agent => hostFactory(agent),
    discoverAgents: async () => { discovered++; return choices(options.ready); },
    astridModelCatalog: async () => {
      if (options.astridCatalogUnavailable) throw new Error('catalog unavailable');
      return { providers: options.astridSignedIn === false
        ? options.astridModelsWithoutAuth ? [{ id: 'provider', name: 'Provider', signedIn: false, models: [{ id: 'alpha', name: 'Alpha' }] }] : []
        : [{ id: 'provider', name: 'Provider', signedIn: true, models: [{ id: 'alpha', name: 'Alpha' }] }], currentModel: 'provider/alpha' };
    },
    idFactory: () => `c${++connection}`,
    chatRegistry: {
      unassigned: async (_project: string, candidates: unknown[]) => candidates,
      assertOwned: async () => {}, markSessionStarted: async () => {},
    } as unknown as ProjectChatRegistry,
  });
  const call = bridgeTestClient(bridge, 'models-token');
  const rpc = (id: string, method: string, params: unknown = {}) => call(`/${id}/rpc`, { method, params });
  return { bridge, call, rpc, hosts, nativeSessions,
    setNew: (fn: typeof newResult) => { newResult = fn; },
    setSetter: (fn: typeof setter) => { setter = fn; },
    discoveries: () => discovered,
  };
}

for (const agent of ids) test(`${agent} applies its model once when created and preserves it through attach, takeover and prompts`, async () => {
  const value = fixture();
  try {
    const selected = modelIds(agent)[1]!;
    expect((await value.call('/connect', { agent, defaultModelId: selected })).status).toBe(200);
    const created = await value.rpc('c1', 'session/new');
    expect(created.status).toBe(200);
    const result = (await created.json() as { result: { sessionId: string; models: { currentModelId: string } } }).result;
    expect(result.models.currentModelId).toBe(selected);
    expect((await value.call('/connect', { agent, defaultModelId: modelIds(agent)[0] })).status).toBe(200);
    const beforeAttach = value.hosts.flatMap(host => host.calls).length;
    expect((await value.rpc('c2', 'session/set_config_option', { sessionId: result.sessionId, configId: 'model', value: modelIds(agent)[0] })).status).toBe(400);
    expect(value.hosts.flatMap(host => host.calls)).toHaveLength(beforeAttach);
    expect((await value.rpc('c2', 'session/load', { sessionId: result.sessionId })).status).toBe(200);
    expect((await value.rpc('c2', 'session/set_config_option', { sessionId: result.sessionId, configId: 'thinking', value: 'low' })).status).toBe(409);
    expect((await value.rpc('c2', 'session/takeover', { sessionId: result.sessionId })).status).toBe(200);
    expect((await value.call('/projects/p/chat/prompt', { connection_id: 'c2', session_id: result.sessionId, prompt: [] })).status).toBe(200);
    expect((await value.rpc('c2', 'session/prompt', { sessionId: result.sessionId, prompt: [] })).status).toBe(200);
    for (const model of [modelIds(agent)[0], 'unadvertised']) {
      const beforeModelWrite = value.hosts.flatMap(host => host.calls).length;
      const rejected = await value.rpc('c2', 'session/set_config_option', { sessionId: result.sessionId, configId: 'model', value: model });
      expect(rejected.status).toBe(409);
      expect(await rejected.json()).toMatchObject({ error: 'session_model_fixed' });
      expect(value.hosts.flatMap(host => host.calls)).toHaveLength(beforeModelWrite);
    }
    expect((await value.call('/model', { model: modelIds(agent)[0] })).status).toBe(404);
    expect((await value.call('/c2/model', { sessionId: result.sessionId, modelId: modelIds(agent)[0] })).status).toBe(404);
    expect((await value.rpc('c2', 'session/set_config_option', { sessionId: result.sessionId, configId: 'thinking', value: 'low' })).status).toBe(200);
    const setters = value.hosts.flatMap(host => host.calls).filter(call => call.method === 'session/set_config_option' && call.params.configId === 'model');
    expect(setters).toEqual([{ method: 'session/set_config_option', params: { sessionId: result.sessionId, configId: 'model', value: selected } }]);
    const snapshot = await value.call(`/c2/models?sessionId=${encodeURIComponent(result.sessionId)}`);
    expect((await snapshot.json() as { models: { currentModelId: string } }).models.currentModelId).toBe(selected);
    expect(value.hosts.every(host => host.disposed === 0)).toBe(true);
  } finally { await value.bridge.close(); }
});

test('agents lazily share one bootstrap per ready agent, hide native sessions, and recheck readiness on menu refresh', async () => {
  const value = fixture();
  try {
    expect(value.hosts).toHaveLength(0);
    const responses = await Promise.all([value.call('/agents'), value.call('/agents'), value.call('/agents')]);
    for (const response of responses) {
      expect(response.status).toBe(200);
      const catalog = localAgentCatalogSchema.parse(await response.json());
      expect(catalog.defaultAgent).toBe('astrid');
      expect(catalog.agents.find(agent => agent.id === 'astrid')?.signIn).toBe('signed-in');
      for (const agent of catalog.agents.filter(agent => agent.id !== 'astrid')) {
        expect(agent.creditLine).toBeUndefined();
        expect(agent.creditUrl).toBeUndefined();
      }
      for (const agent of catalog.agents) {
        expect(agent.models.map(model => model.modelId)).toEqual(modelIds(agent.id));
        expect(agent.defaultModel).toBe(modelIds(agent.id)[0]!);
      }
    }
    expect(value.hosts).toHaveLength(3);
    for (const host of value.hosts) {
      expect(host.calls.map(call => call.method)).toEqual(['session/new', 'session/close']);
      expect(host.disposed).toBe(1);
    }
    await value.call('/agents');
    expect(value.hosts).toHaveLength(3);
    expect(value.discoveries()).toBe(4);
    for (const [index, agent] of ids.entries()) {
      await value.call('/connect', { agent });
      const connection = `c${index + 1}`;
      const listed = await value.rpc(connection, 'session/list');
      expect((await listed.json() as { result: { sessions: Array<{ sessionId: string }> } }).result.sessions.every(item => !value.nativeSessions.includes(item.sessionId))).toBe(true);
      const unassigned = await value.call(`/projects/p/chat/unassigned?connection_id=${connection}`);
      expect((await unassigned.json() as { sessions: Array<{ id: string }> }).sessions.every(item => !value.nativeSessions.includes(item.id))).toBe(true);
      const hidden = value.nativeSessions[index]!;
      expect((await value.rpc(connection, 'session/load', { sessionId: hidden })).status).toBe(404);
      expect((await value.call('/projects/p/chat/associate', { connection_id: connection, session_id: hidden, expected_revision: 0 })).status).toBe(404);
    }
  } finally { await value.bridge.close(); }
});

test('a real new session fills and refreshes its service catalog without a bootstrap', async () => {
  const value = fixture({ ready: ['codex'], astridSignedIn: false });
  try {
    await value.call('/connect', { agent: 'codex', defaultModelId: 'beta' });
    expect((await value.rpc('c1', 'session/new')).status).toBe(200);
    let catalog = localAgentCatalogSchema.parse(await (await value.call('/agents')).json());
    expect(value.hosts).toHaveLength(1);
    expect(catalog.defaultAgent).toBe('codex');
    expect(catalog.agents.find(agent => agent.id === 'codex')?.defaultModel).toBe('beta');
    value.setNew(async () => ({ sessionId: 'codex:refresh', ...metadata('codex', 'gamma', ['gamma']) }));
    value.setSetter(params => metadata('codex', String(params.value), ['gamma']));
    await value.call('/connect', { agent: 'codex', defaultModelId: 'gamma' });
    expect((await value.rpc('c2', 'session/new')).status).toBe(200);
    catalog = localAgentCatalogSchema.parse(await (await value.call('/agents')).json());
    expect(catalog.agents.find(agent => agent.id === 'codex')?.models).toEqual([{ modelId: 'gamma', name: 'Alpha' }]);
    expect(catalog.agents.find(agent => agent.id === 'codex')?.defaultModel).toBe('gamma');
    expect(value.hosts.flatMap(host => host.calls).filter(call => call.method === 'session/new')).toHaveLength(2);
  } finally { await value.bridge.close(); }
});

test('service remembers successful choices and bootstrap exclusions across restarts without changing the CLI default', async () => {
  const directory = mkdtempSync(join(tmpdir(), 'chat-models-state-'));
  const stateFile = join(directory, 'models.json');
  const nativeSessions: string[] = [];
  let value = fixture({ stateFile, nativeSessions, ready: ['astrid', 'codex'] });
  try {
    await value.call('/agents');
    const hiddenIds = [...nativeSessions];
    await value.call('/connect', { agent: 'codex', defaultModelId: 'beta' });
    expect((await value.rpc('c1', 'session/new')).status).toBe(200);
    await value.bridge.close();
    value = fixture({ stateFile, nativeSessions, ready: ['codex'], astridSignedIn: false });
    expect((await value.call('/connect', {})).status).toBe(200);
    const newSession = await value.rpc('c1', 'session/new');
    expect((await newSession.json() as { result: { models: { currentModelId: string } } }).result.models.currentModelId).toBe('beta');
    expect(value.hosts[0]?.agent).toBe('codex');
    const list = await value.rpc('c1', 'session/list');
    const sessions = (await list.json() as { result: { sessions: Array<{ sessionId: string }> } }).result.sessions;
    expect(sessions.some(item => hiddenIds.includes(item.sessionId))).toBe(false);
    expect((await value.call('/models')).status).toBe(200);
    expect(await (await value.call('/models')).json()).toMatchObject({ currentModel: 'provider/alpha' });
    expect(JSON.parse(readFileSync(stateFile, 'utf8'))).toMatchObject({ defaultAgent: 'codex', defaults: { codex: 'beta' } });
  } finally { await value.bridge.close(); rmSync(directory, { recursive: true, force: true }); }
});

test('non-ready agents do not bootstrap and every recovery row has its service line', async () => {
  const value = fixture({ ready: [], astridSignedIn: false });
  try {
    const catalog = localAgentCatalogSchema.parse(await (await value.call('/agents')).json());
    expect(value.hosts).toHaveLength(0);
    expect(catalog.agents.every(agent => agent.state !== 'ready' && Boolean(agent.line))).toBe(true);
    expect(catalog.agents.find(agent => agent.id === 'astrid')).toMatchObject({ state: 'sign-in', line: LOCAL_AGENT_TABLE.astrid.signInLine, models: [], defaultModel: null });
  } finally { await value.bridge.close(); }
});

test('Astrid sign-in preserves keyless model access; unknown auth remains ready', async () => {
  const unsigned = fixture({ astridSignedIn: false });
  const keyless = fixture({ astridSignedIn: false, astridModelsWithoutAuth: true });
  const unknown = fixture({ astridCatalogUnavailable: true });
  try {
    const unsignedCatalog = localAgentCatalogSchema.parse(await (await unsigned.call('/agents')).json());
    expect(unsignedCatalog.agents.find(agent => agent.id === 'astrid')).toMatchObject({ state: 'sign-in', signIn: 'signed-out', line: LOCAL_AGENT_TABLE.astrid.signInLine,
      steps: [{ kind: 'sign-in', command: agentCommand(LOCAL_AGENT_TABLE.astrid.signInLine), label: 'Start Astrid in a terminal' }, { kind: 'sign-in', command: '/login', label: 'Then sign in' }],
      creditLine: 'Built on oh-my-pi', creditUrl: 'https://github.com/can1357/oh-my-pi', terminal: { command: agentCommand(LOCAL_AGENT_TABLE.astrid.signInLine), skillSynced: true },
    });
    const keylessCatalog = localAgentCatalogSchema.parse(await (await keyless.call('/agents')).json());
    expect(keylessCatalog.agents.find(agent => agent.id === 'astrid')).toMatchObject({ state: 'ready', signIn: 'signed-in', steps: [] });
    const unknownCatalog = localAgentCatalogSchema.parse(await (await unknown.call('/agents')).json());
    expect(unknownCatalog.agents.find(agent => agent.id === 'astrid')).toMatchObject({ state: 'ready', signIn: 'unknown', steps: [] });
  } finally { await unsigned.bridge.close(); await keyless.bridge.close(); await unknown.bridge.close(); }
});

test('bootstrap failures release hosts and retry only on the next explicit menu refresh', async () => {
  const value = fixture({ ready: ['codex'], astridSignedIn: false });
  try {
    value.setNew(async () => { throw new Error('adapter temporarily unavailable'); });
    const responses = await Promise.all([value.call('/agents'), value.call('/agents')]);
    expect(responses.map(response => response.status)).toEqual([200, 200]);
    expect(value.hosts).toHaveLength(1);
    expect(value.hosts[0]?.disposed).toBe(1);
    value.setNew(undefined);
    const catalog = localAgentCatalogSchema.parse(await (await value.call('/agents')).json());
    expect(value.hosts).toHaveLength(2);
    expect(catalog.agents.find(agent => agent.id === 'codex')?.models).toHaveLength(2);
  } finally { await value.bridge.close(); }
});

test('a completed empty bootstrap does not create throwaway sessions on every menu opening', async () => {
  const value = fixture({ ready: ['codex'], astridSignedIn: false, noModels: true });
  try {
    await value.call('/agents');
    await value.call('/agents');
    expect(value.hosts).toHaveLength(1);
    expect(value.hosts[0]?.disposed).toBe(1);
  } finally { await value.bridge.close(); }
});

for (const legacy of [false, true]) for (const failure of ['unavailable', 'missing-metadata', 'rejected', 'mismatch'] as const) test(`a ${failure} ${legacy ? 'legacy' : 'config'} model choice fails before any prompt and does not become the remembered agent`, async () => {
  const value = fixture({ legacy, noModels: failure === 'missing-metadata' });
  try {
    if (failure === 'rejected') value.setSetter(() => { throw new Error('selection failed'); });
    if (failure === 'mismatch') value.setSetter(() => legacy ? { models: { currentModelId: 'alpha', availableModels: [{ modelId: 'alpha', name: 'Alpha' }, { modelId: 'beta', name: 'Beta' }] } } : metadata('codex', 'alpha'));
    await value.call('/connect', { agent: 'codex', defaultModelId: failure === 'unavailable' ? 'unknown' : 'beta' });
    const response = await value.rpc('c1', 'session/new');
    expect(response.status).toBe(failure === 'rejected' ? 500 : 409);
    expect(value.hosts[0]?.calls.at(-1)?.method).toBe('session/close');
    expect((await value.rpc('c1', 'session/load', { sessionId: value.nativeSessions[0] })).status).toBe(404);
    expect(value.hosts.flatMap(host => host.calls).some(call => call.method === 'session/prompt')).toBe(false);
    // Connecting again without an agent must still use Astrid.
    await value.call('/connect', {});
    expect(value.hosts.at(-1)?.agent).toBe('astrid');
  } finally { await value.bridge.close(); }
});

test('model discovery and retired mutation routes retain bearer and protocol protections', async () => {
  const value = fixture();
  try {
    for (const path of ['/agents', '/models', '/model']) {
      expect((await value.call(path, path === '/model' ? { model: 'provider/beta' } : undefined, { auth: false })).status).toBe(401);
      expect((await value.call(path, path === '/model' ? { model: 'provider/beta' } : undefined, { protocol: 'v0' })).status).toBe(400);
    }
    expect(value.hosts).toHaveLength(0);
    expect((await value.call('/model', { model: 'provider/beta' })).status).toBe(404);
    expect(value.hosts).toHaveLength(0);
    expect((await value.call('/connect', { agent: 'astrid', defaultModelId: '' })).status).toBe(400);
    expect((await value.call('/connect', { agent: 'astrid', defaultModelId: 'provider/beta', persist: true })).status).toBe(400);
  } finally { await value.bridge.close(); }
});

test('catalog levels belong only to the model that advertised them and stale support clears', async () => {
  const { AgentModelState } = await import('./agent-model-state.ts');
  const state = new AgentModelState();
  const availableModels = [{ modelId: 'a', name: 'A' }, { modelId: 'b', name: 'B' }, { modelId: 'c', name: 'C' }];
  const level = { configId: 'effort', name: 'Thinking level', currentValue: 'high', options: [{ value: 'high', name: 'High' }], recommendedValue: 'high' };
  state.refresh('codex', { availableModels, currentModelId: 'a', thoughtLevel: level });
  expect(state.catalogs.get('codex')?.availableModels).toEqual([
    { ...availableModels[0], thoughtLevel: level }, { ...availableModels[1], thoughtLevel: undefined }, { ...availableModels[2], thoughtLevel: undefined },
  ]);
  state.refresh('codex', { availableModels, currentModelId: 'b', thoughtLevel: { ...level, configId: 'reasoning_effort' } });
  expect(state.catalogs.get('codex')?.availableModels[0]?.thoughtLevel).toEqual(level);
  expect(state.catalogs.get('codex')?.availableModels[1]?.thoughtLevel?.configId).toBe('reasoning_effort');
  expect(state.catalogs.get('codex')?.availableModels[2]?.thoughtLevel).toBeUndefined();
  state.refresh('codex', { availableModels, currentModelId: 'a' });
  expect(state.catalogs.get('codex')?.availableModels[0]?.thoughtLevel).toBeUndefined();
  expect(state.catalogs.get('codex')?.availableModels[1]?.thoughtLevel?.configId).toBe('reasoning_effort');
  expect(state.catalogs.has('hermes')).toBe(false);
});
