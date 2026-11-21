#!/usr/bin/env node
/**
 * FIX-17: an actual hosted Astrid App browser against a separately installed Runtime.
 *
 * This deliberately does NOT synthesize a Runtime, canonical document, save queue,
 * accepted edit, or installed source. Only the second save's deterministic HTTP
 * 413 is injected to exercise the FIX-16 refusal UI; its receipt says "injected".
 * The first edit must be accepted by the genuine installed Runtime.
 *
 * Called only through journey_mixed_version.py with a runner-owned config.
 */
import { createRequire } from 'node:module';
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromiumArgs } from './journey_update_button.mjs';

const EDIT_PATH = /\/v1\/projects\/[^/]+\/timelines\/[^/]+\/commit$/;
const OUTBOX_DB = 'astrid.command-outbox';
const OUTBOX_STORE = 'owners';
const EDITOR_URL = '/tools/video-editor';
const UI_TIMEOUT = 120_000;
const scriptPath = fileURLToPath(import.meta.url);

function check(value, message) {
  if (!value) throw new Error(message);
}

function playwrightFrom(app) {
  const require = createRequire(join(app, 'package.json'));
  for (const name of ['playwright', '@playwright/test']) {
    try { return require(name); } catch { /* next dependency */ }
  }
  throw new Error('Pinned App build lacks its declared Playwright dependency');
}

function isCommit(request) {
  return request.method() === 'POST' && EDIT_PATH.test(new URL(request.url()).pathname);
}

function boundedText(value, limit = 450) {
  return String(value ?? '').slice(0, limit);
}

async function outboxOwners(page, projectId, timelineId) {
  const rows = await page.evaluate(async ({ database, store, project, timeline }) => {
    const names = await indexedDB.databases();
    if (!names.some((entry) => entry.name === database)) return null;
    return await new Promise((resolve, reject) => {
      const open = indexedDB.open(database);
      open.onerror = () => reject(open.error || new Error('Outbox database open failed'));
      open.onsuccess = () => {
        const db = open.result;
        if (!db.objectStoreNames.contains(store)) {
          db.close();
          reject(new Error('Outbox lacks the durable owners store'));
          return;
        }
        const tx = db.transaction(store, 'readonly');
        const get = tx.objectStore(store).getAll();
        tx.oncomplete = () => {
          db.close();
          resolve((get.result || []).filter((owner) =>
            owner && owner.scope && owner.scope.projectId === project &&
            owner.scope.timelineId === timeline));
        };
        tx.onabort = () => { db.close(); reject(tx.error || new Error('Outbox read aborted')); };
      };
    });
  }, { database: OUTBOX_DB, store: OUTBOX_STORE, project: projectId, timeline: timelineId });
  check(Array.isArray(rows), 'Real browser has no durable command outbox');
  return rows.map((row) => ({
    ownerId: row.ownerId,
    projectId: row.scope?.projectId,
    timelineId: row.scope?.timelineId,
    intents: Array.isArray(row.intents) ? row.intents.length : -1,
    pending: row.pending !== undefined && row.pending !== null,
    conflict: row.conflict !== undefined && row.conflict !== null,
  }));
}

async function openEditor(context, config, traffic) {
  const page = await context.newPage();
  page.on('request', (request) => {
    if (isCommit(request)) traffic.push({ method: 'POST', path: new URL(request.url()).pathname });
  });
  await page.goto(config.origin + EDITOR_URL, { waitUntil: 'domcontentloaded', timeout: UI_TIMEOUT });
  await page.locator('[data-testid="runtime-connection-status"][data-state="connected"]')
    .waitFor({ state: 'attached', timeout: UI_TIMEOUT });
  const project = page.getByRole('combobox', { name: 'Select project' });
  await project.waitFor({ state: 'visible', timeout: UI_TIMEOUT });
  check((await project.innerText()).includes(config.projectName),
    'Editor opened another project, not the selected installed Runtime project');
  await page.getByRole('combobox', { name: 'Select timeline' })
    .waitFor({ state: 'visible', timeout: UI_TIMEOUT });
  const track = page.getByRole('textbox', { name: 'Track name' }).first();
  await track.waitFor({ state: 'visible', timeout: UI_TIMEOUT });
  return { page, track };
}

