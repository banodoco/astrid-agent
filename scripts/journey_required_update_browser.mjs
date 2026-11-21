#!/usr/bin/env node
/** Observe genuine hosted App builds and the installed Runtime's save queue. */
import { createRequire } from 'node:module';
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromiumArgs } from './journey_update_button.mjs';

const TIMEOUT = 90_000;
const COMMIT_PATH = /\/v1\/projects\/[^/]+\/timelines\/[^/]+\/commit$/;

function check(value, message) {
  if (!value) throw new Error(message);
}

function digest(value) {
  return createHash('sha256').update(JSON.stringify(value)).digest('hex');
}

function commitPost(request) {
  return request.method() === 'POST' && COMMIT_PATH.test(new URL(request.url()).pathname);
}

async function openEditor(page, config) {
  const diagnosticDeepLink = config.diagnostic && config.projectId && config.timelineId;
  const editorUrl = new URL('/tools/video-editor', config.origin);
  if (diagnosticDeepLink) {
    editorUrl.searchParams.set('project', config.projectId);
    editorUrl.searchParams.set('timeline', config.timelineId);
  }
  await page.goto(editorUrl.toString(), { waitUntil: 'domcontentloaded' });
  await page.locator('[data-testid="runtime-connection-status"][data-state="connected"]')
    .waitFor({ state: 'attached' });
  const project = page.getByRole('combobox', { name: 'Select project' });
  await project.waitFor({ state: 'visible' });
  if (diagnosticDeepLink) {
    const currentUrl = new URL(page.url());
    check(currentUrl.searchParams.get('project') === config.projectId
      && currentUrl.searchParams.get('timeline') === config.timelineId,
    'NON-PROOF diagnostic deep link did not retain the requested project and timeline IDs');
    await page.waitForFunction(({ name }) => {
      const trigger = [...document.querySelectorAll('[role="combobox"][aria-label="Select project"]')]
        .find((element) => element.textContent?.trim() === name);
      return Boolean(trigger);
    }, { name: config.projectName }, { timeout: config.timeoutMs ?? TIMEOUT });
    check((await project.innerText()).trim() === config.projectName,
      `NON-PROOF diagnostic project label mismatch for ${config.projectId}`);
  } else if (config.projectId && config.projectName) {
    await project.click();
    // R13's PickerRow uses the project ID as its cmdk value. Wait for the
    // real loaded row, select it through the App picker, and verify the UI.
    const option = page.locator(`[role="option"][data-value=${JSON.stringify(config.projectId)}]`);
    await option.waitFor({ state: 'visible', timeout: config.timeoutMs ?? TIMEOUT });
    check(await option.locator('span[title]').first().getAttribute('title') === config.projectName,
      `R13 project identity mismatch for ${config.projectId}`);
    if (await option.getAttribute('aria-current') !== 'true') {
      await option.click();
    }
    await page.waitForFunction(({ name }) => {
      const trigger = [...document.querySelectorAll('[role="combobox"][aria-label="Select project"]')]
        .find((element) => element.textContent?.trim() === name);
      return Boolean(trigger);
    }, { name: config.projectName }, { timeout: config.timeoutMs ?? TIMEOUT });
  } else if (!(await project.innerText()).includes(config.projectName)) {
    await project.click();
    await page.getByRole('option', { name: config.projectName, exact: true }).click();
  }
  const track = page.getByRole('textbox', { name: 'Track name' }).first();
  await track.waitFor({ state: 'visible' });
  if (diagnosticDeepLink) {
    check(await track.count() > 0,
      'NON-PROOF diagnostic deep link opened without a rendered track');
  }
  return track;
}
function isCommit(request, config) {
  const path = new URL(request.url()).pathname;
  return request.method() === 'POST' && path.endsWith(
    '/projects/' + encodeURIComponent(config.projectId) + '/timelines/'
    + encodeURIComponent(config.timelineId) + '/commit');
}

async function editTrack(track, value) {
  await track.fill(value);
  await track.press('Enter');
  await track.blur();
}

