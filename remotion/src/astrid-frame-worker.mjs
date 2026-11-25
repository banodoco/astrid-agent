import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { spawnSync } from 'node:child_process';
import readline from 'node:readline';
import {createRequire, syncBuiltinESMExports} from 'node:module';
import { openBrowser, renderFrames, selectComposition } from '@remotion/renderer';
import { sharedBrowserExecutable } from '../shared-browser.mjs';

// Retain the owner's startup temp context before requests can replace TMPDIR.
// A warm Chrome profile must outlive each invocation's temporary media tree.
const ownerBrowserTempRoot = resolve(tmpdir());
let browser = null;
let bundleDir = null;
let projectDir = null;
let sessionFindings = [];
const childProcessApi = createRequire(import.meta.url)('node:child_process');
const unresolvedColdLaunches = [];
let sessionCleanupError = null;

async function openRetainedColdBrowser(options, diagnosticsEnabled = false) {
  if (unresolvedColdLaunches.length) throw new Error('Cold Chrome original graph custody is unresolved');
  // The pinned renderer ESM namespace reads the builtin spawn export. Keep
  // both builtin views synchronized and retain each original ChildProcess
  // until startup returns Browser custody.
  const originalSpawn = childProcessApi.spawn;
  const allocated = [];
  const observations = [];
  const detachObservers = [];
  const observe = (emitter, name, handler, once = false) => {
    if (!emitter) return;
    emitter[once ? 'once' : 'on'](name, handler);
    detachObservers.push(() => emitter.removeListener(name, handler));
  };
  const startedAt = process.hrtime.bigint();
  const elapsed = () => Number(process.hrtime.bigint() - startedAt) / 1e6;
  const requestTempEnvironment = ['TMPDIR', 'TMP', 'TEMP'].map(name => [name, process.env[name]]);
  childProcessApi.spawn = (...args) => {
    // This explicit per-request diagnostic changes only the retained Chrome
    // command. Default/off keeps the original argv and environment untouched.
    const chromeInvocation = Array.isArray(args[1]) && args[1].includes('about:blank') &&
      args[1].some(value => String(value).startsWith('--remote-debugging-port='));
    if (diagnosticsEnabled && chromeInvocation) {
      args = [...args];
      args[1] = [...args[1]];
      for (const flag of ['--enable-logging=stderr', '--v=1']) {
        if (!args[1].includes(flag)) args[1].push(flag);
      }
    }
    const child = originalSpawn(...args);
    allocated.push(child);
    // Observe the original handle without changing its streams, arguments,
    // signals, retry policy, or graph-custody decision. No environment values.
    const observation = {ownerPid: process.pid, ownerParentPid: process.ppid,
      childPid: Number.isSafeInteger(child.pid) ? child.pid : null,
      executable: String(child.spawnfile ?? args[0]).slice(0, 4096),
      argv: (child.spawnargs ?? [args[0], ...(args[1] ?? [])]).slice(0, 128)
        .map(value => String(value).slice(0, 4096)),
      events: [{event: 'returned-original-handle', elapsedMs: elapsed()}],
      stdout: {bytes: 0, tail: ''}, stderr: {bytes: 0, tail: ''}};
    if (diagnosticsEnabled && chromeInvocation) {
      // Names only: never retain provider credentials or other env values.
      observation.diagnostics = {environment_names: Object.keys(args[2]?.env ?? process.env).sort()};
    }
    observations.push(observation);
    const event = (name, extra = {}) => {
      observation.events.push({event: name, elapsedMs: elapsed(), ...extra});
      if (observation.events.length > 16) observation.events.shift();
    };
    for (const [name, stream] of [['stdout', child.stdout], ['stderr', child.stderr]]) {
      observe(stream, 'data', chunk => {
        const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
        const previous = observation[name];
        previous.bytes += bytes.length;
        previous.tail = Buffer.concat([Buffer.from(previous.tail), bytes])
          .subarray(-8192).toString('utf8');
      });
    }
    observe(child, 'spawn', () => event('spawn'), true);
    observe(child, 'error', error => event('error', {name: String(error.name).slice(0, 128),
      message: String(error.message).slice(0, 512)}), true);
    observe(child, 'exit', (code, signal) => event('exit', {code, signal}), true);
    observe(child, 'close', (code, signal) => event('close', {code, signal}), true);
    return child;
  };
  syncBuiltinESMExports();
  try {
    // The qualified renderer creates its Chrome profile with os.tmpdir().
    // Keep that profile under the retained owner's original temp context;
    // request-scoped TMPDIR resumes before render/media work.
    for (const [name] of requestTempEnvironment) process.env[name] = ownerBrowserTempRoot;
    return await openBrowser('chrome', options);
  } catch (error) {
    if (allocated.length) {
      unresolvedColdLaunches.push(...allocated);
      // Stop only through the retained original handle. A root close is not
      // full detached-graph absence proof, so no second native launch or
      // successful release is permitted by this adapter after that failure.
      // Read-only state of at most one original retained Chrome child before
      // the unchanged SIGTERM. No PID adoption or new signal authority.
      const diagnosticIndex = observations.findIndex(record => record.diagnostics);
      if (diagnosticIndex >= 0) {
        const child = allocated[diagnosticIndex];
        const diagnostic = observations[diagnosticIndex].diagnostics;
        if (Number.isSafeInteger(child.pid) && child.pid > 0 && child.exitCode === null && child.signalCode === null) {
          try {
            const sampled = spawnSync('/bin/ps', ['-p', String(child.pid), '-o', 'pid=,ppid=,stat=,%cpu=,time=,etime=,comm='],
              {encoding: 'utf8', timeout: 1000, maxBuffer: 8192, stdio: ['ignore', 'pipe', 'pipe']});
            diagnostic.child_ps_before_stop = {pid: child.pid, status: sampled.status,
              signal: sampled.signal, stdout: String(sampled.stdout ?? '').slice(-4096),
              stderr: String(sampled.stderr ?? '').slice(-1024),
              error: sampled.error ? String(sampled.error.name).slice(0, 128) : null,
              observationalOnly: true};
          } catch (samplingError) {
            diagnostic.child_ps_before_stop = {pid: child.pid, error: String(samplingError.name).slice(0, 128), observationalOnly: true};
          }
        }
      }
      await Promise.all(allocated.map(child => new Promise(resolve => {
        if (child.exitCode !== null || child.signalCode !== null) return resolve();
        const timer = setTimeout(resolve, 5000);
        child.once('close', () => {clearTimeout(timer); resolve();});
        try {child.kill('SIGTERM');} catch { /* custody remains unresolved */ }
      })));
      const failure = new Error(`Cold Chrome original graph cleanup is unverified; native retry refused: ${error instanceof Error ? error.message : String(error)}`, {cause: error});
      failure.coldLaunchObservations = observations.map((record, index) => ({...record,
        exitCode: allocated[index].exitCode, signalCode: allocated[index].signalCode,
        observationalOnly: true}));
      throw failure;
    }
    // With no allocated native child, the failure cannot leave a first
    // Chrome graph behind. Only this case may enter the bounded retry.
    throw error;
  } finally {
    for (const detach of detachObservers) detach();
    for (const [name, value] of requestTempEnvironment) {
      if (value === undefined) delete process.env[name];
      else process.env[name] = value;
    }
    childProcessApi.spawn = originalSpawn;
    syncBuiltinESMExports();
  }
}

