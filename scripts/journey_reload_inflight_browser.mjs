#!/usr/bin/env node
/** Keep the shipped R13 document and its pending save alive during the native R14 update. */
import { existsSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import {
  author, check, commitPost, findDraft, runtimePath,
  selectedEditorUrl, sha256, trackName, waitAt, withBrowserPage,
} from './journey_required_update_browser.mjs';

const UI_TIMEOUT = 45_000;
const NOTICE = 'Editor updated · Reload';
const REFUSAL = /^Could not safely reload/;

function withTimeout(promise, timeout, message) {
  let timer;
  const deadline = new Promise((_, reject) => {
    timer = setTimeout(() => {
      const error = new Error(message);
      error.name = 'TimeoutError';
      reject(error);
    }, timeout);
  });
  return Promise.race([promise, deadline]).finally(() => clearTimeout(timer));
}

export function originalRetryMatches(request, pending, config) {
  const url = new URL(request.url());
  const expectedPath = `/v1/projects/${encodeURIComponent(config.projectId)}/timelines/${encodeURIComponent(config.timelineId)}/commit`;
  return Boolean(pending?.path && pending?.body && pending?.key
    && request.method() === 'POST'
    && url.origin === config.origin
    && runtimePath(url.pathname) === runtimePath(pending.path)
    && runtimePath(url.pathname) === expectedPath
    && request.postData() === pending.body
    && request.headers()['idempotency-key'] === pending.key);
}

export async function clickAndObserveGuardedReload(page, button, config, refusalSignal = null, onTrustedClick = () => {}, watcherWon = () => false, selectedNavigationSeen = null, onClickInitiated = () => {}) {
  let clickObserved = false;
  let clickInitiated = false;
  let selectedAfterClick = null;
  const selected = selectedNavigationSeen?.then((response) => {
    if (clickInitiated) selectedAfterClick = response;
    return response;
  });
  const instrument = async (name, operation) => {
    const task = operation().then((value) => ({ value }), (error) => ({ error }));
    const boundedTask = withTimeout(task, UI_TIMEOUT, `Timed out at ${name}`).then((result) => result, (error) => ({
      error, stalled: { step: name, selector: name === 'attach-trusted-click-binding' ? 'page.exposeBinding' : 'Reload button click listener' },
    }));
    const winner = selected ? await Promise.race([boundedTask, selected.then((response) => ({ navigation: response }))]) : await boundedTask;
    if (winner.stalled) { winner.error.stalled = winner.stalled; throw winner.error; }
    if (winner.navigation) {
      if (watcherWon()) return { navigation: winner.navigation, preClick: true };
      // Navigation can destroy the old document and strand setup work. Fail
      // immediately unless the audited CDP reload event preceded click start.
      return { navigation: winner.navigation, preClick: true, unaudited: true };
    }
    if (winner.error) throw winner.error;
    return winner;
  };
  const bindingName = '__fix17ObserveGuardedReload';
  const binding = await instrument('attach-trusted-click-binding', () => page.exposeBinding(bindingName, ({ frame }) => {
    if (frame === page.mainFrame()) {
      clickObserved = true;
      onTrustedClick();
    }
  }));
  if (binding?.preClick) {
    check(!binding.unaudited, 'Selected navigation began before Reload click without a preceding CDP watcher event');
    return { response: binding.navigation, refused: false, selected_navigation: null,
      click_observed: false, watcher_navigation: true, pre_click_navigation: true };
  }
  const listener = await instrument('attach-original-document-click-listener', () => button.evaluate((element, name) => {
    element.addEventListener('click', (event) => {
      if (event.isTrusted && window.__fix17OriginalDocument === true) {
        void window[name]().catch(() => {});
      }
    }, { capture: true, once: true });
  }, bindingName));
  if (listener?.preClick) {
    check(!listener.unaudited, 'Selected navigation began before Reload click without a preceding CDP watcher event');
    return { response: listener.navigation, refused: false, selected_navigation: null,
      click_observed: false, watcher_navigation: true, pre_click_navigation: true };
  }
  // Set the attempt receipt at the exact point the browser click is invoked.
  clickInitiated = true;
  onClickInitiated();
  const click = withTimeout(button.click({ timeout: UI_TIMEOUT, noWaitAfter: true }), UI_TIMEOUT,
    'Timed out at click-guarded-reload').then(() => ({ clicked: true }), (error) => {
      if (error?.name === 'TimeoutError') error.stalled = { step: 'click-guarded-reload', selector: 'button[name="Reload"]' };
      return { error };
    });
  const clickFailure = click.then((result) => result.error ? ({ clickError: result.error }) : new Promise(() => {}));
  const navigation = selected ? selected.then((response) => ({ response })) : waitAt('wait-for-r14-navigation', 'main-frame editor document commit',
    () => page.waitForNavigation({ waitUntil: 'commit', timeout: UI_TIMEOUT })).then((response) => ({ response }));
  const refused = refusalSignal?.then((value) => value ? ({ refused: true }) : navigation) ?? navigation;
  let result;
  try { result = await withTimeout(Promise.race([navigation, refused, clickFailure]), UI_TIMEOUT,
    'Timed out waiting for committed selected R14 navigation, Reload refusal, or click operation'); }
  catch (error) {
    if (error?.name === 'TimeoutError') error.stalled = { step: 'wait-for-selected-navigation-after-click', selector: 'committed selected main-frame editor navigation or Reload refusal' };
    throw error;
  }
  if (result.clickError) {
    if (selectedAfterClick) result = { response: selectedAfterClick };
    else {
      // Playwright can report that the old document disappeared before it
      // acknowledges the click. Let the already armed navigation settle so a
      // wrong committed URL retains its useful validation error.
      try { result = { response: await withTimeout(navigation.then((item) => item.response), UI_TIMEOUT,
        'Timed out waiting for committed navigation after click acknowledgement failure') }; }
      catch { throw result.clickError; }
    }
  }
  if (result.refused) return { ...result, selected_navigation: null, click_observed: clickObserved };
  check(result.response?.request()?.isNavigationRequest() && result.response.frame() === page.mainFrame()
    && selectedEditorUrl(new URL(result.response.url()), config),
  'The selected editor did not commit a main-frame document navigation');
  if (!result.response) result = { ...result, response: selectedAfterClick };
  // A selected main-frame response committed after the click was initiated is
  // independent proof that Reload ran. The old document can disappear before
  // its exposed binding reports the trusted click.
  let selected_navigation = null;
  if (!result.refused && clickInitiated && result.response?.request()?.isNavigationRequest()
    && result.response.frame() === page.mainFrame()
    && selectedNavigationSeen
    && selectedEditorUrl(new URL(result.response.url()), config)) {
    selected_navigation = { selected: true, committed: true, after_click_initiated: true,
      trigger: 'selected-navigation', frame: 'main', url: result.response.url() };
  }
  if (!result.refused && !(clickObserved || selected_navigation?.selected === true || watcherWon())) {
    check(false, 'The original document did not observe a trusted Reload click');
  }
  return { ...result, selected_navigation, click_observed: clickObserved,
    // This compatibility flag means a committed navigation supplied fallback
    // trigger evidence; the receipt keeps actual watcher evidence separate.
    watcher_navigation: Boolean(watcherWon()) };
}

function writeReceipt(path, value) {
  const temp = path + '.tmp';
  writeFileSync(temp, JSON.stringify(value, null, 2) + '\n', { mode: 0o600 });
  renameSync(temp, path);
}

export async function isPinnedSelectedNavigation(response, page, config) {
  return Boolean(response?.ok() && response.request()?.isNavigationRequest()
    && response.frame() === page.mainFrame()
    && selectedEditorUrl(new URL(response.url()), config)
    && sha256(await response.body()) === config.candidateIndexSha256);
}

// The recovery banner is backed by timeline-drafts, independently of the
// command outbox. Require explicit project/timeline scope, its original owner
// link, and the marker before using that slot as Restore/Save them authority.
export async function verifiedRecoverySlot(page, config, draft) {
  return page.evaluate(async ({ projectId, timelineId, ownerId, marker }) => {
    const database = 'astrid.timeline-drafts';
    let db;
    try {
      if (!(await indexedDB.databases()).some((entry) => entry.name === database)) return null;
      db = await new Promise((resolve, reject) => {
        const request = indexedDB.open(database);
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error);
      });
      if (!db.objectStoreNames.contains('timeline-drafts')) return null;
      return await new Promise((resolve, reject) => {
        const tx = db.transaction('timeline-drafts', 'readonly');
        const request = tx.objectStore('timeline-drafts').openCursor();
        let scanned = 0;
        request.onerror = () => reject(request.error);
        request.onsuccess = async () => {
          const cursor = request.result;
          if (!cursor || scanned++ >= 128) return resolve(null);
          const row = cursor.value;
          const scopedProject = row?.projectId ?? row?.scope?.projectId
            ?? row?.baseHeadProjectId ?? row?.baseHead?.projectId;
          const scopedTimeline = row?.timelineId ?? row?.scope?.timelineId
            ?? row?.baseHeadTimelineId ?? row?.baseHead?.timelineId;
          const tracks = row?.draft?.config?.tracks;
          const markerMatch = Array.isArray(tracks)
            && tracks.filter((track) => track?.id === 'V1' && track.label === marker).length === 1;
          if (scopedProject === projectId && scopedTimeline === timelineId
            && row?.outboxOwnerId === ownerId && markerMatch) {
            const bytes = new TextEncoder().encode(JSON.stringify(row.draft));
            const digest = await crypto.subtle.digest('SHA-256', bytes);
            const payloadSha256 = [...new Uint8Array(digest)]
              .map((value) => value.toString(16).padStart(2, '0')).join('');
            return resolve({ key: row.key ?? cursor.key, projectId: scopedProject,
              timelineId: scopedTimeline, ownerId, outboxOwnerId: row.outboxOwnerId,
              payloadSha256, markerMatch: true });
          }
          cursor.continue();
        };
        tx.onabort = () => reject(tx.error);
      });
    } catch { return null; }
    finally { db?.close(); }
  }, { projectId: config.projectId, timelineId: config.timelineId,
    ownerId: draft?.ownerId, marker: config.draftLabel });
}

