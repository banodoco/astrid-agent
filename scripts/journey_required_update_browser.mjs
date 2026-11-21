#!/usr/bin/env node
/** Observe hosted App publication and replay the installed Runtime's durable save queue. */
import { createRequire } from 'node:module';
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { pathToFileURL } from 'node:url';
import { chromiumArgs } from './journey_update_button.mjs';

const TIMEOUT = 90_000;
const PROMPT_TIMEOUT = 30_000;
const COMMIT_PATH = /^\/v1\/projects\/[^/]+\/timelines\/[^/]+\/commit$/;
const PROFILE_OPTIONS = { permissions: ['local-network-access'], serviceWorkers: 'block' };

export function check(value, message) {
  if (!value) throw new Error(message);
}

export function sha256(value) {
  return createHash('sha256').update(value).digest('hex');
}

export function runtimePath(path) {
  return path.replace(/^\/api\/runtime(?=\/v1\/)/, '');
}

export function commitPost(request) {
  return request.method() === 'POST' && COMMIT_PATH.test(runtimePath(new URL(request.url()).pathname));
}

function selectedPath(config) {
  return '/v1/projects/' + encodeURIComponent(config.projectId) + '/timelines/'
    + encodeURIComponent(config.timelineId) + '/commit';
}

export function isCommit(request, config) {
  return commitPost(request) && runtimePath(new URL(request.url()).pathname) === selectedPath(config);
}

export function trackName(page) {
  return page.getByRole('textbox', { name: 'Track name' }).first();
}

async function openEditor(page, config, onPhase = () => {}) {
  const url = new URL('/tools/video-editor', config.origin);
  url.searchParams.set('project', config.projectId);
  url.searchParams.set('timeline', config.timelineId);
  onPhase('open-editor');
  await page.goto(url.toString(), { waitUntil: 'domcontentloaded' });
  onPhase('wait-for-runtime-connection');
  await page.locator('[data-testid="runtime-connection-status"][data-state="connected"]')
    .waitFor({ state: 'attached' });
  onPhase('wait-for-selected-project');
  await page.waitForFunction(({ name }) => [...document.querySelectorAll('[role="combobox"][aria-label="Select project"]')]
    .some((combobox) => combobox.textContent?.trim() === name), { name: config.projectName });
  const current = new URL(page.url());
  check(current.searchParams.get('project') === config.projectId
    && current.searchParams.get('timeline') === config.timelineId, 'The App opened a different project or timeline');
  onPhase('wait-for-track');
  const track = trackName(page);
  await track.waitFor({ state: 'visible' });
  return track;
}

async function editTrack(page, track, label) {
  await track.click();
  await track.press('Control+A');
  await page.keyboard.insertText(label);
  await track.blur();
}

// Parse the pinned App support/app.ts JSON string bridge. JSON.parse preserves
// own keys such as "__proto__" which Playwright's by-value decoder drops.
export function parseCommandOutboxes(encoded, timelineId) {
  const rows = JSON.parse(encoded);
  check(Array.isArray(rows), 'The command outbox bridge did not return an array');
  return timelineId === undefined ? rows : rows.filter((row) => row?.scope?.timelineId === timelineId);
}

async function queueRows(page, config) {
  const encoded = await page.evaluate(async (timelineId) => {
    const database = 'astrid.command-outbox';
    if (!(await indexedDB.databases()).some((entry) => entry.name === database)) return '[]';
    return new Promise((resolve, reject) => {
      const open = indexedDB.open(database);
      open.onerror = () => reject(open.error);
      open.onsuccess = () => {
        const db = open.result;
        const tx = db.transaction('owners', 'readonly');
        const read = tx.objectStore('owners').getAll();
        tx.oncomplete = () => { db.close(); resolve(JSON.stringify(read.result.filter((row) => row.scope?.timelineId === timelineId))); };
        tx.onabort = () => { db.close(); reject(tx.error); };
      };
    });
  }, config.timelineId);
  return parseCommandOutboxes(encoded, config.timelineId);
}