async function editTrack(page, track, label) {
  await track.fill(label);
  await track.press('Enter');
  await track.blur();
}

async function waitForAcceptedCommit(page, callback) {
  const response = page.waitForResponse((r) => isCommit(r.request()), { timeout: UI_TIMEOUT });
  await callback();
  const settled = await response;
  const status = settled.status();
  check(status >= 200 && status < 300, 'Real installed Runtime rejected a normal App edit, HTTP ' + status);
  const body = await settled.json();
  check(body && body.data && body.data.status === 'accepted',
    'Runtime commit HTTP success lacked an accepted canonical document receipt');
  check(typeof body.data.accepted_revision_id === 'string' && body.data.accepted_revision_id,
    'Runtime accepted no durable revision ID');
  return { status, acceptedRevision: body.data.accepted_revision_id,
    acceptedPublication: body.data.accepted_publication_id ?? null };
}

async function caseR14AppR13Installed(context, config, traffic) {
  const { page, track } = await openEditor(context, config, traffic);
  check((await track.inputValue()) === config.initialLabel,
    'New App did not open the R13-authored parent-track label');
  const edit = await waitForAcceptedCommit(page, () => editTrack(page, track, config.savedLabel));
  await page.close();
  const reopened = await openEditor(context, config, traffic);
  check(await reopened.track.inputValue() === config.savedLabel,
    'R14 App lost the R13-install accepted edit on real browser reopen');
  const afterSave = await outboxOwners(reopened.page, config.projectId, config.timelineId);
  check(afterSave.length >= 1 && afterSave.some((row) => row.intents === 0 && !row.pending),
    'Accepted edit did not settle in the real browser command outbox');

  // Exercise the true FIX-16 App refusal handler without pretending a network
  // stub is a Runtime product defect. The *first* save above hit the Runtime.
  let rejected = 0;
  await reopened.page.route(EDIT_PATH, async (route) => {
    if (!isCommit(route.request()) || rejected !== 0) return route.continue();
    rejected++;
    await route.fulfill({
      status: 413,
      contentType: 'application/json',
      body: JSON.stringify({ code: 'payload_too_large', message: 'FIX-17 deliberately injected HTTP 413' }),
    });
  });
  const refusedLabel = config.savedLabel + ' unsaved';
  await editTrack(reopened.page, reopened.track, refusedLabel);
  await reopened.page.getByRole('button', { name: 'Copy my change out', exact: true })
    .waitFor({ state: 'visible', timeout: UI_TIMEOUT });
  await reopened.page.getByRole('button', {
    name: 'Discard and reload the saved version', exact: true,
  }).waitFor({ state: 'visible', timeout: UI_TIMEOUT });
  check(rejected === 1, 'FIX-16 card appeared without one rejected App commit');
  check(await reopened.track.inputValue() === refusedLabel,
    'FIX-16 refusal discarded the creator’s unsaved edit');
  const afterRefusal = await outboxOwners(reopened.page, config.projectId, config.timelineId);
  check(afterRefusal.some((row) => row.intents > 0 || row.pending),
    'FIX-16 refusal lost its durable pending change');
  check(traffic.length >= 2, 'Expected an accepted and then rejected editor commit');
  await reopened.page.close();
  return {
    mode: config.mode, realInstalledEdit: edit, savedLabel: config.savedLabel,
    reopened: true, rejectedHttp: { status: 413, injected: true, count: rejected },
    fix16Card: true, refusedLabelPreserved: true,
    outboxBefore: afterSave, outboxAfter: afterRefusal,
  };
}

async function caseR14Author(context, config, traffic) {
  const { page, track } = await openEditor(context, config, traffic);
  check((await track.inputValue()) === config.initialLabel,
    'R14 App cannot open the installed R14 realm before authoring');
  const edit = await waitForAcceptedCommit(page, () => editTrack(page, track, config.savedLabel));
  await page.close();
  const reopened = await openEditor(context, config, traffic);
  check(await reopened.track.inputValue() === config.savedLabel,
    'R14-authored creative edit did not survive browser reopen');
  await reopened.page.close();
  return { mode: config.mode, realInstalledEdit: edit, savedLabel: config.savedLabel, reopened: true };
}

