// Native node:test observer, used alongside the existing reporters/guards.
// This reporter never changes selection, outcomes, or process.exitCode.
import { openSync, writeSync, closeSync } from 'node:fs';
import { join, relative, resolve, sep } from 'node:path';

export async function* observeNodeEvents(source) {
  const directory = process.env.CEILING_STEP_DIR;
  const active = directory && !process.env.CEILING_REPORTER_ACTIVE;
  if (active) process.env.CEILING_REPORTER_ACTIVE = String(process.pid);
  const fd = active ? openSync(join(directory, `node-${process.pid}-${Date.now()}.jsonl`), 'wx') : null;
  const write = (value) => { if (fd !== null) writeSync(fd, `${JSON.stringify(value)}\n`); };
  const key = (d) => JSON.stringify([d.file ? relative(process.cwd(), d.file).split(sep).join('/') : '', d.line ?? null, d.column ?? null, d.nesting ?? 0, d.name]);
  const collected = new Map();
  const results = new Map();
  let summary;
  write({ event: 'start', runner: 'node' });
  for await (const event of source) {
    yield event;
    const d = event.data ?? {};
    if (event.type === 'test:enqueue') collected.set(key(d), d);
    if (event.type === 'test:pass' || event.type === 'test:fail') {
      if (d.details?.type === 'suite') { collected.delete(key(d)); continue; }
      const native_id = key(d);
      const skipped = d.skip !== undefined || d.todo !== undefined;
      const error = d.details?.error;
      const diagnostic = skipped ? String(d.skip ?? d.todo ?? '') : error ? `${error.code ?? error.name ?? 'Error'}: ${error.message ?? String(error)}` : '';
      results.set(native_id, { event: 'result', native_id, parameter: null, stage: 'call', finished: true,
        outcome: skipped ? 'skipped' : event.type === 'test:fail' ? 'failed' : 'passed', diagnostic });
    }
    if (event.type === 'test:summary' && !d.file) summary = d;
  }
  // Node can enqueue a file container that is not itself an executable test.
  // It is excluded only when the stream explicitly identifies that container
  // in its summary; a queued ordinary case without a result remains pending.
  for (const [id, d] of collected) {
    if (d.type === 'suite' || (d.file && resolve(d.name) === resolve(d.file) && d.line === 1 && d.column === 1 && !results.has(id))) collected.delete(id);
  }
  write({ event: 'collection', tests: [...collected.keys()].map((native_id) => ({ native_id })) });
  for (const result of results.values()) write(result);
  const pending = [...collected.keys()].filter((id) => !results.has(id)).length;
  const failed = [...results.values()].some((row) => row.outcome === 'failed');
  if (summary) write({ event: 'finish', count: summary.counts.tests, pending, exitstatus: failed ? 1 : 0, native_summary: summary });
  if (fd !== null) closeSync(fd);
}

export default async function* ceilingNode(source) {
  for await (const event of observeNodeEvents(source)) { /* Native observer writes its own stream. */ }
}
