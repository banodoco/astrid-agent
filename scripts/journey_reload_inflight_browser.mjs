#!/usr/bin/env node
/** Keep the shipped R13 document and its pending save alive during the native R14 update. */
import { existsSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { pathToFileURL } from 'node:url';
import {
  author, check, clickAndObserveEditorNavigation, commitPost, findDraft, runtimePath,
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

export async function clickAndObserveGuardedReload(page, button, config, refusalSignal = null, onTrustedClick = () => {}, watcherWon = () => false) {
  let clickObserved = false;
  const bindingName = '__fix17ObserveGuardedReload';
  await page.exposeBinding(bindingName, ({ frame }) => {
    if (frame === page.mainFrame()) {
      clickObserved = true;
      onTrustedClick();
    }
  });
  await button.evaluate((element, name) => {
    element.addEventListener('click', (event) => {
      if (event.isTrusted && window.__fix17OriginalDocument === true) {
        void window[name]().catch(() => {});
      }
    }, { capture: true, once: true });
  }, bindingName);
  const result = await clickAndObserveEditorNavigation(page, button, config, refusalSignal, UI_TIMEOUT);
  if (!result.refused && !clickObserved && !watcherWon()) {
    check(false, 'The original document did not observe a trusted Reload click');
  }
  return { ...result, click_observed: clickObserved, watcher_navigation: !clickObserved && watcherWon() };
}

function writeReceipt(path, value) {
  const temp = path + '.tmp';
  writeFileSync(temp, JSON.stringify(value, null, 2) + '\n', { mode: 0o600 });
  renameSync(temp, path);
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
  let offered = false;
  const saveThemButton = page.locator('[data-save-bar="recovered"]').getByRole('button', { name: 'Save them', exact: true });
  const deadline = Date.now() + 15_000;
  do {
    restored = await track.inputValue({ timeout: 2000 }).catch(() => null);
    pending = (await findDraft(page, { ...config, expectedDraft: draft }))?.draft ?? null;
    if (pending) matchedDraft = pending;
    offered = Boolean(pending) && (await restoreButton.isVisible() || await saveThemButton.isVisible());
    if ((restored === config.draftLabel && (pending || originalDraftVerified)) || offered) break;
    await page.waitForTimeout(150);
  } while (Date.now() < deadline);
  let recovery = { action: 'automatic', method: 'automatic', completed: false };
  if (offered && restored !== config.draftLabel) {
    check(pending && Object.keys(draft).every((key) => pending[key] === draft[key]),
      'Restore was offered without the matching original pending draft');
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
      throw new Error('Restored draft readback did not contain the in-flight marker');
    });
    recovery = { action, method: action, clicked: true, draft_provenance: true,
      completed: restored === config.draftLabel };
  } else {
    const provenance = Boolean(originalDraftVerified);
    recovery = { action: 'automatic', method: 'automatic', draft_provenance: provenance,
      completed: restored === config.draftLabel && provenance };
  }
  const url = new URL(page.url());
  return {
    app_sha: config.expectedApp, index_sha256: config.candidateIndexSha256, document_replaced: true,
    project_id: url.searchParams.get('project'), timeline_id: url.searchParams.get('timeline'),
    draft_digest: draft.payloadSha256,
    persisted_label: restored, readback_label: restored, offered, recovery, method: recovery.method,
    pending: Boolean(matchedDraft), draft_label: config.draftLabel, draft_id: matchedDraft?.intentId ?? null,
  };
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
  let resolveSelectedNavigation;
  let resolveR14Build;
  const selectedNavigationSeen = new Promise((resolve) => { resolveSelectedNavigation = resolve; });
  const r14BuildSeen = new Promise((resolve) => { resolveR14Build = resolve; });
  const buildUrl = config.origin + '/build.json';

  const record = (stage) => {
    receipt.stage = stage;
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
      const recovered = receipt.restore.recovery?.completed === true;
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
    receipt.reload.clicked = true;
    record('guarded-reload-clicked');
    const outcome = await clickAndObserveGuardedReload(page, button, config, refusal,
      () => { receipt.reload.click_observed = true; writeReceipt(config.result, receipt); },
      () => Boolean(watcherNavigation));
    check(watcherNavigation || receipt.reload.click_observed, 'R14 navigation had neither a watcher event nor a trusted click');
    receipt.reload.trigger = watcherNavigation && !receipt.reload.click_observed ? 'watcher' : 'click';
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
    const recovered = receipt.restore.recovery?.completed === true;
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
