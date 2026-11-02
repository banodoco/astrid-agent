"use strict";

// Local reproduction only. The pinned pinokiod entrypoint has no supported
// bind-host setting and calls Server.listen(42000, callback). Restrict exactly
// that first call to loopback without changing the pinned checkout or PTerm's
// authentic pinokio://127.0.0.1:42000 route.
const path = require("path");

const expectedEntry = process.env.ASTRID_LOCAL_PINOKIOD_ENTRY;
const selectedHost = process.env.ASTRID_LOCAL_PINOKIOD_BIND_HOST;
const actualEntry = process.argv[1];

if (
  selectedHost === "127.0.0.1" &&
  expectedEntry &&
  path.isAbsolute(expectedEntry) &&
  actualEntry &&
  path.resolve(actualEntry) === path.resolve(expectedEntry)
) {
  const net = require("net");
  const originalListen = net.Server.prototype.listen;

  net.Server.prototype.listen = function localLoopbackListen(port, callback) {
    if (port === 42000 && typeof callback === "function" && arguments.length === 2) {
      // One-shot: only pinokiod's control server is affected.
      net.Server.prototype.listen = originalListen;
      return originalListen.call(this, port, selectedHost, callback);
    }
    return originalListen.apply(this, arguments);
  };
}
