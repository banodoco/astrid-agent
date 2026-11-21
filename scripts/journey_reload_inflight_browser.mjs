#!/usr/bin/env node
/** Keep the shipped R13 document and its pending save alive during the native R14 update. */
import { existsSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import {
  author, check, commitPost, findDraft, runtimePath, sha256, trackName, withBrowserPage,
} from './journey_required_update_browser.mjs';

const UI_TIMEOUT = 45_000;
const NOTICE = 'Editor updated · Reload';
const REFUSAL = /^Could not safely reload/;

function writeReceipt(path, value) {
  const temp = path + '.tmp';
  writeFileSync(temp, JSON.stringify(value, null, 2) + '\n', { mode: 0o600 });
  renameSync(temp, path);
}

function isEditor(url, config) {
  return url.origin === config.origin && url.pathname === '/tools/video-editor'
    && url.searchParams.get('project') === config.projectId
    && url.searchParams.get('timeline') === config.timelineId;
}

async function observeRestore(page, config, response, draft) {
  check(response?.ok() && isEditor(new URL(response.url()), config), 'No R14 editor navigation response');
  check(sha256(await response.body()) === config.candidateIndexSha256, 'Reload did not receive the pinned R14 editor page');
  check(await page.evaluate(() => window.__fix17OriginalDocument !== true), 'The original R13 document was not replaced');
  const track = trackName(page);
  const retry = page.locator('[data-save-bar]').getByRole('button', { name: 'Try again', exact: true });
  const arrival = page.locator('[data-testid="runtime-connection-status"][data-state="connected"]');
  try { await arrival.waitFor({ state: 'attached', timeout: UI_TIMEOUT }); } catch { /* Report what is present. */ }
  let restored = null;
  let pending = null;
  let offered = false;
  const deadline = Date.now() + 15_000;
  do {
    restored = await track.inputValue({ timeout: 2000 }).catch(() => null);
    pending = (await findDraft(page, { ...config, expectedDraft: draft }))?.draft ?? null;
    offered = Boolean(pending) && await retry.isVisible();
    if (restored === config.draftLabel || offered) break;
    await page.waitForTimeout(150);
  } while (Date.now() < deadline);
  const url = new URL(page.url());
  return {
    app_sha: config.expectedApp, index_sha256: config.candidateIndexSha256, document_replaced: true,
    project_id: url.searchParams.get('project'), timeline_id: url.searchParams.get('timeline'),
    draft_digest: draft.payloadSha256,
    persisted_label: restored, offered,
    pending: Boolean(pending), draft_label: config.draftLabel, draft_id: pending?.intentId ?? null,
  };
}

async function reloadInflight(page, config) {
  const receipt = {
    status: 'fail', mode: config.mode, origin: config.origin, stage: 'start',
    reload: { clicked: false }, r14_audit: { requests: [], r13_save_requests_after_switch: [] },
  };
  let heldRoute = null;
  let switched = false;
  let newDocument = false;
  const buildUrl = config.origin + '/build.json';

  const record = (stage) => {
    receipt.stage = stage;
    writeReceipt(config.result, receipt);
  };
  const onNavigation = (frame) => {
    if (receipt.reload.clicked && frame === page.mainFrame() && isEditor(new URL(frame.url()), config)) {
      newDocument = true;
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
      receipt.r14_audit.requests.push({
        method: request.method(), path: url.pathname, source: newDocument ? 'R14' : 'R13',
        kind: request.isNavigationRequest() ? 'navigation' : 'build-watcher',
      });
    }
  };
  page.on('framenavigated', onNavigation);
  page.on('request', onRequest);
  try {
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
    record('native-r14-published');
    const [buildResponse] = await Promise.all([
      page.waitForResponse((response) => response.ok() && response.url() === buildUrl, { timeout: UI_TIMEOUT }),
      page.evaluate(() => window.dispatchEvent(new Event('focus'))),
    ]);
    const buildBytes = await buildResponse.body();
    const build = JSON.parse(buildBytes.toString('utf8'));
    const buildDigest = sha256(buildBytes);
    check(build.app === config.expectedApp && build.release === 14 && buildDigest === config.candidateBuildSha256,
      'Watcher did not read the pinned R14 build bytes');
    receipt.transition = {
      origin: config.origin, url: buildUrl, app_sha: build.app, build_sha256: buildDigest, initial_app_sha: oldBuild.app,
    };
    record('r14-build-observed');

    const notice = page.getByRole('status').filter({ hasText: NOTICE });
    const button = notice.getByRole('button', { name: 'Reload', exact: true });
    await button.waitFor({ state: 'visible', timeout: UI_TIMEOUT });
    const noticeText = (await notice.innerText()).replace(/\s+/g, ' ').trim();
    check(noticeText === NOTICE && await page.evaluate(() => window.__fix17OriginalDocument === true),
      'The exact Reload notice did not appear in the original R13 document');
    receipt.notice = { text: noticeText, visible: true, original_document: true };
    const warning = notice.getByRole('alert').filter({ hasText: REFUSAL });
    const navigation = page.waitForResponse((response) => response.request().isNavigationRequest()
      && response.frame() === page.mainFrame() && isEditor(new URL(response.url()), config), { timeout: UI_TIMEOUT });
    const clickOutcome = Promise.race([
      navigation.then((response) => ({ response })),
      warning.waitFor({ state: 'visible', timeout: UI_TIMEOUT }).then(async () => ({
        refused: (await warning.innerText()).replace(/\s+/g, ' ').trim().slice(0, 300),
      })),
    ]);
    // Observe the refusal even if the Playwright click itself later times out.
    clickOutcome.catch(() => {});
    receipt.reload.clicked = true;
    record('guarded-reload-clicked');
    await button.click({ timeout: UI_TIMEOUT });
    const outcome = await clickOutcome;
    let response = outcome.response;
    if (outcome.refused) {
      receipt.reload.result = 'unsafe-reload-fallback';
      receipt.reload.warning = outcome.refused;
      let dialog = null;
      page.once('dialog', async (prompt) => {
        dialog = prompt.type();
        await prompt.accept();
      });
      record('guard-refused-plain-reload');
      response = await page.reload({ waitUntil: 'domcontentloaded', timeout: UI_TIMEOUT });
      receipt.reload.plain_dialog = dialog;
    } else {
      receipt.reload.result = 'reloaded';
    }
    receipt.restore = await observeRestore(page, config, response, authored.draft);
    const recovered = receipt.restore.persisted_label === config.draftLabel || receipt.restore.offered;
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
    receipt.error = /^[\w .';-]{1,160}$/.test(message) ? message
      : error?.name === 'TimeoutError' ? 'browser-timeout' : 'browser-operation-failed';
    if (error?.authorEvidence) receipt.author = { status: 'fail', mode: 'author', ...error.authorEvidence };
  } finally {
    if (heldRoute) await heldRoute.abort('internetdisconnected').catch(() => {});
    page.off('framenavigated', onNavigation);
    page.off('request', onRequest);
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