async function draftIntent(page, config) {
  const rows = await page.evaluate(async ({ projectId, timelineId }) => {
    const database = 'astrid.command-outbox';
    if (!(await indexedDB.databases()).some((entry) => entry.name === database)) return [];
    return new Promise((resolve, reject) => {
      const open = indexedDB.open(database);
      open.onerror = () => reject(open.error);
      open.onsuccess = () => {
        const db = open.result;
        const tx = db.transaction('owners', 'readonly');
        const read = tx.objectStore('owners').getAll();
        tx.oncomplete = () => {
          db.close();
          resolve(read.result.filter(({ scope }) =>
            scope.projectId === projectId && scope.timelineId === timelineId));
        };
        tx.onabort = () => { db.close(); reject(tx.error); };
      };
    });
  }, config);
  for (const row of rows) {
    for (const intent of row.intents) {
      if (!JSON.stringify(intent.payload).includes(JSON.stringify(config.draftLabel))) continue;
      const candidate = {
        ...row.scope, ownerId: row.ownerId, intentId: intent.id,
        payloadSha256: digest(intent.payload),
      };
      if (!config.expectedDraft || Object.keys(candidate).every(
        (key) => candidate[key] === config.expectedDraft[key])) return candidate;
    }
  }
  return null;
}

async function waitForDraft(page, config) {
  const deadline = Date.now() + 30_000;
  do {
    const draft = await draftIntent(page, config);
    if (draft) return draft;
    await page.waitForTimeout(250);
  } while (Date.now() < deadline);
  throw new Error('The original scoped change was missing from the durable save queue');
}

// Keep an actual user edit in progress by pausing its POST at the browser boundary.
// No Runtime response or stored draft is fabricated, and reads stay on the real Runtime.
async function pauseCommits(page) {
  let hits = 0;
  await page.route(COMMIT_PATH, (route) => {
    if (route.request().method() !== 'POST') return route.continue();
    hits++;
    return route.abort('internetdisconnected');
  });
  return () => hits;
}

async function author(page, config) {
  let queueing = false;
  let blockedPosts = 0;
  await page.route(COMMIT_PATH, (route) => {
    const request = route.request();
    if (!commitPost(request) || (!queueing && isCommit(request, config))) return route.continue();
    if (isCommit(request, config)) blockedPosts++;
    return route.abort('internetdisconnected');
  });
  const track = await openEditor(page, config);
  check(await track.inputValue() === config.initialLabel, 'R13 did not open the seeded timeline');
  await page.getByTestId('required-update-reason').waitFor({ state: 'hidden', timeout: 5_000 });
  const response = page.waitForResponse((r) => isCommit(r.request(), config));
  await editTrack(track, config.savedLabel);
  const accepted = await response;
  const body = await accepted.json();
  check(accepted.ok() && body.data?.status === 'accepted' && body.data.accepted_revision_id
    && body.receipt?.receipt_id, 'R13 did not accept the saved timeline edit');
  queueing = true;
  await Promise.all([
    page.waitForEvent('requestfailed', (request) => isCommit(request, config)),
    editTrack(track, config.draftLabel),
  ]);
  const draft = await waitForDraft(page, config);
  check(blockedPosts > 0, 'R13 App did not attempt to send the queued edit');
  return { draft, blockedPosts, accepted: {
    revision: body.data.accepted_revision_id, publication: body.receipt.receipt_id,
  } };
}

async function prompt(page, config) {
  await pauseCommits(page);
  const published = page.waitForResponse((response) =>
    response.url() === config.origin + '/install-manifest.json' && response.ok());
  await page.goto(config.origin + '/tools/video-editor', { waitUntil: 'domcontentloaded' });
  const required = page.getByTestId('quiet-update-notice').getByTestId('required-update-reason');
  const summary = required.locator('summary');
  await summary.getByText('Required', { exact: true }).waitFor({ state: 'visible' });
  const title = (await summary.innerText()).trim();
  check(/Update Astrid|Release 14 is ready/.test(title), 'Required notice has no update title');
  if (!(await required.evaluate((details) => details.open))) await summary.click();
  const reason = required.getByText('This update is required: ' + config.requiredReason, { exact: false });
  await reason.waitFor({ state: 'visible' });
  const action = required.getByRole('button', { name: 'Update now', exact: true })
    .or(required.getByText('Run it yourself', { exact: true })).first();
  await action.waitFor({ state: 'visible' });
  // An independent prompt probe is useful even when the preceding author
  // leg failed. It records no preservation result and never invents a draft.
  const draft = config.expectedDraft ? await waitForDraft(page, config) :
    (config.diagnostic ? null : await waitForDraft(page, config));
  const response = await published;
  const manifest = await response.body();
  return {
    ...(config.diagnostic ? { evidence_class: 'NON-PROOF' } : {}),
    draft, browserManifestSha256: createHash('sha256').update(manifest).digest('hex'),
    prompt: { title, reason: await reason.innerText(), action: await action.innerText() },
  };
}

