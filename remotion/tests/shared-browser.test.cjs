const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const {spawnSync} = require('node:child_process');
const {test} = require('node:test');

// Pure module: no renderer, Chromium or network. The Remotion version comes from a
// fake installed package under a temp project root.
const {sharedBrowserExecutable, browserRelativePath, BROWSER_ENV} = require('../shared-browser.mjs');

const tmp = fs.mkdtempSync(path.join(os.tmpdir(), 'shared-browser-'));
const project = path.join(tmp, 'project');
const root = path.join(tmp, 'cache');
fs.mkdirSync(path.join(project, 'node_modules', '@remotion', 'renderer'), {recursive: true});
fs.writeFileSync(path.join(project, 'node_modules', '@remotion', 'renderer', 'package.json'), '{"version":"9.9.9"}');
test.after(() => fs.rmSync(tmp, {recursive: true, force: true}));

test('no shared copy yet: resolves to null so Remotion keeps its per-project default', () => {
  assert.equal(sharedBrowserExecutable({env: {}, projectRoot: project, root, platform: 'darwin', arch: 'arm64'}), null);
});

test('a copy for the installed Remotion version is used, other versions are not', () => {
  const rel = browserRelativePath('darwin', 'arm64');
  const exe = path.join(root, '9.9.9', rel);
  fs.mkdirSync(path.dirname(exe), {recursive: true});
  fs.writeFileSync(exe, '');
  assert.equal(sharedBrowserExecutable({env: {}, projectRoot: project, root, platform: 'darwin', arch: 'arm64'}), exe);
  assert.equal(sharedBrowserExecutable({env: {}, projectRoot: project, root: path.join(tmp, 'other'), platform: 'darwin', arch: 'arm64'}), null);
});

test('an explicit override wins and must exist', () => {
  const exe = path.join(tmp, 'explicit-browser');
  fs.writeFileSync(exe, '');
  assert.equal(sharedBrowserExecutable({env: {[BROWSER_ENV]: exe}, projectRoot: project, root}), exe);
  assert.throws(() => sharedBrowserExecutable({env: {[BROWSER_ENV]: path.join(tmp, 'missing')}, projectRoot: project, root}), /missing browser/);
});

test('layout matches Remotion download paths for each supported platform', () => {
  assert.equal(browserRelativePath('darwin', 'arm64'), path.join('chrome-headless-shell', 'mac-arm64', 'chrome-headless-shell-mac-arm64', 'chrome-headless-shell'));
  assert.equal(browserRelativePath('darwin', 'x64'), path.join('chrome-headless-shell', 'mac-x64', 'chrome-headless-shell-mac-x64', 'chrome-headless-shell'));
  assert.equal(browserRelativePath('linux', 'arm64'), path.join('chrome-headless-shell', 'linux-arm64', 'chrome-headless-shell-linux-arm64', 'headless_shell'));
  let amazonLinux2023 = false;
  try {
    const release = fs.readFileSync('/etc/os-release', 'utf8');
    amazonLinux2023 = release.includes('Amazon Linux') && release.includes('VERSION="2023"');
  } catch {}
  const linuxX64Executable = amazonLinux2023 ? 'headless_shell' : 'chrome-headless-shell';
  assert.equal(browserRelativePath('linux', 'x64'), path.join('chrome-headless-shell', 'linux64', 'chrome-headless-shell-linux64', linuxX64Executable));
  assert.equal(browserRelativePath('win32', 'x64'), path.join('chrome-headless-shell', 'win64', 'chrome-headless-shell-win64', 'chrome-headless-shell.exe'));
});

test('unsupported platforms resolve to no shared browser', () => {
  assert.equal(browserRelativePath('freebsd', 'x64'), null);
  assert.equal(browserRelativePath('win32', 'arm64'), null);
  assert.equal(sharedBrowserExecutable({env: {}, projectRoot: project, root, platform: 'freebsd', arch: 'x64'}), null);
});

test('Amazon Linux 2023 x64 uses Remotion headless_shell filename', () => {
  const preload = path.join(tmp, 'amazon-linux.cjs');
  const moduleUrl = require('node:url').pathToFileURL(path.join(__dirname, '..', 'shared-browser.mjs')).href;
  fs.writeFileSync(preload, [
    "const fs = require('node:fs');",
    'const readFileSync = fs.readFileSync.bind(fs);',
    "fs.readFileSync = (file, ...args) => String(file) === '/etc/os-release' ? 'NAME=Amazon Linux\\nVERSION=\"2023\"\\n' : readFileSync(file, ...args);",
  ].join('\n'));
  const result = spawnSync(process.execPath, [
    '--require', preload,
    '--input-type=module',
    '-e',
    `import {browserRelativePath} from ${JSON.stringify(moduleUrl)}; console.log(browserRelativePath('linux', 'x64'));`,
  ], {encoding: 'utf8'});
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stdout.trim(), path.join('chrome-headless-shell', 'linux64', 'chrome-headless-shell-linux64', 'headless_shell'));
});