function trackLabelFromWire(raw) {
  try {
    const { commands } = JSON.parse(raw);
    if (!Array.isArray(commands) || commands.length !== 1) return undefined;
    const [command] = commands;
    if (command?.op === 'track.set' && command.select === 'parent/track:V1' && command.args?.clear?.length === 0) {
      return command.args.values?.label;
    }
    if (command?.op !== 'doc.patch' || command.select !== null || !Array.isArray(command.args?.changes)) return undefined;
    // R13's installed editor can encode the same label-only gesture as a
    // document patch, using either the parent or its config's tracks collection.
    const labelChanges = command.args.changes.filter((change) => {
      if (change?.op !== 'set' || change.ref !== 'parent' || !Array.isArray(change.path)) return false;
      const path = change.path[0] === 'config' ? change.path.slice(1) : change.path;
      return path.length === 3 && path[0] === 'tracks' && path[1] === 'id:V1' && path[2] === 'label';
    });
    return labelChanges.length === 1 ? labelChanges[0].value : undefined;
  } catch {
    return undefined;
  }
}

function sameScope(row, config) {
  return row.scope?.projectId === config.projectId && row.scope?.timelineId === config.timelineId;
}

function canonicalTrackLabel(document, config) {
  if (document?.project_id !== config.projectId || document.timeline_id !== config.timelineId) return undefined;
  const tracks = document.parent_revision?.payload?.config?.tracks;
  if (!Array.isArray(tracks)) return undefined;
  const target = tracks.filter((track) => track?.id === 'V1');
  return target.length === 1 ? target[0].label : undefined;
}

// Keep the raw persisted request in memory. Only scoped IDs and digests leave the browser runner.
export async function findDraft(page, config) {
  const matches = [];
  for (const row of await queueRows(page, config)) {
    if (!sameScope(row, config)) continue;
    const pending = row.pending?.request;
    if (!pending || pending.method !== 'POST' || typeof pending.path !== 'string'
      || runtimePath(pending.path) !== selectedPath(config)
      || typeof pending.body !== 'string' || typeof pending.key !== 'string' || !pending.key
      || trackLabelFromWire(pending.body) !== config.draftLabel) continue;
    const intent = row.intents?.find((entry) => entry.id === row.pending.intentId);
    if (!intent || canonicalTrackLabel(intent.payload?.after, config) !== config.draftLabel) continue;
    const draft = {
      realmId: row.scope.realmId, actorId: row.scope.actorId, projectId: row.scope.projectId,
      timelineId: row.scope.timelineId, ownerId: row.ownerId, intentId: intent.id,
      payloadSha256: sha256(JSON.stringify(intent.payload)), requestSha256: sha256(pending.body), keySha256: sha256(pending.key),
    };
    if (!config.expectedDraft || Object.keys(draft).every((key) => draft[key] === config.expectedDraft[key])) {
      matches.push({ draft, pending });
    }
  }
  check(matches.length <= 1, 'More than one queued request matches the original draft');
  return matches[0];
}

async function waitForSettledAccepted(page, config, accepted) {
  const deadline = Date.now() + PROMPT_TIMEOUT;
  do {
    const rows = (await queueRows(page, config)).filter((row) => sameScope(row, config)
      && row.accepted?.head?.revisionId === accepted.revision
      && row.accepted.head.publicationId === accepted.publication
      && !row.pending && row.intents.length === 0);
    check(rows.length <= 1, 'Accepted edit matched multiple durable queue owners');
    if (rows.length === 1) return rows[0].ownerId;
    await page.waitForTimeout(150);
  } while (Date.now() < deadline);
  throw new Error('Accepted R13 revision and publication did not settle in the durable queue owner');
}

async function waitForDraft(page, config) {
  const deadline = Date.now() + 20_000;
  do {
    const match = await findDraft(page, config);
    if (match) return match;
    await page.waitForTimeout(200);
  } while (Date.now() < deadline);
  throw new Error('The original scoped request was missing from the durable save queue');
}

