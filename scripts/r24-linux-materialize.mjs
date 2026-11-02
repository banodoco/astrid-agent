#!/usr/bin/env node
// Runner-local diagnostic only. This is not an M4 executor configuration.
import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { readFile, realpath, lstat, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { pathToFileURL } from 'node:url';
import { isDeepStrictEqual } from 'node:util';

const providerSha = '603298861a359a04642143aea11e60dbd030505e';
const tuple = Object.freeze({ app: '5c95abb00525e1a77d00c32042b6453d9f14f4e7', agent: 'e425903f7a9fa5b82973281d15054c0716b46e2d', runtime: '5453366458f7b4ee874c94d15ae7952e3e82d3c3', pinokio: '2f085c5fc0cde192ac4a022065883c85dcccd0de' });
const sourceHashes = Object.freeze({
  'pinokio/providers/linux-provider.mjs': '917037e87b0fdbba97113c5e3053b5e7f837b70f24b6faf73767b6e5ce49493a',
  'pinokio/providers/linux-action-settlement.mjs': '8fe167ca02a6d75dcf1f71b143d8546ec074129d062bb2a1e10f21450304f704',
  'pinokio/providers/linux-custody-observations.mjs': '218c4ae7a707a7e242e3b19a26e84dfa27ae60372ed8460f51535f39afc27437',
  'pinokio/providers/linux-preflight-fault.mjs': 'f5eb7ed1182c4fb57a521bcc36b9e69538f081770987f83708476300917a316b',
  'pinokio/providers/linux-native-evidence-harness.mjs': '811fa6cfb3f41f583118ca9f9f8be89be670956af2d729bfc4e0c7a1cf9ac163',
  'pinokio/providers/linux-observations.mjs': 'b107091be8e2766909e61223b9e962ae34ae6b797dd9a971c265d305702f0d2f',
});
const interfaceSha256 = '2f685c8aec21bef116f95645920717e4b12ef33c1ca820fe6185eb01ffea115d';
const awaits = [
  { id: 'R-21', reason: 'approved old-launcher stopped-path integration is unbound' },
  { id: 'R-36', receipt: 'trusted collector and verifier receipt', reason: 'trusted collector, approved profiles, immutable binding and verifier are unbound' },
];
const expectedPlan = {
  kind: 'pinokio-linux-m4-harness', interface: 'astrid-qualification/v1', interfaceSha256,
  harness: 'M4-4a', title: 'old-launcher first click, Stop/retry and Stop to Start',
  state: 'not-run', qualification: 'closed', await: awaits, externalAwait: awaits,
  producers: { settlement: 'diagnostic-only', preflightFault: 'diagnostic-only', custody: 'diagnostic-only' },
};
function keys(value, expected, label) {
  if (!value || typeof value !== 'object' || Array.isArray(value) || !isDeepStrictEqual(Object.keys(value).sort(), [...expected].sort())) throw new Error(`invalid ${label} schema`);
}
function git(dir, ...args) {
  return execFileSync('git', ['--no-replace-objects', '-C', dir, ...args], { encoding: 'utf8', env: { PATH: process.env.PATH, HOME: '/nonexistent', GIT_CONFIG_NOSYSTEM: '1', GIT_TERMINAL_PROMPT: '0', LC_ALL: 'C' }, stdio: ['ignore', 'pipe', 'pipe'] }).trim();
}
async function checkout(input, sha, label) {
  if (typeof input !== 'string' || !path.isAbsolute(input) || (await realpath(input)) !== input || !(await lstat(input)).isDirectory()) throw new Error(`${label} must be an absolute real checkout root`);
  if (git(input, 'rev-parse', '--show-toplevel') !== input || git(input, 'rev-parse', 'HEAD') !== sha || git(input, 'status', '--porcelain', '--untracked-files=all')) throw new Error(`${label} identity or cleanliness mismatch`);
  return input;
}
async function materialize(request) {
  if (process.platform !== 'linux') throw new Error('Linux runner required');
  keys(request, ['providerCheckout', 'checkouts', 'harness', 'outputDirectory'], 'request');
  keys(request.checkouts, Object.keys(tuple), 'checkouts');
  if (request.harness !== 'M4-4a') throw new Error('only M4-4a is selected');
  const provider = await checkout(request.providerCheckout, providerSha, 'provider');
  const checkouts = {};
  for (const name of Object.keys(tuple)) checkouts[name] = await checkout(request.checkouts[name], tuple[name], name);
  const roots = [provider, ...Object.values(checkouts)];
  if (new Set(roots).size !== roots.length || roots.some((root, i) => roots.some((other, j) => i !== j && root.startsWith(other + path.sep)))) throw new Error('checkouts must be distinct and non-nested');
  for (const [file, expected] of Object.entries(sourceHashes)) {
    const full = path.join(provider, file);
    if (await realpath(full) !== full || !(await lstat(full)).isFile() || createHash('sha256').update(await readFile(full)).digest('hex') !== expected) throw new Error(`provider source mismatch: ${file}`);
  }
  const output = request.outputDirectory;
  if (typeof output !== 'string' || !path.isAbsolute(output) || await realpath(output) !== output || !(await lstat(output)).isDirectory() || roots.some(root => output === root || output.startsWith(root + path.sep))) throw new Error('outputDirectory must be an existing real directory outside checkouts');
  const module = await import(pathToFileURL(path.join(provider, 'pinokio/providers/linux-provider.mjs')).href);
  if (module.INTERFACE_SHA256 !== interfaceSha256 || typeof module.createLinuxProviderSurface !== 'function') throw new Error('unexpected provider exports');
  const surface = module.createLinuxProviderSurface();
  if (surface.platform !== 'linux' || surface.interface !== 'astrid-qualification/v1' || surface.interfaceSha256 !== interfaceSha256 || surface.state !== 'unbound' || surface.qualification !== 'closed' || typeof surface.prepareM4Harness !== 'function') throw new Error('unexpected provider surface');
  const plan = surface.prepareM4Harness(request.harness);
  if (!isDeepStrictEqual(plan, expectedPlan)) throw new Error('unexpected provider plan schema or values');
  const result = { kind: 'r24-linux-runner-diagnostic-plan', version: 1, qualification: 'closed', state: 'not-run', executorConfig: false, provider: { repository: 'banodoco/astrid-pinokio', ref: providerSha, checkout: provider, sourceSha256: sourceHashes }, candidate: { tuple, checkouts }, plan };
  const resultPath = path.join(output, 'r24-linux-m4-4a-diagnostic-plan.json');
  await writeFile(resultPath, JSON.stringify(result, null, 2) + '\n', { flag: 'wx', mode: 0o600 });
  return resultPath;
}
try {
  if (process.argv.length !== 3) throw new Error('usage: node r24-linux-materialize.mjs /absolute/request.json');
  if (!path.isAbsolute(process.argv[2])) throw new Error('request path must be absolute');
  const resultPath = await materialize(JSON.parse(await readFile(process.argv[2], 'utf8')));
  process.stdout.write(`${resultPath}\n`);
} catch (error) {
  process.stderr.write(`r24-linux materialization failed closed: ${error.message}\n`);
  process.exitCode = 1;
}