function failureDetails(error, depth = 0) {
  const value = error instanceof Error ? error : new Error(String(error));
  const details = {name: value.name.slice(0, 128), message: value.message.slice(0, 2048),
    stack: String(value.stack ?? '').slice(0, 4096)};
  if (Array.isArray(value.coldLaunchObservations)) {
    details.cold_launches = value.coldLaunchObservations;
  }
  if (depth < 2 && value.cause !== undefined) details.cause = failureDetails(value.cause, depth + 1);
  return details;
}

function reply(payload) {
  process.stdout.write(`${JSON.stringify(payload)}\n`);
}

async function closeSession() {
  if (sessionCleanupError) throw sessionCleanupError;
  if (unresolvedColdLaunches.length) throw new Error('Cold Chrome original graph custody is unresolved; release cannot acknowledge cleanup');
  if (browser) {
    try {
      await browser.close({silent: true});
    } catch (error) {
      // Preserve the original Browser and sticky failure. A failed close
      // cannot authorize a second allocation or a successful release.
      sessionCleanupError = new Error(`Original Chrome cleanup is unresolved: ${error instanceof Error ? error.message : String(error)}`, {cause: error});
      throw sessionCleanupError;
    }
  }
  browser = null;
  if (bundleDir) rmSync(bundleDir, { recursive: true, force: true });
  bundleDir = null;
  projectDir = null;
}

