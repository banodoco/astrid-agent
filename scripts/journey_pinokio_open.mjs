#!/usr/bin/env node
/** Open the launcher menu target with the frozen hosted App and the live local Runtime. */
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';
import { mkdir, stat, writeFile } from 'node:fs/promises';
import path from 'node:path';

const [distArgument, target, output, appSha, manifestSha, appCheckout] = process.argv.slice(2);
const dist = path.resolve(distArgument);
const origin = 'https://astrid.haus';
const healthUrl = 'http://127.0.0.1:17340/api/runtime/v1/health';
if (target !== origin + '/?connect=1') throw new Error('Unexpected Pinokio Open Astrid URL');
const require = createRequire(path.join(path.resolve(appCheckout), 'package.json'));
const { chromium } = require('@playwright/test');
const browser = await chromium.launch();
try {
  const context = await browser.newContext({ permissions: ['local-network-access'], serviceWorkers: 'block' });
  const page = await context.newPage();
  // Only the immutable hosted assets use interception. The browser's local
  // Runtime connection, permission check, and responses are real.
  await page.route(origin + '/**', async (route) => {
    const url = new URL(route.request().url());
    if (route.request().method() !== 'GET' || url.origin !== origin) return route.abort();
    const requested = decodeURIComponent(url.pathname);
    const file = path.resolve(dist, requested === '/' ? 'index.html' : '.' + requested);
    if (!file.startsWith(dist + path.sep)) return route.abort();
    if ((await stat(file).catch(() => null))?.isFile()) return route.fulfill({ path: file });
    if (!path.extname(file)) return route.fulfill({ path: path.join(dist, 'index.html') });
    return route.fulfill({ status: 404 });
  });
  // Consume the CDP response body immediately. Playwright can lose its
  // response identifier while the connection UI finishes rendering.
  // Attach both branches immediately: response parsing can reject while the
  // navigation and connection locator are still waiting.
  const healthResponse = page.waitForResponse((value) => value.url() === healthUrl && value.ok(),
    { timeout: 60_000 }).then((value) => value.json())
    .then((health) => ({ health }), (error) => ({ error }));
  const loaded = await page.goto(target, { waitUntil: 'domcontentloaded', timeout: 60_000 });
  if (!loaded?.ok()) throw new Error('Pinokio Open Astrid failed to load the App');
  const connected = page.locator('[data-testid="runtime-connection-status"][data-state="connected"]');
  await connected.waitFor({ state: 'attached', timeout: 60_000 });
  const capturedHealth = await healthResponse;
  let health = capturedHealth.health;
  if (capturedHealth.error) {
    if (capturedHealth.error.name !== 'TimeoutError'
      && !/No resource with given identifier found/i.test(String(capturedHealth.error))) {
      throw capturedHealth.error;
    }
    // The original body expired or was unavailable. This bounded GET uses
    // the same page and its normal CDP routing as the initial Runtime request.
    health = await page.evaluate(async (url) => {
      const response = await fetch(url, { cache: 'no-store', signal: AbortSignal.timeout(10_000) });
      if (!response.ok) throw new Error(`Runtime health fallback failed (${response.status})`);
      return response.json();
    }, healthUrl);
  }
  // Use the browser page for these reads so they follow the same frozen asset route.
  const asset = await page.evaluate(async () => {
    const [build, manifest] = await Promise.all([fetch('/build.json'), fetch('/install-manifest.json')]);
    if (!build.ok || !manifest.ok) throw new Error('Missing frozen hosted App assets');
    return { build: await build.json(), manifest: await manifest.text() };
  });
  const actualManifestSha = createHash('sha256').update(asset.manifest).digest('hex');
  if (asset.build.app !== appSha || actualManifestSha !== manifestSha) throw new Error('Browser read different frozen App assets');
  if (health.status !== 'ok' || health.protocol !== 'workspace.v1'
    || health.identity?.release !== 14 || health.identity?.managed_by !== 'pinokio'
    || !health.runtime_instance_id || !health.schema_digest) throw new Error('Browser received the wrong live Runtime identity');
  await mkdir(path.dirname(output), { recursive: true });
  await writeFile(output, JSON.stringify({
    target, connected: true, runtimeInstanceId: health.runtime_instance_id, runtimeIdentity: health.identity,
    schemaDigest: health.schema_digest, appBuild: asset.build.app,
    manifestSha256: actualManifestSha, manifestVerified: true,
  }, null, 2) + '\n');
} finally {
  await browser.close();
}
