import { test } from 'bun:test';
import assert from 'node:assert/strict';
import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { ProjectChatRegistry } from './astrid-project-chat.ts';
import { ApiError, type Project } from './runtime-client.ts';
import { isExternalAgentSession, sessionAgentId } from './localAgents.ts';

test('routes prefixed external sessions, including Hermes, to their own agent identity', () => {
  assert.equal(isExternalAgentSession('hermes:session-1'), true);
  assert.equal(isExternalAgentSession('hermes:session-1', 'hermes'), true);
  assert.equal(isExternalAgentSession('astrid:session-1'), false);
  assert.equal(sessionAgentId('hermes:session-1'), 'hermes');
  assert.equal(sessionAgentId('codex:session-1'), 'codex');
});

function registryFor(project: Project) {
  const claimDirectory = mkdtempSync(join(tmpdir(), 'project-chat-test-'));
  const registry = new ProjectChatRegistry({
    getProject: async () => structuredClone(project),
    listProjects: async () => ({ items: [structuredClone(project)], next_cursor: null }),
    updateProject: async (_id, _key, version, _name, metadata) => {
      assert.equal(version, project.version);
      project = { ...project, version: version + 1, metadata: metadata! };
      return structuredClone(project);
    },
  }, 'test', Date.now, claimDirectory);
  return { registry, cleanup: () => rmSync(claimDirectory, { recursive: true, force: true }) };
}

test('New selects and promotes the created chat to Main', async () => {
  const project: Project = {
    project_id: 'p', realm_id: 'r', slug: 'p', name: 'p', version: 1,
    created_at: '', updated_at: '', metadata: {
      astrid_agent_chat_v1: { test: {
        revision: 4, selected_session_id: 'old', main_session_id: 'main',
        sessions: [{ id: 'main' }, { id: 'old' }], draft: { text: '', revision: 0, queued_messages: [] },
      } },
    },
  };
  const { registry, cleanup } = registryFor(project);
  try {
    let creates = 0;
    const create = async () => { creates++; return { sessionId: 'new-session' }; };
    const result = await registry.create('p', 'new', 'new-operation', create);
    assert.equal(result.selected_session_id, 'new-session');
    assert.equal(result.main_session_id, 'new-session');
    assert.deepEqual(result.sessions.map(session => session.id), ['main', 'old', 'new-session']);
    const replay = await registry.create('p', 'new', 'new-operation', create);
    assert.equal(creates, 1);
    assert.equal(replay.main_session_id, 'new-session');
    assert.equal(replay.selected_session_id, 'new-session');
    await registry.setMain('p', replay.revision, 'old');
    const delayedReplay = await registry.create('p', 'new', 'new-operation', create);
    assert.equal(creates, 1);
    assert.equal(delayedReplay.operation_session_id, 'new-session');
    assert.equal(delayedReplay.main_session_id, 'old');
  } finally {
    cleanup();
  }
});

test('New assigns Main when the project has no previous Main chat', async () => {
  const project: Project = { project_id: 'p', realm_id: 'r', slug: 'p', name: 'p', metadata: {}, version: 1, created_at: '', updated_at: '' };
  const { registry, cleanup } = registryFor(project);
  try {
    const result = await registry.create('p', 'new', 'new-operation', async () => ({ sessionId: 'new-session' }));
    assert.equal(result.selected_session_id, 'new-session');
    assert.equal(result.main_session_id, 'new-session');
  } finally {
    cleanup();
  }
});

test('failed chat creation clears its claim so an immediate retry can succeed', async () => {
  const project: Project = { project_id: 'p', realm_id: 'r', slug: 'p', name: 'p', metadata: {}, version: 1, created_at: '', updated_at: '' };
  const { registry, cleanup } = registryFor(project);
  try {
    let creates = 0;
    await assert.rejects(registry.create('p', 'new', 'retry-create', async () => {
      creates++;
      throw new Error('create failed');
    }), /create failed/);
    const result = await registry.create('p', 'new', 'retry-create', async () => {
      creates++;
      return { sessionId: 'retry-session' };
    });
    assert.equal(creates, 2);
    assert.equal(result.operation_session_id, 'retry-session');
    assert.equal(result.selected_session_id, 'retry-session');
  } finally {
    cleanup();
  }
});

