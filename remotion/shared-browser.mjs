// One chrome-headless-shell for every Astrid Remotion stack.
//
// Remotion downloads its browser into `<project>/node_modules/.remotion` (the
// nearest package.json above the cwd), so each stack carried its own 193 MB
// copy. This module points Remotion at one copy per Remotion version under
// ~/Library/Caches/astrid-remotion-browser/<version>/, which
// scripts/fetch-shared-browser.mjs fills once. When that copy is absent the
// functions return null and Remotion keeps its per-project default, so nothing
// here downloads during a render.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

// Overrides the shared location (an explicit path must exist; it is never ignored).
export const BROWSER_ENV = 'ASTRID_REMOTION_BROWSER_EXECUTABLE';

// Remotion evaluates remotion.config.ts (and what it imports) as bundled CommonJS, where import.meta is empty:
// an unconditional fileURLToPath(import.meta.url) here threw while the config loaded, so no render could start.
// Native ESM (the frame worker, the scripts) keeps the module-relative default; the config loader runs from
// the selected Remotion root, so its working directory is the project.
const here = typeof import.meta.url === 'string' ? path.dirname(fileURLToPath(import.meta.url)) : null;
const defaultProjectRoot = () => (here === null ? process.cwd() : here);

export const sharedBrowserRoot = () =>
  path.join(os.homedir(), 'Library', 'Caches', 'astrid-remotion-browser');

// Remotion's own layout inside a download: chrome-headless-shell/<platform>/<dir>/<executable>.
export function browserPlatformDir(platform = process.platform, arch = process.arch) {
  if (platform === 'darwin' && arch === 'arm64') return 'mac-arm64';
  if (platform === 'darwin' && arch === 'x64') return 'mac-x64';
  if (platform === 'linux' && arch === 'arm64') return 'linux-arm64';
  if (platform === 'linux' && arch === 'x64') return 'linux64';
  if (platform === 'win32' && arch === 'x64') return 'win64';
  return null;
}

export function browserRelativePath(platform = process.platform, arch = process.arch) {
  const dir = browserPlatformDir(platform, arch);
  if (!dir) return null;
  const exe = platform === 'win32' ? 'chrome-headless-shell.exe' : 'chrome-headless-shell';
  const linuxHeadlessShell = platform === 'linux' &&
    (arch === 'arm64' || isAmazonLinux2023());
  const executable = linuxHeadlessShell ? 'headless_shell' : exe;
  return path.join('chrome-headless-shell', dir, `chrome-headless-shell-${dir}`, executable);
}

function isAmazonLinux2023() {
  try {
    const release = fs.readFileSync('/etc/os-release', 'utf8');
    return release.includes('Amazon Linux') && release.includes('VERSION="2023"');
  } catch {
    return false;
  }
}

// The Remotion version this project renders with, read from its installed package
// (not via require.resolve: @remotion/renderer's exports map may hide package.json).
// Returns null when the package is not installed: the config must still load (tests, typecheck).
export function remotionRendererVersion(projectRoot = defaultProjectRoot()) {
  const pkg = path.join(projectRoot, 'node_modules', '@remotion', 'renderer', 'package.json');
  try {
    return JSON.parse(fs.readFileSync(pkg, 'utf8')).version ?? null;
  } catch {
    return null;
  }
}

export function sharedBrowserExecutable({
  env = process.env,
  projectRoot = defaultProjectRoot(),
  root = sharedBrowserRoot(),
  platform = process.platform,
  arch = process.arch,
} = {}) {
  const explicit = env[BROWSER_ENV];
  if (explicit) {
    if (!fs.existsSync(explicit)) throw new Error(`${BROWSER_ENV} points at a missing browser: ${explicit}`);
    return explicit;
  }
  const version = remotionRendererVersion(projectRoot);
  if (!version) return null;
  const relative = browserRelativePath(platform, arch);
  if (!relative) return null;
  const candidate = path.join(root, version, relative);
  return fs.existsSync(candidate) ? candidate : null;
}
