'use strict';
// Same websocket request path as PTerm, with a stable explicit request ID.
const fs = require('node:fs');
const path = require('node:path');
const { WebSocket } = require(path.join(process.env.PTERM_DIR, 'node_modules/unws'));
const out = process.env.M46_OUTPUT;
const uri = '~/api/astrid/pinokio/install.js';
const id = process.env.M46_REQUEST_ID;
const ws = new WebSocket(`ws://127.0.0.1:${process.env.M46_PORT}`, { headers: { 'x-pinokio-client': 'pterm' } });
let bytes = 0;
let sent = false;
function log(row) {
  const line = JSON.stringify(row) + '\n';
  if (bytes + Buffer.byteLength(line) > 1024 * 1024) return;
  fs.appendFileSync(path.join(out, 'packets.jsonl'), line, { mode: 0o600 });
  bytes += Buffer.byteLength(line);
}
const timer = setTimeout(() => { log({ diagnostic: 'timeout' }); ws.close(); process.exit(3); }, 720000);
ws.addEventListener('open', () => {
  const request = { uri, id, action: 'run', input: {}, source: 'pterm', client: { cols: 120, rows: 40, source: 'pterm' } };
  log({ diagnostic: 'send', request });
  ws.send(JSON.stringify(request)); sent = true;
});
ws.addEventListener('message', e => {
  let packet; try { packet = JSON.parse(String(e.data)); } catch { log({ diagnostic: 'invalid-packet' }); return; }
  log(packet);
  if (packet.type === 'disconnect') { clearTimeout(timer); ws.close(); process.exit(0); }
});
ws.addEventListener('error', () => { log({ diagnostic: 'socket-error', sent }); clearTimeout(timer); process.exit(2); });
ws.addEventListener('close', () => { log({ diagnostic: 'socket-close', sent }); clearTimeout(timer); process.exit(2); });
