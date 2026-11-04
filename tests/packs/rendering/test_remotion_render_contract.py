"""Contract checks for the Remotion render surface.

The Remotion typecheck requires `remotion/node_modules`, which is absent in CI
by default, so that check remains an opportunistic smoke. The text-card check
requires a provisioned, selected Remotion runtime and proves that the public
registry component renders visible markup rather than an empty frame.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import unittest
from pathlib import Path

import pytest

from astrid.core.rendering.remotion_runtime import resolve_remotion_project_dir

ROOT = Path(__file__).resolve().parents[3]
REMOTION = ROOT / "remotion"


class RemotionAugmentationImportTest(unittest.TestCase):
    def test_root_augmentation_import_resolves(self) -> None:
        # The tracked augmentation surface is `types.augmentations.d.ts` (.gitignore keeps every other
        # `remotion/src/types.augmentations.*` out of the tree).
        aug_path = REMOTION / "src" / "types.augmentations.d.ts"
        root_tsx = (REMOTION / "src" / "Root.tsx").read_text(encoding="utf-8")
        # Collect the symbols Root.tsx imports from ./types.augmentations.
        match = re.search(
            r"import type \{([^}]*)\} from './types\.augmentations'", root_tsx
        )
        self.assertIsNotNone(match, "Root.tsx must import types from ./types.augmentations")
        imported = {name.strip() for name in match.group(1).split(",") if name.strip()}

        aug_src = aug_path.read_text(encoding="utf-8")
        for name in imported:
            self.assertRegex(
                aug_src,
                rf"export (interface|type) {re.escape(name)}\b",
                f"types.augmentations.ts must export {name}",
            )


class TextCardVisibleMarkupTest(unittest.TestCase):
    def test_text_card_renders_visible_markup(self) -> None:
        import tempfile

        project_dir = resolve_remotion_project_dir()
        snapshot_package_path = REMOTION / "package.json"
        snapshot_lock_path = REMOTION / "package-lock.json"
        runtime_package_path = project_dir / "package.json"
        runtime_lock_path = project_dir / "package-lock.json"

        self.assertTrue(
            runtime_package_path.is_file(),
            f"selected Remotion runtime is missing package.json: {runtime_package_path}",
        )
        self.assertTrue(
            runtime_lock_path.is_file(),
            f"selected Remotion runtime is missing package-lock.json: {runtime_lock_path}",
        )
        snapshot_package = json.loads(snapshot_package_path.read_text(encoding="utf-8"))
        runtime_package = json.loads(runtime_package_path.read_text(encoding="utf-8"))
        snapshot_lock = json.loads(snapshot_lock_path.read_text(encoding="utf-8"))
        runtime_lock = json.loads(runtime_lock_path.read_text(encoding="utf-8"))

        # The configured runtime owns dependencies, but the wrapper and the
        # expected package contract must still come from this source snapshot.
        # Comparing the tracked documents prevents an unrelated installed graph
        # (for example App's node_modules) from satisfying this SSR test.
        self.assertEqual(
            runtime_package,
            snapshot_package,
            f"selected Remotion runtime {runtime_package_path} disagrees with snapshot package.json",
        )
        self.assertEqual(
            runtime_lock,
            snapshot_lock,
            f"selected Remotion runtime {runtime_lock_path} disagrees with snapshot package-lock.json",
        )

        node = os.environ.get("ASTRID_NODE_EXECUTABLE") or shutil.which("node")
        if not node:
            self.fail(
                "SSR contract requires the Node version pinned by remotion/package.json; "
                "set ASTRID_NODE_EXECUTABLE "
                "or install the pinned Node toolchain"
            )
        node_version = subprocess.run(
            [node, "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        self.assertEqual(
            node_version.returncode,
            0,
            msg=f"could not probe Node executable {node}: {node_version.stderr}",
        )
        self.assertEqual(
            node_version.stdout.strip(),
            "v"
            + snapshot_package["engines"]["node"].removeprefix("="),
            msg="SSR contract must resolve the Node version pinned by remotion/package.json",
        )

        node_modules = project_dir / "node_modules"
        if not node_modules.is_dir():
            self.fail(
                f"SSR contract dependencies are missing from selected runtime {node_modules}; "
                "provision the process-configured Remotion runtime with the Node version pinned by remotion/package.json"
            )

        node_script = r"""
import React from 'react';
import {renderToStaticMarkup} from 'react-dom/server';
import {EFFECT_REGISTRY} from '@banodoco/timeline-composition/effects.generated';
import {
  DEFAULT_THEME,
  ElementRegistryProvider,
  resolveElementRegistries,
  toRuntimeTheme,
} from '@banodoco/timeline-composition';
import {Internals} from 'remotion';

const registryTextCard = EFFECT_REGISTRY['text-card'];
const resolvedTextCard = resolveElementRegistries(DEFAULT_THEME).effects['text-card'];
if (resolvedTextCard !== registryTextCard) {
  throw new Error('the public text-card registry identity is inconsistent');
}
const PublicTextCard = resolvedTextCard;