export async function author(page, config, { onQueuedAttempt, onPhase, newDocument } = {}) {
  let queueing = false;
  let blockedPosts = 0;
  let queued = null;
  let aborted = false;
  const evidence = { outcome: 'starting', request: null, responseStatus: null, requestFailure: null };
  await page.route('**/*', (route) => {
    const request = route.request();
    if (!commitPost(request)) return route.fallback();
    if (newDocument?.()) return route.continue();
    if (!isCommit(request, config)) return route.abort('internetdisconnected');
    if (!queueing) return route.continue();
    blockedPosts++;
    if (queued) return route.abort('internetdisconnected');
    queued = { route, request };
    return onQueuedAttempt?.(route, request);
  });
  const onSaveRequest = (request) => {
    if (!evidence.request && isCommit(request, config)) evidence.request = {
      method: request.method(), path: runtimePath(new URL(request.url()).pathname),
      bodySha256: sha256(request.postData() ?? ''), keySha256: sha256(request.headers()['idempotency-key'] ?? ''),
      completeLabel: trackLabelFromWire(request.postData() ?? '') === config.savedLabel,
    };
  };
  const onSaveFailure = (request) => {
    if (!queueing && isCommit(request, config)) evidence.requestFailure = 'scoped-post-request-failed';
  };
  page.on('request', onSaveRequest);
  page.on('requestfailed', onSaveFailure);
  try {
    const track = await openEditor(page, config, onPhase);
    onPhase?.('wait-for-editor-card');
    // The mounted field remains inert until the connection card has left.
    await page.getByTestId('runtime-not-connected').waitFor({ state: 'detached', timeout: PROMPT_TIMEOUT });
    onPhase?.('accept-r13-edit');
    evidence.initialLabelMatches = await track.inputValue() === config.initialLabel;
    check(evidence.initialLabelMatches, 'R13 did not open the seeded composition');
    const requiredReason = page.getByTestId('required-update-reason');
    if (await requiredReason.isVisible().catch(() => false)) {
      evidence.outcome = 'required-notice';
      throw new Error('A required update notice blocked R13 authoring');
    }

    const first = page.waitForResponse((response) => isCommit(response.request(), config)
      && trackLabelFromWire(response.request().postData() ?? '') === config.savedLabel, { timeout: PROMPT_TIMEOUT })
      .catch(() => null);
    await editTrack(page, track, config.savedLabel);
    evidence.editedLabelMatches = await track.inputValue() === config.savedLabel;
    check(evidence.editedLabelMatches, 'R13 track field did not retain the edited value');
    const response = await first;
    if (!response) {
      evidence.outcome = evidence.requestFailure ? 'scoped-post-failed' : 'no-matching-http';
      throw new Error(evidence.requestFailure ? 'R13 scoped commit request failed' : 'No scoped response carried the complete saved label');
    }

    evidence.responseStatus = response.status();
    const body = await response.json().catch(() => ({}));
    if (/^[a-zA-Z0-9_.-]{1,80}$/.test(body.error?.code ?? '')) evidence.responseCode = body.error.code;
    evidence.acceptedLabelMatches = canonicalTrackLabel(body.data?.canonical_document, config) === config.savedLabel;
    evidence.outcome = 'scoped-post-failed';
    check(response.ok() && body.data?.status === 'accepted' && body.data.accepted_revision_id
      && body.receipt?.receipt_id && evidence.acceptedLabelMatches, 'R13 did not accept the saved composition edit');
    const accepted = { revision: body.data.accepted_revision_id, publication: body.receipt.receipt_id };
    evidence.outcome = 'accepted';
    onPhase?.('settle-r13-edit');
    const ownerId = await waitForSettledAccepted(page, config, accepted);
    evidence.acceptedOwnerSettled = true;

    queueing = true;
    onPhase?.('hold-r13-draft');
    await editTrack(page, track, config.draftLabel);
    evidence.draftLabelMatches = await track.inputValue() === config.draftLabel;
    check(evidence.draftLabelMatches, 'R13 track field did not retain the complete draft label');
    const deadline = Date.now() + PROMPT_TIMEOUT;
    while (!queued && Date.now() < deadline) await page.waitForTimeout(50);
    check(queued, 'The scoped R13 draft request was not captured');
    const { request } = queued;
    const requestBody = request.postData() ?? '';
    evidence.heldLabelMatches = trackLabelFromWire(requestBody) === config.draftLabel;
    check(evidence.heldLabelMatches, 'The held request did not carry the complete draft label');
    const { draft, pending } = await waitForDraft(page, config);
    check(draft.ownerId === ownerId && blockedPosts > 0 && pending.body === requestBody
      && pending.key === request.headers()['idempotency-key'],
    'R13 queued request does not match the blocked edit on the network');
    evidence.draftPersistedWhileHeld = true;
    if (!onQueuedAttempt) {
      await queued.route.abort('internetdisconnected');
      aborted = true;
      await waitForDraft(page, { ...config, expectedDraft: draft });
      evidence.draftPersistedAfterAbort = true;
    }
    return { draft, blockedPosts, evidence, accepted };
  } catch (error) {
    if (evidence.outcome === 'starting') evidence.outcome = error?.name === 'TimeoutError' ? 'field-actionability' : 'authoring-failed';
    if (error instanceof Error) error.authorEvidence = evidence;
    throw error;
  } finally {
    if (queued && !onQueuedAttempt && !aborted) {
      await queued.route.abort('internetdisconnected').catch(() => {});
    }
    page.off('request', onSaveRequest);
    page.off('requestfailed', onSaveFailure);
  }
}

