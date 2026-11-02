"use strict";

// Local venue driver for the pinned setup API. The stock install-mode helper
// initializes Shells before Bin has injected the Kernel into each bin module;
// Aria2.env() then dereferences an unset kernel. Keep the official preset,
// checks, installer modules, and retry behavior, but initialize Bin first.
const fs = require("fs");
const path = require("path");

async function main() {
  if (process.argv.length !== 3 || !path.isAbsolute(process.argv[2])) {
    throw new Error("absolute pinned pinokiod root required");
  }
  const root = fs.realpathSync(process.argv[2]);
  const mode = process.env.PINOKIO_SETUP_MODE;
  const home = process.env.PINOKIO_HOME;
  if (mode !== "javascript" || !home || !path.isAbsolute(home)) {
    throw new Error("exact local Pinokio setup environment required");
  }

  const Kernel = require(path.join(root, "kernel"));
  console.log(`[local-seed] mode=${mode} home=${home}`);
  const kernel = new Kernel({ store: {} });
  await kernel.init({});
  await kernel.bin.init();
  await kernel.shell.init();
  await kernel.bin.refreshInstalled();
  if (kernel.refresh_interval) clearInterval(kernel.refresh_interval);
  kernel.server_running = true;

  await kernel.bin.check({ bin: kernel.bin.preset(mode) });
  await kernel.bin.install({ mode }, (packet) => {
    if (packet && typeof packet.raw === "string") process.stdout.write(packet.raw);
  });
  if (kernel.refresh_interval) clearInterval(kernel.refresh_interval);
  console.log("[local-seed] completed");
}

main().then(() => process.exit(0)).catch((error) => {
  console.error("[local-seed] failed", error);
  process.exit(1);
});