const composition = {
  id: 'TextCardSSR',
  component: () => null,
  durationInFrames: 90,
  fps: 30,
  width: 1920,
  height: 1080,
  defaultProps: {},
};
const videoConfig = {
  width: 1920,
  height: 1080,
  fps: 30,
  durationInFrames: 90,
};
const manager = {
  compositions: [composition],
  folders: [],
  currentCompositionMetadata: videoConfig,
  canvasContent: {type: 'composition', compositionId: composition.id},
};
const sequence = {
  absoluteFrom: 0,
  cumulatedFrom: 0,
  cumulatedNegativeFrom: 0,
  relativeFrom: 0,
  parentFrom: 0,
  durationInFrames: 90,
  id: composition.id,
  width: composition.width,
  height: composition.height,
  premounting: false,
  postmounting: false,
  premountDisplay: null,
  postmountDisplay: null,
};
const timeline = {
  frame: {[composition.id]: 0},
  playing: false,
  imperativePlaying: {current: false},
  audioAndVideoTags: {current: []},
};
const runtimeTheme = toRuntimeTheme(DEFAULT_THEME);
const registries = resolveElementRegistries(runtimeTheme);
const tree = React.createElement(
  Internals.CanUseRemotionHooks.Provider,
  {value: true},
  React.createElement(
    Internals.CompositionManager.Provider,
    {value: manager},
    React.createElement(
      Internals.SequenceContext.Provider,
      {value: sequence},
      React.createElement(
        Internals.TimelineContext.Provider,
        {value: timeline},
        React.createElement(
          ElementRegistryProvider,
          {value: registries},
          React.createElement(PublicTextCard, {
            clip: {id: 'clip-1', from: 0, to: 90},
            params: {
              content: 'Visible SSR text-card contract',
              align: 'center',
              entrance: 'none',
              exit: 'none',
              typewriter: false,
            },
            theme: runtimeTheme,
            fps: 30,
          }),
        ),
      ),
    ),
  ),
);
process.stdout.write(renderToStaticMarkup(tree));
"""

        with tempfile.TemporaryDirectory(prefix="astrid-text-card-ssr-") as temp:
            stage = Path(temp)
            (stage / "node_modules").symlink_to(node_modules, target_is_directory=True)
            script = stage / "render.mjs"
            script.write_text(node_script, encoding="utf-8")
            result = subprocess.run(
                [node, str(script)],
                cwd=stage,
                capture_output=True,
                text=True,
                check=False,
                timeout=20,
            )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"public text-card SSR failed:\n{result.stdout}\n{result.stderr}",
        )
        html = result.stdout
        self.assertTrue(html.strip(), "text-card SSR must not return empty markup")
        self.assertIn("Visible SSR text-card contract", html)
        self.assertRegex(html, r"<div\b[^>]*style=\"[^\"]*position:absolute")
        self.assertRegex(html, r"<div\b[^>]*style=\"[^\"]*display:flex")
        self.assertNotIn("opacity:0", html)


@pytest.mark.remotion
class RemotionTypecheckSmokeTest(unittest.TestCase):
    def test_remotion_typecheck_when_dependencies_present(self) -> None:
        result = subprocess.run(
            ["npm", "run", "typecheck"],
            cwd=str(REMOTION),
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(
            result.returncode,
            0,
            msg=f"remotion typecheck failed:\n{result.stdout}\n{result.stderr}",
        )


if __name__ == "__main__":
    unittest.main()


@pytest.mark.node
def test_frame_worker_cold_connection_retry_is_bounded_and_reported(tmp_path):
    """Execute the real worker with a fake renderer; no browser or build."""
    import json
    import os
    import shutil
    node = shutil.which('node')
    worker = tmp_path / 'remotion' / 'src' / 'worker.mjs'
    worker.parent.mkdir(parents=True)
    worker.write_bytes((REMOTION / 'src' / 'astrid-frame-worker.mjs').read_bytes())
    # The worker imports ../shared-browser.mjs, so the sibling module must sit beside src/.
    (tmp_path / 'remotion' / 'shared-browser.mjs').write_bytes((REMOTION / 'shared-browser.mjs').read_bytes())
    module = tmp_path / 'node_modules' / '@remotion' / 'renderer'; module.mkdir(parents=True)
    (module / 'package.json').write_text(json.dumps({'type': 'module', 'exports': './index.mjs'}))
    (module / 'index.mjs').write_text('''
import {writeFileSync} from 'node:fs';
import {createRequire} from 'node:module';
const childProcess = createRequire(import.meta.url)('node:child_process');
let attempts = 0;
export async function openBrowser() {
  attempts++; writeFileSync(process.env.COUNTS, String(attempts));
  if (process.env.CASE === 'retry' && attempts === 1) throw new Error('ECONNREFUSED cold Chrome');
  if (process.env.CASE === 'timeout' && attempts === 1) throw new Error('Timed out after 25000 ms while trying to connect to the browser!');
  if (process.env.CASE === 'allocated') {
    childProcess.spawn(process.execPath, ['-e', 'setInterval(() => {}, 1000)']);
    throw new Error('ECONNREFUSED after original child allocation');
  }
  if (process.env.CASE === 'twice') throw new Error('WebSocket connection closed');
  if (process.env.CASE === 'unrelated') throw new Error('invalid executable configuration');
  return {close: async () => {}};
}
export async function selectComposition() {return {width:1920,height:1080,fps:30};}
export async function renderFrames() {if (process.env.CASE === 'render') throw new Error('render defect');}
''')
    cli = tmp_path / 'bundle.mjs'; cli.write_text('process.exit(0);')
    props = tmp_path / 'props.json'; props.write_text('{}')
    request = {'type': 'render', 'projectDir': str(tmp_path), 'nodeExecutable': node,
               'remotionCli': str(cli), 'propsPath': str(props), 'compositionId': 'Fake',
               'outputDir': str(tmp_path / 'out'), 'frames': [0], 'port': 3333}
    for case, expected_attempts in [('retry', 2), ('timeout', 2), ('twice', 2), ('unrelated', 1), ('render', 1)]:
        counts = tmp_path / (case + '.count')
        result = subprocess.run([node, str(worker)], input=json.dumps(request)+'\n'+json.dumps({'type':'shutdown'})+'\n',
                                capture_output=True, text=True, timeout=15,
                                env={**os.environ, 'CASE':case, 'COUNTS':str(counts), 'ASTRID_FRAME_WORKER_ROOT':str(tmp_path / 'scratch')})
        assert result.returncode == 0, result.stderr
        response = json.loads(result.stdout.splitlines()[0])
        assert int(counts.read_text()) == expected_attempts
        if case in {'retry', 'timeout'}:
            assert response['ok']
            assert response['findings'][0]['code'] == 'cold_chrome_connection_retried'
            assert response['findings'][0]['attempts'] == 2
        else:
            assert response['ok'] is False
            if case == 'twice': assert 'first connection' in response['error']
    counts = tmp_path / 'allocated.count'
    result = subprocess.run([node, str(worker)], input=json.dumps(request)+'\n'+json.dumps({'type':'shutdown'})+'\n',
                            capture_output=True, text=True, timeout=15,
                            env={**os.environ, 'CASE':'allocated', 'COUNTS':str(counts), 'ASTRID_FRAME_WORKER_ROOT':str(tmp_path / 'scratch')})
    response = json.loads(result.stdout.splitlines()[0])
    assert int(counts.read_text()) == 1
    assert response['ok'] is False and response['cleanup_uncertain'] is True
    assert 'native retry refused' in response['error']
    assert result.returncode != 0  # No successful graph-cleanup acknowledgement.



@pytest.mark.remotion
def test_streaming_gate_uses_only_explicit_admitted_memory(tmp_path):
    import json
    import shutil
    node = shutil.which('node')
    esbuild = REMOTION / 'node_modules' / 'esbuild' / 'lib' / 'main.js'
    script = '''
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
import assert from 'node:assert/strict';
const require = createRequire(import.meta.url);
const esbuild = require(process.argv[2]);
const js = esbuild.transformSync(readFileSync(process.argv[3], 'utf8'), {loader:'ts', format:'esm'}).code;
const module = await import('data:text/javascript;base64,' + Buffer.from(js).toString('base64'));
const args = {width:1920,height:1080,admittedMemoryBytes:0};
assert.equal(module.streamingDecision(args).hasEnoughMemory, false);
assert.equal(module.streamingDecision({...args,admittedMemoryBytes:1920*1080*1000+2*1024**3}).hasEnoughMemory, true);
assert.equal(module.streamingDecision({...args,admittedMemoryBytes:Infinity}).hasEnoughMemory, false);
const gate = {shouldUseParallelEncoding: () => {throw new Error('ambient memory consulted');}};
module.installFrameStreaming({rendererEntry:'/renderer/index.js',admittedMemoryBytes:0,load:()=>gate});
assert.equal(gate.shouldUseParallelEncoding({width:1920,height:1080}).hasEnoughMemory, false);
assert.throws(() => module.installFrameStreaming({rendererEntry:'/renderer/index.js',admittedMemoryBytes:0,load:()=>({})}), /no supported memory gate/);
'''
    path = tmp_path / 'gate-test.mjs'; path.write_text(script)
    result = subprocess.run([node, str(path), str(esbuild), str(REMOTION / 'stream-frames.ts')], capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr


def _node_executable() -> str:
    """ASTRID_NODE_EXECUTABLE if exported, else node on PATH (the remotion lane guarantees one)."""
    import os
    import shutil

    node = os.environ.get('ASTRID_NODE_EXECUTABLE') or shutil.which('node')
    assert node, 'node executable unavailable (set ASTRID_NODE_EXECUTABLE)'
    return node


# Needs remotion/node_modules (npm install in remotion/) and a node executable.
@pytest.mark.remotion
@pytest.mark.parametrize("close_fails", [False, True])
def test_frame_worker_failed_browser_close_preserves_original_session(tmp_path, close_fails):
    """Actual qualified ESM close body on a synthetic receiver, no native graph claim."""
    import json
    import os
    import shutil
    import pytest
    node = _node_executable()
    assert Path(node).is_file()
    renderer = REMOTION / 'node_modules/@remotion/renderer/dist/esm/index.mjs'
    assert renderer.is_file()
    worker = tmp_path / 'remotion' / 'src' / 'worker.mjs'
    worker.parent.mkdir(parents=True)
    worker.write_bytes((REMOTION / 'src' / 'astrid-frame-worker.mjs').read_bytes())
    # The worker imports ../shared-browser.mjs, so the sibling module must sit beside src/.
    (tmp_path / 'remotion' / 'shared-browser.mjs').write_bytes((REMOTION / 'shared-browser.mjs').read_bytes())
    module = tmp_path / 'node_modules/@remotion/renderer'; module.mkdir(parents=True)
    (module / 'package.json').write_text(json.dumps({'type':'module','exports':'./index.mjs'}))
    (module / 'index.mjs').write_text(r'''
import {writeFileSync, readFileSync, existsSync, mkdtempSync, rmSync} from 'node:fs';
import {tmpdir} from 'node:os';
import {join} from 'node:path';
import assert from 'node:assert/strict';
import {createRequire, syncBuiltinESMExports} from 'node:module';
import {EventEmitter} from 'node:events';
import {PassThrough} from 'node:stream';
const childProcessApi = createRequire(import.meta.url)('node:child_process');
const syntheticChild = new EventEmitter();
syntheticChild.pid = undefined; syntheticChild.exitCode = null; syntheticChild.signalCode = null;
syntheticChild.stdout = new PassThrough(); syntheticChild.stderr = new PassThrough();
syntheticChild.on('error', () => {});
syntheticChild.stderr.on('data', () => {});
const listenerBaseline = () => ({error: syntheticChild.listenerCount('error'),
  spawn: syntheticChild.listenerCount('spawn'), exit: syntheticChild.listenerCount('exit'),
  close: syntheticChild.listenerCount('close'), stdout: syntheticChild.stdout.listenerCount('data'),
  stderr: syntheticChild.stderr.listenerCount('data')});
const expectedListeners = listenerBaseline();
childProcessApi.spawn = () => syntheticChild;
syncBuiltinESMExports();
const source = readFileSync(process.env.REAL_RENDERER, 'utf8');
const body = source.match(/async close\(\{ silent \}\) \{([\s\S]*?)\n  \}\n  disconnect\(\)/);
assert.ok(body, 'qualified ESM Browser.close options API is absent');
const actualClose = new Function('return async function close({silent}) {' + body[1] + '\n};')();
let allocations = 0, nativeCloseCalls = 0, events = [];
let profilePath = null;
const requestTemps = [];
const save = () => writeFileSync(process.env.CLOSE_COUNTS, JSON.stringify({nativeCloseCalls, events, profilePath, requestTemps}));
export async function openBrowser() {
  childProcessApi.spawn('/synthetic-browser', []);
  writeFileSync(process.env.COUNTS, String(++allocations));
  profilePath = mkdtempSync(join(tmpdir(), 'synthetic-qualified-browser-profile-'));
  writeFileSync(join(profilePath, 'profile-marker'), 'retained'); save();
  return {close: actualClose, runner: {closeProcess: async () => {
    nativeCloseCalls++; save();
    if (process.env.CLOSE_FAILS === '1') throw new Error('retained browser close failed');
    rmSync(profilePath, {recursive: true, force: true});
  }}, pages: async () => [], disconnect: () => {}, emit: event => {events.push(event); save();}};
}
export async function selectComposition() {return {width:96,height:54,fps:30};}
export async function renderFrames() {
  assert.deepEqual(listenerBaseline(), expectedListeners, 'openBrowser diagnostic listeners leaked into warm render');
  syntheticChild.emit('error', new Error('late synthetic error reaches only original listener'));
  assert.ok(existsSync(join(profilePath, 'profile-marker')), 'warm profile disappeared with a request temp');
  requestTemps.push(process.env.TMPDIR);
  rmSync(process.env.TMPDIR, {recursive: true, force: true}); save();
}
''')
    cli = tmp_path / 'bundle.mjs'; cli.write_text('process.exit(0);')
    props = tmp_path / 'props.json'; props.write_text('{}')
    counts = tmp_path / 'count'
    render = {'type':'render','projectDir':str(tmp_path),'nodeExecutable':node,
              'remotionCli':str(cli),'propsPath':str(props),'compositionId':'Fake',
              'outputDir':str(tmp_path / 'out'),'frames':[0],'port':3333}
    owner_tmp = tmp_path / 'owner-temp'
    first_tmp = tmp_path / 'request-one-temp'
    second_tmp = tmp_path / 'request-two-temp'
    for directory in (owner_tmp, first_tmp, second_tmp):
        directory.mkdir()
    render['environment'] = {'TMPDIR': str(first_tmp)}
    second = {**render, 'environment': {'TMPDIR': str(second_tmp)}}
    requests = ([render, second, {'type':'release','force':True}, render, {'type':'shutdown'}]
                if close_fails else [render, second, {'type':'release','force':True}])
    result = subprocess.run([node,str(worker)], input=''.join(json.dumps(r)+'\n' for r in requests),
        capture_output=True,text=True,timeout=15,
        env={**os.environ,'COUNTS':str(counts),'CLOSE_COUNTS':str(tmp_path/'close-counts'),
             'REAL_RENDERER':str(renderer),'CLOSE_FAILS':'1' if close_fails else '0',
             'ASTRID_FRAME_WORKER_ROOT':str(tmp_path/'scratch'),'TMPDIR':str(owner_tmp)})
    (tmp_path/'node-result.json').write_text(json.dumps({'returncode':result.returncode,
        'stdout':result.stdout[:8192],'stderr':result.stderr[:8192]}))
    replies = [json.loads(line) for line in result.stdout.splitlines()]
    assert replies, result.stderr
    assert replies[0]['ok'] is True and replies[1]['ok'] is True, result.stderr
    assert len(replies) == len(requests)
    close_counts = json.loads((tmp_path/'close-counts').read_text())
    profile = Path(close_counts['profilePath'])
    assert profile.parent.resolve() == owner_tmp.resolve()
    assert close_counts['requestTemps'] == [str(first_tmp), str(second_tmp)]
    assert not first_tmp.exists() and not second_tmp.exists()
    if close_fails:
        assert profile.is_dir() and (profile / 'profile-marker').is_file()
        assert counts.read_text() == '1'
        assert all(r['ok'] is False and r['cleanup_uncertain'] is True for r in replies[2:])
        assert all('retained browser close failed' in r['cleanup_error'] for r in replies[2:])
        assert result.returncode != 0
        assert close_counts['nativeCloseCalls'] == 1 and close_counts['events'] == []
    else:
        assert counts.read_text() == '1' and result.returncode == 0
        assert all(r['ok'] is True for r in replies)
        assert replies[2]['released'] is True and replies[2]['exit'] is True
        assert not profile.exists()
        assert close_counts['nativeCloseCalls'] == 1 and close_counts['events'] == ['closed-silent']


import pytest


# Needs remotion/node_modules (npm install in remotion/) and a node executable.
@pytest.mark.remotion
@pytest.mark.parametrize('stream,budget', [('0', None), ('1', None), ('1', '0'), ('1', '4294967296'), ('1', '-1')])
def test_real_cli_config_loader_captures_root_and_streaming_gate(tmp_path, stream, budget):
    """Use the selected CLI's real prepare/eval path; no renderer workload."""
    import json
    import os
    node = _node_executable()
    assert Path(node).is_file()
    assert (REMOTION / 'node_modules/@remotion/cli/dist/load-config.js').is_file()
    script = r'''
import {createRequire} from 'node:module';
import fs from 'node:fs';
import path from 'node:path';
import assert from 'node:assert/strict';
const root = process.argv[2];
const require = createRequire(path.join(root, 'package.json'));
const cliEntry = require.resolve('@remotion/cli');
const cliRequire = createRequire(cliEntry);
const renderer = require.resolve('@remotion/renderer');
assert.equal(fs.realpathSync(renderer), fs.realpathSync(cliRequire.resolve('@remotion/renderer')));
const loader = require(path.join(path.dirname(cliEntry), 'load-config.js'));
const {ConfigInternals} = require('@remotion/cli/config');
const gate = require(path.join(path.dirname(renderer), 'prestitcher-memory-usage.js'));
const original = gate.shouldUseParallelEncoding;
const prepared = await loader.prepareConfigFile(root, 'remotion.config.ts', false);
if (process.env.ASTRID_REMOTION_STREAM_MEMORY_BUDGET_BYTES === '-1') {
  assert.throws(() => loader.executeConfigFile(prepared), /non-negative owner-admitted/);
  assert.equal(gate.shouldUseParallelEncoding, original);
} else {
  loader.executeConfigFile(prepared);
  process.chdir(process.argv[3]);
  const webpack = await ConfigInternals.getWebpackOverrideFn()({resolve:{alias:{keep:'kept'}}});
  const repo = path.dirname(root);
  assert.equal(webpack.resolve.alias.keep, 'kept');
  for (const kind of ['effects','animations','transitions']) {
    const alias = webpack.resolve.alias['@workspace-' + kind];
    assert.ok(alias && fs.existsSync(alias));
    assert.ok(alias.startsWith(repo + '/astrid/packs/rendering/'));
    assert.ok(!alias.includes('/@remotion/cli/dist/'));
    assert.equal(webpack.resolve.alias['@theme-' + kind], repo + '/_active_theme/' + kind);
  }
  assert.equal(webpack.resolve.modules.at(-1), root + '/node_modules');
  if (process.env.ASTRID_REMOTION_STREAM_FRAMES === '0') assert.equal(gate.shouldUseParallelEncoding, original);
  else {
    assert.notEqual(gate.shouldUseParallelEncoding, original);
    const answer = gate.shouldUseParallelEncoding({width:320,height:180});
    const budget = Number(process.env.ASTRID_REMOTION_STREAM_MEMORY_BUDGET_BYTES ?? 0);
    assert.equal(answer.freeMemory, budget);
    assert.equal(answer.hasEnoughMemory, budget >= 320*180*1000 + 2*1024**3);
  }
}
console.log(JSON.stringify({renderer:fs.realpathSync(renderer), cli:fs.realpathSync(cliEntry), ok:true}));
'''
    file = tmp_path / 'loader.mjs'; file.write_text(script)
    env = {k:v for k,v in os.environ.items() if k not in {'NODE_OPTIONS','NODE_PATH','ASTRID_PACKS_PATH','ASTRID_REMOTION_STREAM_MEMORY_BUDGET_BYTES','ASTRID_REMOTION_PCM_AAC_OUTPUT'}}
    env['ASTRID_REMOTION_STREAM_FRAMES'] = stream
    if budget is not None: env['ASTRID_REMOTION_STREAM_MEMORY_BUDGET_BYTES'] = budget
    result = subprocess.run([node, str(file), str(REMOTION), str(tmp_path)], env=env, capture_output=True, text=True, timeout=45)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['ok'] is True