function observeRequests(page, origin) {
  const saveAttempts = [];
  const manifestReads = [];
  page.on('request', (request) => {
    if (!commitPost(request)) return;
    saveAttempts.push({
      method: request.method(), path: runtimePath(new URL(request.url()).pathname),
      bodySha256: sha256(request.postData() ?? ''),
    });
  });
  page.on('response', (response) => {
    if (response.ok() && response.url() === origin + '/install-manifest.json') {
      manifestReads.push(response.body().then(sha256, () => null));
    }
  });
  return { saveAttempts, manifestReads };
}

async function notice(page, config) {
  const required = page.getByTestId('quiet-update-notice').getByTestId('required-update-reason');
  const summary = required.locator('summary');
  await summary.getByText('Required', { exact: true }).waitFor({ state: 'visible', timeout: PROMPT_TIMEOUT });
  if (!(await required.evaluate((details) => details.open))) await summary.click();
  const reason = required.getByText('This update is required: ' + config.requiredReason, { exact: false });
  const action = required.getByRole('button', { name: 'Update now', exact: true })
    .or(required.getByText('Run it yourself', { exact: true })).first();
  await reason.waitFor({ state: 'visible' });
  await action.waitFor({ state: 'visible' });
  return { title: (await summary.innerText()).trim(), reason: (await reason.innerText()).trim(),
    action: (await action.innerText()).trim() };
}

async function visitReceipt(page, config, kind, requests, initialAttempt = 0) {
  page.setDefaultTimeout(PROMPT_TIMEOUT);
  let prompt = null;
  let failure = null;
  try {
    prompt = await notice(page, config);
  } catch (error) {
    failure = error?.name === 'TimeoutError' ? 'required-notice-timeout' : 'required-notice-error';
  }
  const saveAttempts = requests.saveAttempts.slice(initialAttempt);
  const manifestResponseSha256 = (await Promise.all(requests.manifestReads)).filter(Boolean);
  return { kind, visible: Boolean(prompt), prompt, failure, manifestResponseSha256,
    saveAttemptsBeforePrompt: saveAttempts.length, saveAttempts };
}

async function freshContext(browser, config, action) {
  const instance = await browser.launch({ args: chromiumArgs(config) });
  try {
    const context = await instance.newContext(PROFILE_OPTIONS);
    await context.addInitScript(() => localStorage.setItem('astrid-editor-tour-v1', '1'));
    const page = await context.newPage();
    page.setDefaultTimeout(TIMEOUT);
    return await action(page);
  } finally {
    await instance.close();
  }
}