test('New preserves a concurrent Make Main choice while selecting its created chat', async () => {
  const project: Project = {
    project_id: 'p', realm_id: 'r', slug: 'p', name: 'p', version: 1,
    created_at: '', updated_at: '', metadata: {
      astrid_agent_chat_v1: { test: {
        revision: 2, selected_session_id: 'main', main_session_id: 'main',
        sessions: [{ id: 'main' }, { id: 'other' }], draft: { text: '', revision: 0, queued_messages: [] },
      } },
    },
  };
  const { registry, cleanup } = registryFor(project);
  try {
    let finish!: (value: { sessionId: string }) => void;
    let started!: () => void;
    const beginning = new Promise<void>(resolve => { started = resolve; });
    const pending = registry.create('p', 'new', 'deferred-new', () => {
      started();
      return new Promise(resolve => { finish = resolve; });
    });
    await beginning;
    const explicitMain = await registry.setMain('p', (await registry.get('p')).revision, 'other');
    finish({ sessionId: 'new-session' });
    const completed = await pending;
    assert.equal(explicitMain.main_session_id, 'other');
    assert.equal(completed.main_session_id, 'other');
    assert.equal(completed.selected_session_id, 'new-session');
    assert.deepEqual(completed.sessions.map(session => session.id), ['main', 'other', 'new-session']);
  } finally {
    cleanup();
  }
});

test('rename trims titles, enforces ownership, and advances the project chat revision', async () => {
  const project: Project = { project_id: 'p', realm_id: 'r', slug: 'p', name: 'p', metadata: {
    astrid_agent_chat_v1: { test: { revision: 2, selected_session_id: 's1', main_session_id: null, sessions: [{ id: 's1' }], draft: { text: '', revision: 0, queued_messages: [] } } },
  }, version: 1, created_at: '', updated_at: '' };
  const { registry, cleanup } = registryFor(project);
  try {
    const renamed = await registry.rename('p', 2, 's1', '  Planning  ');
    assert.equal(renamed.sessions[0]?.title, 'Planning');
    assert.equal(renamed.revision, 3);
    await assert.rejects(registry.rename('p', 2, 's1', 'stale'), (error: unknown) => Boolean(error && typeof error === 'object' && 'status' in error && error.status === 409));
    for (const title of ['', '   ', 'x'.repeat(121)]) {
      await assert.rejects(registry.rename('p', 3, 's1', title), (error: unknown) => Boolean(error && typeof error === 'object' && 'status' in error && error.status === 400));
    }
    await assert.rejects(registry.rename('p', 3, 'foreign', 'Owned?'), (error: unknown) => Boolean(error && typeof error === 'object' && 'status' in error && error.status === 403));
    const title = 'x'.repeat(120);
    await registry.rename('p', 3, 's1', `  ${title}  `);
    assert.equal((await registry.get('p')).sessions[0]?.title, title);
  } finally {
    cleanup();
  }
});

test('ensure replaces an empty selected session in its existing chat entry when its transcript is missing', async () => {
  const project: Project = {
    project_id: 'p', realm_id: 'r', slug: 'p', name: 'p', version: 1,
    created_at: '', updated_at: '', metadata: {
      astrid_agent_chat_v1: { test: {
        revision: 4, selected_session_id: 'empty', main_session_id: 'empty',
        sessions: [{ id: 'older' }, { id: 'empty', title: 'Empty Claude chat', pending_first_message: true }, { id: 'newer' }],
        draft: { text: '', revision: 0, queued_messages: [] },
      } },
    },
  };
  const { registry, cleanup } = registryFor(project);
  try {
    let creates = 0;
    const result = await registry.create('p', 'ensure', 'reload-empty', async () => {
      creates++;
      return { sessionId: 'claude-code:fresh' };
    }, async () => false);
    assert.equal(creates, 1);
    assert.equal(result.operation_session_id, 'claude-code:fresh');
    assert.equal(result.selected_session_id, 'claude-code:fresh');
    assert.equal(result.main_session_id, 'claude-code:fresh');
    assert.deepEqual(result.sessions, [
      { id: 'older' },
      { id: 'claude-code:fresh', title: 'Empty Claude chat', pending_first_message: true },
      { id: 'newer' },
    ]);
  } finally {
    cleanup();
  }
});