# Needs remotion/node_modules (npm install in remotion/) and a node executable.
@pytest.mark.remotion
def test_real_cli_config_rejects_missing_selected_config(tmp_path):
    import os
    node = _node_executable()
    script = r'''
import {createRequire} from 'node:module';
import path from 'node:path';
import assert from 'node:assert/strict';
const require = createRequire(process.argv[2] + '/package.json');
const loader = require(path.join(path.dirname(require.resolve('@remotion/cli')), 'load-config.js'));
const prepared = await loader.prepareConfigFile(process.argv[2], 'remotion.config.ts', false);
assert.throws(() => loader.executeConfigFile({...prepared,remotionRoot:process.argv[3]}), /ENOENT|selected absolute/);
'''
    file = tmp_path / 'missing-config.mjs'; file.write_text(script)
    result = subprocess.run([node, str(file), str(REMOTION), str(tmp_path)], capture_output=True, text=True, timeout=45,
                            env={**os.environ, 'ASTRID_REMOTION_STREAM_FRAMES':'0'})
    assert result.returncode == 0, result.stderr


# Needs remotion/node_modules (npm install in remotion/) and a node executable.
@pytest.mark.remotion
def test_streaming_native_esm_install_requires_selected_absolute_root(tmp_path):
    import os
    node = _node_executable()
    script = r'''
import {createRequire} from 'node:module';
import fs from 'node:fs';
import assert from 'node:assert/strict';
const require = createRequire(process.argv[2] + '/package.json');
const js = require('esbuild').transformSync(fs.readFileSync(process.argv[2] + '/stream-frames.ts','utf8'),{loader:'ts',format:'esm'}).code;
const mod = await import('data:text/javascript;base64,' + Buffer.from(js).toString('base64'));
process.env.ASTRID_REMOTION_STREAM_FRAMES = '1';
assert.throws(() => mod.installAdmittedFrameStreaming({remotionRoot:undefined}), /absolute/);
assert.throws(() => mod.installAdmittedFrameStreaming({remotionRoot:'relative'}), /absolute/);
mod.installAdmittedFrameStreaming({remotionRoot:process.argv[2]});
const path = await import('node:path');
const gate = require(path.join(path.dirname(require.resolve('@remotion/renderer')),'prestitcher-memory-usage.js'));
assert.equal(gate.shouldUseParallelEncoding({width:320,height:180}).hasEnoughMemory,false);
'''
    file = tmp_path / 'native-stream.mjs'; file.write_text(script)
    env = {k:v for k,v in os.environ.items() if k not in {'ASTRID_REMOTION_STREAM_MEMORY_BUDGET_BYTES','NODE_OPTIONS','NODE_PATH'}}
    result = subprocess.run([node,str(file),str(REMOTION)],env=env,capture_output=True,text=True,timeout=45)
    assert result.returncode == 0, result.stderr