async function publication(page, context, browser, config, onPhase) {
  let published = false;
  const manifestUrl = config.origin + '/install-manifest.json';
  const oldManifest = Buffer.from(config.initialManifestB64, 'base64');
  await context.route(manifestUrl, (route) => published ? route.continue() : route.fulfill({
    status: 200, contentType: 'application/json', body: oldManifest,
    headers: { 'cache-control': 'no-store' },
  }));
  const requests = observeRequests(page, config.origin);
  const authored = await author(page, config, { onPhase });
  await page.evaluate(() => { window.__astridFix17OriginalDocument = true; });
  const initialAttempt = requests.saveAttempts.length;
  published = true;
  onPhase?.('observe-required-prompt');
  const fresh = await freshContext(browser, config, async (tab) => {
    const freshRequests = observeRequests(tab, config.origin);
    await tab.route('**/*', (route) => commitPost(route.request()) ? route.abort('internetdisconnected') : route.continue());
    await tab.goto(config.origin + '/tools/video-editor', { waitUntil: 'domcontentloaded' });
    return { ...await visitReceipt(tab, config, 'fresh-visit', freshRequests), freshProfile: true };
  });
  const warm = {
    kind: 'already-open-edited-tab',
    sameDocument: await page.evaluate(() => window.__astridFix17OriginalDocument === true),
    initialManifestSha256: sha256(oldManifest),
    manifestResponseSha256: (await Promise.all(requests.manifestReads)).filter(Boolean),
    saveAttempts: requests.saveAttempts.slice(initialAttempt),
    draft: (await findDraft(page, { ...config, expectedDraft: authored.draft }))?.draft ?? null,
  };
  // The published R13 document caches the release status. The unchanged page
  // proves draft retention while the fresh R13 page proves the required prompt.
  const complete = warm.sameDocument && warm.draft && warm.saveAttempts.length === 0
    && warm.manifestResponseSha256.includes(warm.initialManifestSha256)
    && fresh.visible && fresh.saveAttemptsBeforePrompt === 0 && fresh.manifestResponseSha256.includes(config.manifestSha256);
  return { status: complete ? 'pass' : 'fail', author: { status: 'pass', mode: 'author', ...authored },
    publication_model: 'r13-cached-open-tab',
    draft: authored.draft, visits: [warm, fresh], browserManifestSha256: fresh.manifestResponseSha256.at(-1) ?? null,
    prompt: fresh.prompt, ...(complete ? {} : { error: 'Required prompt or unchanged R13 draft evidence was incomplete' }) };
}

async function reopen(page, browser, config, onPhase) {
  let pending = null;
  let clicked = false;
  const attempts = [];
  const allowed = new Set();
  await page.route('**/*', (route) => {
    const request = route.request();
    if (!commitPost(request)) return route.continue();
    const body = request.postData() ?? '';
    const key = request.headers()['idempotency-key'] ?? '';
    const matches = Boolean(pending && isCommit(request, config) && body === pending.body && key === pending.key);
    const permit = clicked && matches && allowed.size === 0;
    attempts.push({ method: request.method(), path: runtimePath(new URL(request.url()).pathname),
      bodySha256: sha256(body), keySha256: sha256(key), disposition: permit ? 'allowed' : 'blocked' });
    if (!permit) return route.abort('internetdisconnected');
    allowed.add(request);
    return route.continue();
  });
  const track = await openEditor(page, config, onPhase);
  onPhase?.('read-prior-draft');
  const original = await waitForDraft(page, config);
  pending = original.pending;
  const label = await track.inputValue();
  check(label === config.draftLabel || label === config.savedLabel, 'The App opened a different composition');
  const button = page.locator('[data-save-bar]').getByRole('button', { name: 'Try again', exact: true });
  await button.waitFor({ state: 'visible', timeout: PROMPT_TIMEOUT });
  onPhase?.('retry-prior-draft');
  const accepted = page.waitForResponse((response) => allowed.has(response.request()),
    { timeout: PROMPT_TIMEOUT }).catch(() => null);
  clicked = true;
  await button.click();
  const response = await accepted;
  if (!response) return { status: 'fail', error: 'Try again sent no matching original queued request',
    draft: original.draft, retry: { clicked }, saveAttempts: attempts };
  const body = await response.json();
  if (!response.ok() || body.data?.status !== 'accepted' || !body.data.accepted_revision_id
    || !body.receipt?.receipt_id || body.receipt.idempotency_key !== pending.key) {
    const code = typeof body.error?.code === 'string' && /^[a-zA-Z0-9_.-]{1,80}$/.test(body.error.code)
      ? body.error.code : 'unverified-response';
    return { status: 'fail', error: 'Try again did not accept the original queued edit',
      draft: original.draft, retry: { clicked, responseStatus: response.status(), errorCode: code }, saveAttempts: attempts };
  }
  const deadline = Date.now() + 10_000;
  while ((await queueRows(page, config)).some((row) => row.ownerId === original.draft.ownerId
    && (row.pending?.intentId === original.draft.intentId
      || row.intents?.some((intent) => intent.id === original.draft.intentId)))) {
    check(Date.now() < deadline, 'Accepted request remained in the save queue');
    await page.waitForTimeout(200);
  }
  const retry = { clicked, name: 'Try again', responseStatus: response.status(), revision: body.data.accepted_revision_id,
    publication: body.receipt.receipt_id, removedFromQueue: true, idempotencyKeyMatches: true };
  const reopenedLabel = await freshContext(browser, config, async (freshPage) => {
    await freshPage.route('**/*', (route) => commitPost(route.request()) ? route.abort('internetdisconnected') : route.continue());
    const freshTrack = await openEditor(freshPage, config);
    return freshTrack.inputValue();
  });
  return { status: reopenedLabel === config.draftLabel ? 'pass' : 'fail',
    draft: original.draft, beforeRetryLabel: label, reopenedLabel, draftVisible: reopenedLabel === config.draftLabel,
    freshProfile: true, retry, saveAttempts: attempts,
    ...(reopenedLabel === config.draftLabel ? {} : { error: 'Fresh R14 client did not reopen the accepted draft composition label' }) };
}