async function reopen(page, config) {
  const blocked = await pauseCommits(page);
  const track = await openEditor(page, config);
  const draft = await waitForDraft(page, config);
  const label = await track.inputValue();
  check([config.savedLabel, config.draftLabel].includes(label),
    'R14 App opened neither the R13 saved edit nor its recovered draft');
  let draftVisible = label === config.draftLabel;
  if (config.requireDraftUI && !draftVisible) {
    await page.locator('[data-save-bar]').getByRole('button', { name: 'Try again', exact: true })
      .waitFor({ state: 'visible' });
    draftVisible = true;
  }
  return { draft, reopenedLabel: label, draftVisible, blockedPosts: blocked() };
}

// Bounded diagnostic replay: permit only the selected project's exact pending
// commit, while all other workspace writes remain blocked. Never save bodies,
// keys, labels, or raw server messages in the receipt.
async function recoveryProbe(page, config, context) {
  const began = Date.now();
  const deadline = began + 30_000;
  const selectedPath = `/v1/projects/${encodeURIComponent(config.projectId)}/timelines/${encodeURIComponent(config.timelineId)}/commit`;
  const commitEvents = [];
  let selectedIntent = null;
  let baselineInventory = null;
  let inventoryAfter = null;
  let clickAttempted = false;
  let acceptedReceipt = null;
  let responseCode = null;
  let failure = null;
  const remaining = () => Math.max(1, deadline - Date.now());
  const digestInventory = async (rows) => Promise.all(rows.map(async (row) => ({
    ownerSha256: createHash('sha256').update(String(row.ownerId)).digest('hex'),
    scopeSha256: createHash('sha256').update(JSON.stringify(row.scope)).digest('hex'),
    intents: (row.intents || []).filter((intent) => !(intent.id === config.expectedDraft.intentId
      && row.ownerId === config.expectedDraft.ownerId && row.scope?.projectId === config.projectId
      && row.scope?.timelineId === config.timelineId))
      .map((intent) => ({ id: intent.id, payloadSha256: intent.payloadSha256 ?? digest(intent.payload) })),
    pending: row.pending && !(row.pending.intentId === config.expectedDraft.intentId
      && row.ownerId === config.expectedDraft.ownerId && row.scope?.projectId === config.projectId
      && row.scope?.timelineId === config.timelineId) ? { intentId: row.pending.intentId,
      keySha256: createHash('sha256').update(String(row.pending.request?.key)).digest('hex'),
      bodySha256: createHash('sha256').update(String(row.pending.request?.body)).digest('hex') } : null,
  })));
  const rawInventory = async (tab) => tab.evaluate(async ({ projectId, timelineId }) => {
    if (!(await indexedDB.databases()).some((entry) => entry.name === 'astrid.command-outbox')) return [];
    const db = await new Promise((resolve, reject) => {
      const open = indexedDB.open('astrid.command-outbox');
      open.onerror = () => reject(open.error); open.onsuccess = () => resolve(open.result);
    });
    try {
      const tx = db.transaction('owners', 'readonly'); const read = tx.objectStore('owners').getAll();
      return await new Promise((resolve, reject) => {
        tx.oncomplete = () => resolve(read.result);
        tx.onerror = () => reject(tx.error);
      });
    } finally { db.close(); }
  }, { projectId: config.projectId, timelineId: config.timelineId });
  await context.route('**/v1/projects/**/timelines/**/commit', async (route) => {
    const request = route.request();
    if (request.method() !== 'POST') return route.continue();
    const url = new URL(request.url());
    const body = request.postData() || '';
    const exact = url.pathname === selectedPath;
    let correlation = false;
    if (exact && selectedIntent) {
      const key = request.headers()['idempotency-key'];
      correlation = Boolean(key && key === selectedIntent.key && body === selectedIntent.body);
    }
    commitEvents.push({ selected: exact, correlated: correlation,
      bodySha256: createHash('sha256').update(body).digest('hex'),
      keySha256: request.headers()['idempotency-key']
        ? createHash('sha256').update(request.headers()['idempotency-key']).digest('hex') : null,
      allowed: exact && correlation && clickAttempted });
    if (!(exact && correlation && clickAttempted)) return route.abort('blockedbyclient');
    return route.continue();
  });
  const fail = (kind, code = null) => { failure = { kind, code }; };
  try {
    page.setDefaultTimeout(remaining());
    const track = await openEditor(page, { ...config, timeoutMs: remaining() });
    const beforeRows = await rawInventory(page);
    baselineInventory = await digestInventory(beforeRows.map((row) => ({
      ownerId: row.ownerId, scope: row.scope,
      intents: (row.intents || []).map((intent) => ({ id: intent.id, payloadSha256: digest(JSON.stringify(intent.payload)) })),
      pending: row.pending ? { intentId: row.pending.intentId, request: row.pending.request } : null,
    })));
    const matches = beforeRows.flatMap((row) => (row.intents || []).filter((intent) =>
      intent.id === config.expectedDraft.intentId && row.ownerId === config.expectedDraft.ownerId
      && row.scope?.realmId === config.expectedDraft.realmId
      && row.scope?.actorId === config.expectedDraft.actorId
      && row.scope?.projectId === config.projectId && row.scope?.timelineId === config.timelineId
      && digest(intent.payload) === config.expectedDraft.payloadSha256
      && row.pending?.intentId === intent.id && row.pending.request?.key && row.pending.request?.body)
      .map((intent) => ({ row, intent, pending: row.pending })));
    if (matches.length !== 1) { fail('scope_ambiguity'); }
    else {
      const { row, intent, pending } = matches[0];
      selectedIntent = { key: pending.request.key, body: pending.request.body };
      if (pending.request.method !== 'POST' || pending.request.path !== selectedPath
        || row.scope?.realmId !== config.expectedDraft.realmId
        || row.scope?.actorId !== config.expectedDraft.actorId
        || intent.id !== config.expectedDraft.intentId
        || digest(intent.payload) !== config.expectedDraft.payloadSha256) {
        fail('scope_ambiguity');
      }
    }
    if (!failure) {
      const retry = page.locator('[data-save-bar]').getByRole('button', { name: 'Try again', exact: true });
      while (Date.now() < deadline && !(await retry.isVisible().catch(() => false))) {
        await page.waitForTimeout(Math.min(250, remaining()));
      }
      if (await retry.isVisible().catch(() => false)) {
        clickAttempted = true;
        const responsePromise = page.waitForResponse((response) => response.request().method() === 'POST'
          && new URL(response.url()).pathname === selectedPath, { timeout: remaining() }).catch(() => null);
        await retry.click({ timeout: remaining() });
        const response = await responsePromise;
        if (!response) fail(Date.now() >= deadline ? 'timeout' : 'no_selected_response');
        else {
          responseCode = response.status();
          let parsed = null;
          try { parsed = await response.json(); } catch {}
          const safeCode = typeof parsed?.error?.code === 'string'
            && /^[A-Za-z0-9_.-]{1,80}$/.test(parsed.error.code) ? parsed.error.code : null;
          if (!response.ok()) fail('rejected', safeCode || `http_${response.status()}`);
          else if (parsed?.data?.status === 'accepted' && parsed?.data?.accepted_revision_id
            && parsed?.receipt?.receipt_id && parsed?.receipt?.idempotency_key === selectedIntent.key) {
            acceptedReceipt = { revision: parsed.data.accepted_revision_id, publication: parsed.receipt.receipt_id,
              idempotencyKeyMatches: true };
          } else fail('unverified_response', safeCode || 'acceptance_receipt_mismatch');
        }
      } else fail('try_again_button_missing');
    }
    const afterRows = await rawInventory(page);
    const targetQueueEntryRemoved = !afterRows.some((row) =>
      row.ownerId === config.expectedDraft.ownerId
      && row.scope?.realmId === config.expectedDraft.realmId
      && row.scope?.actorId === config.expectedDraft.actorId
      && row.scope?.projectId === config.projectId && row.scope?.timelineId === config.timelineId
      && ((row.intents || []).some((intent) => intent.id === config.expectedDraft.intentId
        && digest(intent.payload) === config.expectedDraft.payloadSha256)
        || row.pending?.intentId === config.expectedDraft.intentId));
    inventoryAfter = await digestInventory(afterRows.map((row) => ({
      ownerId: row.ownerId, scope: row.scope,
      intents: (row.intents || []).map((intent) => ({ id: intent.id, payloadSha256: digest(JSON.stringify(intent.payload)) })),
      pending: row.pending ? { intentId: row.pending.intentId, request: row.pending.request } : null,
    })));
    const fresh = await context.newPage();
    fresh.setDefaultTimeout(remaining());
    const freshTrack = await openEditor(fresh, { ...config, timeoutMs: remaining() });
    const reopenedLabel = await freshTrack.inputValue();
    const freshDraft = reopenedLabel === config.draftLabel;
    const stableInventory = JSON.stringify(baselineInventory) === JSON.stringify(inventoryAfter);
    const correlated = commitEvents.some((event) => event.selected && event.correlated && event.allowed);
    if (!failure && !stableInventory) fail('scope_ambiguity', 'queued_inventory_changed');
    if (!failure && !correlated) fail('scope_ambiguity', 'request_correlation_missing');
    if (!failure && !acceptedReceipt) fail('acceptance_missing', 'accepted_revision_or_publication_absent');
    if (!failure && !targetQueueEntryRemoved) fail('acceptance_missing', 'accepted_queue_entry_remains');
    if (!failure && !freshDraft) fail('draft_not_reopened', 'fresh_document_label_mismatch');
    const success = Boolean(acceptedReceipt && targetQueueEntryRemoved && freshDraft
      && correlated && stableInventory && !failure);
    return { evidence_class: 'NON-PROOF', probe: 'exact-draft-recovery', state: success ? 'pass' : 'fail',
      step: { state: success ? 'pass' : 'fail', clickAttempted, responseCode,
        correlation: correlated ? 'exact_pending_idempotency_and_body' : 'missing_or_ambiguous',
        accepted: Boolean(acceptedReceipt), reopenedDraftLabel: freshDraft,
        acceptedQueueEntryRemoved: targetQueueEntryRemoved,
        stableScopedInventory: stableInventory, ...(failure ? { failure } : {}) },
    acceptedReceipt, commitEvents, baselineInventory, inventoryAfter,
      elapsedMs: Date.now() - began };
  } catch (error) {
    if (!failure) fail(Date.now() >= deadline ? 'timeout' : 'probe_error',
      typeof error?.name === 'string' ? error.name.slice(0, 60) : 'Error');
    return { evidence_class: 'NON-PROOF', probe: 'exact-draft-recovery', state: 'fail',
      step: { state: 'fail', clickAttempted, responseCode, failure }, commitEvents,
      baselineInventory, inventoryAfter, elapsedMs: Date.now() - began };
  }
}