test('targeted reload reuses an empty chat when its ACP session still resumes', async () => {
  const project: Project = {
    project_id: 'p', realm_id: 'r', slug: 'p', name: 'p', version: 1,
    created_at: '', updated_at: '', metadata: {
      astrid_agent_chat_v1: { test: {
        revision: 1, selected_session_id: 'codex:empty', main_session_id: 'other',
        sessions: [{ id: 'codex:empty', pending_first_message: true }, { id: 'other' }],
        draft: { text: '', revision: 0, queued_messages: [] },
      } },
    },
  };
  const { registry, cleanup } = registryFor(project);
  try {
    let creates = 0;
    const result = await registry.create('p', 'ensure', 'reload-still-live', async () => {
      creates++;
      return { sessionId: 'codex:unexpected' };
    }, async id => id === 'codex:empty', 'codex:empty');
    assert.equal(creates, 0);
    assert.equal(result.operation_session_id, 'codex:empty');
    assert.equal(result.selected_session_id, 'codex:empty');
    assert.equal(result.main_session_id, 'other');
    assert.deepEqual(result.sessions, [{ id: 'codex:empty', pending_first_message: true }, { id: 'other' }]);
  } finally {
    cleanup();
  }
});

test('empty marker and model metadata writes preserve the chat selection revision', async () => {
  const project: Project = {
    project_id: 'p', realm_id: 'r', slug: 'p', name: 'p', version: 1,
    created_at: '', updated_at: '', metadata: {
      astrid_agent_chat_v1: { test: {
        revision: 7, selected_session_id: 'codex:empty', main_session_id: null,
        sessions: [{ id: 'codex:empty', pending_first_message: true, model_id: 'alpha' }],
        draft: { text: '', revision: 0, queued_messages: [] },
      } },
    },
  };
  const { registry, cleanup } = registryFor(project);
  try {
    await registry.markSessionStarted('p', 'codex:empty');
    await registry.setSessionModel('p', 'codex:empty', 'beta');
    const result = await registry.get('p');
    assert.equal(result.revision, 7);
    assert.deepEqual(result.sessions, [{ id: 'codex:empty', pending_first_message: false, model_id: 'beta' }]);
  } finally {
    cleanup();
  }
});

test('identical draft retries acknowledge without writes while divergent stale payloads retain CAS protection', async () => {
  let project: Project = { project_id: 'p', realm_id: 'r', slug: 'p', name: 'p', metadata: {}, version: 1, created_at: '', updated_at: '' };
  let writes = 0;
  const registry = new ProjectChatRegistry({
    getProject: async () => structuredClone(project),
    listProjects: async () => ({ items: [structuredClone(project)], next_cursor: null }),
    updateProject: async (_id, _key, version, _name, metadata) => {
      assert.equal(version, project.version);
      writes++;
      project = { ...project, version: version + 1, metadata: metadata! };
      return structuredClone(project);
    },
  }, 'test');
  const queue = [{ id: 'q', text: 'queued', session_id: 's', attachments: [{ object_id: 'asset-a', variant: { id: 'v1' } }] }];
  assert.equal((await registry.draft('p', 0, '', [])).draft.revision, 0);
  assert.equal(writes, 0);
  const first = await registry.draft('p', 0, 'local', queue);
  assert.equal(first.draft.revision, 1);
  const duplicate = await registry.draft('p', 0, 'local', structuredClone(queue));
  assert.deepEqual(duplicate, first);
  assert.equal(writes, 1);
  for (const changed of [
    { text: 'other', queue },
    { text: 'local', queue: [] },
    { text: 'local', queue: [{ ...queue[0], session_id: 'other-session' }] },
    { text: 'local', queue: [{ ...queue[0], attachments: [{ object_id: 'asset-b', variant: { id: 'v1' } }] }] },
  ]) {
    await assert.rejects(registry.draft('p', 0, changed.text, changed.queue), (error: unknown) => Boolean(error && typeof error === 'object' && 'code' in error && error.code === 'draft_conflict'));
    assert.deepEqual(await registry.get('p'), first);
  }
  assert.equal(writes, 1);
});

test('parallel identical drafts survive project version retries without advancing twice', async () => {
  let project: Project = { project_id: 'p', realm_id: 'r', slug: 'p', name: 'p', metadata: {}, version: 1, created_at: '', updated_at: '' };
  let writes = 0;
  const registry = new ProjectChatRegistry({
    getProject: async () => structuredClone(project),
    listProjects: async () => ({ items: [structuredClone(project)], next_cursor: null }),
    updateProject: async (_id, _key, version, _name, metadata) => {
      if (version !== project.version) throw new ApiError(409, 'version_conflict', 'changed');
      writes++;
      project = { ...project, version: version + 1, metadata: metadata! };
      return structuredClone(project);
    },
  }, 'test');
  const [first, second] = await Promise.all([registry.draft('p', 0, 'same', []), registry.draft('p', 0, 'same', [])]);
  assert.deepEqual(first, second);
  assert.equal(writes, 1);
});
