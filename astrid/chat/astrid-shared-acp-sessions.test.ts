import { test } from 'bun:test';
import assert from 'node:assert/strict';
import { ChatError } from './astrid-project-chat.ts';
import { SharedAcpSessions, type SharedAcpSessionClient } from './astrid-shared-acp-sessions.ts';
import type { AcpProcessHost, AcpRpcNotification } from './acpProcessHost.ts';

type Json = Record<string, unknown>;
function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

function fakeHost(handler?: (method: string, params?: unknown) => unknown) {
  const requests: Array<{ method: string; params?: unknown }> = [];
  const cancels: unknown[] = [];
  const host = {
    request: async (method: string, params?: unknown) => {
      requests.push({ method, params });
      return handler ? await handler(method, params) : {};
    },
    cancel: async (params: unknown) => { cancels.push(params); },
  } as unknown as AcpProcessHost;
  return { host, requests, cancels };
}

function client(id: string, agent: SharedAcpSessionClient['agent'], host: AcpProcessHost, localMediaOnly = false): SharedAcpSessionClient {
  return { id, agent, host, localMediaOnly };
}

function failures() {
  return async (operation: Promise<unknown>, code: string) => {
    await assert.rejects(operation, error => error instanceof ChatError && error.code === code && error.status === 409);
  };
}

test('concurrent session loads share one native load, keep its full result, and replay journal once per client', async () => {
  const load = deferred<unknown>();
  const firstHost = fakeHost(method => method === 'session/load' ? load.promise : {});
  const secondHost = fakeHost();
  const lateHost = fakeHost();
  const emitted = new Map<string, AcpRpcNotification[]>();
  const registry = new SharedAcpSessions((target, notification) => emitted.set(target.id, [...(emitted.get(target.id) ?? []), notification]));
  const first = client('first', 'claude-code', firstHost.host);
  const second = client('second', 'claude-code', secondHost.host);
  const late = client('late', 'claude-code', lateHost.host);
  const params = { sessionId: 'claude-code:native-1', cwd: '/project' };

  const firstLoad = registry.request(first, 'session/load', params);
  const secondLoad = registry.request(second, 'session/load', params);
  assert.equal(firstHost.requests.filter(item => item.method === 'session/load').length, 1);
  assert.equal(secondHost.requests.length, 0);
  const live: AcpRpcNotification = { jsonrpc: '2.0', method: 'session/update', params: { sessionId: params.sessionId, update: { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: 'live while loading' } } } };
  registry.notification(first, live);
  load.resolve({ sessionId: params.sessionId, nativeField: 'original-result' });
  const [firstResult, secondResult] = await Promise.all([firstLoad, secondLoad]) as Json[];
  assert.equal(firstResult.nativeField, 'original-result');
  assert.equal((firstResult._astridSession as Json).role, 'writer');
  assert.equal((secondResult._astridSession as Json).role, 'follower');
  assert.equal(emitted.get('first')?.filter(event => event.method === 'session/update').length, 2); // live + control
  assert.equal(emitted.get('second')?.filter(event => event.method === 'session/update').length, 2); // live + control; not duplicated by replay

  const lateResult = await registry.request(late, 'session/load', params) as Json;
  assert.equal(lateResult.nativeField, 'original-result');
  assert.equal(lateHost.requests.length, 0);
  assert.equal(emitted.get('late')?.filter(event => event.method === 'session/update').length, 2); // journal + control

  const ownerAgain = await registry.request(first, 'session/load', params) as Json;
  assert.equal(ownerAgain.nativeField, 'original-result');
  assert.equal(emitted.get('first')?.filter(event => event.method === 'session/update').length, 4); // full journal + control on cached replay
  assert.equal(firstHost.requests.filter(item => item.method === 'session/load').length, 1);
  registry.close();
});