async function caseR13ReadR14(context, config, traffic) {
  const { page, track } = await openEditor(context, config, traffic);
  check((await track.inputValue()) === config.savedLabel,
    'Published R13 App could not read an R14-authored parent timeline');
  const before = await outboxOwners(page, config.projectId, config.timelineId);
  check(before.length >= 1 && before.some((row) => !row.pending && row.intents === 0),
    'Published R13 App could not read an idle durable save queue');
  await page.close();
  const reopen = await openEditor(context, config, traffic);
  check(await reopen.track.inputValue() === config.savedLabel,
    'Published R13 App could not reopen R14-authored timeline');
  const after = await outboxOwners(reopen.page, config.projectId, config.timelineId);
  check(after.length >= 1 && after.some((row) => !row.pending && row.intents === 0),
    'Published R13 App lost or changed its save queue on reopen');
  check(traffic.length === 0, 'Read-only R13 App sent an unexpected timeline commit');
  await reopen.page.close();
  return { mode: config.mode, savedLabel: config.savedLabel,
    legacyAppOpened: true, legacyQueueRead: true, reopened: true,
    outboxBefore: before, outboxAfter: after, editorPosts: traffic.length };
}

export async function execute(config, environment = {}) {
  check(config.origin === 'https://astrid.haus', 'Mixed-version browser requires real hosted origin');
  check(/^http:\/\/127\.0\.0\.1:\d+$/.test(config.door), 'Runtime door must be loopback');
  check(typeof config.spki === 'string' && config.spki.length >= 32, 'Pinned TLS leaf is required');
  check(['r14-app-r13-installed', 'r14-author-r14-installed', 'r13-app-r14-installed'].includes(config.mode),
    'Unknown FIX-17 mixed-version browser mode');
  for (const name of ['app', 'projectId', 'timelineId', 'projectName', 'initialLabel', 'savedLabel', 'origin']) {
    check(typeof config[name] === 'string' && config[name].length > 0, 'Missing mixed-version input: ' + name);
  }
  const browserAPI = environment.chromium || playwrightFrom(config.app).chromium;
  const browser = await browserAPI.launch({ args: chromiumArgs({ origin: config.origin, spki: config.spki }) });
  const traffic = [];
  try {
    const probe = await browser.newPage();
    const userAgent = (await probe.evaluate(() => navigator.userAgent)) + ' astrid-fix17-mixed-version';
    await probe.close();
    const context = await browser.newContext({
      userAgent,
      permissions: ['local-network-access'],
      storageState: { cookies: [], origins: [{ origin: config.origin,
        localStorage: [{ name: 'astrid-editor-tour-v1', value: '1' }] }] },
    });
    const observed = config.mode === 'r14-app-r13-installed'
      ? await caseR14AppR13Installed(context, config, traffic)
      : config.mode === 'r14-author-r14-installed'
        ? await caseR14Author(context, config, traffic)
        : await caseR13ReadR14(context, config, traffic);
    await context.close();
    return { status: 'pass', phase: 'completed', mode: config.mode,
      browserApp: config.browserAppSha, realRuntime: true, traffic, ...observed };
  } finally {
    await browser.close();
  }
}

async function main() {
  const config = JSON.parse(readFileSync(process.argv[2], 'utf8'));
  let result;
  try {
    result = await execute(config);
  } catch (error) {
    result = { status: 'fail', mode: config.mode,
      error: boundedText(error?.name) + ': ' + boundedText(error?.message) };
  }
  writeFileSync(config.result, JSON.stringify(result, null, 2) + '\n');
  if (result.status !== 'pass') {
    console.error('FIX-17 mixed-version browser failed:', result.error);
    process.exitCode = 1;
  }
}

if (scriptPath === process.argv[1]) await main();