// Diagnostic only: keep a genuine R13 document alive while only its hosted
// manifest response changes. Runtime health/handshake/read responses pass through.
async function publicationProbe(page, config, context) {
  const oldManifest = readFileSync(config.r13ManifestPath);
  const nextManifest = readFileSync(config.publicationManifestPath);
  const publicationSha256 = createHash('sha256').update(nextManifest).digest('hex');
  const started = Date.now();
  const events = [];
  const mutationAttempts = [];
  let published = false;
  const manifestReads = { live: 0, fresh: 0 };
  const manifestShas = { live: [], fresh: [] };
  const pageNames = new Map([[page, 'live']]);
  const at = () => Date.now() - started;
  const methods = ['POST', 'PUT', 'PATCH', 'DELETE'];
  const isWorkspaceWrite = (pathname, method) => methods.includes(method)
    && (/^\/v1\/projects(?:\/|$)/.test(pathname)
      || /^\/v1\/(?:timelines|shots|references|preferences|app-state)(?:\/|$)/.test(pathname));
  const safeRoute = (pathname) => pathname.split('/').slice(0, 2).join('/').slice(0, 80);
  const pageFor = (request) => { try { return request.frame()?.page(); } catch { return null; } };
  context.on('request', (request) => {
    let pathname;
    try { pathname = new URL(request.url()).pathname; } catch { return; }
    const name = pageNames.get(pageFor(request));
    if (!name) return;
    if (/^\/v1\/handshake(?:\/|$)/.test(pathname)) events.push({ tab: name, kind: 'handshake', ms: at() });
    if (isWorkspaceWrite(pathname, request.method())) mutationAttempts.push({
      tab: name, route: safeRoute(pathname), method: request.method(), ms: at(), blocked: true,
    });
  });
  await context.route('**/install-manifest.json', async (route) => {
    const tab = pageNames.get(pageFor(route.request()));
    if (!tab) return route.continue();
    manifestReads[tab]++;
    const bytes = published ? nextManifest : oldManifest;
    manifestShas[tab].push(createHash('sha256').update(bytes).digest('hex'));
    return route.fulfill({ status: 200, contentType: 'application/json', body: bytes });
  });
  // Preserve real Runtime reads, health and handshake. Block only writes on the
  // known workspace routes, including the exact /v1/projects collection route.
  await context.route('**/v1/**', async (route) => {
    const request = route.request();
    let pathname;
    try { pathname = new URL(request.url()).pathname; } catch { return route.continue(); }
    if (isWorkspaceWrite(pathname, request.method())) return route.abort('blockedbyclient');
    return route.continue();
  });
  const r13ManifestResponse = page.waitForResponse((response) =>
    response.url().endsWith('/install-manifest.json') && response.ok(), { timeout: 30_000 });
  await page.goto(config.origin + '/tools/video-editor', { waitUntil: 'domcontentloaded', timeout: 30_000 });
  await page.locator('[data-testid="runtime-connection-status"][data-state="connected"]')
    .waitFor({ state: 'attached', timeout: 30_000 });
  const initial = await r13ManifestResponse;
  const initialSha256 = createHash('sha256').update(await initial.body()).digest('hex');
  const build = await page.evaluate(async () => {
    const response = await fetch('/build.json', { cache: 'no-store' });
    if (!response.ok) return null;
    const value = await response.json();
    return typeof value.app === 'string' ? value.app : null;
  });
  const scriptNames = await page.locator('script[src]').evaluateAll((scripts) => scripts.map((script) => {
    try { return new URL(script.src).pathname.split('/').pop().slice(0, 120); } catch { return null; }
  }).filter(Boolean));
  // Let the initial page finish its ordinary status read before switching bytes.
  await page.waitForTimeout(1_000);
  published = true;
  const publicationMs = at();
  const fresh = await context.newPage();
  pageNames.set(fresh, 'fresh');
  fresh.setDefaultTimeout(30_000);
  try {
    await fresh.goto(config.origin + '/tools/video-editor',
      { waitUntil: 'domcontentloaded', timeout: 30_000 });
  } catch {} // keep the live tab's bounded observation even if the fresh tab fails
  const inspect = async (tab) => {
    const began = Date.now();
    let connected = false;
    try {
      await tab.locator('[data-testid="runtime-connection-status"][data-state="connected"]')
        .waitFor({ state: 'attached', timeout: 30_000 });
      connected = true;
    } catch {}
    const deadline = began + 30_000;
    const required = tab.getByTestId('quiet-update-notice').getByTestId('required-update-reason');
    const summary = required.locator('summary');
    let promptMs = null;
    while (connected && Date.now() < deadline) {
      if (await summary.getByText('Required', { exact: true }).isVisible().catch(() => false)) {
        promptMs = at(); // capture first sighting before expanding details
        break;
      }
      await tab.waitForTimeout(Math.min(300, deadline - Date.now()));
    }
    let reasonMatches = false;
    let action = '';
    if (promptMs !== null) {
      if (!(await required.evaluate((details) => details.open).catch(() => false))) await summary.click();
      reasonMatches = await required.getByText(
        'This update is required: ' + config.requiredReason, { exact: true },
      ).isVisible().catch(() => false);
      const update = required.getByRole('button', { name: 'Update now', exact: true });
      const run = required.getByText('Run it yourself', { exact: true });
      if (await update.isVisible().catch(() => false)) action = (await update.innerText()).trim();
      else if (await run.isVisible().catch(() => false)) action = (await run.innerText()).trim();
    }
    return { state: !connected ? 'connection-failed' : promptMs === null ? 'incomplete' : 'observed',
      connected, observedR14Required: promptMs !== null, reasonMatches,
      observedAction: Boolean(action), action, promptMs,
      waitMs: Math.min(30_000, Date.now() - began) };
  };
  const inspectTab = async (tab) => {
    try { return await inspect(tab); }
    catch { return { state: 'tab-failed', connected: false, observedR14Required: false,
      reasonMatches: false, observedAction: false, action: '', promptMs: null, waitMs: 0 }; }
  };
  const freshPrompt = await inspectTab(fresh);
  await page.bringToFront(); // real focus/visibility transition in the browser
  const livePrompt = await inspectTab(page);
  const freshServedBuild = await fresh.evaluate(async () => {
    const response = await fetch('/build.json', { cache: 'no-store' });
    if (!response.ok) return null;
    const value = await response.json();
    return typeof value.app === 'string' ? value.app : null;
  });
  const liveCurrentManifest = manifestShas.live.includes(publicationSha256);
  const freshCurrentManifest = manifestShas.fresh.includes(publicationSha256);
  return {
    evidence_class: 'NON-PROOF', probe: 'r13-manifest-publication',
    pinnedApp: config.expectedAppCommit, servedBuildApp: build, freshServedBuildApp: freshServedBuild,
    pinnedBuildMatches: build === config.expectedAppCommit
      && freshServedBuild === config.expectedAppCommit,
    servedScriptNames: scriptNames, r13ManifestSha256: initialSha256,
    stagedR14ManifestSha256: publicationSha256,
    publicationMs, observations: {
      live: { currentManifestRequested: liveCurrentManifest, manifestReads: manifestReads.live,
        manifestResponseSha256: manifestShas.live, ...livePrompt },
      fresh: { currentManifestRequested: freshCurrentManifest, manifestReads: manifestReads.fresh,
        manifestResponseSha256: manifestShas.fresh, ...freshPrompt,
        positiveFreshControl: freshPrompt.connected && freshPrompt.observedR14Required
          && freshPrompt.reasonMatches && freshPrompt.observedAction && freshCurrentManifest },
    },
    events: events.map(({ tab, kind, ms }) => ({ tab, kind,
      msRelativeToPublication: ms - publicationMs,
      msRelativeToPrompt: (tab === 'live' ? livePrompt.promptMs : freshPrompt.promptMs) === null
        ? null : ms - (tab === 'live' ? livePrompt.promptMs : freshPrompt.promptMs) })),
    mutationAttemptsRelativeToPublication: mutationAttempts.map(({ tab, route, method, ms, blocked }) =>
      ({ tab, route, method, msRelativeToPublication: ms - publicationMs,
        msRelativeToPrompt: (tab === 'live' ? livePrompt.promptMs : freshPrompt.promptMs) === null
          ? null : ms - (tab === 'live' ? livePrompt.promptMs : freshPrompt.promptMs), blocked })),
    boundedObservationMs: { live: livePrompt.waitMs, fresh: freshPrompt.waitMs },
  };
}