export function recoveryOfferAllowed({ pending, recoverySlot, restoreVisible, saveThemVisible }) {
  return Boolean(restoreVisible || saveThemVisible) && Boolean(pending || recoverySlot);
}

async function observeRestore(page, config, response, draft, originalDraftVerified) {
  check(response?.ok() && selectedEditorUrl(new URL(response.url()), config), 'No R14 editor navigation response');
  check(sha256(await response.body()) === config.candidateIndexSha256, 'Reload did not receive the pinned R14 editor page');
  check(response?.request()?.isNavigationRequest() && response.frame() === page.mainFrame(),
    'The selected R14 main-frame response did not replace the original document');
  const track = trackName(page);
  const restoreButton = page.locator('[data-save-bar="recovered"]').getByRole('button', { name: 'Restore', exact: true });
  const arrival = page.locator('[data-testid="runtime-connection-status"][data-state="connected"]');
  await waitAt('wait-for-r14-runtime', '[data-testid="runtime-connection-status"][data-state="connected"]',
    () => arrival.waitFor({ state: 'attached', timeout: UI_TIMEOUT }));
  await waitAt('wait-for-editor-card', '[data-testid="runtime-not-connected"] detached',
    () => page.getByTestId('runtime-not-connected').waitFor({ state: 'detached', timeout: UI_TIMEOUT }));
  await waitAt('wait-for-restored-track', 'textbox[name="Track name"] visible',
    () => track.waitFor({ state: 'visible', timeout: UI_TIMEOUT }));
  let restored = null;
  let pending = null;
  let matchedDraft = null;
  let recoverySlot = null;
  let offered = false;
  const saveThemButton = page.locator('[data-save-bar="recovered"]').getByRole('button', { name: 'Save them', exact: true });
  const retryButton = page.locator('[data-save-bar]').getByRole('button', { name: 'Try again', exact: true });
  const deadline = Date.now() + 15_000;
  let originalPending = null;
  do {
    restored = await track.inputValue({ timeout: 2000 }).catch(() => null);
    const match = await findDraft(page, { ...config, expectedDraft: draft });
    pending = match?.draft ?? null;
    originalPending = match?.pending ?? originalPending;
    if (pending) matchedDraft = pending;
    recoverySlot = await verifiedRecoverySlot(page, config, draft);
    const retryVisible = await retryButton.isVisible().catch(() => false);
    const restoreVisible = await restoreButton.isVisible().catch(() => false);
    const saveThemVisible = await saveThemButton.isVisible().catch(() => false);
    offered = recoveryOfferAllowed({ pending, recoverySlot, restoreVisible, saveThemVisible });
    if (retryVisible && pending) offered = true;
    if (restored === config.draftLabel || offered) break;
    await page.waitForTimeout(150);
  } while (Date.now() < deadline);
  let recovery = { action: 'automatic', method: 'automatic', completed: false };
  if (offered && await retryButton.isVisible().catch(() => false)) {
    check(originalPending?.body && originalPending?.key && matchedDraft
      && matchedDraft.requestSha256 === sha256(originalPending.body)
      && matchedDraft.keySha256 === sha256(originalPending.key),
    'Try again was offered without the exact original queued request');
    const allowed = new Set();
    const attempts = [];
    await page.route('**/*', (route) => {
      const request = route.request();
      if (!commitPost(request)) return route.continue();
      const exact = originalRetryMatches(request, originalPending, config);
      const permit = exact && allowed.size === 0;
      attempts.push({ body_sha256: sha256(request.postData() ?? ''), key_sha256: sha256(request.headers()['idempotency-key'] ?? ''), disposition: permit ? 'allowed' : 'blocked' });
      if (!permit) return route.abort('internetdisconnected');
      allowed.add(request);
      return route.continue();
    });
    await waitAt('wait-for-r14-retry-ready', '[data-save-bar] button[name="Try again"]', () => retryButton.click({ trial: true, timeout: UI_TIMEOUT }));
    const accepted = page.waitForResponse((item) => allowed.has(item.request()), { timeout: UI_TIMEOUT })
      .then(async (item) => ({ response: item, body: await item.json() }));
    await waitAt('click-r14-retry-original-request', '[data-save-bar] button[name="Try again"]', () => retryButton.click({ timeout: UI_TIMEOUT }));
    const { response: retryResponse, body } = await accepted;
    check(retryResponse.ok() && body.data?.status === 'accepted' && body.data.accepted_revision_id
      && body.receipt?.receipt_id && body.receipt.idempotency_key === originalPending.key,
    'Try again did not accept the original queued edit');
    const acceptedReceipt = { receipt_id: body.receipt.receipt_id,
      key_sha256: sha256(body.receipt.idempotency_key),
      accepted_revision_id: body.data.accepted_revision_id,
      status: body.data.status, response_status: retryResponse.status() };
    const queueDeadline = Date.now() + UI_TIMEOUT;
    while (await findDraft(page, { ...config, expectedDraft: draft })) {
      check(Date.now() < queueDeadline, 'Accepted request remained in the save queue');
      await page.waitForTimeout(150);
    }
    const queueRemoval = { empty_after_acceptance: true,
      accepted_revision_id: acceptedReceipt.accepted_revision_id,
      receipt_id: acceptedReceipt.receipt_id,
      key_sha256: acceptedReceipt.key_sha256 };
    // Reload into a new R14 document with all writes blocked and verify the accepted label.
    const freshResponse = await page.reload({ waitUntil: 'domcontentloaded', timeout: UI_TIMEOUT });
    check(await isPinnedSelectedNavigation(freshResponse, page, config),
      'Fresh retry readback did not receive the selected main-frame pinned R14 editor navigation');
    await waitAt('wait-for-fresh-r14-runtime', '[data-testid="runtime-connection-status"][data-state="connected"]',
      () => page.locator('[data-testid="runtime-connection-status"][data-state="connected"]').waitFor({ state: 'attached', timeout: UI_TIMEOUT }));
    restored = await waitAt('read-fresh-r14-track-label', 'textbox[name="Track name"]', () => track.inputValue({ timeout: UI_TIMEOUT }));
    recovery = { action: 'try-again', method: 'try-again', clicked: true, draft_provenance: true,
      accepted_revision_id: body.data.accepted_revision_id, queue_removed: true,
      accepted_receipt: acceptedReceipt, queue_removal: queueRemoval,
      fresh_navigation: { url: freshResponse.url(), index_sha256: config.candidateIndexSha256,
        selected: true, main_frame: true },
      retry_attempts: attempts, fresh_label_readback: restored, completed: restored === config.draftLabel };
  } else if (offered && restored !== config.draftLabel) {
    check((pending && Object.keys(draft).every((key) => pending[key] === draft[key])) || recoverySlot,
      'Restore was offered without matching pending provenance or a verified scoped recovery slot');
    const recoveryButton = await restoreButton.isVisible() ? restoreButton : saveThemButton;
    const action = recoveryButton === restoreButton ? 'restore' : 'save-them';
    await waitAt(action === 'restore' ? 'restore-inflight-draft' : 'save-them-inflight-draft',
      `[data-save-bar="recovered"] button[name="${action === 'restore' ? 'Restore' : 'Save them'}"]`,
      () => recoveryButton.click({ timeout: UI_TIMEOUT }));
    await waitAt('readback-inflight-draft', 'textbox[name="Track name"] containing the in-flight marker', async () => {
      const deadline = Date.now() + UI_TIMEOUT;
      while (Date.now() < deadline) {
        restored = await track.inputValue({ timeout: 2000 }).catch(() => null);
        if (restored === config.draftLabel) return;
        await page.waitForTimeout(150);
      }
      const error = new Error('Restored draft readback did not contain the in-flight marker');
      error.name = 'TimeoutError';
      error.stalled = { step: 'readback-inflight-draft', selector: 'textbox[name="Track name"] containing the in-flight marker' };
      throw error;
    });
    recovery = { action, method: action, clicked: true, draft_provenance: true,
      ...(recoverySlot ? { recovery_slot_proof: recoverySlot } : {}),
      completed: restored === config.draftLabel };
  } else {
    const provenance = Boolean(originalDraftVerified);
    recovery = { action: 'automatic', method: 'automatic', draft_provenance: provenance,
      completed: restored === config.draftLabel && provenance };
  }
  const url = new URL(page.url());
  const persisted = (await findDraft(page, { ...config, expectedDraft: draft }))?.draft ?? null;
  const comparedReadback = Boolean(persisted) && Object.keys(draft).every((key) => persisted[key] === draft[key]);
  const readbackFailed = !recovery.completed;
  const diagnostics = readbackFailed
    ? await timeoutDiagnostics(page, { ...config, expectedDraft: draft, stage: 'observe-r14-restoration' },
      { stalled: { step: 'readback-inflight-draft', selector: 'Restore offer or textbox[name="Track name"] containing the in-flight marker' } })
    : null;
  return {
    app_sha: config.expectedApp, index_sha256: config.candidateIndexSha256, document_replaced: true,
    project_id: url.searchParams.get('project'), timeline_id: url.searchParams.get('timeline'),
    draft_digest: draft.payloadSha256,
    persisted_label: restored, readback_label: restored, offered, recovery, method: recovery.method,
    pending: Boolean(matchedDraft), draft_label: config.draftLabel, draft_id: matchedDraft?.intentId ?? null,
    recovery_slot_status: recoverySlot ? 'verified' : 'unverified-or-absent',
    ...(recoverySlot ? { recovery_slot_proof: recoverySlot } : {}),
    required_update_readback: { compared: comparedReadback,
      matches_original_draft: comparedReadback || Boolean(originalDraftVerified) && recovery.completed && restored === config.draftLabel,
      payload_sha256: persisted?.payloadSha256 ?? null },
    ...(diagnostics ? { timeout_diagnostics: diagnostics } : {}),
  };
}