export async function withBrowserPage(config, chromium, run) {
  check(config.origin === 'https://astrid.haus' && config.spki?.length >= 32 && config.profileDir,
    'An isolated hosted origin, TLS pin and persistent profile are required');
  const browser = chromium ?? createRequire(join(config.browserDriverApp, 'package.json'))('@playwright/test').chromium;
  const context = await browser.launchPersistentContext(config.profileDir, {
    args: chromiumArgs(config), ...PROFILE_OPTIONS,
  });
  try {
    await context.addInitScript(() => localStorage.setItem('astrid-editor-tour-v1', '1'));
    const page = context.pages()[0] ?? await context.newPage();
    page.setDefaultTimeout(TIMEOUT);
    return await run(page, context, browser);
  } finally {
    await context.close();
  }
}

export async function execute(config, chromium, onPhase) {
  check(['author', 'publication', 'reopen'].includes(config.mode), 'Unknown FIX-17 browser step');
  return withBrowserPage(config, chromium, async (page, context, browser) => {
    let result;
    if (config.mode === 'publication') result = await publication(page, context, browser, config, onPhase);
    else if (config.mode === 'reopen') result = await reopen(page, browser, config, onPhase);
    else result = { status: 'pass', ...await author(page, config, { onPhase }) };
    return { mode: config.mode, ...result };
  });
}

async function main() {
  const config = JSON.parse(readFileSync(process.argv[2], 'utf8'));
  let result;
  let stage = 'launch-browser';
  try {
    result = await execute(config, undefined, (nextStage) => { stage = nextStage; });
  } catch (error) {
    // Playwright errors may contain request data. Retain a bounded safe stage signal.
    const code = error?.name === 'TimeoutError' ? 'browser-timeout' : 'browser-operation-failed';
    const safe = typeof error?.message === 'string' && /^[a-zA-Z0-9 .'-]{1,175}$/.test(error.message);
    result = { status: 'fail', mode: config.mode, stage, error: safe ? error.message : code,
      ...(error?.authorEvidence ? { author: { status: 'fail', mode: 'author', ...error.authorEvidence } } : {}) };
  }
  writeFileSync(config.result, JSON.stringify(result, null, 2) + '\n');
}

if (pathToFileURL(process.argv[1] ?? '').href === import.meta.url) await main();