const actions = { author, prompt, reopen, publicationProbe, recoveryProbe };

export async function execute(config, chromium) {
  check(Object.hasOwn(actions, config.mode), 'Unknown FIX-17 browser step');
  check(config.mode !== 'recoveryProbe' || config.diagnostic === true,
    'Exact draft recovery is available only in diagnostic mode');
  check(config.origin === 'https://astrid.haus' && config.spki?.length >= 32 && config.profileDir,
    'An isolated hosted origin, TLS pin and persistent profile are required');
  const browser = chromium ?? createRequire(join(config.browserDriverApp, 'package.json'))('@playwright/test').chromium;
  const context = await browser.launchPersistentContext(config.profileDir, {
    args: chromiumArgs(config), permissions: ['local-network-access'], serviceWorkers: 'block',
  });
  try {
    await context.addInitScript(() => {
      localStorage.setItem('astrid-editor-tour-v1', '1');
    });
    const page = context.pages()[0] ?? await context.newPage();
    page.setDefaultTimeout(TIMEOUT);
    if (config.mode === 'publicationProbe') {
      return { status: 'non-proof', mode: config.mode,
        ...await publicationProbe(page, config, context) };
    }
    if (config.mode === 'recoveryProbe') {
      return { status: 'non-proof', mode: config.mode,
        ...await recoveryProbe(page, config, context) };
    }
    return { status: 'pass', mode: config.mode, ...await actions[config.mode](page, config) };
  } finally {
    await context.close();
  }
}

async function main() {
  const config = JSON.parse(readFileSync(process.argv[2], 'utf8'));
  let result;
  try {
    result = await execute(config);
  } catch (error) {
    result = { status: 'fail', mode: config.mode,
      error: ['publicationProbe', 'recoveryProbe'].includes(config.mode)
        ? `${error?.name || 'Error'}: sanitized diagnostic failure`
        : String(error).slice(0, 500) };
    if (!['publicationProbe', 'recoveryProbe'].includes(config.mode)) console.error(result.error);
    process.exitCode = 1;
  }
  writeFileSync(config.result, JSON.stringify(result, null, 2) + '\n');
}

if (fileURLToPath(import.meta.url) === process.argv[1]) await main();