test('unsupported platform fetch prints one note and never calls ensureBrowser', () => {
  const fixture = path.join(tmp, 'unsupported');
  const remotion = path.join(fixture, 'remotion');
  const renderer = path.join(remotion, 'node_modules', '@remotion', 'renderer');
  const fetchMarker = path.join(fixture, 'ensure-called');
  const preload = path.join(fixture, 'platform.cjs');
  fs.mkdirSync(path.join(remotion, 'scripts'), {recursive: true});
  fs.mkdirSync(renderer, {recursive: true});
  fs.writeFileSync(path.join(remotion, 'package.json'), '{"type":"module"}');
  fs.writeFileSync(path.join(renderer, 'package.json'), JSON.stringify({name: '@remotion/renderer', type: 'module', exports: './index.js'}));
  fs.writeFileSync(path.join(renderer, 'index.js'), `export async function ensureBrowser() { await import('node:fs').then(({default: fs}) => fs.writeFileSync(${JSON.stringify(fetchMarker)}, 'called')); }`);
  fs.copyFileSync(path.join(__dirname, '..', 'scripts', 'fetch-shared-browser.mjs'), path.join(remotion, 'scripts', 'fetch-shared-browser.mjs'));
  fs.copyFileSync(path.join(__dirname, '..', 'shared-browser.mjs'), path.join(remotion, 'shared-browser.mjs'));
  fs.writeFileSync(preload, "Object.defineProperty(process, 'platform', {value: 'freebsd'}); Object.defineProperty(process, 'arch', {value: 'x64'});\n");

  const result = spawnSync(process.execPath, ['--require', preload, 'scripts/fetch-shared-browser.mjs'], {
    cwd: remotion,
    encoding: 'utf8',
    env: {...process.env, HOME: path.join(fixture, 'home'), USERPROFILE: path.join(fixture, 'home')},
  });
  assert.equal(result.status, 0, result.stderr);
  assert.equal(result.stdout, '');
  assert.equal(result.stderr.trim(), 'Skipping shared Chrome Headless Shell fetch on unsupported platform freebsd/x64; renders will report the missing browser.');
  assert.equal(fs.existsSync(fetchMarker), false);
});

test('a project without Remotion installed resolves to null instead of throwing', () => {
  assert.equal(sharedBrowserExecutable({env: {}, projectRoot: path.join(tmp, 'empty'), root, platform: 'darwin', arch: 'arm64'}), null);
});

test('reinstall reuses the shared browser without downloading it again', () => {
  const fixture = path.join(tmp, 'reinstall');
  const remotion = path.join(fixture, 'remotion');
  const renderer = path.join(remotion, 'node_modules', '@remotion', 'renderer');
  const version = '4.0.509';
  const downloaded = path.join(remotion, 'node_modules', '.remotion', 'chrome-headless-shell');
  const downloadedExecutable = path.join(downloaded, browserRelativePath().replace(/^chrome-headless-shell[/\\]/, ''));
  const marker = path.join(fixture, 'download-count');
  fs.mkdirSync(path.join(remotion, 'scripts'), {recursive: true});
  fs.mkdirSync(renderer, {recursive: true});
  fs.writeFileSync(path.join(remotion, 'package.json'), '{"type":"module"}');
  fs.writeFileSync(path.join(renderer, 'package.json'), JSON.stringify({name: '@remotion/renderer', version, type: 'module', exports: './index.js'}));
  fs.writeFileSync(path.join(renderer, 'index.js'), `import fs from 'node:fs'; import path from 'node:path'; export async function ensureBrowser() { const count = Number(fs.readFileSync(${JSON.stringify(marker)}, 'utf8')) + 1; fs.writeFileSync(${JSON.stringify(marker)}, String(count)); fs.mkdirSync(path.dirname(${JSON.stringify(downloadedExecutable)}), {recursive: true}); fs.writeFileSync(${JSON.stringify(downloadedExecutable)}, 'fake browser'); return {path: 'fake'}; }`);
  fs.copyFileSync(path.join(__dirname, '..', 'scripts', 'fetch-shared-browser.mjs'), path.join(remotion, 'scripts', 'fetch-shared-browser.mjs'));
  fs.copyFileSync(path.join(__dirname, '..', 'shared-browser.mjs'), path.join(remotion, 'shared-browser.mjs'));
  fs.writeFileSync(marker, '0');

  const childEnv = {...process.env, HOME: path.join(fixture, 'home'), USERPROFILE: path.join(fixture, 'home')};
  delete childEnv.ASTRID_REMOTION_BROWSER_EXECUTABLE;
  const runInstall = () => spawnSync(process.execPath, ['scripts/fetch-shared-browser.mjs'], {
    cwd: remotion,
    encoding: 'utf8',
    env: childEnv,
  });

  const first = runInstall();
  assert.equal(first.status, 0, first.stderr);
  assert.match(first.stdout, /shared browser ready/);
  assert.equal(fs.readFileSync(marker, 'utf8'), '1');

  const second = runInstall();
  assert.equal(second.status, 0, second.stderr);
  assert.match(second.stdout, /shared browser already present/);
  assert.equal(fs.readFileSync(marker, 'utf8'), '1', 'reinstall must not download a second browser');
});

