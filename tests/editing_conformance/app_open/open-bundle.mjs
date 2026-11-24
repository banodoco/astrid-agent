// Phase 1 of the Agent -> App contract: parse the exact saved envelope.
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import net from 'node:net';
import tls from 'node:tls';
import http from 'node:http';
import https from 'node:https';
import http2 from 'node:http2';
import dns from 'node:dns';
import dgram from 'node:dgram';
import { syncBuiltinESMExports } from 'node:module';

// The Python socket guard cannot cover a subprocess. Refuse ordinary Node
// network APIs before importing the real parser; no package install or server.
function offline() {
  throw new Error('App parser qualification must not access the network');
}
net.connect = net.createConnection = net.Socket.prototype.connect = offline;
tls.connect = offline;
http.request = http.get = https.request = https.get = http2.connect = offline;
dgram.createSocket = offline;
for (const module of [dns, dns.promises]) {
  for (const name of Object.keys(module)) {
    if ((name.startsWith('resolve') || name === 'lookup' || name === 'lookupService' || name === 'reverse')
        && typeof module[name] === 'function') {
      module[name] = offline;
    }
  }
}
globalThis.fetch = offline;
syncBuiltinESMExports();

try {
  const root = process.env.ASTRID_APP_ROOT;
  const bundlePath = process.argv[2];
  if (!root || !bundlePath || process.argv.length !== 3) {
    throw new Error('ASTRID_APP_ROOT and exactly one bundle JSON path are required');
  }
  const source = pathToFileURL(resolve(root, 'src/tools/video-editor/data/typed/timelineBundle.ts')).href;
  const { parseTimelineBundle, TimelineBundleParseError } = await import(source);
  const bundle = JSON.parse(readFileSync(bundlePath, 'utf8'));
  try {
    parseTimelineBundle(bundle);
    console.log(JSON.stringify({ ok: true, name: '', message: '' }));
  } catch (error) {
    // Import, network, or programming errors are not parser rejections and
    // must never make a negative control look like it passed.
    if (!(error instanceof TimelineBundleParseError)) throw error;
    console.log(JSON.stringify({ ok: false, name: error.name, message: error.message }));
    process.exitCode = 1;
  }
} catch (error) {
  console.error(error);
  process.exitCode = 2;
}
