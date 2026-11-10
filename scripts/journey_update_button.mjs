#!/usr/bin/env node
// Click the hosted App's own "Update now" in a real Chromium and report the request the page sent.
//
// Run by scripts/journey_hosted_update.py on the fresh Linux row. The page is the candidate's
// `npm run build:hosted` output, served by the journey's astrid.haus intercept at
// https://astrid.haus, so the page origin is the one the installed Runtime's door admits and its
// /install-manifest.json is the stand-in release. The browser pins the intercept's throwaway leaf by
// its public key; nothing else is trusted.
//
// Usage: node journey_update_button.mjs <config.json>
// The config names: app (the App checkout that holds Playwright), origin, entry, spki, door,
// updatePath, agentMark, timeoutMs, result, screenshot; optionally clicked and liveTimeoutMs.
//
// With liveTimeoutMs the browser stays on the page after the click: it writes the click's result to
// `clicked` at once, then waits for the open page to show the finished update by itself (UR-15k) and
// records when it did. The journey never reloads the page; the App may reload itself onto the
// restarted Astrid, and those navigations are counted.
import { readFileSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import path from 'node:path';

export const UPDATE_NOW = 'Update now';
/** The App's live line once an update has finished under an open page. */
export const UPDATED_LINE = /Astrid updated: Release \d+ → Release \d+/;

/** Chromium switches: the hosted origin on loopback, its leaf pinned, and the page treated as public. */
export function chromiumArgs({ origin, spki }) {
  const url = new URL(origin);
  const port = url.port || '443';
  return [
    '--no-sandbox',
    '--disable-dev-shm-usage',
    '--no-proxy-server',
    `--host-resolver-rules=MAP ${url.hostname} 127.0.0.1, MAP *.supabase.co 127.0.0.1:1, MAP *.supabase.in 127.0.0.1:1`,
    `--ignore-certificate-errors-spki-list=${spki}`,
    // The origin resolves to loopback here; production reaches the door from a public address.
    `--ip-address-space-overrides=127.0.0.1:${port}=public`,
  ];
}

/** The receipt fields of one observed browser request; header names are lower case. */
export function describeRequest({ method, url, headers, status }) {
  const names = Object.keys(headers).map((name) => name.toLowerCase()).sort();
  const lower = Object.fromEntries(Object.entries(headers).map(([key, value]) => [key.toLowerCase(), value]));
  const length = lower['content-length'];
  return {
    method,
    path: new URL(url).pathname,
    url,
    origin: lower.origin ?? null,
    content_length: length === undefined ? null : Number(length),
    headers: names,
    authorization: 'authorization' in lower,
    cookie: 'cookie' in lower,
    status,
  };
}

function loadPlaywright(app) {
  // An explicit module wins (a workstation whose browsers belong to another Playwright install).
  if (process.env.ASTRID_JOURNEY_PLAYWRIGHT) return createRequire(import.meta.url)(process.env.ASTRID_JOURNEY_PLAYWRIGHT);
  const require = createRequire(path.join(app, 'package.json'));
  for (const name of ['playwright', '@playwright/test']) {
    try { return require(name); } catch { /* try the next */ }
  }
  return createRequire(import.meta.url)('playwright');
}

export async function run(config) {
  const result = { stage: 'launch', door_requests: {} };
  let browser;
  try {
    const { chromium } = loadPlaywright(config.app);
    browser = await chromium.launch({ args: chromiumArgs(config) });
  } catch (error) {
    result.error = `${error?.name ?? 'Error'}: ${String(error?.message ?? error).slice(0, 1000)}`;
    return result;
  }
  try {
    const probe = await browser.newPage();
    const userAgent = `${await probe.evaluate(() => navigator.userAgent)} ${config.agentMark}`;
    await probe.close();
    const context = await browser.newContext({
      userAgent,
      permissions: ['local-network-access'],
      // The editor's first-run tour would cover the page; the visitor has seen it.
      storageState: { cookies: [], origins: [{ origin: config.origin, localStorage: [{ name: 'astrid-editor-tour-v1', value: '1' }] }] },
    });
    const page = await context.newPage();
    page.on('request', (request) => {
      if (!request.url().startsWith(config.door)) return;
      const line = `${request.method()} ${new URL(request.url()).pathname}`;
      result.door_requests[line] = (result.door_requests[line] ?? 0) + 1;
    });
    result.stage = 'open';
    await page.goto(`${config.origin}${config.entry}`);
    const dialog = page.getByRole('dialog');
    const button = dialog.getByRole('button', { name: UPDATE_NOW, exact: true });
    result.stage = 'offer';
    await button.waitFor({ state: 'visible', timeout: config.timeoutMs });
    const deadline = Date.now() + config.timeoutMs;
    while (!(await button.isEnabled())) {
      if (Date.now() > deadline) throw new Error('Update now stayed disabled');
      await page.waitForTimeout(500);
    }
    result.offered = { text: (await dialog.innerText()).slice(0, 4000) };
    const isUpdatePost = (request) => request.method() === 'POST' && request.url() === `${config.door}${config.updatePath}`;
    const posted = page.waitForRequest(isUpdatePost, { timeout: 60_000 });
    const answered = page.waitForResponse((response) => isUpdatePost(response.request()), { timeout: 60_000 });
    result.stage = 'click';
    await button.click();
    const request = await posted;
    const response = await answered;
    let body = null;
    try { body = await response.json(); } catch { body = null; }
    result.request = describeRequest({
      method: request.method(), url: request.url(), headers: await request.allHeaders(), status: response.status(),
    });
    result.response = { status: response.status(), body };
    result.posts = result.door_requests[`POST ${config.updatePath}`] ?? 0;
    result.stage = 'clicked';
    try {
      await dialog.getByText('Update in progress', { exact: false }).first().waitFor({ timeout: 15_000 });
      result.page_progress = true;
    } catch { result.page_progress = false; }
    // Counted again once the page has settled: a second POST from the same click would show here.
    result.posts = result.door_requests[`POST ${config.updatePath}`] ?? 0;
    if (config.clicked && config.liveTimeoutMs > 0) {
      writeFileSync(config.clicked, `${JSON.stringify(result, null, 2)}\n`);
      let navigations = 0;
      page.on('framenavigated', (frame) => { if (frame === page.mainFrame()) navigations += 1; });
      result.stage = 'live';
      try {
        const line = page.getByText(UPDATED_LINE).first();
        await line.waitFor({ state: 'visible', timeout: config.liveTimeoutMs });
        result.live = { text: (await line.innerText()).trim(), seen_at: Date.now(), navigations };
      } catch (error) {
        result.live_error = `${error?.name ?? 'Error'}: ${String(error?.message ?? error).slice(0, 500)}`;
        result.page_text = (await page.locator('body').innerText().catch(() => '')).slice(0, 4000);
      }
      result.posts = result.door_requests[`POST ${config.updatePath}`] ?? 0;
    }
    if (config.screenshot) await page.screenshot({ path: config.screenshot, fullPage: true }).catch(() => {});
  } catch (error) {
    result.error = `${error?.name ?? 'Error'}: ${String(error?.message ?? error).slice(0, 1000)}`;
    try {
      const page = browser.contexts().flatMap((context) => context.pages()).pop();
      if (page && config.screenshot) await page.screenshot({ path: config.screenshot, fullPage: true });
      if (page) result.page_text = (await page.locator('body').innerText()).slice(0, 4000);
    } catch { /* diagnostics only */ }
  } finally {
    await browser.close();
  }
  return result;
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const config = JSON.parse(readFileSync(process.argv[2], 'utf8'));
  const result = await run(config);
  writeFileSync(config.result, `${JSON.stringify(result, null, 2)}\n`);
  process.exit(result.error ? 1 : 0);
}
