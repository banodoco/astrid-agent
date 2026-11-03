'use strict';
// Diagnostic only: the isolated daemon and its children can write these rows.
const fs = require('node:fs');
const cp = require('node:child_process');
const { syncBuiltinESMExports } = require('node:module');
const target = '/data/pinokio/api/astrid/pinokio/shell.mjs';
if (process.argv[1] === target) {
  const original = cp.spawn;
  cp.spawn = function (command, args, options) {
    const child = original.apply(this, arguments);
    const row = { schema: 'astrid.m46.child-diagnostic.v1', kind: 'spawn', pid: child.pid,
      parent_pid: process.pid, command, args, cwd: options?.cwd || null,
      uid: process.getuid(), gid: process.getgid(), groups: process.getgroups(),
      observed_at: new Date().toISOString() };
    function emit(value) {
      try {
        const file = '/capture/child.jsonl';
        if (fs.existsSync(file) && fs.statSync(file).size > 60000) return;
        fs.appendFileSync(file, JSON.stringify(value) + '\n', { mode: 0o600 });
      } catch { /* missing evidence is a refusal */ }
    }
    emit(row);
    child.once('close', (code, signal) => emit({ schema: row.schema, kind: 'close', pid: child.pid,
      parent_pid: process.pid, code, signal, observed_at: new Date().toISOString() }));
    return child;
  };
  syncBuiltinESMExports();
}