test('cached load clears caller queue immediately before full journal replay only', async () => {
  const load = deferred<unknown>();
  const sourceHost = fakeHost(method => method === 'session/load' ? load.promise : {});
  const joiningHost = fakeHost();
  const events = new Map<string, AcpRpcNotification[]>();
  const cleared: Array<{ client: string; sessionId: string }> = [];
  const registry = new SharedAcpSessions(
    (target, notification) => events.set(target.id, [...(events.get(target.id) ?? []), notification]),
    (target, id) => cleared.push({ client: target.id, sessionId: id }),
  );
  const source = client('source', 'codex', sourceHost.host);
  const joining = client('joining', 'codex', joiningHost.host);
  const params = { sessionId: 'codex:queue-clear' };
  const pendingSource = registry.request(source, 'session/load', params);
  const pendingJoiner = registry.request(joining, 'session/load', params);
  assert.deepEqual(cleared, []); // a pending join must preserve notifications queued during native load
  const live: AcpRpcNotification = { jsonrpc: '2.0', method: 'session/update', params: { sessionId: params.sessionId, update: { sessionUpdate: 'agent_message_chunk' } } };
  registry.notification(source, live);
  load.resolve({ sessionId: params.sessionId });
  await Promise.all([pendingSource, pendingJoiner]);
  assert.deepEqual(cleared, []);
  await registry.request(joining, 'session/load', params);
  assert.deepEqual(cleared, [{ client: 'joining', sessionId: params.sessionId }]);
  assert.equal(events.get('joining')?.filter(event => event.method === 'session/update').length, 4); // initial live + control, then full replay + control
  registry.close();
});

test('writer takeover is atomic around turns, retries dedupe, and detach retains a shared physical host', async () => {
  const turn = deferred<unknown>();
  const emitted = new Map<string, AcpRpcNotification[]>();
  const sharedHost = fakeHost((method) => method === 'session/new'
    ? { sessionId: 'codex:shared', configOptions: [{ id: 'model', currentValue: 'a', options: [{ value: 'a' }, { value: 'b' }] }] }
    : method === 'session/prompt' ? turn.promise
    : method === 'session/fork' ? { sessionId: 'codex:forked' }
    : {});
  const followerHost = fakeHost();
  const registry = new SharedAcpSessions((target, notification) => emitted.set(target.id, [...(emitted.get(target.id) ?? []), notification]));
  const writer = client('writer', 'codex', sharedHost.host);
  const follower = client('follower', 'codex', followerHost.host);
  await registry.request(writer, 'session/new');
  await registry.request(follower, 'session/load', { sessionId: 'codex:shared' });
  const fail = failures();
  const makePrompt = (text: string, captured: string, revision: string) => ({
    sessionId: 'codex:shared', _astridRequestId: 'queue-22',
    prompt: [{ type: 'text', text: `hello ${text}\n<astrid_editor_context>${JSON.stringify({ request_id: 'queue-22', context_revision: revision, captured_at: captured, project: { id: 'p1' } })}</astrid_editor_context>` }],
  });
  const prompt = makePrompt('same', '2026-10-19T10:00:00Z', 'snapshot-1');
  const retryPrompt = makePrompt('same', '2026-10-19T10:01:00Z', 'snapshot-2');
  const firstPrompt = registry.request(writer, 'session/prompt', prompt);
  const retry = registry.request(writer, 'session/prompt', retryPrompt);
  assert.equal(sharedHost.requests.filter(item => item.method === 'session/prompt').length, 1);
  const takeover = await registry.takeover(follower, 'codex:shared') as Json;
  assert.equal((takeover._astridSession as Json).role, 'writer');
  assert.equal((takeover._astridSession as Json).active, true);
  await fail(registry.request(writer, 'session/prompt', { sessionId: 'codex:shared', _astridRequestId: 'old-writer-send', prompt: [] }), 'session_read_only');
  await fail(registry.request(writer, 'session/prompt', makePrompt('different user text', '2026-10-19T10:02:00Z', 'snapshot-3')), 'request_id_conflict');
  await fail(registry.cancel(writer, 'codex:shared'), 'session_read_only');
  await fail(registry.request(follower, 'session/prompt', { sessionId: 'codex:shared', _astridRequestId: 'new-send', prompt: [] }), 'session_busy');
  await registry.cancel(follower, 'codex:shared');
  assert.deepEqual(sharedHost.cancels, [{ sessionId: 'codex:shared' }]);

  const liveUpdate: AcpRpcNotification = { jsonrpc: '2.0', method: 'session/update', params: { sessionId: 'codex:shared', update: { sessionUpdate: 'agent_message_chunk', content: { type: 'text', text: 'stream' } } } };
  registry.notification(writer, liveUpdate);
  const followerEvents = emitted.get('follower') ?? [];
  const nativeLive = followerEvents.find(event => (event.params as Json)?.update && ((event.params as Json).update as Json).content);
  assert.equal((((nativeLive?.params as Json).update as Json).live), true);
  assert.equal((((nativeLive?.params as Json).update as Json).requestId), 'queue-22');
  const synthetic = followerEvents.find(event => ((((event.params as Json)?.update as Json)?.messageId) === 'astrid-live:queue-22'));
  assert.ok(synthetic);

  const result = { stopReason: 'end_turn' };
  turn.resolve(result);
  assert.deepEqual(await Promise.all([firstPrompt, retry]), [result, result]);
  assert.equal(sharedHost.requests.filter(item => item.method === 'session/prompt').length, 1);
  const loadedAfterTurn = await registry.request(follower, 'session/load', { sessionId: 'codex:shared' }) as Json;
  assert.deepEqual(loadedAfterTurn._astridSession, { role: 'writer', active: false, activeRequestId: null });
  assert.equal(((loadedAfterTurn.configOptions as Json[])[0] as Json).currentValue, 'a');
  await registry.request(follower, 'session/set_config_option', { sessionId: 'codex:shared', configId: 'model', value: 'b' });
  const afterConfig = await registry.request(writer, 'session/load', { sessionId: 'codex:shared' }) as Json;
  assert.equal(((afterConfig.configOptions as Json[])[0] as Json).currentValue, 'b');
  const forked = await registry.request(follower, 'session/fork', { sessionId: 'codex:shared' }) as Json;
  assert.equal(forked.sessionId, 'codex:forked');
  assert.deepEqual(forked._astridSession, { role: 'writer', active: false, activeRequestId: null });
  const forkReaderHost = fakeHost();
  const forkReader = client('fork-reader', 'codex', forkReaderHost.host);
  const attachedFork = await registry.request(forkReader, 'session/load', { sessionId: 'codex:forked' }) as Json;
  assert.equal((attachedFork._astridSession as Json).role, 'follower');
  assert.equal(forkReaderHost.requests.length, 0);
  assert.equal(sharedHost.requests.filter(item => item.method === 'session/fork').length, 1);
  await registry.cancel(follower, 'codex:shared'); // idle cancel remains a valid writer operation
  assert.deepEqual(sharedHost.cancels, [{ sessionId: 'codex:shared' }, { sessionId: 'codex:shared' }]);

  registry.detach(writer);
  assert.equal(registry.retainsHost(writer), true); // follower still uses the creator's physical host
  await fail(registry.request(writer, 'session/prompt', prompt), 'session_not_attached');
  registry.forgetHost(writer);
  assert.equal(registry.retainsHost(writer), false);
  const disconnected = (emitted.get('follower') ?? []).find(event => event.method === 'astrid/acp/disconnected');
  assert.deepEqual(disconnected?.params, { code: 'acp_process_exited' });
  registry.close();
});