test('cross-volume staging copies the downloaded browser after rename returns EXDEV', () => {
  const fixture = path.join(tmp, 'cross-volume');
  const remotion = path.join(fixture, 'remotion');
  const renderer = path.join(remotion, 'node_modules', '@remotion', 'renderer');
  const downloaded = path.join(remotion, 'node_modules', '.remotion', 'chrome-headless-shell');
  const downloadedExecutable = path.join(downloaded, browserRelativePath().replace(/^chrome-headless-shell[/\\]/, ''));
  const sharedRoot = path.join(fixture, 'home', 'Library', 'Caches', 'astrid-remotion-browser');
  const sharedBrowserDir = path.join(sharedRoot, '4.0.509', 'chrome-headless-shell');
  const expectedSharedExecutable = path.join(sharedBrowserDir, browserRelativePath().replace(/^chrome-headless-shell[/\\]/, ''));
  const preload = path.join(fixture, 'exdev.cjs');
  fs.mkdirSync(path.join(remotion, 'scripts'), {recursive: true});
  fs.mkdirSync(renderer, {recursive: true});
  fs.writeFileSync(path.join(remotion, 'package.json'), '{"type":"module"}');
  fs.writeFileSync(path.join(renderer, 'package.json'), JSON.stringify({name: '@remotion/renderer', version: '4.0.509', type: 'module', exports: './index.js'}));
  fs.writeFileSync(path.join(renderer, 'index.js'), `import fs from 'node:fs'; import path from 'node:path'; export async function ensureBrowser() { fs.mkdirSync(path.dirname(${JSON.stringify(downloadedExecutable)}), {recursive: true}); fs.writeFileSync(${JSON.stringify(downloadedExecutable)}, 'fake browser'); return {path: 'fake'}; }`);
  fs.copyFileSync(path.join(__dirname, '..', 'scripts', 'fetch-shared-browser.mjs'), path.join(remotion, 'scripts', 'fetch-shared-browser.mjs'));
  fs.copyFileSync(path.join(__dirname, '..', 'shared-browser.mjs'), path.join(remotion, 'shared-browser.mjs'));
  fs.writeFileSync(preload, [
    `require('node:os').homedir = () => ${JSON.stringify(path.join(fixture, 'home'))};`,
    "const fs = require('node:fs');",
    'const renameSync = fs.renameSync.bind(fs);',
    `fs.renameSync = (source, target) => { if (String(source) === ${JSON.stringify(downloaded)} && String(target) === ${JSON.stringify(sharedBrowserDir)}) { const error = new Error('cross-device link not permitted'); error.code = 'EXDEV'; throw error; } return renameSync(source, target); };`,
  ].join('\n'));
  const result = spawnSync(process.execPath, ['--require', preload, 'scripts/fetch-shared-browser.mjs'], {
    cwd: remotion,
    encoding: 'utf8',
    env: {...process.env, HOME: path.join(fixture, 'home'), USERPROFILE: path.join(fixture, 'home')},
  });

  assert.equal(result.status, 0, result.stderr);
  assert.match(result.stdout, /shared browser ready/);
  assert.equal(fs.existsSync(expectedSharedExecutable), true, JSON.stringify({stdout: result.stdout, stderr: result.stderr, expectedSharedExecutable, downloadedExecutable}));
  assert.equal(fs.readFileSync(expectedSharedExecutable, 'utf8'), 'fake browser');
  assert.equal(fs.readFileSync(downloadedExecutable, 'utf8'), 'fake browser');
});
