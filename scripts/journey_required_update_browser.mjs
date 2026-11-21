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

async function editTrack(track, label) {
  await track.fill(label);
  await track.press('Enter');
  await track.blur();
}

async function queueRows(page) {
  return page.evaluate(async () => {
    const database = 'astrid.command-outbox';
    if (!(await indexedDB.databases()).some((entry) => entry.name === database)) return [];
    return new Promise((resolve, reject) => {
      const open = indexedDB.open(database);
      open.onerror = () => reject(open.error);
      open.onsuccess = () => {
        const db = open.result;
        const tx = db.transaction('owners', 'readonly');
        const read = tx.objectStore('owners').getAll();
        tx.oncomplete = () => { db.close(); resolve(read.result); };
        tx.onabort = () => { db.close(); reject(tx.error); };
      };
    });
  });
}

// Keep the raw persisted request in memory. Only scoped IDs and digests leave the browser runner.
export async function findDraft(page, config) {
  const matches = [];
  for (const row of await queueRows(page)) {
    if (row.scope?.projectId !== config.projectId || row.scope?.timelineId !== config.timelineId) continue;
    for (const intent of row.intents ?? []) {
      const payload = JSON.stringify(intent.payload);
      if (!payload.includes(JSON.stringify(config.draftLabel))) continue;
      const pending = row.pending?.intentId === intent.id ? row.pending.request : null;
      if (!pending || pending.method !== 'POST' || typeof pending.path !== 'string'
        || runtimePath(pending.path) !== selectedPath(config)
        || typeof pending.body !== 'string' || typeof pending.key !== 'string' || !pending.key) continue;
      const draft = {
        realmId: row.scope.realmId, actorId: row.scope.actorId, projectId: row.scope.projectId,
        timelineId: row.scope.timelineId, ownerId: row.ownerId, intentId: intent.id,
        payloadSha256: sha256(payload), requestSha256: sha256(pending.body), keySha256: sha256(pending.key),
      };
      if (!config.expectedDraft || Object.keys(draft).every((key) => draft[key] === config.expectedDraft[key])) {
        matches.push({ draft, pending });
      }
    }
  }
  check(matches.length <= 1, 'More than one queued request matches the original draft');
  return matches[0];
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
  let queuedAttempt = null;
  await page.route('**/*', (route) => {
    const request = route.request();
    if (!commitPost(request)) return route.fallback();
    if (newDocument?.()) return route.continue();
    if (!queueing && isCommit(request, config)) return route.continue();
    if (isCommit(request, config)) {
      blockedPosts++;
      if (queueing && onQueuedAttempt) { queuedAttempt ??= request; return onQueuedAttempt(route, request); }
    }
    return route.abort('internetdisconnected');
  });
  const track = await openEditor(page, config, onPhase);
  onPhase?.('accept-r13-edit');
  check(await track.inputValue() === config.initialLabel, 'R13 did not open the seeded composition');
  await page.getByTestId('required-update-reason').waitFor({ state: 'hidden', timeout: 5_000 });
  const first = page.waitForResponse((response) => isCommit(response.request(), config));
  await editTrack(track, config.savedLabel);
  const response = await first;
  const body = await response.json();
  check(response.ok() && body.data?.status === 'accepted' && body.data.accepted_revision_id
    && body.receipt?.receipt_id, 'R13 did not accept the saved composition edit');
  queueing = true;
  onPhase?.('hold-r13-draft');
  let attempt;
  if (onQueuedAttempt) {
    await editTrack(track, config.draftLabel);
    const attemptDeadline = Date.now() + PROMPT_TIMEOUT;
    while (!queuedAttempt && Date.now() < attemptDeadline) await page.waitForTimeout(25);
    check(queuedAttempt, 'The second scoped R13 save was not captured');
    attempt = queuedAttempt;
  } else {
    [attempt] = await Promise.all([
      page.waitForEvent('requestfailed', (request) => isCommit(request, config)),
      editTrack(track, config.draftLabel),
    ]);
  }
  const { draft } = await waitForDraft(page, config);
  check(blockedPosts > 0 && draft.requestSha256 === sha256(attempt.postData() ?? '')
    && draft.keySha256 === sha256(attempt.headers()['idempotency-key'] ?? ''),
  'R13 queued request does not match the blocked edit on the network');
  return { draft, blockedPosts, accepted: {
    revision: body.data.accepted_revision_id, publication: body.receipt.receipt_id,
  } };
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
  const [warm, fresh] = await Promise.all([
    visitReceipt(page, config, 'already-open-edited-tab', requests, initialAttempt),
    freshContext(browser, config, async (tab) => {
      const freshRequests = observeRequests(tab, config.origin);
      await tab.route('**/*', (route) => commitPost(route.request()) ? route.abort('internetdisconnected') : route.continue());
      await tab.goto(config.origin + '/tools/video-editor', { waitUntil: 'domcontentloaded' });
      return { ...await visitReceipt(tab, config, 'fresh-visit', freshRequests), freshProfile: true };
    }),
  ]);
  warm.sameDocument = await page.evaluate(() => window.__astridFix17OriginalDocument === true);
  warm.initialManifestSha256 = sha256(oldManifest);
  warm.draft = (await findDraft(page, { ...config, expectedDraft: authored.draft }))?.draft ?? null;
  const complete = warm.sameDocument && warm.draft && [warm, fresh].every((visit) =>
    visit.visible && visit.saveAttemptsBeforePrompt === 0 && visit.manifestResponseSha256.includes(config.manifestSha256));
  return { status: complete ? 'pass' : 'fail', author: { status: 'pass', mode: 'author', ...authored },
    draft: authored.draft, visits: [warm, fresh], browserManifestSha256: fresh.manifestResponseSha256.at(-1) ?? null,
    prompt: fresh.prompt, ...(complete ? {} : { error: 'R13 publication visits did not both prove a required prompt before save' }) };
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
  while ((await queueRows(page)).some((row) => row.ownerId === original.draft.ownerId
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
    result = { status: 'fail', mode: config.mode, stage, error: safe ? error.message : code };
  }
  writeFileSync(config.result, JSON.stringify(result, null, 2) + '\n');
}

if (pathToFileURL(process.argv[1] ?? '').href === import.meta.url) await main();
