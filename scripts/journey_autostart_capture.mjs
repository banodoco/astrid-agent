// Captures the autostart-off card from the candidate's hosted App, served at https://astrid.haus through
// the journey's intercept, in a real Chromium that pins the intercept's leaf. Run by
// scripts/journey_version_jumps.py for the fresh-install-autostart-off rows.
//
// Usage: node journey_autostart_capture.mjs <config.json>
// The config names: playwright (the module to load), origin, entry, spki, text (the approved card sentence),
// dpr, screenshot, result and timeoutMs. The run fails if the card's sentence is not shown.
import { readFileSync, writeFileSync } from 'node:fs';
import { chromiumArgs } from './journey_update_button.mjs';

export async function capture(config) {
  const { chromium } = await import(config.playwright);
  const browser = await chromium.launch({ args: chromiumArgs({ origin: config.origin, spki: config.spki }) });
  try {
    const context = await browser.newContext({
      deviceScaleFactor: config.dpr,
      viewport: { width: 1280, height: 900 },
      storageState: { cookies: [], origins: [{ origin: config.origin, localStorage: [{ name: 'astrid-editor-tour-v1', value: '1' }] }] },
    });
    const page = await context.newPage();
    await page.goto(`${config.origin}${config.entry}`, { waitUntil: 'domcontentloaded', timeout: config.timeoutMs });
    // The wait is the card itself: it fails, with a timeout, unless the approved sentence is shown.
    await page.getByText(config.text, { exact: true }).first().waitFor({ state: 'visible', timeout: config.timeoutMs });
    await page.screenshot({ path: config.screenshot, fullPage: false });
    const result = { ok: true, device_scale_factor: config.dpr, text: config.text, url: `${config.origin}${config.entry}` };
    writeFileSync(config.result, `${JSON.stringify(result, null, 2)}\n`);
    return result;
  } finally {
    await browser.close();
  }
}

if (import.meta.url === `file://${process.argv[1]}`) {
  const config = JSON.parse(readFileSync(process.argv[2], 'utf8'));
  await capture(config);
}