# Needs remotion/node_modules (npm install in remotion/) and a node executable.
@pytest.mark.remotion
def test_actual_cli_compiler_resolves_full_selected_import_closure(tmp_path):
    """Actual production webpack compilation, without emit/browser/render."""
    import os
    import json
    node = _node_executable()
    script = r'''
import {createRequire} from 'node:module';
import path from 'node:path';
import fs from 'node:fs';
import assert from 'node:assert/strict';
const root = process.argv[2];
const require = createRequire(root + '/package.json');
const cliEntry = require.resolve('@remotion/cli');
const loader = require(path.join(path.dirname(cliEntry),'load-config.js'));
loader.executeConfigFile(await loader.prepareConfigFile(root,'remotion.config.ts',false));
const {ConfigInternals} = require('@remotion/cli/config');
const {BundlerInternals,webpack} = require('@remotion/bundler');
const [hash,config] = await BundlerInternals.getConfig({entryPoint:root+'/src/index.ts',outDir:process.argv[3]+'/never-emitted',resolvedRemotionRoot:root,options:{enableCaching:false,webpackOverride:ConfigInternals.getWebpackOverrideFn(),bundlerOverride:ConfigInternals.getBundlerOverrideFn()}});
assert.equal(config.cache,false);
const compiler = webpack(config);
const resolver = compiler.resolverFactory.get('normal',{dependencyType:'esm'});
const resolution = [];
try {
  for (const importer of [root+'/src',path.dirname(root)+'/astrid/packs/rendering/rendering/elements/effects/text-card']) {
    for (const specifier of ['@remotion/media','react','remotion']) {
      const file = await new Promise((resolve,reject)=>resolver.resolve({},importer,specifier,{},(error,result)=>error?reject(error):resolve(result)));
      assert.ok(fs.realpathSync(file).startsWith(root+'/node_modules/'));
      resolution.push({importer,specifier,file:fs.realpathSync(file)});
    }
  }
  const compilation = await new Promise((resolve,reject)=>compiler.compile((error,result)=>error?reject(error):resolve(result)));
  const stats = compilation.getStats().toJson({all:false,errors:true,errorDetails:true,warnings:true,moduleTrace:true,modules:true,assets:true});
  fs.writeFileSync(process.argv[3]+'/compiler-full-errors.json',JSON.stringify({hash,resolve:compiler.options.resolve,resolution,stats},null,2));
  assert.equal(stats.errors.length,0,JSON.stringify(stats.errors));
  assert.ok(stats.modules.length>0);
  assert.ok(!fs.existsSync(process.argv[3]+'/never-emitted'));
} finally {
  await new Promise((resolve,reject)=>compiler.close(error=>error?reject(error):resolve()));
}
console.log(JSON.stringify({ok:true,resolution}));
'''
    file = tmp_path / 'real-compile.mjs'; file.write_text(script)
    env = {k:v for k,v in os.environ.items() if k not in {'NODE_PATH','NODE_OPTIONS','ASTRID_PACKS_PATH','ASTRID_REMOTION_PCM_AAC_OUTPUT'}}
    env['ASTRID_REMOTION_STREAM_FRAMES'] = '0'
    result = subprocess.run([node,str(file),str(REMOTION),str(tmp_path)],env=env,capture_output=True,text=True,timeout=110)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)['ok'] is True


