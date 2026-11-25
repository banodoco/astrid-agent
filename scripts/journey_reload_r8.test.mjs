import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { unlinkSync } from 'node:fs';
import nodeTest from 'node:test';
import * as reload from './journey_reload_inflight_browser.mjs';

const test = (name, run) => nodeTest(name, { timeout: 5000 }, run);
const config = { origin: 'https://astrid.haus', projectId: 'p1', timelineId: 't1' };
const url = 'https://astrid.haus/tools/video-editor?project=p1&timeline=t1';

test('only the original queued request body and idempotency key qualify for retry', () => {
  const pending = { path: '/api/runtime/v1/projects/p1/timelines/t1/commit', body: '{"label":"SYNTHETIC"}', key: 'original-key' };
  const request = (body, key, requestUrl = 'https://astrid.haus/api/runtime/v1/projects/p1/timelines/t1/commit') => ({
    method: () => 'POST', url: () => requestUrl, postData: () => body, headers: () => ({ 'idempotency-key': key }),
  });
  assert.equal(reload.originalRetryMatches(request(pending.body, pending.key), pending, config), true);
  assert.equal(reload.originalRetryMatches(request('{"label":"other"}', pending.key), pending, config), false);
  assert.equal(reload.originalRetryMatches(request(pending.body, 'new-key'), pending, config), false);
  assert.equal(reload.originalRetryMatches(request(pending.body, pending.key,
    'https://astrid.haus/api/runtime/v1/projects/p1/timelines/t2/commit'), pending, config), false);
});

test('verified scoped recovery slot can offer Restore without outbox pending', () => {
  const slot = { projectId: 'p1', timelineId: 't1', ownerId: 'owner-1',
    outboxOwnerId: 'owner-1', markerMatch: true, payloadSha256: 'a'.repeat(64) };
  assert.equal(reload.recoveryOfferAllowed({ recoverySlot: slot, restoreVisible: true }), true);
  assert.equal(reload.recoveryOfferAllowed({ recoverySlot: slot, saveThemVisible: true }), true);
  assert.equal(reload.recoveryOfferAllowed({ recoverySlot: slot }), false, 'a hidden button is not an offer');
  assert.equal(reload.recoveryOfferAllowed({ restoreVisible: true }), false, 'an unverified/absent slot is not authority');
  assert.equal(reload.recoveryOfferAllowed({ pending: {}, restoreVisible: true }), true);
});

test('recovery slot readback requires exact project, timeline, owner link, and marker', async () => {
  const originalIndexedDB = globalThis.indexedDB;
  const row = { key: 't1', timelineId: 't1', projectId: 'p1', outboxOwnerId: 'owner-1',
    draft: { config: { tracks: [{ id: 'V1', label: 'SYNTHETIC' }] } } };
  const makeIndexedDB = (current) => ({ databases: async () => [{ name: 'astrid.timeline-drafts' }],
    open: () => {
      const request = { result: { objectStoreNames: { contains: () => true }, close() {},
        transaction: () => ({ objectStore: () => ({ openCursor: () => {
          const cursorRequest = {};
          setImmediate(() => {
            const cursor = current ? { key: current.key, value: current,
              continue: () => setImmediate(() => { cursorRequest.result = null; cursorRequest.onsuccess?.(); }) } : null;
            cursorRequest.result = cursor;
            cursorRequest.onsuccess?.();
          });
          return cursorRequest;
        } }) }),
      } };
      setImmediate(() => request.onsuccess?.());
      return request;
    } });
  const page = { evaluate: (fn, args) => fn(args) };
  const config = { projectId: 'p1', timelineId: 't1', draftLabel: 'SYNTHETIC' };
  try {
    globalThis.indexedDB = makeIndexedDB(row);
    const proof = await reload.verifiedRecoverySlot(page, config, { ownerId: 'owner-1' });
    assert.equal(proof.projectId, 'p1');
    assert.equal(proof.outboxOwnerId, 'owner-1');
    assert.match(proof.payloadSha256, /^[a-f0-9]{64}$/);
    assert.equal(reload.recoveryOfferAllowed({ recoverySlot: proof, restoreVisible: true }), true);

    for (const changed of [
      { ...row, projectId: undefined },
      { ...row, projectId: 'p2' },
      { ...row, timelineId: 't2' },
      { ...row, outboxOwnerId: 'owner-2' },
      { ...row, draft: { config: { tracks: [{ id: 'V1', label: 'OTHER' }] } } },
    ]) {
      globalThis.indexedDB = makeIndexedDB(changed);
      assert.equal(await reload.verifiedRecoverySlot(page, config, { ownerId: 'owner-1' }), null);
    }
  } finally {
    if (originalIndexedDB === undefined) delete globalThis.indexedDB;
    else globalThis.indexedDB = originalIndexedDB;
  }
});

