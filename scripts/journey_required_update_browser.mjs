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
  await page.goto(config.origin + '/tools/video-editor', { waitUntil: 'domcontentloaded' });
  await page.locator('[data-testid="runtime-connection-status"][data-state="connected"]')
    .waitFor({ state: 'attached' });
  const project = page.getByRole('combobox', { name: 'Select project' });
  await project.waitFor({ state: 'visible' });
  if (!(await project.innerText()).includes(config.projectName)) {
    await project.click();
    await page.getByRole('option', { name: config.projectName, exact: true }).click();
  }
  const track = page.getByRole('textbox', { name: 'Track name' }).first();
  await track.waitFor({ state: 'visible' });
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
  const draft = await waitForDraft(page, config);
  const response = await published;
  const manifest = await response.body();
  return {
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

const actions = { author, prompt, reopen };

export async function execute(config, chromium) {
  check(Object.hasOwn(actions, config.mode), 'Unknown FIX-17 browser step');
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
    result = { status: 'fail', mode: config.mode, error: String(error).slice(0, 500) };
    console.error(result.error);
    process.exitCode = 1;
  }
  writeFileSync(config.result, JSON.stringify(result, null, 2) + '\n');
}

if (fileURLToPath(import.meta.url) === process.argv[1]) await main();