# Needs remotion/node_modules (npm install in remotion/) and a node executable.
@pytest.mark.remotion
@pytest.mark.parametrize('failure_stage', ['allocated', 'before-allocation'])
@pytest.mark.parametrize('diagnostic_mode', ['unset', '0', '1'])
def test_frame_worker_real_esm_spawn_capture_preserves_cold_failure(
    tmp_path, failure_stage, diagnostic_mode,
):
    """Real pinned renderer ESM + actual worker; fake only OS child allocation.

    No Chrome, browser service, module replacement inside the qualified tree,
    or native signal is permitted. This proves the original ChildProcess is
    retained on allocation and that both builtin spawn views are restored.
    """
    import hashlib
    import json
    import os
    import shutil
    import sys

    # On Linux the pinned renderer (wrap-with-setpriv in 4.0.509) launches the
    # browser as `setpriv --pdeathsig SIGKILL <browser> ...` whenever setpriv is
    # on PATH. setpriv execs the browser, so it is one child process and one
    # spawn, not an extra helper; only the spawn identity differs by platform.
    # The fake therefore accepts exactly this one expected launch prefix and
    # rejects every other executable or wrapper.
    browser = '/usr/bin/true'
    setpriv = shutil.which('setpriv') if sys.platform == 'linux' else None
    launch_prefix = [setpriv, '--pdeathsig', 'SIGKILL', browser] if setpriv else [browser]

    node = _node_executable()
    renderer = REMOTION / 'node_modules/@remotion/renderer/dist/esm/index.mjs'
    assert Path(node).is_file() and renderer.is_file()
    source = renderer.read_text()
    assert re.search(r'import \* as childProcess from [\'\"]node:child_process[\'\"]', source)
    assert 'childProcess.spawn(' in source
    worker = tmp_path / 'remotion' / 'src' / 'worker.mjs'
    worker.parent.mkdir(parents=True)
    worker.write_bytes((REMOTION / 'src' / 'astrid-frame-worker.mjs').read_bytes())
    # The worker imports ../shared-browser.mjs, so the sibling module must sit beside src/.
    (tmp_path / 'remotion' / 'shared-browser.mjs').write_bytes((REMOTION / 'shared-browser.mjs').read_bytes())
    package = tmp_path / 'node_modules/@remotion/renderer'
    package.mkdir(parents=True)
    (package / 'package.json').write_text(json.dumps({'type': 'module', 'exports': './index.mjs'}))
    # Install a synthetic kernel boundary BEFORE importing the real ESM
    # renderer; its static namespace starts at this baseline. The actual
    # worker must synchronize its temporary hook into that existing namespace.
    (package / 'index.mjs').write_text(r"""
import {createRequire, syncBuiltinESMExports} from 'node:module';
import * as builtin from 'node:child_process';
import {EventEmitter} from 'node:events';
import {PassThrough} from 'node:stream';
import {writeFileSync} from 'node:fs';
import {pathToFileURL} from 'node:url';
const api = createRequire(import.meta.url)('node:child_process');
let calls = 0;
const fakeSignals = [];
const syntheticChildren = [];
const spawnEnvironments = [];
const spawnLaunches = [];
const expectedLaunch = JSON.parse(process.env.EXPECTED_LAUNCH_PREFIX);
const bundleEnvironments = [];
const psCalls = [];
const originalProcessKill = process.kill;
process.kill = () => {throw new Error('native process signaling is forbidden by this test');};
const fakeSpawn = (file, args) => {
  calls++;
  spawnEnvironments.push({tmpdir: process.env.TMPDIR, profile: args.find(arg => arg.startsWith('--user-data-dir=')), diagnosticFlags: args.filter(arg => arg === '--enable-logging=stderr' || arg === '--v=1')});
  const launch = [file, ...args.slice(0, expectedLaunch.length - 1)];
  spawnLaunches.push(launch);
  if (JSON.stringify(launch) !== JSON.stringify(expectedLaunch)) throw new Error('unexpected browser executable');
  if (process.env.FAILURE_STAGE === 'before-allocation') {
    throw new Error('ECONNREFUSED before synthetic allocation');
  }
  const child = new EventEmitter();
  child.pid = 424242; child.exitCode = null; child.signalCode = null;
  child.spawnfile = file; child.spawnargs = [file, ...args];
  child.stdin = new PassThrough(); child.stdout = new PassThrough(); child.stderr = new PassThrough();
  child.on('error', () => {});
  child.stderr.on('data', () => {});
  syntheticChildren.push(child);
  child.kill = signal => {
    fakeSignals.push(signal); child.signalCode = signal;
    child.emit('exit', null, signal);
    child.emit('close', null, signal); return true;
  };
  setImmediate(() => {
    child.emit('spawn');
    child.stdout.write('synthetic stdout only\n');
    child.stderr.write('x'.repeat(9000) + 'ECONNREFUSED synthetic cold connect\n');
    child.emit('error', new Error('ECONNREFUSED synthetic browser connection'));
  });
  return child;
};
api.spawn = fakeSpawn;
// Bundle work is outside this startup-boundary test; prevent even a fake CLI
// subprocess while still using the production worker's bundle/start sequence.
api.spawnSync = (_file, _args, options) => {
  if (_file === '/bin/ps') {
    psCalls.push({args:_args, timeout:options.timeout, maxBuffer:options.maxBuffer});
    return {status:0, signal:null, stdout:'424242 1 S 0.0 0:00.01 00:01 fake-chrome\n', stderr:''};
  }
  bundleEnvironments.push({ownerTmpdir: process.env.TMPDIR, childTmpdir: options.env.TMPDIR});
  return {status: 0, stdout: '', stderr: ''};
};
syncBuiltinESMExports();
const real = await import(pathToFileURL(process.env.REAL_RENDERER).href);
process.on('exit', () => {
  writeFileSync(process.env.SPAWN_SUMMARY, JSON.stringify({
    calls, fakeSignals, spawnEnvironments, spawnLaunches, bundleEnvironments, psCalls,
    listenerCounts: syntheticChildren.map(child => ({
      error: child.listenerCount('error'), spawn: child.listenerCount('spawn'),
      exit: child.listenerCount('exit'), close: child.listenerCount('close'),
      stdout: child.stdout.listenerCount('data'), stderr: child.stderr.listenerCount('data')})),
    cjsRestored: api.spawn === fakeSpawn,
    esmRestored: builtin.spawn === fakeSpawn,
    actualEsmRenderer: process.env.REAL_RENDERER,
    noNativeProcessSpawned: true,
  }));
  process.kill = originalProcessKill;
});
export const openBrowser = real.openBrowser;
export async function selectComposition() {throw new Error('startup must fail before composition');}
export async function renderFrames() {throw new Error('startup must fail before frames');}
""")
    props = tmp_path / 'props.json'
    props.write_text('{}')
    summary_path = tmp_path / 'spawn-summary.json'
    render = {
        'type': 'render', 'projectDir': str(tmp_path), 'nodeExecutable': node,
        'remotionCli': str(tmp_path / 'unused-cli.js'), 'propsPath': str(props),
        'compositionId': 'NeverRendered', 'outputDir': str(tmp_path / 'unused-output'),
        'frames': [0], 'port': 3333, 'browserExecutable': '/usr/bin/true',
    }
    inherited_tmp = tmp_path / 'inherited-tmp'
    first_tmp = tmp_path / 'request-one-tmp'
    second_tmp = tmp_path / 'request-two-tmp'
    for directory in (inherited_tmp, first_tmp, second_tmp):
        directory.mkdir()
    switch = {} if diagnostic_mode == 'unset' else {'ASTRID_FRAME_WORKER_CHROME_DIAGNOSTICS': diagnostic_mode}
    render['environment'] = {'TMPDIR': str(first_tmp), **switch}
    second = {**render, 'environment': {'TMPDIR': str(second_tmp), **switch}}
    requests = [render, second, {'type': 'release', 'force': True}]
    observed = subprocess.run(
        [node, str(worker)], input=''.join(json.dumps(row) + '\n' for row in requests),
        capture_output=True, text=True, timeout=20,
        env={**os.environ, 'REAL_RENDERER': str(renderer),
             'EXPECTED_LAUNCH_PREFIX': json.dumps(launch_prefix),
             'FAILURE_STAGE': failure_stage, 'SPAWN_SUMMARY': str(summary_path),
             'ASTRID_FRAME_WORKER_ROOT': str(tmp_path / 'scratch'), 'TMPDIR': str(inherited_tmp),
             'ASTRID_FRAME_WORKER_CHROME_DIAGNOSTICS': '1', 'SENTINEL_SECRET': 'DO_NOT_RETAIN_THIS_VALUE'},
    )
    (tmp_path / 'node-result.json').write_text(json.dumps({
        'returncode': observed.returncode, 'stdout': observed.stdout,
        'stderr': observed.stderr, 'renderer_sha256': hashlib.sha256(renderer.read_bytes()).hexdigest(),
    }, indent=2))
    replies = [json.loads(line) for line in observed.stdout.splitlines()]
    assert len(replies) == 3, observed.stderr
    summary = json.loads(summary_path.read_text())
    assert summary['cjsRestored'] and summary['esmRestored']
    assert summary['noNativeProcessSpawned'] is True
    assert summary['spawnLaunches'] == [launch_prefix] * summary['calls']
    expected_temps = [str(inherited_tmp)] * (1 if failure_stage == 'allocated' else 4)
    assert [row['tmpdir'] for row in summary['spawnEnvironments']] == expected_temps
    for actual, expected in zip(summary['spawnEnvironments'], expected_temps, strict=True):
        profile = actual['profile'].removeprefix('--user-data-dir=')
        assert Path(profile).parent.resolve() == Path(expected).resolve(), actual
    expected_bundles = ([str(first_tmp)] if failure_stage == 'allocated'
                        else [str(first_tmp), str(second_tmp)])
    assert summary['bundleEnvironments'] == [
        {'ownerTmpdir': directory, 'childTmpdir': directory}
        for directory in expected_bundles
    ]
    expected_flags = ['--enable-logging=stderr', '--v=1'] if diagnostic_mode == '1' else []
    assert all(row['diagnosticFlags'] == expected_flags for row in summary['spawnEnvironments'])
    assert 'DO_NOT_RETAIN_THIS_VALUE' not in observed.stdout
    assert replies[0]['ok'] is False
    primary = replies[0]['error_details']
    assert primary['name'] == 'Error' and primary['message'] == replies[0]['error']
    assert 'ECONNREFUSED' in primary['stack'] and primary['cause']['stack']
    if failure_stage == 'allocated':
        assert replies[0]['cleanup_error_details']['message'] == replies[0]['cleanup_error']
        assert replies[0]['cleanup_error_details']['stack']
    if failure_stage == 'allocated':
        launches = primary['cold_launches']
        assert len(launches) == 1
        launch = launches[0]
        assert launch['observationalOnly'] is True and launch['childPid'] == 424242
        if diagnostic_mode == '1':
            names = launch['diagnostics']['environment_names']
            assert names == sorted(names) and 'SENTINEL_SECRET' in names
            sample = launch['diagnostics']['child_ps_before_stop']
            assert sample['pid'] == 424242 and sample['status'] == 0 and sample['observationalOnly'] is True
            assert summary['psCalls'] == [{'args':['-p','424242','-o','pid=,ppid=,stat=,%cpu=,time=,etime=,comm='], 'timeout':1000, 'maxBuffer':8192}]
        else:
            assert 'diagnostics' not in launch and summary['psCalls'] == []
        assert launch['ownerPid'] > 0 and launch['ownerParentPid'] > 0
        assert launch['executable'] == launch_prefix[0]
        assert launch['argv'][:len(launch_prefix)] == launch_prefix
        assert any(arg.startswith('--user-data-dir=') for arg in launch['argv'])
        assert launch['stderr']['bytes'] > 9000
        assert len(launch['stderr']['tail'].encode()) <= 8192
        assert launch['stderr']['tail'].endswith('ECONNREFUSED synthetic cold connect\n')
        assert launch['stdout']['tail'] == 'synthetic stdout only\n'
        assert [event['event'] for event in launch['events']] == [
            'returned-original-handle', 'spawn', 'error', 'exit', 'close']
        assert [event['elapsedMs'] for event in launch['events']] == sorted(
            event['elapsedMs'] for event in launch['events'])
        assert launch['exitCode'] is None and launch['signalCode'] == 'SIGTERM'
        assert summary['listenerCounts'] == [
            {'error': 1, 'spawn': 0, 'exit': 0, 'close': 0, 'stdout': 0, 'stderr': 1}]
        assert summary['calls'] == 1, 'An allocated original child must prohibit native retry'
        assert summary['fakeSignals'] == ['SIGTERM']
        assert replies[0]['cleanup_uncertain'] is True
        assert 'native retry refused' in replies[0]['error']
        assert all(row['ok'] is False and row['cleanup_uncertain'] is True for row in replies[1:])
        assert observed.returncode != 0
    else:
        assert 'cold_launches' not in primary
        assert summary['calls'] == 4, 'Each independent no-child request retains exactly its bounded retry'
        assert replies[1]['ok'] is False and replies[1]['cleanup_uncertain'] is False
        assert summary['fakeSignals'] == []
        assert replies[0]['cleanup_uncertain'] is False
        assert replies[2]['ok'] is True and replies[2]['released'] is True and replies[2]['exit'] is True
        assert observed.returncode == 0


def test_frame_session_transports_explicit_chrome_diagnostic_environment(tmp_path, monkeypatch):
    """Existing actual Python request producer transports the switch unchanged."""
    from astrid.packs.rendering.rendering.renderers.remotion import run as backend
    session = backend.PersistentRemotionFrameSession()
    requests = []
    class HostLease:
        def request(self, identity, request, parser):
            requests.append(request)
            return {'ok': True, 'findings': []}
    monkeypatch.setattr('astrid.core.execution.remotion_host_session.host_remotion_client', lambda: HostLease())
    environment = {'TMPDIR':str(tmp_path), 'ASTRID_FRAME_WORKER_CHROME_DIAGNOSTICS':'1'}
    session.render(project_dir=tmp_path, composition_id='Fixture', node_executable=tmp_path/'node',
        remotion_cli=tmp_path/'cli.js', props_path=tmp_path/'props.json', output_dir=tmp_path/'frames',
        frames=[0], resolution=(320,180), port=3333, environment=environment, identity='fixture-identity')
    assert requests[0]['environment'] == environment
    assert requests[0]['environment'] is not environment