test('fresh retry navigation must be selected main-frame R14 with the pinned index', async () => {
  const body = Buffer.from('pinned-index');
  const main = { id: 'main' };
  const makeResponse = (targetUrl = url, frame = main, bytes = body) => ({ ok: () => true,
    url: () => targetUrl, body: async () => bytes,
    request: () => ({ isNavigationRequest: () => true }), frame: () => frame });
  const page = { mainFrame: () => main };
  const pinnedConfig = { ...config, candidateIndexSha256: createHash('sha256').update(body).digest('hex') };
  assert.equal(await reload.isPinnedSelectedNavigation(makeResponse(), page, pinnedConfig), true);
  assert.equal(await reload.isPinnedSelectedNavigation(makeResponse(url.replace('project=p1', 'project=p2')),
    page, pinnedConfig), false);
  assert.equal(await reload.isPinnedSelectedNavigation(makeResponse(url, { id: 'other' }), page, pinnedConfig), false);
  assert.equal(await reload.isPinnedSelectedNavigation(makeResponse(url, main, Buffer.from('other')),
    page, pinnedConfig), false);
});

function response() {
  const main = { id: 'main' };
  return { response: { ok: () => true, url: () => url,
    request: () => ({ isNavigationRequest: () => true }), frame: () => main }, main };
}

test('selected commit during async listener setup is pre-click and does not invoke Reload', async () => {
  const nav = response();
  let resolveNavigation;
  const selected = new Promise((resolve) => { resolveNavigation = resolve; });
  let clicks = 0;
  const page = { mainFrame: () => nav.main, exposeBinding: async () => {},
    waitForNavigation: async () => nav.response };
  const button = { evaluate: async () => { await new Promise((resolve) => setImmediate(resolve)); resolveNavigation(nav.response); },
    click: async () => { clicks++; } };
  const result = await reload.clickAndObserveGuardedReload(page, button, config, null, () => {}, () => true, selected);
  assert.equal(result.pre_click_navigation, true);
  assert.equal(result.selected_navigation, null);
  assert.equal(result.watcher_navigation, true);
  assert.equal(clicks, 0);
});

test('unaudited pre-click commit fails promptly and does not claim watcher navigation', async () => {
  const nav = response();
  let resolveNavigation;
  const selected = new Promise((resolve) => { resolveNavigation = resolve; });
  let clicks = 0;
  const page = { mainFrame: () => nav.main, exposeBinding: async () => {},
    waitForNavigation: async () => nav.response };
  const button = { evaluate: async () => { resolveNavigation(nav.response); }, click: async () => { clicks++; } };
  await assert.rejects(reload.clickAndObserveGuardedReload(page, button, config, null, () => {}, () => false, selected),
    /before Reload click without a preceding CDP watcher event/);
  assert.equal(clicks, 0);
});

