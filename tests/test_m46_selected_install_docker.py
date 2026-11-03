import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
spec = importlib.util.spec_from_file_location('m46_docker', ROOT / 'scripts/m46_selected_install_docker.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

class DockerExecutorTests(unittest.TestCase):
    def request(self):
        return {'schema':module.REQUEST,'runner_head':'a'*40,'request_id':'1'*32,'action':'install','mode':'backend',
                'checkouts':{},'engine_checkout':'/engine','pterm_checkout':'/pterm','output_directory':'/output'}

    def test_closed_request_refuses_extra_keys_before_checkout(self):
        req = self.request(); req['extra'] = True
        with self.assertRaises(module.Refusal): module.validate(req)

    def test_hosted_run_head_must_match(self):
        req = self.request()
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            roots = {k:base/k for k in ('runner','pinokio')}
            for p in roots.values(): p.mkdir()
            out = base/'out'; out.mkdir()
            extras = [base/'engine',base/'pterm']
            for p in extras: p.mkdir()
            with mock.patch.object(module.admission,'validate_request',return_value=(roots,out,{})), \
                 mock.patch.object(module,'absolute_existing_directory',side_effect=extras), \
                 mock.patch.object(module,'validate_checkout'), mock.patch.object(module,'CLOSURE',{}), \
                 mock.patch.dict(os.environ,{'GITHUB_ACTIONS':'true','GITHUB_SHA':'b'*40}):
                with self.assertRaisesRegex(module.Refusal,'checkout binding'): module.validate(req)

    def test_docker_failure_records_blocked_without_invocation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); output = root/'out'; output.mkdir()
            req = self.request(); req['output_directory'] = str(output)
            with mock.patch.object(module,'validate',return_value=({'runner':root},output,{},(root,root))), \
                 mock.patch.dict(os.environ,{'RUNNER_TEMP':temp}), \
                 mock.patch.object(module.subprocess,'run',side_effect=FileNotFoundError('docker missing')):
                self.assertEqual(module.run(req),2)
            result = json.loads((output/'result.json').read_text())
            self.assertEqual(result['reason'],'docker-info-failed')
            self.assertFalse(result['action_invoked'])
            self.assertIsNone(result['child_exit'])
            self.assertIsNone(result['readback'])
            self.assertEqual(result['settlement'],'unknown')
            self.assertIsNone(result['commands'][0]['exit_code'])

    def test_closure_includes_literal_dynamic_imports_and_shell_helper(self):
        self.assertTrue({'pinokio/session.mjs','pinokio/lifecycle.mjs','pinokio/update-stage.mjs','pinokio/shell.mjs'} <= module.CLOSURE.keys())

    def test_u2_source_capture_leaves_executor_output_empty(self):
        workflow = (ROOT / '.github/workflows/m46-selected-install-docker.yml').read_text()
        captures = [line.strip() for line in workflow.splitlines()
                    if 'rev-parse HEAD >' in line and 'u2-source-sha.txt' in line]
        self.assertEqual(len(captures), 1)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            out = root / 'm46-evidence'; out.mkdir(mode=0o700)
            verifier = root / 'u2-verifier'; verifier.mkdir()
            subprocess.run(['git', 'init', '-q', str(verifier)], check=True)
            subprocess.run(['git', '-C', str(verifier), '-c', 'user.name=Test', '-c',
                            'user.email=test@example.invalid', 'commit', '--allow-empty', '-qm', 'test'], check=True)
            result = subprocess.run(['bash', '-eu', '-c', captures[0]],
                                    env=dict(os.environ, RUNNER_TEMP=temp), capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            self.assertEqual(list(out.iterdir()), [])
            self.assertEqual((root / 'u2-source-sha.txt').read_text().strip(),
                             subprocess.check_output(['git', '-C', str(verifier), 'rev-parse', 'HEAD'], text=True).strip())

    @unittest.skipUnless(os.environ.get('M46_PINOKIOD_SOURCE'), 'pinned Pinokiod checkout required')
    def test_pinned_seed_waits_for_bound_runtime_and_keeps_output_empty(self):
        source = Path(os.environ['M46_PINOKIOD_SOURCE'])
        self.assertEqual(subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip(),
                         'add4a674ad1ba95bb62ec427fce519fe5bf0e9bf')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); context = root/'engine'; (context/'script').mkdir(parents=True)
            (context/'Dockerfile').write_bytes((source/'Dockerfile').read_bytes())
            (context/'script/install-mode.js').write_bytes((source/'script/install-mode.js').read_bytes())
            out = root/'m46-evidence'; out.mkdir()
            helper = ROOT/'.github/venue/m46-engine-overlay.cjs'
            result = subprocess.run(['node', str(helper), str(context)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(list(out.iterdir()), [])
            self.assertIn('node script/m46-install-mode.js', (context/'Dockerfile').read_text())
            seed = (context/'script/m46-install-mode.js').read_text()
            self.assertIn('await kernel.sysReady', seed)
            self.assertIn('await kernel.bin.check({ bin: kernel.bin.preset(mode) })', seed)
            self.assertIn('await kernel.bin.install({ mode }, () => {})', seed)
            # Exercise the actual pinned Bluefairy against the seed entry point.
            # Its kernel is bound only when simulated asynchronous startup completes.
            harness = root/'harness.cjs'
            harness.write_text(r"""
const Module = require('module');
const load = Module._load;
Module._load = function(request, parent, ...rest) {
  return request === 'semver' ? {} : load.call(this, request, parent, ...rest);
};
const Bluefairy = require(process.argv[3]);
let installed = false;
class Kernel {
  constructor() {
    this.blue = new Bluefairy();
    this.bin = {path: name => '/seed/bin/' + name, refreshInstalled: async () => {},
      preset: mode => mode, check: async ({bin}) => { if (bin !== 'prod_dev') throw Error('wrong preset'); },
      install: async ({mode}) => { if (mode !== 'prod_dev') throw Error('wrong mode'); installed = true; }};
    this.shell = {init: async () => this.blue.env()};
  }
  async init() {
    this.sysReady = new Promise(resolve => setTimeout(async () => {
      this.blue.kernel = this;
      if ((await this.shell.init()).BLUEFAIRY_HOME !== '/seed/bin/bluefairy') throw Error('wrong home');
      resolve();
    }, 30));
  }
}
Module._load = function(request, parent, ...rest) {
  return request === '../kernel' ? Kernel : request === 'semver' ? {} : load.call(this, request, parent, ...rest);
};
process.exit = code => {
  if (code !== 0 || !installed) { process.exitCode = 1; }
};
require(process.argv[2]);
""")
            original = subprocess.run(['node', str(harness), str(context/'script/install-mode.js'),
                                       str(source/'kernel/bin/bluefairy.js')], capture_output=True, text=True, timeout=10)
            self.assertNotEqual(original.returncode, 0)
            self.assertIn("reading 'bin'", original.stderr)
            run = subprocess.run(['node', str(harness), str(context/'script/m46-install-mode.js'),
                                  str(source/'kernel/bin/bluefairy.js')], capture_output=True, text=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn('[seed] completed', run.stdout)
            self.assertEqual(list(out.iterdir()), [])
            # Mismatched pinned input must fail closed.
            (context/'Dockerfile').write_text('unexpected')
            refused = subprocess.run(['node', str(helper), str(context)], capture_output=True, text=True)
            self.assertNotEqual(refused.returncode, 0)

    def test_r4_caller_selects_only_diagnostic_job(self):
        workflow = (ROOT/'.github/workflows/chat.yml').read_text()
        branch = "refs/heads/lane/M-VENUE/V-22-URI-TEST-R4"
        self.assertEqual(workflow.count("github.ref != '" + branch + "'"), 2)
        self.assertEqual(workflow.count("github.ref == '" + branch + "'"), 1)
        self.assertIn("workflow_dispatch", workflow)

    def test_dispatch_sends_exact_pinokio_local_install_uri(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            unws = root / 'node_modules/unws'
            unws.mkdir(parents=True)
            (unws / 'index.js').write_text('''
const fs = require('node:fs');
exports.WebSocket = class {
  constructor() { this.listeners = {}; setImmediate(() => this.listeners.open()); }
  addEventListener(name, fn) { this.listeners[name] = fn; }
  send(data) {
    fs.writeFileSync(process.env.M46_OUTPUT + '/sent.json', data);
    setImmediate(() => this.listeners.message({data: JSON.stringify({type: 'disconnect'})}));
  }
  close() {}
};
''')
            env = dict(os.environ, PTERM_DIR=temp, M46_OUTPUT=temp, M46_PORT='42000', M46_REQUEST_ID='1'*32)
            result = subprocess.run(['node', str(ROOT / '.github/venue/m46-dispatch.cjs')], env=env, capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr.decode())
            request = json.loads((root / 'sent.json').read_text())
            self.assertEqual(request['uri'], '~/api/astrid/pinokio/install.js')
            self.assertEqual(request['id'], '1'*32)
            self.assertEqual(request['action'], 'run')
            self.assertEqual(request['input'], {})
            packets = [json.loads(line) for line in (root / 'packets.jsonl').read_text().splitlines()]
            self.assertEqual(packets[0], {'diagnostic':'send', 'request':request})
            self.assertEqual(packets[1], {'type':'disconnect'})

if __name__ == '__main__': unittest.main()