test('rejected prompts release their request ID for same or different payload retries', async () => {
  const failure = new Error('prompt failed');
  let promptCalls = 0;
  const host = fakeHost(method => {
    if (method === 'session/new') return { sessionId: 'codex:retry' };
    if (method === 'session/prompt' && (promptCalls++ === 0 || promptCalls === 3)) throw failure;
    return { stopReason: 'end_turn' };
  });
  const registry = new SharedAcpSessions(() => {});
  const writer = client('writer', 'codex', host.host);
  await registry.request(writer, 'session/new');
  const prompt = (requestId: string, text: string) => ({
    sessionId: 'codex:retry', _astridRequestId: requestId, prompt: [{ type: 'text', text }],
  });

  await assert.rejects(registry.request(writer, 'session/prompt', prompt('same-payload', 'hello')), error => error === failure);
  assert.deepEqual(await registry.request(writer, 'session/prompt', prompt('same-payload', 'hello')), { stopReason: 'end_turn' });
  await assert.rejects(registry.request(writer, 'session/prompt', prompt('changed-payload', 'first')), error => error === failure);
  assert.deepEqual(await registry.request(writer, 'session/prompt', prompt('changed-payload', 'second')), { stopReason: 'end_turn' });
  assert.equal(host.requests.filter(item => item.method === 'session/prompt').length, 4);
  registry.close();
});

