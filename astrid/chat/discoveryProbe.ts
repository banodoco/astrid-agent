import { execFileSync, spawn, type ExecFileOptions } from 'node:child_process';
import { join } from 'node:path';
import { commandForPlatform } from './acpProcessHost.ts';

/** Resolve command shims the same way as ACP hosts before spawning a probe. */
export function discoveryProbeInvocation(command: string, args: string[], platform: NodeJS.Platform = process.platform) {
  return commandForPlatform(command, args, platform);
}

/** Discovery owns the whole probe tree, including CLIs launched by a wrapper. */
export function discoveryProbe(command: string, args: string[], options: ExecFileOptions & { input?: string } = {}): Promise<{ stdout: string; stderr: string }> {
  return new Promise((resolve, reject) => {
    const invocation = discoveryProbeInvocation(command, args);
    const child = spawn(invocation.command, invocation.args, {
      cwd: options.cwd, env: options.env, detached: process.platform !== 'win32',
      windowsHide: true, windowsVerbatimArguments: invocation.windowsVerbatimArguments,
      stdio: [options.input === undefined ? 'ignore' : 'pipe', 'pipe', 'pipe'],
    });
    const output: Buffer[][] = [[], []];
    const sizes = [0, 0];
    let failure: Error | undefined;
    let settled = false;
    let stopping = false;
    const stop = () => {
      if (stopping) return;
      stopping = true;
      if (child.pid) {
        if (process.platform === 'win32') {
          // Terminate descendants while the root PID still identifies its tree.
          const taskkill = join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'taskkill.exe');
          try { execFileSync(taskkill, ['/PID', String(child.pid), '/T', '/F'], { timeout: 1_000, windowsHide: true, stdio: 'ignore' }); } catch { /* Still terminate the root below. */ }
        } else {
          try { process.kill(-child.pid, 'SIGKILL'); } catch { /* The probe group has already exited. */ }
        }
      }
      try { child.kill('SIGKILL'); } catch { /* The root may already have exited. */ }
    };
    const finish = (code: number | null, signal: NodeJS.Signals | null = null) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      if (process.platform !== 'win32' && child.pid) {
        try { process.kill(-child.pid, 'SIGKILL'); } catch { /* The probe group has already exited. */ }
      }
      const result = { stdout: Buffer.concat(output[0]).toString('utf8'), stderr: Buffer.concat(output[1]).toString('utf8') };
      if (failure || code !== 0) reject(Object.assign(failure ?? new Error('Agent discovery probe failed.'), {
        ...result,
        code: failure && 'code' in failure ? Reflect.get(failure, 'code') : code,
        status: code,
        signal,
      }));
      else resolve(result);
    };
    const timer = setTimeout(() => {
      failure = Object.assign(new Error('Agent discovery probe timed out.'), { killed: true, timedOut: true });
      stop();
    }, options.timeout ?? 5_000);
    if (child.stdin) {
      child.stdin.on('error', () => { failure = new Error('Agent input could not be delivered.'); stop(); });
      child.stdin.end(options.input);
    }
    [child.stdout, child.stderr].forEach((stream, index) => stream.on('data', (chunk: Buffer) => {
      if (stopping) return;
      sizes[index] += chunk.length;
      if (sizes[index] > (options.maxBuffer ?? 1024 * 1024)) {
        failure = Object.assign(new Error('Agent discovery output exceeded its limit.'), { killed: true, outputLimit: true });
        stop();
      } else output[index].push(chunk);
    }));
    child.once('error', error => { failure = error; finish(null); });
    child.once('close', (code, signal) => finish(code, signal));
  });
}