for (const [label, selected] of [
  ['wrong frame', { ...response(), response: { ...response().response, frame: () => ({ id: 'other' }) } }],
  ['wrong URL', { ...response(), response: { ...response().response, url: () => 'https://astrid.haus/tools/video-editor?project=p2&timeline=t1' } }],
]) {
  test(`rejects selected-looking commit from ${label}`, async () => {
    let resolveNavigation;
    let binding;
    const main = response().main;
    const navigation = new Promise((resolve) => { resolveNavigation = resolve; });
    const page = { mainFrame: () => main, exposeBinding: async (_name, callback) => { binding = callback; }, waitForNavigation: async () => selected.response };
    const button = { evaluate: async () => {}, click: async () => { binding({ frame: page.mainFrame() }); resolveNavigation(selected.response); } };
    await assert.rejects(reload.clickAndObserveGuardedReload(page, button, config, null, () => {}, () => false, navigation),
      /selected editor did not commit a main-frame document navigation/);
  });
}

test('committed selected main-frame response after click initiation is accepted when the old document disappears', async () => {
  const nav = response();
  let resolveNavigation;
  const selected = new Promise((resolve) => { resolveNavigation = resolve; });
  const page = { mainFrame: () => nav.main, exposeBinding: async () => {}, waitForNavigation: async () => nav.response };
  const button = { evaluate: async () => {}, click: async () => { resolveNavigation(nav.response); throw new Error('old document destroyed'); } };
  const result = await reload.clickAndObserveGuardedReload(page, button, config, null, () => {}, () => false, selected);
  assert.equal(result.selected_navigation.selected, true);
  assert.equal(result.selected_navigation.committed, true);
  assert.equal(result.selected_navigation.after_click_initiated, true);
  assert.equal(result.selected_navigation.url, url);
  assert.equal(result.watcher_navigation, false);
});

test('diagnostic captures text, indexed cursor keys and screenshot independently with explicit errors', async () => {
  const calls = [];
  const screenshot = Buffer.from('faithful-screenshot');
  const page = {
    evaluate: async (fn) => {
      if (String(fn).includes('document.body')) {
        globalThis.document = { body: { innerText: 'S'.repeat(3000) } };
        try { return fn(); } finally { delete globalThis.document; }
      }
      throw new Error('IndexedDB unavailable');
    },
    screenshot: async (options) => { calls.push(options); return screenshot; },
  };
  const screenshotPath = '/tmp/fix17-r8-diagnostic.jpg';
  const diagnostic = await reload.timeoutDiagnostics(page,
    { stage: 'observe-r14-restoration', diagnosticScreenshot: screenshotPath },
    { stalled: { step: 'restore', selector: 'Restore' } });
  assert.equal(diagnostic.visible_text_first_2kb.length, 2048);
  assert.equal(diagnostic.indexeddb.command_outbox.capture_error, 'Error');
  assert.equal(diagnostic.indexeddb.command_outbox.status, 'error');
  assert.equal(diagnostic.indexeddb.timeline_drafts.capture_error, 'Error');
  assert.equal(diagnostic.indexeddb.timeline_drafts.status, 'error');
  assert.equal(diagnostic.screenshot.bytes, screenshot.length);
  assert.equal(calls[0].type, 'jpeg');
  assert.equal(diagnostic.stalled.step, 'restore');
  unlinkSync(screenshotPath);
});

