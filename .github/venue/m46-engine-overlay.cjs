'use strict';
// Prepare only the temporary, pinned Docker build context. The engine's
// Kernel.init starts bin/shell initialization asynchronously; wait for its
// own readiness before using those modules to perform the normal seed.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const root = process.argv[2];
const dockerfile = path.join(root, 'Dockerfile');
const seed = path.join(root, 'script/install-mode.js');
const sha = data => crypto.createHash('sha256').update(data).digest('hex');
const docker = fs.readFileSync(dockerfile, 'utf8');
const original = fs.readFileSync(seed, 'utf8');
const before = '  await kernel.shell.init()\n  await kernel.bin.init()\n';
const command = 'node script/install-mode.js';
if (sha(docker) !== '0e3f25686ce367846408bd2adb2bc28138e1456b0247c1f4986fd105f4de4ab3' ||
    sha(original) !== '9a13473ec9c69881b2978d258d17fce4843ba8b07ef8f4583ef2f146a0f8f03a' ||
    original.split(before).length !== 2 || docker.split(command).length !== 2) {
  throw Error('pinned Docker seed input mismatch');
}
const serialized = original.replace(before, '  await kernel.sysReady\n');
fs.writeFileSync(path.join(root, 'script/m46-install-mode.js'), serialized, { flag: 'wx' });
fs.writeFileSync(dockerfile, docker.replace(command, 'node script/m46-install-mode.js'));
console.log(JSON.stringify({dockerfile_source_sha256: sha(docker), seed_source_sha256: sha(original), seed_input_sha256: sha(serialized)}));