test('session agent IDs remain isolated across Astrid, Claude Code, and Codex and hosted attach stays sticky', async () => {
  const astridHost = fakeHost(() => ({ sessionId: 'native-astrid' }));
  const claudeHost = fakeHost(() => ({ sessionId: 'claude-code:shared' }));
  const codexHost = fakeHost(() => ({ sessionId: 'codex:shared' }));
  const followerHost = fakeHost();
  const registry = new SharedAcpSessions(() => {});
  const astrid = client('astrid', 'astrid', astridHost.host);
  const claude = client('claude', 'claude-code', claudeHost.host);
  const codex = client('codex', 'codex', codexHost.host);
  const hosted = client('hosted', 'claude-code', followerHost.host, true);
  await registry.request(astrid, 'session/new');
  await registry.request(claude, 'session/new');
  await registry.request(codex, 'session/new');
  await assert.rejects(registry.request(codex, 'session/load', { sessionId: 'claude-code:shared' }), error => error instanceof ChatError && error.code === 'session_agent_mismatch');
  const nonHostedHost = fakeHost(async method => method === 'session/load' ? { sessionId: 'claude-code:private' } : {});
  const nonHosted = client('non-hosted', 'claude-code', nonHostedHost.host, false);
  await registry.request(nonHosted, 'session/load', { sessionId: 'claude-code:private' });
  await assert.rejects(registry.request(hosted, 'session/load', { sessionId: 'claude-code:private' }), error => error instanceof ChatError && error.code === 'hosted_connection_required');

  const hostedOwnerHost = fakeHost(() => ({ sessionId: 'claude-code:hosted' }));
  const hostedOwner = client('hosted-owner', 'claude-code', hostedOwnerHost.host, true);
  await registry.request(hostedOwner, 'session/new');
  await assert.rejects(registry.request(nonHosted, 'session/load', { sessionId: 'claude-code:hosted' }), error => error instanceof ChatError && error.code === 'hosted_connection_required');
  const wrongIdHost = fakeHost(() => ({ sessionId: 'codex:wrong-agent' }));
  await assert.rejects(registry.request(client('wrong', 'claude-code', wrongIdHost.host), 'session/new'), error => error instanceof ChatError && error.code === 'session_agent_mismatch');

  const pending = deferred<unknown>();
  const loadingHost = fakeHost(() => pending.promise);
  const loadingClient = client('loading-only', 'codex', loadingHost.host);
  const loadingRegistry = new SharedAcpSessions(() => {});
  const loadingRequest = loadingRegistry.request(loadingClient, 'session/load', { sessionId: 'codex:loading' });
  loadingRegistry.detach(loadingClient);
  assert.equal(loadingRegistry.retainsHost(loadingClient), true);
  pending.resolve({ sessionId: 'codex:loading' });
  await loadingRequest;
  assert.equal(loadingRegistry.retainsHost(loadingClient), false);
  loadingRegistry.close();
  registry.close();
});

test('a model switch reserves the native session across takeover until its acknowledgement', async () => {
  const setter = deferred<unknown>();
  const host = fakeHost(method => method === 'session/new'
    ? { sessionId: 'codex:model-change', models: { currentModelId: 'a', availableModels: [{ modelId: 'a', name: 'A' }, { modelId: 'b', name: 'B' }] } }
    : method === 'session/set_model' ? setter.promise : {});
  const registry = new SharedAcpSessions(() => {});
  const writer = client('writer', 'codex', host.host);
  const follower = client('follower', 'codex', fakeHost().host);
  const params = { sessionId: 'codex:model-change' };
  await registry.request(writer, 'session/new');
  await registry.request(follower, 'session/load', params);
  const switching = registry.request(writer, 'session/set_model', { ...params, modelId: 'b' });
  await registry.takeover(follower, params.sessionId);
  const fail = failures();
  await fail(registry.request(follower, 'session/prompt', { ...params, prompt: [] }), 'session_busy');
  const nextSwitch = registry.request(follower, 'session/set_model', { ...params, modelId: 'a' });
  assert.equal(host.requests.filter(item => item.method === 'session/set_model').length, 1);
  await fail(registry.request(writer, 'session/set_model', { ...params, modelId: 'a' }), 'session_read_only');
  registry.detach(writer);
  assert.equal(registry.retainsHost(writer), true);
  setter.resolve({}); // legacy ACP adapters may acknowledge without an event
  await switching;
  await nextSwitch;
  assert.equal((registry.snapshot(follower, params.sessionId) as { models: { currentModelId: string } }).models.currentModelId, 'a');
  assert.deepEqual(host.requests.map(item => item.method), ['session/new', 'session/set_model', 'session/set_model']);
  await registry.request(follower, 'session/prompt', { ...params, prompt: [] });
  assert.equal(host.requests.at(-1)?.method, 'session/prompt');
  registry.close();
});