test('diagnostic lists actual bounded recovery and outbox keys and compares only the scoped outbox payload hash', async () => {
  const expectedDraft = { ownerId: 'owner-1', intentId: 'intent-1', projectId: 'p1', timelineId: 't1' };
  const payload = { after: { project_id: 'p1', timeline_id: 't1' } };
  expectedDraft.payloadSha256 = createHash('sha256').update(JSON.stringify(payload)).digest('hex');
  const records = {
    'astrid.command-outbox': [{ key: 'owner-1', value: { ownerId: 'owner-1', scope: { projectId: 'p1', timelineId: 't1' },
      intents: [{ id: 'intent-1', payload }] } }],
    'astrid.timeline-drafts': [{ key: 't1', value: { key: 't1', timelineId: 't1', outboxOwnerId: 'owner-1',
      baseHeadProjectId: 'p1', baseHeadTimelineId: 't1', draft: { config: { tracks: [{ id: 'V1', label: 'SYNTHETIC' }] } } } }],
  };
  globalThis.indexedDB = {
    databases: async () => Object.keys(records).map((name) => ({ name })),
    open: (name) => {
      const request = { result: { objectStoreNames: { contains: () => true }, close() {},
        transaction: (store) => ({ objectStore: () => ({ openCursor: () => {
          const entries = records[name];
          const cursorRequest = {};
          let index = 0;
          const emit = () => {
            const item = entries[index];
            const cursor = item ? { key: item.key, value: item.value, continue: () => { index++; setImmediate(emit); } } : null;
            cursorRequest.result = cursor;
            cursorRequest.onsuccess?.({ target: { result: cursor } });
          };
          setImmediate(emit);
          return cursorRequest;
        } }) }),
      } };
      setImmediate(() => request.onsuccess?.({ target: request }));
      return request;
    },
  };
  const page = { evaluate: async (fn, args) => {
    if (String(fn).includes('document.body')) return 'visible';
    return fn(args);
  }, screenshot: async () => Buffer.from('png') };
  const result = await reload.timeoutDiagnostics(page, { expectedDraft, projectId: 'p1', timelineId: 't1', draftLabel: 'SYNTHETIC' }, {});
  assert.deepEqual(result.indexeddb.command_outbox.indexed_keys, ['owner-1']);
  assert.equal(result.indexeddb.command_outbox.owner_key, 'owner-1');
  assert.equal(result.indexeddb.command_outbox.slot_payload_sha256_match, true);
  assert.deepEqual(result.indexeddb.timeline_drafts.indexed_keys, ['t1']);
  assert.equal(result.indexeddb.timeline_drafts.scoped_records[0].outboxOwnerId, 'owner-1');
  assert.equal(result.indexeddb.timeline_drafts.scoped_records[0].baseVersion, null);
  assert.equal(result.indexeddb.timeline_drafts.scoped_records[0].track_v1_matches_marker, true);
  delete globalThis.indexedDB;
});

test('timeline draft with unknown project scope keeps its key but is not reported as scoped', async () => {
  const entries = [{ key: 't1', value: { key: 't1', timelineId: 't1',
    draft: { config: { tracks: [{ id: 'V1', label: 'SYNTHETIC' }] } } } }];
  globalThis.indexedDB = {
    databases: async () => [{ name: 'astrid.timeline-drafts' }],
    open: () => {
      const request = { result: { objectStoreNames: { contains: () => true }, close() {},
        transaction: () => ({ objectStore: () => ({ openCursor: () => {
          const cursorRequest = {};
          let index = 0;
          const emit = () => {
            const item = entries[index];
            const cursor = item ? { key: item.key, value: item.value,
              continue: () => { index++; setImmediate(emit); } } : null;
            cursorRequest.result = cursor;
            cursorRequest.onsuccess?.({ target: { result: cursor } });
          };
          setImmediate(emit);
          return cursorRequest;
        } }) }),
      } };
      setImmediate(() => request.onsuccess?.({ target: request }));
      return request;
    },
  };
  const page = { evaluate: async (fn, args) => String(fn).includes('document.body') ? 'visible' : fn(args),
    screenshot: async () => Buffer.from('image') };
  const result = await reload.timeoutDiagnostics(page, { projectId: 'p1', timelineId: 't1',
    expectedDraft: { ownerId: 'owner-p1' } }, {});
  assert.deepEqual(result.indexeddb.timeline_drafts.indexed_keys, ['t1']);
  assert.deepEqual(result.indexeddb.timeline_drafts.scoped_records, []);
  assert.deepEqual(result.indexeddb.timeline_drafts.unknown_scope_records,
    [{ key: 't1', timelineId: 't1', projectId: null, scope_status: 'unknown' }]);
  delete globalThis.indexedDB;
});