async function startSession(request) {
  await closeSession();
  projectDir = resolve(request.projectDir);
  if (!existsSync(projectDir)) throw new Error(`Remotion project does not exist: ${projectDir}`);
  await bundleProject(request);
  sessionFindings = [];
  // Read the admitted request field directly, so a previous request or
  // inherited owner environment cannot silently enable this diagnostic.
  const diagnosticsEnabled = request.environment?.ASTRID_FRAME_WORKER_CHROME_DIAGNOSTICS === '1';
  const options = {browserExecutable: request.browserExecutable ?? sharedBrowserExecutable() ?? null,
    chromiumOptions: {headless: true}, logLevel: 'error'};
  try {
    browser = await openRetainedColdBrowser(options, diagnosticsEnabled);
  } catch (firstError) {
    if (unresolvedColdLaunches.length) throw firstError;
    const detail = firstError instanceof Error ? firstError.message : String(firstError);
    if (!/ECONNREFUSED|ECONNRESET|failed to connect|timed out.*connect|connection.*(?:closed|timeout)|WebSocket.*(?:closed|error)/i.test(detail)) throw firstError;
    // The pinned renderer bounds each cold connect at 25 seconds. Retry once;
    // keep both causes if the retry also fails, and never retry render errors.
    try {
      browser = await openRetainedColdBrowser(options, diagnosticsEnabled);
      sessionFindings.push({code: 'cold_chrome_connection_retried', attempts: 2,
        firstError: detail.slice(-512), scope: 'frame_owner_session', firstSpawnedChildren: 0});
    } catch (finalError) {
      throw new Error(`Cold Chrome retry failed: ${finalError instanceof Error ? finalError.message : String(finalError)}; first connection: ${detail.slice(-512)}`, {cause: finalError});
    }
  }
}

async function bundleProject(request) {
  if (bundleDir) rmSync(bundleDir, { recursive: true, force: true });
  const workerRoot = resolve(process.env.ASTRID_FRAME_WORKER_ROOT ?? tmpdir());
  // The owner is deliberately process-independent from GenericPackHost.  The
  // host may therefore clean up a root it created after launching us while we
  // are still retained for the next request; recreate the bounded root at the
  // point of use rather than assuming the launcher's directory still exists.
  mkdirSync(workerRoot, { recursive: true, mode: 0o700 });
  bundleDir = mkdtempSync(join(workerRoot, 'astrid-remotion-frame-bundle-'));
  const environment = { ...process.env, ...(request.environment ?? {}) };
  const bundled = spawnSync(
    request.nodeExecutable,
    [request.remotionCli, 'bundle', 'src/index.ts', '--out-dir', bundleDir, '--bundle-cache=false'],
    { cwd: projectDir, env: environment, encoding: 'utf8', stdio: ['ignore', 'ignore', 'pipe'] },
  );
  if (bundled.status !== 0) {
    rmSync(bundleDir, { recursive: true, force: true });
    bundleDir = null;
    const details = String(bundled.stderr ?? '').trim().slice(-4000);
    throw new Error(`Remotion frame worker bundle failed${details ? `: ${details}` : ''}`);
  }
}

// Opt-in (astridBounds): BoundsProbe logs one ASTRID_BOUNDS line per frame;
// collect them and write frame-<n>.bounds.json beside the PNGs. The marker
// and parse mirror remotion/src/bounds.ts (this file cannot import it).
function boundsCollector(props) {
  const enabled = props.astridBounds === true || props.timeline?.app?.astrid_bounds === true;
  const lines = [];
  const onBrowserLog = enabled
    ? (log) => {
        if (typeof log?.text === 'string' && log.text.startsWith('ASTRID_BOUNDS ')) lines.push(log.text);
      }
    : undefined;
  const write = (request) => {
    let files = 0;
    if (!enabled) return files;
    // Remotion pads [frame] to the digit count of the last requested frame
    // (get-frame-padded-index.js, actual-frames count type), so match that.
    const frames = request.frames.map(Number);
    const padLength = String(frames[frames.length - 1]).length;
    const outputDir = resolve(request.outputDir);
    for (const text of lines) {
      let parsed = null;
      try {
        parsed = JSON.parse(text.slice('ASTRID_BOUNDS '.length));
      } catch {
        parsed = null;
      }
      if (!parsed || typeof parsed.frame !== 'number' || !Array.isArray(parsed.clips)) continue;
      const name = `frame-${String(parsed.frame).padStart(padLength, '0')}.bounds.json`;
      writeFileSync(join(outputDir, name), JSON.stringify(parsed));
      files += 1;
    }
    return files;
  };
  return { onBrowserLog, write };
}

function framesOptions(request, props, composition, onBrowserLog = null, diagnostics = null) {
  return {
    serveUrl: bundleDir,
    // Keep the authored composition dimensions.  The Python adapter
    // downscales the completed PNGs after rendering; overriding these values
    // here would clip manual-layout clips against the thumbnail dimensions.
    composition,
    inputProps: props,
    port: Number(request.port),
    frames: request.frames.map(Number),
    outputDir: resolve(request.outputDir),
    imageFormat: 'png',
    imageSequencePattern: 'frame-[frame].[ext]',
    // A frame sequence has no audio output. Remotion's own muted mode skips
    // registering VO, music and video audio assets while keeping video pixels.
    muted: true,
    puppeteerInstance: browser,
    concurrency: 1,
    logLevel: 'error',
    ...(onBrowserLog ? { onBrowserLog } : {}),
    ...(diagnostics ? {
      onStart: () => { diagnostics.startedMs = Date.now() - diagnostics.epoch; },
      onFrameUpdate: (_count, frame, renderMs) => {
        diagnostics.frameUpdates.push({ frame, renderMs, elapsedMs: Date.now() - diagnostics.epoch });
      },
    } : {}),
  };
}