test('queued config changes recheck prompt state after a non-prompt RPC fails', async () => {
  const read = deferred<unknown>();
  const turn = deferred<unknown>();
  const host = fakeHost(method => method === 'session/new' ? { sessionId: 'codex:queued' }
    : method === 'session/list' ? read.promise
    : method === 'session/prompt' ? turn.promise : {});
  const registry = new SharedAcpSessions(() => {});
  const writer = client('writer', 'codex', host.host);
  await registry.request(writer, 'session/new');
  const listing = registry.request(writer, 'session/list');
  const change = registry.request(writer, 'session/set_config_option', { sessionId: 'codex:queued', configId: 'effort', value: 'high' });
  const prompt = registry.request(writer, 'session/prompt', { sessionId: 'codex:queued', prompt: [] });
  const failedRead = assert.rejects(listing, /read failed/);
  const busy = failures()(change, 'acp_request_in_progress');
  read.reject(new Error('read failed'));
  await failedRead;
  await busy;
  assert.equal(host.requests.some(item => item.method === 'session/set_config_option'), false);
  turn.resolve({});
  await prompt;
  await registry.request(writer, 'session/set_config_option', { sessionId: 'codex:queued', configId: 'effort', value: 'high' });
  assert.equal(host.requests.at(-1)?.method, 'session/set_config_option');
  registry.close();
});

test('forwarded config waits on the physical session host after creator handoff', async () => {
  const id = 'codex:handoff';
  const read = deferred<unknown>();
  const original = fakeHost(method => method === 'session/new' ? { sessionId: id } : method === 'session/list' ? read.promise : {});
  const other = fakeHost(() => { throw new Error('forwarded setter used the subscriber host'); });
  const registry = new SharedAcpSessions(() => {});
  const creator = client('creator', 'codex', original.host);
  const survivor = client('survivor', 'codex', other.host);
  await registry.request(creator, 'session/new');
  await registry.request(survivor, 'session/load', { sessionId: id });
  const listing = registry.request(creator, 'session/list');
  registry.detach(creator);
  let resolved = false;
  const change = registry.changeConfig(survivor, id, () => {
    resolved = true;
    return { method: 'session/set_config_option', params: { sessionId: id, configId: 'effort', value: 'high' } };
  });
  await Promise.resolve();
  assert.equal(resolved, false);
  assert.equal(registry.retainsHost(creator), true);
  read.resolve({});
  await Promise.all([listing, change]);
  assert.equal(resolved, true);
  assert.equal(original.requests.at(-1)?.method, 'session/set_config_option');
  assert.equal(other.requests.length, 0);
  registry.close();
});

for (const transition of ['takeover', 'detach'] as const) {
  test(`queued config rechecks attachment and writer after ${transition}`, async () => {
    const id = 'codex:queued-writer';
    const read = deferred<unknown>();
    const original = fakeHost(method => method === 'session/new' ? { sessionId: id } : method === 'session/list' ? read.promise : {});
    const registry = new SharedAcpSessions(() => {});
    const writer = client('writer', 'codex', original.host);
    const follower = client('follower', 'codex', fakeHost().host);
    await registry.request(writer, 'session/new');
    await registry.request(follower, 'session/load', { sessionId: id });
    const listing = registry.request(writer, 'session/list');
    const change = registry.changeConfig(writer, id, () => { throw new Error('stale writer reached config validation'); });
    const rejection = failures()(change, transition === 'takeover' ? 'session_read_only' : 'session_not_attached');
    if (transition === 'takeover') await registry.takeover(follower, id);
    else registry.detach(writer);
    read.resolve({});
    await Promise.all([listing, rejection]);
    assert.equal(original.requests.some(request => request.method === 'session/set_config_option'), false);
    registry.close();
  });
}

test('queued validation receives metadata accepted by the previous setter', async () => {
  const id = 'codex:queued-options';
  const setter = deferred<unknown>();
  const options = [{ id: 'updated-effort', category: 'thought_level', type: 'select', currentValue: 'low', options: [{ value: 'low' }, { value: 'high' }] }];
  const original = fakeHost((method, params) => method === 'session/new' ? { sessionId: id }
    : method === 'session/set_model' ? setter.promise : { configOptions: options.map(option => ({ ...option, currentValue: (params as Json).value })) });
  const registry = new SharedAcpSessions(() => {});
  const writer = client('writer', 'codex', original.host);
  await registry.request(writer, 'session/new');
  const first = registry.request(writer, 'session/set_model', { sessionId: id, modelId: 'b' });
  let resolved = false;
  const second = registry.changeConfig(writer, id, snapshot => {
    resolved = true;
    const current = (snapshot as { configOptions: typeof options }).configOptions[0];
    assert.equal(current.id, 'updated-effort');
    return { method: 'session/set_config_option', params: { sessionId: id, configId: current.id, value: 'high' } };
  });
  assert.equal(resolved, false);
  setter.resolve({ configOptions: options });
  await Promise.all([first, second]);
  assert.equal(((registry.snapshot(writer, id) as Json).configOptions as Json[])[0].currentValue, 'high');
  registry.close();
});
