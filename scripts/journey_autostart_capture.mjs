// Captures the autostart-off card from the live Runtime's App at deviceScaleFactor 2.
// Used by the fresh-install-autostart-off journey row (scripts/journey_version_jumps.py).
// Inputs (environment): ASTRID_CAPTURE_URL, ASTRID_CAPTURE_OUT (png), ASTRID_CAPTURE_META (json),
// ASTRID_CAPTURE_DPR (default 2), and ASTRID_AUTOSTART_CAPTURE_DIR (where playwright was installed).
import { writeFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const url = process.env.ASTRID_CAPTURE_URL;
const out = process.env.ASTRID_CAPTURE_OUT;
const meta = process.env.ASTRID_CAPTURE_META;
const dpr = Number(process.env.ASTRID_CAPTURE_DPR || "2");
if (!url || !out || !meta) {
  throw new Error("ASTRID_CAPTURE_URL, ASTRID_CAPTURE_OUT and ASTRID_CAPTURE_META are required");
}
const installDir = process.env.ASTRID_AUTOSTART_CAPTURE_DIR;
const playwrightModule = installDir
  ? pathToFileURL(`${installDir}/node_modules/playwright/index.mjs`).href
  : "playwright";
const { chromium } = await import(playwrightModule);

const browser = await chromium.launch();
try {
  const context = await browser.newContext({ deviceScaleFactor: dpr, viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();
  await page.goto(url, { waitUntil: "networkidle", timeout: 120000 });
  // The Runtime's own autostart-off message is the card's text; wait for it before capturing.
  await page.getByText(/saved autostart is off/i).first().waitFor({ timeout: 60000 });
  await page.screenshot({ path: out, fullPage: false });
  writeFileSync(meta, JSON.stringify({ device_scale_factor: dpr, surface: "page", origin: new URL(url).origin }));
} finally {
  await browser.close();
}
