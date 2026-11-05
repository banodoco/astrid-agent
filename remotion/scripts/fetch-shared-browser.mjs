// One-time setup: download Remotion's chrome-headless-shell once and move it into
// the shared cache (see shared-browser.mjs). Run from the remotion/ directory:
//   node scripts/fetch-shared-browser.mjs
// The project's node_modules/.remotion/chrome-headless-shell becomes a symlink to
// the shared copy, so Remotion's own lookup still resolves without a second copy.
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {ensureBrowser} from '@remotion/renderer';
import {
  browserRelativePath,
  remotionRendererVersion,
  sharedBrowserExecutable,
  sharedBrowserRoot,
} from '../shared-browser.mjs';

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const relative = browserRelativePath();
if (!relative) {
  console.error(`Skipping shared Chrome Headless Shell fetch on unsupported platform ${process.platform}/${process.arch}; renders will report the missing browser.`);
  process.exit(0);
}
const version = remotionRendererVersion(projectRoot);
const existing = sharedBrowserExecutable({projectRoot});
if (existing) {
  const downloaded = path.join(projectRoot, 'node_modules', '.remotion', 'chrome-headless-shell');
  if (fs.lstatSync(downloaded, {throwIfNoEntry: false})?.isSymbolicLink()) {
    fs.rmSync(downloaded, {recursive: true, force: true});
    fs.cpSync(path.join(sharedBrowserRoot(), version, 'chrome-headless-shell'), downloaded, {recursive: true});
  }
  console.log(`shared browser already present: ${existing}`);
  process.exit(0);
}
const status = await ensureBrowser({logLevel: 'info'});
const downloaded = path.join(projectRoot, 'node_modules', '.remotion', 'chrome-headless-shell');
const target = path.join(sharedBrowserRoot(), version, 'chrome-headless-shell');
if (!fs.existsSync(path.join(downloaded, relative.replace(/^chrome-headless-shell[/\\]/, '')))) {
  throw new Error(`Remotion reported ${status.path} but ${downloaded} has no browser to share`);
}
fs.mkdirSync(path.dirname(target), {recursive: true});
if (fs.existsSync(target)) fs.rmSync(target, {recursive: true, force: true});
try {
  fs.renameSync(downloaded, target);
} catch (error) {
  if (error?.code !== 'EXDEV') throw error;
  // The install tree and user cache can be on different Windows volumes.
  // Preserve the download until the cache copy succeeds, then continue with
  // the same project-local copy used after a successful rename.
  fs.cpSync(downloaded, target, {recursive: true, errorOnExist: true});
  fs.rmSync(downloaded, {recursive: true, force: true});
}
// Directory symlinks require Developer Mode or elevation on Windows. A copy
// keeps Remotion's project-local lookup working for ordinary user installs.
fs.cpSync(target, downloaded, {recursive: true, errorOnExist: true});
const shared = sharedBrowserExecutable({projectRoot});
if (!shared) throw new Error(`shared browser did not resolve after move: ${target}`);
console.log(`shared browser ready: ${shared}`);