export async function timeoutDiagnostics(page, config, error) {
  const bounded = async (name, work) => {
    try { return await withTimeout(work(), 5000, `Timed out capturing ${name}`); }
    catch (cause) { return { status: 'error', capture_error: cause?.name || 'capture-failed' }; }
  };
  const textCapture = bounded('visible text', () => page.evaluate(() => {
    const text = document.body?.innerText;
    if (typeof text !== 'string') throw new Error('visible text unavailable');
    return new TextDecoder().decode(new TextEncoder().encode(text).subarray(0, 2048));
  }));
  const indexeddb = {};
  const outboxCapture = bounded('command outbox', () => page.evaluate(async (expected) => {
    const output = { database: 'astrid.command-outbox', store: 'owners', status: 'missing', bounded_scan_limit: 128,
      indexed_keys: [], indexed_keys_omitted: 0, cursor_complete: false, present: false, owner_key: null,
      scope_match: false, intent_match: false,
      payload_sha256: null, slot_payload_sha256_match: false };
    let db;
    try {
      if (!(await indexedDB.databases()).some((entry) => entry.name === output.database)) return output;
      db = await new Promise((resolve, reject) => {
        const request = indexedDB.open(output.database);
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error);
      });
      if (!db.objectStoreNames.contains(output.store)) { output.status = 'store-missing'; return output; }
      const rows = await new Promise((resolve, reject) => {
        const tx = db.transaction(output.store, 'readonly');
        const request = tx.objectStore(output.store).openCursor();
        let scanned = 0;
        let found = null;
        request.onerror = () => reject(request.error);
        request.onsuccess = async () => {
          const cursor = request.result;
          if (!cursor || scanned >= output.bounded_scan_limit) { output.cursor_complete = !cursor; return resolve(found); }
          scanned++;
          const keyBytes = JSON.stringify(cursor.key);
          if (keyBytes.length <= 256 && output.indexed_keys.length < 64) output.indexed_keys.push(cursor.key);
          else output.indexed_keys_omitted++;
          if (cursor.value?.ownerId === expected.ownerId) found = { key: cursor.key, value: cursor.value };
          cursor.continue();
        };
        tx.onabort = () => reject(tx.error);
      });
      output.status = output.cursor_complete ? 'complete' : 'truncated';
      const match = rows;
      if (!match) return output;
      const row = match.value;
      output.present = true;
      output.owner_key = match.key;
      output.scope_match = row.scope?.projectId === expected.projectId
        && row.scope?.timelineId === expected.timelineId;
      const intent = row.intents?.find((entry) => entry.id === expected.intentId);
      output.intent_match = Boolean(intent);
      if (output.scope_match && intent?.payload) {
        const bytes = new TextEncoder().encode(JSON.stringify(intent.payload));
        const digest = await crypto.subtle.digest('SHA-256', bytes);
        output.payload_sha256 = [...new Uint8Array(digest)].map((value) => value.toString(16).padStart(2, '0')).join('');
        output.slot_payload_sha256_match = output.payload_sha256 === expected.payloadSha256;
        output.intent_payload_sha256 = output.payload_sha256;
      }
      return output;
    } catch (cause) {
      return { ...output, status: 'error', capture_error: cause?.name || 'indexeddb-read-failed' };
    } finally { db?.close(); }
  }, config.expectedDraft ?? {}));
  const recoveryCapture = bounded('timeline recovery draft', () => page.evaluate(async (expected) => {
    const output = { database: 'astrid.timeline-drafts', store: 'timeline-drafts', status: 'missing',
      bounded_scan_limit: 128, indexed_keys: [], indexed_keys_omitted: 0, cursor_complete: false,
      scoped_records: [], scoped_records_omitted: 0 };
    let db;
    try {
      if (!(await indexedDB.databases()).some((entry) => entry.name === output.database)) return output;
      db = await new Promise((resolve, reject) => {
        const request = indexedDB.open(output.database);
        request.onsuccess = () => resolve(request.result);
        request.onerror = () => reject(request.error);
      });
      if (!db.objectStoreNames.contains(output.store)) { output.status = 'store-missing'; return output; }
      await new Promise((resolve, reject) => {
        const tx = db.transaction(output.store, 'readonly');
        const request = tx.objectStore(output.store).openCursor();
        let scanned = 0;
        request.onerror = () => reject(request.error);
        request.onsuccess = async () => {
          const cursor = request.result;
          if (!cursor || scanned >= output.bounded_scan_limit) {
            output.cursor_complete = !cursor;
            output.status = cursor ? 'truncated' : 'complete';
            return resolve();
          }
          scanned++;
          const keyBytes = JSON.stringify(cursor.key);
          if (keyBytes.length <= 256 && output.indexed_keys.length < 64) output.indexed_keys.push(cursor.key);
          else output.indexed_keys_omitted++;
          const row = cursor.value;
          const timelineMatch = row?.timelineId === expected.timelineId || row?.key === expected.timelineId;
          const projectId = row?.projectId ?? row?.scope?.projectId ?? row?.baseHeadProjectId ?? row?.baseHead?.projectId;
          const metadataProjectMatch = projectId === expected.projectId;
          const ownerLinkMatch = Boolean(expected.ownerId && row?.outboxOwnerId === expected.ownerId);
          const scopeVerified = metadataProjectMatch || ownerLinkMatch;
          if (timelineMatch && scopeVerified) {
            if (output.scoped_records.length < 8) {
              const tracks = row.draft?.config?.tracks;
              const v1 = Array.isArray(tracks) ? tracks.filter((track) => track?.id === 'V1') : [];
              const recoveryPayload = row.draft ?? null;
              const recoveryShape = recoveryPayload && typeof recoveryPayload === 'object'
                ? Object.keys(recoveryPayload).sort().join(',') : null;
              const intentShape = expected.intentPayloadShape ?? null;
              const recoveryPayloadSha256 = recoveryPayload
                ? await crypto.subtle.digest('SHA-256', new TextEncoder().encode(JSON.stringify(recoveryPayload)))
                    .then((digest) => [...new Uint8Array(digest)].map((value) => value.toString(16).padStart(2, '0')).join(''))
                : null;
              output.scoped_records.push({ key: row.key, timelineId: row.timelineId,
                projectId: projectId ?? null, scope_status: 'verified',
                scope_basis: metadataProjectMatch ? 'project-metadata' : 'scoped-owner-link',
                ownerId: row.ownerId ?? null, outboxOwnerId: row.outboxOwnerId ?? null,
                baseVersion: row.baseVersion ?? null, updatedAt: row.updatedAt ?? null,
                baseHeadProjectId: row.baseHeadProjectId ?? null,
                baseHeadTimelineId: row.baseHeadTimelineId ?? null,
                baseHeadRevisionId: row.baseHeadRevisionId ?? null,
                draftIdentity: row.draftIdentity ?? null,
                acknowledgementIdentity: row.acknowledgementIdentity ?? null,
                recovery_payload_sha256: recoveryPayloadSha256,
                intent_payload_sha256: expected.payloadSha256 ?? null,
                payload_hash_relationship: !recoveryPayload || !intentShape ? 'not-comparable'
                  : recoveryShape === intentShape ? (recoveryPayloadSha256 === expected.payloadSha256 ? 'matches' : 'differs')
                    : 'not-comparable',
                track_v1_matches_marker:
                  v1.length === 1 && v1[0].label === expected.draftLabel });
            } else output.scoped_records_omitted++;
          } else if (timelineMatch) {
            output.unknown_scope_records ??= [];
            output.unknown_scope_records_omitted ??= 0;
            if (output.unknown_scope_records.length < 8) {
              output.unknown_scope_records.push({ key: cursor.key, timelineId: row?.timelineId ?? null,
                projectId: projectId ?? null, scope_status: 'unknown' });
            } else output.unknown_scope_records_omitted++;
          }
          cursor.continue();
        };
        tx.onabort = () => reject(tx.error);
      });
      return output;
    } catch (cause) {
      return { ...output, status: 'error', capture_error: cause?.name || 'indexeddb-read-failed' };
    } finally { db?.close(); }
  }, { projectId: config.projectId, timelineId: config.timelineId, draftLabel: config.draftLabel,
    ownerId: config.expectedDraft?.ownerId ?? null,
    payloadSha256: config.expectedDraft?.payloadSha256 ?? null,
    intentPayloadShape: config.expectedDraft?.intentPayloadShape ?? null }));
  const screenshotCapture = bounded('screenshot', async () => {
    if (!config.diagnosticScreenshot) throw new Error('screenshot path unavailable');
    const bytes = await page.screenshot({ type: 'jpeg', quality: 55, fullPage: false, timeout: 5000 });
    if (bytes.length > 2 * 1024 * 1024) throw new Error('screenshot exceeds 2 MiB cap');
    writeFileSync(config.diagnosticScreenshot, bytes, { mode: 0o600 });
    return { status: 'captured', path: config.diagnosticScreenshot, bytes: bytes.length, format: 'jpeg' };
  });
  const [visibleText, outbox, recoveryDraft, screenshot] = await Promise.all([textCapture, outboxCapture, recoveryCapture, screenshotCapture]);
  indexeddb.command_outbox = outbox;
  indexeddb.timeline_drafts = recoveryDraft;
  return { stage: config.stage, stalled: error?.stalled ?? null,
    visible_text_first_2kb: visibleText, indexeddb, screenshot };
}