// Opt-in (request.diagnostics): per-frame browser probes for the frame benchmark.
function frameDiagnostics(request, props, bounds = {}) {
  if (request.diagnostics !== true) return { props, onBrowserLog: bounds.onBrowserLog ?? null, result: null };
  const result = { epoch: Date.now(), startedMs: null, frameUpdates: [], browser: [] };
  return {
    props: { ...props, astridFrameDiagnostics: true },
    onBrowserLog: (log) => {
      bounds.onBrowserLog?.(log);
      if (typeof log?.text === 'string' && log.text.startsWith('ASTRID_FRAME_DIAG ')) {
        if (result.browser.length < 120) result.browser.push({ elapsedMs: Date.now() - result.epoch, data: log.text.slice(18) });
      }
    },
    result,
  };
}

// Keep this worker self-contained: supported host fixtures may copy the
// worker without its optional sibling modules.
const applyFrameRequestEnvironment = (target, environment) => {
  delete target.ASTRID_RENDER_PACK_ROOTS;
  delete target.ASTRID_RENDER_REGISTRY_ROOT;
  delete target.ASTRID_PACKS_PATH;
  Object.assign(target, environment ?? {});
};

export const _test = { framesOptions, frameDiagnostics, boundsCollector, applyFrameRequestEnvironment };

async function renderRequest(request) {
  if (sessionCleanupError) throw sessionCleanupError;
  if (unresolvedColdLaunches.length) throw new Error('Cold Chrome original graph custody is unresolved; capture refused');
  // Apply this invocation's admitted environment before cold Chrome chooses
  // its profile/temp paths, and before either cold or warm bundle work.
  // The long-lived host-owned child must not use its inherited context here.
  applyFrameRequestEnvironment(process.env, request.environment);
  if (!browser || projectDir !== resolve(request.projectDir)) {
    await startSession(request);
  } else {
    // Effect assets are staged under an invocation-specific public prefix.
    // Rebundle the current public tree while retaining the Chromium browser;
    // otherwise a warm owner serves the first request's static assets only.
    await bundleProject(request);
  }
  const props = JSON.parse(readFileSync(request.propsPath, 'utf8'));
  const composition = await selectComposition({
    serveUrl: bundleDir,
    id: request.compositionId,
    inputProps: props,
    port: Number(request.port),
    puppeteerInstance: browser,
    logLevel: 'error',
  });
  const bounds = boundsCollector(props);
  const diagnostic = frameDiagnostics(request, props, bounds);
  await renderFrames(framesOptions(request, diagnostic.props, composition, diagnostic.onBrowserLog, diagnostic.result));
  const boundsFiles = bounds.write(request);
  return diagnostic.result || boundsFiles ? { ...(diagnostic.result ?? {}), ...(boundsFiles ? { boundsFiles } : {}) } : null;
}

async function handleRequest(request) {
  if (request.type === 'release') {
    await closeSession();
    return { ok: true, released: true, exit: Boolean(request.force) };
  }
  if (request.type === 'shutdown') {
    await closeSession();
    return { ok: true, shutdown: true, exit: true };
  }
  if (request.type !== 'render') throw new Error(`unknown request type: ${String(request.type)}`);
  const diagnostics = await renderRequest(request);
  return { ok: true, findings: sessionFindings, ...(diagnostics ? { diagnostics } : {}) };
}

// The pack host owns this child over stdio and decides when it idles out.
const input = readline.createInterface({ input: process.stdin, crlfDelay: Infinity });
for await (const line of input) {
  if (!line.trim()) continue;
  let request;
  try {
    request = JSON.parse(line);
    const response = await handleRequest(request);
    reply(response);
    if (response.exit) process.exit(0);
  } catch (error) {
    const primaryDetails = failureDetails(error);
    let cleanupError = null;
    try {await closeSession();} catch (failure) {cleanupError = failure;}
    reply({ok: false, cleanup_uncertain: Boolean(cleanupError),
      error: primaryDetails.message, error_details: primaryDetails,
      cleanup_error: cleanupError instanceof Error ? cleanupError.message : null,
      cleanup_error_details: cleanupError ? failureDetails(cleanupError) : null});
  }
}

await closeSession();