async function reloadInflight(page, config) {
  const receipt = {
    status: 'fail', mode: config.mode, origin: config.origin, stage: 'start',
    reload: { clicked: false, click_observed: false, watcher_navigation: null }, r14_audit: { requests: [], r13_save_requests_after_switch: [] },
  };
  let heldRoute = null;
  let switched = false;
  let newDocument = false;
  let originalFrameId = null;
  let cdp = null;
  let watcherNavigation = null;
  let r14BuildResponse = null;
  let selectedResponse = null;
  let verifiedDraft = null;
  let resolveSelectedNavigation;
  let resolveR14Build;
  const selectedNavigationSeen = new Promise((resolve) => { resolveSelectedNavigation = resolve; });
  const r14BuildSeen = new Promise((resolve) => { resolveR14Build = resolve; });
  const buildUrl = config.origin + '/build.json';

  const record = (stage) => {
    receipt.stage = stage;
    config.stage = stage;
    writeReceipt(config.result, receipt);
  };
  const onNavigation = (frame) => {
    if (existsSync(config.publishedPath) && frame === page.mainFrame()
      && selectedResponse?.request()?.isNavigationRequest()
      && selectedResponse.frame() === page.mainFrame()
      && selectedEditorUrl(new URL(frame.url()), config)
      && selectedEditorUrl(new URL(selectedResponse.url()), config)) {
      newDocument = true;
      const navigationAudit = { frame: 'main', selected: true, after_r14_switch: true, committed: true };
      receipt.r14_audit.selected_navigation = navigationAudit;
      resolveSelectedNavigation(selectedResponse);
      writeReceipt(config.result, receipt);
    }
  };
  const onResponse = async (response) => {
    const request = response.request();
    const url = new URL(response.url());
    if (existsSync(config.publishedPath) && request.isNavigationRequest()
      && response.frame() === page.mainFrame() && selectedEditorUrl(url, config)) {
      selectedResponse = response;
      if (newDocument && selectedEditorUrl(new URL(page.mainFrame().url()), config)) {
        const navigationAudit = { frame: 'main', selected: true, after_r14_switch: true, committed: true };
        receipt.r14_audit.selected_navigation = navigationAudit;
        resolveSelectedNavigation(response);
        writeReceipt(config.result, receipt);
      }
    }
    if (response.ok() && response.url() === buildUrl && !r14BuildResponse) {
      try {
        const bytes = await response.body();
        const build = JSON.parse(bytes.toString('utf8'));
        if (build.app === config.expectedApp && build.release === 14
          && sha256(bytes) === config.candidateBuildSha256) {
          r14BuildResponse = response;
          resolveR14Build(response);
        }
      } catch { /* Ignore non-R14 or incomplete build responses. */ }
    }
  };
  const onRequest = (request) => {
    if (!switched && !existsSync(config.publishedPath)) return;
    const url = new URL(request.url());
    if (commitPost(request) && !newDocument) {
      receipt.r14_audit.r13_save_requests_after_switch.push({
        method: request.method(), path: runtimePath(url.pathname), disposition: 'blocked',
        body_sha256: sha256(request.postData() ?? ''),
      });
    }
    if (url.origin === config.origin && (url.pathname === '/build.json' || request.isNavigationRequest())) {
      const audit = {
        method: request.method(), path: url.pathname, source: newDocument ? 'R14' : 'R13',
        kind: request.isNavigationRequest() ? 'navigation' : 'build-watcher',
      };
      receipt.r14_audit.requests.push(audit);
      if (request.isNavigationRequest() && request.frame() === page.mainFrame()
        && selectedEditorUrl(url, config) && existsSync(config.publishedPath)) {
        receipt.r14_audit.requests.push({ ...audit, frame: 'main', selected: true, after_r14_switch: true });
      }
    }
  };
  page.on('framenavigated', onNavigation);
  page.on('request', onRequest);
  page.on('response', onResponse);
  try {
    cdp = await page.context().newCDPSession(page);
    await cdp.send('Page.enable');
    originalFrameId = (await cdp.send('Page.getFrameTree')).frameTree.frame.id;
    cdp.on('Page.frameRequestedNavigation', (event) => {
      if (event.frameId !== originalFrameId || event.reason !== 'reload'
        || !existsSync(config.publishedPath) || receipt.reload.clicked || receipt.reload.click_observed
        || !selectedEditorUrl(new URL(event.url), config)) return;
      watcherNavigation = { frame_id: event.frameId, reason: event.reason, url: event.url,
        disposition: event.disposition ?? null, observed_at: 'after-publication-before-commit' };
      receipt.reload.watcher_navigation = watcherNavigation;
      receipt.reload.trigger = 'watcher';
      writeReceipt(config.result, receipt);
    });
    const authored = await author(page, config, {
      onPhase: record, newDocument: () => newDocument,
      onQueuedAttempt: (route, request) => {
        if (heldRoute) return route.abort('internetdisconnected');
        heldRoute = route;
        receipt.held_save = {
          method: request.method(), path: runtimePath(new URL(request.url()).pathname),
          body_sha256: sha256(request.postData() ?? ''),
          key_sha256: sha256(request.headers()['idempotency-key'] ?? ''),
          held: true, release_after_reload: false,
        };
      },
    });
    verifiedDraft = authored.draft;
    const originalPending = (await findDraft(page, { ...config, expectedDraft: authored.draft }))?.draft ?? null;
    check(originalPending && Object.keys(authored.draft).every((key) => originalPending[key] === authored.draft[key]),
      'The original scoped pending draft was not present before publication');
    const oldBytes = await page.evaluate(async () => {
      const response = await fetch('/build.json', { cache: 'no-store', signal: AbortSignal.timeout(8000) });
      return response.ok ? response.text() : null;
    });
    check(oldBytes !== null, 'Initial R13 build was not served');
    const oldBuild = JSON.parse(oldBytes);
    check(oldBuild.app === config.initialApp && oldBuild.release === 13
      && sha256(oldBytes) === config.initialBuildSha256, 'Initial response was not the pinned R13 build');
    check(heldRoute && receipt.held_save.body_sha256 === authored.draft.requestSha256
      && receipt.held_save.key_sha256 === authored.draft.keySha256, 'Held commit did not match the original durable draft');
    receipt.author = {
      ...authored, status: 'pass', mode: 'author', app: config.initialApp,
      manifest_sha256: config.manifestSha256, build_sha256: config.initialBuildSha256,
    };
    await page.evaluate(() => { window.__fix17OriginalDocument = true; });
    record('r13-save-held');
    writeReceipt(config.heldPath, receipt.held_save);
    const deadline = Date.now() + 900_000;
    while (!existsSync(config.publishedPath) && Date.now() < deadline) await page.waitForTimeout(100);
    check(existsSync(config.publishedPath), 'Timed out waiting for verified native R14 publication');

    switched = true;
    // The native update ends the old Runtime connection. Abort the held POST
    // without forwarding it to the replacement Runtime.
    await heldRoute.abort('connectionclosed');
    receipt.original_request_disposition = 'connection-closed-after-native-update';
    record('native-r14-published');
    const [buildBytes] = await Promise.all([
      waitAt('wait-for-r14-build-watcher', 'GET /build.json',
        () => withTimeout(r14BuildSeen, UI_TIMEOUT, 'Timed out waiting for the R14 build watcher'))
        .then((response) => response.body()),
      page.evaluate(() => window.dispatchEvent(new Event('focus'))).catch(() => null),
    ]);
    const build = JSON.parse(buildBytes.toString('utf8'));
    const buildDigest = sha256(buildBytes);
    check(build.app === config.expectedApp && build.release === 14 && buildDigest === config.candidateBuildSha256,
      'Watcher did not read the pinned R14 build bytes');
    receipt.transition = {
      origin: config.origin, url: buildUrl, app_sha: build.app, build_sha256: buildDigest, initial_app_sha: oldBuild.app,
    };
    record('r14-build-observed');

    const navigation = withTimeout(selectedNavigationSeen.then((response) => ({ response })),
      UI_TIMEOUT, 'Timed out waiting for the selected R14 navigation').then((value) => value, () => null);
    const notice = page.getByRole('status').filter({ hasText: NOTICE });
    const button = notice.getByRole('button', { name: 'Reload', exact: true });
    const controlOrNavigation = await Promise.race([
      waitAt('wait-for-reload-control', '[role="status"] button[name="Reload"]',
        () => button.waitFor({ state: 'visible', timeout: UI_TIMEOUT })).then(() => 'control'),
      navigation.then((value) => value ? 'navigation' : 'no-navigation'),
    ]);
    check(controlOrNavigation !== 'no-navigation', 'Timed out waiting for the selected R14 navigation or Reload control');
    if (controlOrNavigation === 'navigation') {
      const nav = await navigation;
      check((watcherNavigation || receipt.reload.click_observed) && nav?.response?.request()?.isNavigationRequest()
        && nav.response.frame() === page.mainFrame()
        && selectedEditorUrl(new URL(nav.response.url()), config),
      'No qualifying main-frame watcher reload preceded the editor navigation');
      receipt.reload = { ...receipt.reload, clicked: false, click_observed: false, trigger: 'watcher',
        watcher_navigation: watcherNavigation, result: 'reloaded', path: 'guarded-automatic', original_document: true };
      record('guarded-automatic-reload');
      record('observe-r14-restoration');
      receipt.restore = await observeRestore(page, config, nav.response, authored.draft, true);
      const recovered = receipt.restore.recovery?.completed === true
        && receipt.restore.required_update_readback?.matches_original_draft === true;
      receipt.status = recovered && receipt.r14_audit.r13_save_requests_after_switch.length === 0
        ? 'pass' : 'fail';
      if (receipt.status !== 'pass') receipt.error = recovered
        ? 'The original R13 document attempted a save after the R14 switch'
        : 'R14 did not read back the in-flight draft after automatic restoration';
      record('restoration-observed');
      return receipt;
    }
    const noticeText = (await notice.innerText()).replace(/\s+/g, ' ').trim();
    check(noticeText === NOTICE && await page.evaluate(() => window.__fix17OriginalDocument === true),
      'The exact Reload notice did not appear in the original R13 document');
    receipt.notice = { text: noticeText, visible: true, original_document: true };
    const warning = notice.getByRole('alert').filter({ hasText: REFUSAL });
    const refusal = warning.waitFor({ state: 'visible', timeout: UI_TIMEOUT })
      .then(() => ({ refused: true }), () => null);
    record('guarded-reload-clicked');
    const outcome = await clickAndObserveGuardedReload(page, button, config, refusal,
      () => { receipt.reload.click_observed = true; writeReceipt(config.result, receipt); },
      () => Boolean(watcherNavigation), selectedNavigationSeen,
      () => { receipt.reload.clicked = true; writeReceipt(config.result, receipt); });
    check(watcherNavigation || receipt.reload.click_observed || outcome.selected_navigation?.selected === true
      || outcome.pre_click_navigation === true && outcome.watcher_navigation === true,
      'R14 navigation had neither a watcher event, trusted click nor committed selected navigation');
    receipt.reload.selected_navigation = outcome.selected_navigation;
    receipt.reload.selected_url = outcome.selected_navigation?.url ?? outcome.response?.url() ?? null;
    receipt.reload.trigger = (outcome.pre_click_navigation || watcherNavigation) && !receipt.reload.click_observed ? 'watcher'
      : outcome.selected_navigation?.trigger && !receipt.reload.click_observed
        ? outcome.selected_navigation.trigger : 'click';
    receipt.reload.watcher_navigation = watcherNavigation;
    let response = outcome.response;
    if (outcome.refused) {
      receipt.reload.result = 'unsafe-reload-fallback';
      receipt.reload.refused = true;
      let dialog = null;
      page.once('dialog', async (prompt) => {
        dialog = prompt.type();
        await prompt.accept();
      });
      record('guard-refused-plain-reload');
      response = await waitAt('guard-refused-plain-reload', 'main-frame editor document',
        () => page.reload({ waitUntil: 'domcontentloaded', timeout: UI_TIMEOUT }));
      receipt.reload.plain_dialog = dialog;
    } else {
      receipt.reload.result = 'reloaded';
    }
    record('observe-r14-restoration');
    receipt.restore = await observeRestore(page, config, response, authored.draft, true);
    const recovered = receipt.restore.recovery?.completed === true
      && receipt.restore.required_update_readback?.matches_original_draft === true;
    if (!outcome.refused && recovered && receipt.r14_audit.r13_save_requests_after_switch.length === 0) {
      receipt.status = 'pass';
    } else {
      receipt.error = outcome.refused ? 'Guarded Reload refused; see plain reload restoration'
        : recovered ? 'The original R13 document attempted a save after the R14 switch'
          : 'R14 did not show or offer the original draft';
    }
    record('restoration-observed');
  } catch (error) {
    const message = error instanceof Error ? error.message : '';
    if (error?.stalled) receipt.stalled = error.stalled;
    if (error?.name === 'TimeoutError' && (receipt.reload.clicked
        || ['guarded-reload-clicked', 'observe-r14-restoration', 'restoration-observed'].includes(receipt.stage))) {
      receipt.timeout_diagnostics = await timeoutDiagnostics(page,
        { ...config, expectedDraft: verifiedDraft, stage: receipt.stage }, error);
    } else if (receipt.stage === 'observe-r14-restoration') {
      // A full readback failure is useful evidence even when it is not a timeout.
      receipt.timeout_diagnostics = await timeoutDiagnostics(page,
        { ...config, expectedDraft: verifiedDraft, stage: receipt.stage }, error);
    }
    receipt.error = /^[\w .';-]{1,160}$/.test(message) ? message
      : error?.name === 'TimeoutError' ? 'browser-timeout' : 'browser-operation-failed';
    if (error?.authorEvidence) receipt.author = { status: 'fail', mode: 'author', ...error.authorEvidence };
  } finally {
    if (heldRoute && !receipt.original_request_disposition) await heldRoute.abort('internetdisconnected').catch(() => {});
    page.off('framenavigated', onNavigation);
    page.off('request', onRequest);
    page.off('response', onResponse);
    if (cdp) await cdp.detach().catch(() => {});
  }
  return receipt;
}

export async function execute(config, chromium) {
  return withBrowserPage(config, chromium, (page) => reloadInflight(page, config));
}

if (pathToFileURL(process.argv[1] ?? '').href === import.meta.url) {
  const config = JSON.parse(readFileSync(process.argv[2], 'utf8'));
  let result;
  try {
    result = await execute(config);
  } catch {
    result = { status: 'fail', mode: config.mode, error: 'browser-context-failed' };
  }
  writeReceipt(config.result, result);
}
