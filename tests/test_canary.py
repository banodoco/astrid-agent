"""CPU-only: real harness composition + files/Git, fakes at child/transport edges."""
import copy
import importlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from installed_canary.child import audit_shapes
from installed_canary.common import GateError, inventory, pages, read, save, sha
from installed_canary.contract import ASSETS, BOUNDARIES, SOURCES, validate
from installed_canary.evidence import assert_ready, restart_snapshot
from installed_canary.journey import restart_and_validate, validate_child
from installed_canary.tool import child_env, execute, fingerprint, main

RUN = subprocess.run
TOOL_ROOT = Path(__file__).resolve().parents[1]


class Fixture:
    def __init__(self, root):
        self.root = root
        self.task, self.work = root / 'selected-task', root / 'selected-work'
        self.task.mkdir(); self.work.mkdir()
        self.sources = {}
        for name in SOURCES:
            source = root / name
            source.mkdir()
            files = {rel: '# selected frozen ' + rel + '\n' for rel in BOUNDARIES.get(name, ())}
            if name == 'agent':
                files['runtime.lock.json'] = json.dumps({'source_commit': self.sources['runtime']['sha'], 'files': {}})
                files['config/astrid-beta-capabilities.json'] = json.dumps({'schema_version': 1, 'other': 'preserved',
                    'capabilities': [{'id': 'h3_av.prepare', 'disposition': 'required', 'adapter_family': 'cpu',
                                      'resource_keys': ['cpu'], 'evidence_reason': 'fixture'}]})
            for rel, data in files.items():
                path = source / rel; path.parent.mkdir(parents=True, exist_ok=True); path.write_text(data)
            if not files:
                (source / 'selected.txt').write_text(name)
            self.git(source, 'init', '-q')
            self.git(source, 'add', '.')
            self.git(source, '-c', 'user.name=CPU fixture', '-c', 'user.email=cpu@example.invalid',
                     'commit', '-qm', 'fixture [skip ci]')
            self.sources[name] = {'root': str(source), 'sha': self.git(source, 'rev-parse', 'HEAD'),
                                  'tree': self.git(source, 'rev-parse', 'HEAD^{tree}')}
        self.sources['agent_base'] = copy.deepcopy(self.sources['agent'])
        self.version = self.work / 'install/program/versions/fake-installed'
        self.purelib = self.version / 'py/lib/python3.11/site-packages'
        self.purelib.mkdir(parents=True)
        (self.version / 'py/bin').mkdir()
        (self.version / 'py/bin/python').write_text('fake; must never execute\n')
        (self.work / 'install/program/current').write_text(self.version.name + '\n')
        for source, relatives in BOUNDARIES.items():
            for rel in relatives:
                target = self.purelib / rel; target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(Path(self.sources[source]['root']) / rel, target)
        save(self.version / 'manifest.json', {n: {'sha': s['sha']} for n,s in self.sources.items() if n != 'agent_base'})
        save(self.task / 'logs/installer-exit.json', {'exit': 0, 'marker': 'selected-only'})
        self.worker = self.version / 'worker-profile.json'
        save(self.worker, {'source_checkout': str(self.purelib), 'execution_mode': 'host_only_cpu'})
        installed_matrix = self.version / 'a/config/astrid-beta-capabilities.json'
        installed_matrix.parent.mkdir(parents=True)
        shutil.copy2(Path(self.sources['agent']['root']) / 'config/astrid-beta-capabilities.json', installed_matrix)
        pack_root = self.purelib / 'astrid/packs'; pack_root.mkdir()
        boot = self.work / 'install/data/runtime/astrid-host/boot-manifest.json'
        save(boot, {'fixture': 'product owns boot validation'})
        save(self.worker, {'source_checkout': str(self.purelib), 'execution_mode': 'host_only_cpu',
             'max_concurrency': 2, 'pack_root': str(pack_root), 'boot_manifest_path': str(boot),
             'boot_manifest_hash': 'sha256:' + sha(boot), 'environment': {}, 'capability_matrix': str(installed_matrix)})
        services = self.version / 'services.json'
        save(services, {'services': [{'name': 'pack-host', 'argv': ['python', '--verified-local-worker'],
                                    'ready': {'file': 'worker-astrid-ready.json'}}]})
        save(self.version / 'profile.json', {'worker_profile': str(self.worker), 'service_declaration': str(services),
             'source_checkout': str(self.purelib), 'runtime_environment': str(self.version / 'py')})
        save(self.work / 'install/program/install.json', {'data_root': str(self.work / 'install/data')})
        credential = self.work / 'install/data/runtime/credentials/owner.token'
        credential.parent.mkdir(parents=True); credential.write_text('fake; never read by test controller')
        save(self.work / 'install/data/runtime/discovery.json', {'active_realm': 'fake-realm',
             'endpoint': 'http://127.0.0.1:1', 'credential_file': str(credential)})
        uv = self.work / 'install/bin/uv'; uv.parent.mkdir(); uv.write_text('fake; must never execute')
        self.ready = self.work / 'install/data/runtime/worker-astrid-ready.json'
        save(self.ready, {'source_checkout': str(self.purelib),
                         'python_executable': str(self.version / 'py/bin/python'),
                         'ready_capabilities': ['installed_canary.parent', 'h3_av.prepare']})
        log = self.work / 'install/xdg-state/astrid-runtime/logs/service-pack-host.log'
        log.parent.mkdir(parents=True); log.write_text('selected complete log\n' * 3)
        row = {'id': 'installed_canary.parent', 'disposition': 'optional', 'evidence_reason': 'reviewed fixture',
               'adapter_family': 'cpu', 'resource_keys': ['cpu']}
        selected = read(Path(self.sources['agent']['root']) / 'config/astrid-beta-capabilities.json')
        selected['capabilities'].append(row)
        matrix_path = self.task / 'selected-capabilities.json'; save(matrix_path, selected)
        self.value = {'schema_version': 1, 'task_dir': str(self.task), 'work_dir': str(self.work),
            'sources': self.sources, 'manifest_sha256': sha(self.version / 'manifest.json'),
            'matrix': {'shipped_sha256': sha(Path(self.sources['agent']['root']) / 'config/astrid-beta-capabilities.json'),
                       'selected_path': str(matrix_path), 'selected_sha256': sha(matrix_path), 'added_row': row},
            'parent_inventory': inventory(ASSETS)}
        self.tuple = self.task / 'tuple.json'; self.freeze()

    @staticmethod
    def git(root, *args):
        p = RUN(['git', '-C', str(root), *args], check=True, capture_output=True, text=True,
                env={'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8', 'HOME': str(root),
                     'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': '/dev/null'})
        return p.stdout.strip()

    def freeze(self):
        save(self.tuple, self.value)

    def context(self):
        return validate(self.tuple, self.task, self.work)

    def commit_source(self, name):
        source = Path(self.sources[name]['root'])
        self.git(source, 'add', '.')
        self.git(source, '-c', 'user.name=CPU fixture', '-c', 'user.email=cpu@example.invalid',
                 'commit', '-qm', 'selected fixture [skip ci]')
        self.sources[name].update(sha=self.git(source, 'rev-parse', 'HEAD'),
                                  tree=self.git(source, 'rev-parse', 'HEAD^{tree}'))
        manifest = self.version / 'manifest.json'; value = read(manifest)
        value[name]['sha'] = self.sources[name]['sha']; save(manifest, value)
        self.value['manifest_sha256'] = sha(manifest)
        self.freeze()

    def caps(self, *, cursor=None, limit=100):
        if cursor is None:
            return [{'capability_id': 'h3_av.prepare', 'definition_digest': 'sha256:prepare', 'status': 'ready'}], 'p2'
        return [{'capability_id': 'installed_canary.parent', 'definition_digest': 'sha256:parent', 'status': 'ready'}], None


# Runtime precedes Agent to provide a non-recursive lock pin.
SOURCES = ('runtime', 'agent', 'vibecomfy', 'lifecycle', 'runpod')


class CanaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='canary-', dir=os.environ['TMPDIR'])
        self.addCleanup(self.tmp.cleanup)
        self.f = Fixture(Path(self.tmp.name))

    def reject(self, text):
        self.f.freeze()
        with self.assertRaisesRegex((GateError, OSError, ValueError, KeyError), text):
            self.f.context()
        self.assertFalse((self.f.task / 'logs/installed-canary').exists())
        self.assertFalse((self.f.work / 'install/canary-pack').exists())

    def test_valid_inventory_cli_routes_only_selected_tree(self):
        stale = self.f.root / 'prior-task'; (stale / 'logs').mkdir(parents=True)
        save(stale / 'logs/installer-exit.json', {'exit': 999})
        self.assertEqual(main(['inventory', '--tuple', str(self.f.tuple), '--task-dir', str(self.f.task),
                               '--work-dir', str(self.f.work)]), 0)
        record = read(self.f.task / 'logs/installed-canary/collection.json')
        self.assertEqual(record['installer']['marker'], 'selected-only')
        self.assertEqual(len(record['boundary']), 11)
        retained = self.f.task / 'logs/installed-canary/ordinary-service-logs/service-pack-host.log'
        self.assertEqual(retained.read_text(), 'selected complete log\n' * 3)
        self.assertFalse((stale / 'logs/installed-canary').exists())

    def test_missing_tuple_member(self):
        del self.f.value['sources']['runpod']; self.reject('full five-source')

    def test_short_commit(self):
        self.f.value['sources']['runtime']['sha'] = '123abc'; self.reject('full lowercase')

    def test_wrong_tree(self):
        self.f.value['sources']['runtime']['tree'] = 'a' * 40; self.reject('tree mismatch')

    def test_wrong_commit(self):
        self.f.value['sources']['runtime']['sha'] = self.f.sources['agent']['sha']; self.reject('command=')

    def test_tree_object_is_not_commit(self):
        self.f.value['sources']['runtime']['sha'] = self.f.sources['runtime']['tree']; self.reject('must be a commit')

    def test_dirty_selected_source(self):
        (Path(self.f.sources['agent']['root']) / 'untracked.py').write_text('stale')
        self.reject('dirty selected')

    def test_stale_head(self):
        root = Path(self.f.sources['runtime']['root']); (root / 'new.txt').write_text('new')
        self.f.git(root, 'add', '.')
        self.f.git(root, '-c', 'user.name=CPU', '-c', 'user.email=cpu@example.invalid', 'commit', '-qm', 'new [skip ci]')
        self.reject('stale HEAD')

    def test_mismatched_task(self):
        self.f.value['task_dir'] = str(self.f.root / 'prior'); self.reject('task/work mismatch')

    def test_mismatched_work(self):
        self.f.value['work_dir'] = str(self.f.root / 'prior'); self.reject('task/work mismatch')

    def test_missing_source_root(self):
        self.f.value['sources']['runtime']['root'] = str(self.f.root / 'absent'); self.reject('existing directory')

    def test_symlink_source_root(self):
        link = self.f.root / 'alias'; link.symlink_to(self.f.sources['runtime']['root'])
        self.f.value['sources']['runtime']['root'] = str(link); self.reject('alias refused')

    def test_parent_path_segment(self):
        self.f.value['sources']['runtime']['root'] += '/../runtime'; self.reject('canonical absolute')

    def test_manifest_hash_mismatch(self):
        self.f.value['manifest_sha256'] = '0' * 64; self.reject('manifest digest')

    def test_manifest_wrong_source_even_when_repinned(self):
        path = self.f.version / 'manifest.json'; value = read(path); value['runpod']['sha'] = 'a' * 40
        save(path, value); self.f.value['manifest_sha256'] = sha(path); self.reject('source tuple differs')

    def test_boundary_missing(self):
        (self.f.purelib / 'astrid/services.py').unlink(); self.reject('existing file')

    def test_boundary_changed(self):
        (self.f.purelib / 'astrid/services.py').write_text('# wrong')
        self.reject('installed bytes differ')

    def test_boundary_symlink(self):
        path = self.f.purelib / 'astrid/services.py'; path.unlink()
        path.symlink_to(Path(self.f.sources['agent']['root']) / 'astrid/services.py')
        self.reject('alias refused')

    def test_wrong_inventory_membership(self):
        self.f.value['parent_inventory']['unexpected.py'] = '0' * 64; self.reject('exact parent bytes')

    def test_reviewed_parent_golden_bytes(self):
        self.assertEqual(inventory(ASSETS), {
            'actions/parent.py': 'e03ad1a3f46f9ee4a9765b199d15d34581f92c1cbe1099f52331d98f8bb22acd',
            'pack.yaml': 'd163d0c20efc54dcfdc2f942e5cc4c53a0db42b0905e3bca2838dab5e0789ace'})

    def test_matrix_mutation_rejected_even_when_repinned(self):
        path = Path(self.f.value['matrix']['selected_path']); value = read(path)
        value['other'] = 'changed'; save(path, value)
        self.f.value['matrix']['selected_sha256'] = sha(path); self.reject('preserved exactly')

    def test_matrix_unsupported_parent(self):
        self.f.value['matrix']['added_row']['disposition'] = 'unsupported'; self.reject('optional CPU')

    def test_selected_matrix_must_be_task_owned(self):
        original = Path(self.f.value['matrix']['selected_path']); target = self.f.work / original.name
        shutil.copy2(original, target); self.f.value['matrix']['selected_path'] = str(target)
        self.reject('task-owned and pinned')

    def test_missing_installer_receipt(self):
        (self.f.task / 'logs/installer-exit.json').unlink(); self.reject('existing file')

    def test_missing_current(self):
        (self.f.work / 'install/program/current').unlink(); self.reject('existing file')

    def test_invalid_current_escape(self):
        (self.f.work / 'install/program/current').write_text('../other'); self.reject('version selector')

    def test_multiple_versions(self):
        (self.f.version.parent / 'stale').mkdir(); self.reject('exactly one')

    def test_existing_receipts_never_overwritten(self):
        path = self.f.task / 'logs/installed-canary'; path.mkdir(); (path / 'sentinel').write_text('keep')
        with self.assertRaisesRegex(GateError, 'exists'):
            self.f.context()
        self.assertEqual((path / 'sentinel').read_text(), 'keep')

    def test_failed_installer_captured_before_refusal_without_child(self):
        save(self.f.task / 'logs/installer-exit.json', {'exit': 7})
        ctx = self.f.context()
        with patch('installed_canary.tool.subprocess.run', side_effect=AssertionError('child invoked')):
            with self.assertRaisesRegex(GateError, 'installer did not succeed'):
                execute(ctx, 'qualify')
        self.assertEqual(read(ctx.evidence / 'collection.json')['installer']['exit'], 7)
        self.assertEqual(read(ctx.evidence / 'failure.json')['stage'], 'installer-receipt')

    def test_qualification_process_orchestration(self):
        ctx = self.f.context(); stages = []
        def process(argv, **kwargs):
            self.assertEqual(kwargs['cwd'], ctx.version)
            self.assertNotIn('PYTHONPATH', kwargs['env'])
            self.assertNotIn('ASTRID_WORKSPACE_CREDENTIAL', kwargs['env'])
            self.assertEqual(argv[:3], [str(ctx.python), '-I', '-B'])
            stage = 'cli' if '-m' in argv else argv[argv.index('--stage') + 1]
            stages.append(stage)
            self.assertTrue((ctx.evidence / 'collection.json').is_file())
            if stage == 'api':
                shapes = read(TOOL_ROOT / 'installed_canary/api_shapes.json')
                save(ctx.evidence / 'api.json', {'result': 'PASS', 'api_invocations': 0,
                     'checked': len(shapes), 'fingerprint': fingerprint(ctx)})
            if stage == 'identity':
                save(ctx.evidence / 'identity.json', {'result': 'PASS', 'fingerprint': fingerprint(ctx)})
            if stage == 'contracts':
                from installed_canary.contracts import CONTRACT_NAMES
                save(ctx.evidence / 'contracts.json', {**{n: {'result': 'PASS'} for n in CONTRACT_NAMES},
                                                       'fingerprint': fingerprint(ctx)})
            if stage == 'journey':
                save(ctx.evidence / 'parent-ready-receipt.json', {'result': 'PASS'})
                save(ctx.evidence / 'h3-terminal-pass.json', {'result': 'PASS'})
            return types.SimpleNamespace(returncode=0)
        with patch('installed_canary.tool.subprocess.run', side_effect=process):
            execute(ctx, 'qualify')
        self.assertEqual(stages, ['api', 'identity', 'cli', 'contracts', 'journey'])
        self.assertEqual(read(ctx.evidence / 'qualification.json')['result'], 'PASS')

    def test_child_failure_withholds_following_stages(self):
        ctx = self.f.context(); stages = []
        def process(argv, **kwargs):
            stages.append(argv[argv.index('--stage') + 1])
            return types.SimpleNamespace(returncode=8)
        with patch('installed_canary.tool.subprocess.run', side_effect=process):
            with self.assertRaisesRegex(GateError, 'command=.*exit=8'):
                execute(ctx, 'qualify')
        self.assertEqual(stages, ['api']); self.assertFalse((ctx.evidence / 'qualification.json').exists())
        self.assertEqual(read(ctx.evidence / 'failure.json')['stage'], 'api')

    def test_incomplete_api_receipt_blocks_next_stage(self):
        ctx = self.f.context()
        def process(argv, **kwargs):
            save(ctx.evidence / 'api.json', {'result': 'PASS', 'api_invocations': 0, 'checked': 0,
                                           'fingerprint': fingerprint(ctx)})
            return types.SimpleNamespace(returncode=0)
        with patch('installed_canary.tool.subprocess.run', side_effect=process) as edge:
            with self.assertRaisesRegex(GateError, 'incomplete or stale'):
                execute(ctx, 'qualify')
        self.assertEqual(edge.call_count, 1)

    def test_explicit_child_environment_ignores_ambient_selectors(self):
        ctx = self.f.context()
        with patch.dict(os.environ, {'PYTHONPATH': '/bad', 'ASTRID_WORKSPACE_ENDPOINT': 'bad', 'HOME': '/bad'}):
            env = child_env(ctx)
        self.assertEqual(env['HOME'], str(ctx.install / 'home'))
        self.assertEqual(env['TMPDIR'], str(ctx.install / 'tmp'))
        self.assertNotIn('PYTHONPATH', env); self.assertNotIn('ASTRID_WORKSPACE_ENDPOINT', env)

    def test_capture_before_verify_with_real_service_process_edge(self):
        ctx = self.f.context(); ctx.evidence.mkdir(parents=True)
        launcher = self.f.root / 'service-edge.py'
        launcher.write_text('import pathlib,sys\npathlib.Path(sys.argv[1]).write_text(sys.argv[2])\n')
        trace = self.f.root / 'edge.txt'
        def start():
            RUN([sys.executable, '-I', '-B', str(launcher), str(trace), 'started'], check=True, env=child_env(ctx))
        def verify():
            for name in ('canary-ready.json', 'canary-worker-profile.json', 'canary-capabilities.json', 'restart-snapshots.json'):
                self.assertTrue((ctx.evidence / name).is_file(), name)
            self.assertTrue((ctx.evidence / 'pre-assert-service-logs/service-pack-host.log').is_file())
            RUN([sys.executable, '-I', '-B', str(launcher), str(trace), 'verified'], check=True, env=child_env(ctx))
        restart_and_validate(ctx, self.f.worker, start=start, verify=verify,
                             stop=lambda: self.fail('unexpected stop'), list_capabilities=self.f.caps)
        self.assertEqual(trace.read_text(), 'verified')
        self.assertEqual(read(ctx.evidence / 'parent-ready-receipt.json')['result'], 'PASS')

    def test_start_failure_still_captures_and_stops(self):
        ctx = self.f.context(); ctx.evidence.mkdir(parents=True); stopped = []
        def fail():
            raise RuntimeError('service start failed')
        with self.assertRaisesRegex(RuntimeError, 'service start failed'):
            restart_and_validate(ctx, self.f.worker, start=fail, verify=lambda: self.fail('verify ran'),
                                 stop=lambda: stopped.append(True), list_capabilities=self.f.caps)
        self.assertTrue((ctx.evidence / 'restart-snapshots.json').is_file())
        self.assertTrue((ctx.evidence / 'canary-capabilities.json').is_file())
        self.assertEqual(stopped, [True])

    def test_missing_ready_and_transport_failure_still_capture_logs(self):
        ctx = self.f.context(); ctx.evidence.mkdir(parents=True); self.f.ready.unlink()
        def fail(**kwargs):
            raise OSError('transport down')
        captured = restart_snapshot(ctx, self.f.worker, fail)
        self.assertEqual(len(captured['errors']), 2)
        self.assertTrue(captured['service_logs'])
        with self.assertRaisesRegex(GateError, 'restart-capture'):
            assert_ready(ctx, captured)

    def test_verify_failure_keeps_snapshot(self):
        ctx = self.f.context(); ctx.evidence.mkdir(parents=True); stopped = []
        def fail():
            self.assertTrue((ctx.evidence / 'restart-snapshots.json').is_file())
            raise RuntimeError('verify failed')
        with self.assertRaisesRegex(RuntimeError, 'verify failed'):
            restart_and_validate(ctx, self.f.worker, start=lambda: None, verify=fail,
                                 stop=lambda: stopped.append(True), list_capabilities=self.f.caps)
        self.assertEqual(stopped, [True]); self.assertFalse((ctx.evidence / 'parent-ready-receipt.json').exists())

    def test_missing_parent_readiness_withholds_receipt(self):
        ctx = self.f.context(); ctx.evidence.mkdir(parents=True)
        value = read(self.f.ready); value['ready_capabilities'] = ['h3_av.prepare']; save(self.f.ready, value)
        snap = restart_snapshot(ctx, self.f.worker, self.f.caps)
        with self.assertRaisesRegex(GateError, 'installed_canary.parent absent'):
            assert_ready(ctx, snap)
        self.assertTrue((ctx.evidence / 'canary-ready.json').exists())
        self.assertFalse((ctx.evidence / 'parent-ready-receipt.json').exists())

    def test_pagination_empty_intermediate_page(self):
        seen = []
        def fetch(*, cursor, limit):
            seen.append(cursor)
            return {None: ([1], 'a'), 'a': ([], 'b'), 'b': ([2], None)}[cursor]
        self.assertEqual(pages(fetch), [1, 2]); self.assertEqual(seen, [None, 'a', 'b'])

    def test_pagination_cycle_refused(self):
        with self.assertRaisesRegex(GateError, 'repeated cursor'):
            pages(lambda **kwargs: ([], 'same'))

    def test_api_audit_binds_without_calls_and_refuses_bad_origin_shape(self):
        module_path = self.f.purelib / 'canary_cpu_api.py'
        module_path.write_text('def edge(value, *, selected):\n    raise AssertionError("API was invoked")\n')
        sys.path.insert(0, str(self.f.purelib)); self.addCleanup(sys.path.remove, str(self.f.purelib))
        self.addCleanup(sys.modules.pop, 'canary_cpu_api', None)
        shape = {'module': 'canary_cpu_api', 'attribute': 'edge', 'positional': 1, 'keywords': ['selected']}
        good = audit_shapes([shape], self.f.purelib)
        self.assertEqual(good[0]['result'], 'PASS')
        bad = audit_shapes([{**shape, 'keywords': ['unknown']}], self.f.purelib)
        self.assertEqual(bad[0]['result'], 'FAIL')
        outside = audit_shapes([shape], self.f.root / 'other-purelib')
        self.assertIn('outside installed purelib', outside[0]['error'])
        missing = audit_shapes([{**shape, 'attribute': 'absent'}], self.f.purelib)
        self.assertEqual(missing[0]['error_type'], 'AttributeError')

    def test_exact_api_inventory_has_unique_reviewed_shapes(self):
        rows = read(TOOL_ROOT / 'installed_canary/api_shapes.json')
        self.assertEqual(len(rows), 31)
        self.assertEqual(len({(r['module'], r['attribute']) for r in rows}), 31)
        self.assertTrue(any(r['attribute'] == 'Backend.verify' and r['positional'] == 4 for r in rows))

    def test_child_validator_uses_exact_project_pins_inputs_and_binding(self):
        binding = {'actual': {'kind': 'machine', 'id': 'test-machine'}, 'verification': {'verified': True}}
        child = {'task_id': 'child', 'attempt_id': 'attempt', 'capability_id': 'h3_av.prepare',
                 'capability_digest': 'prepare-pin', 'project_id': 'project', 'input_object_ids': ['object'],
                 'execution_binding': {'actual_target': binding['actual'], 'verification': {'verified': True}},
                 'spec': {'delegated_parent': {'parent_task_id': 'parent', 'project_id': 'project',
                          'stage': 'prepare', 'parent_placement': binding}}}
        kwargs = dict(parent_id='parent', project_id='project', caps={'h3_av.prepare': 'prepare-pin'},
                      binding=binding, objects={'request': 'object'})
        validate_child(child, **kwargs)
        for field, wrong in [('project_id', 'other'), ('capability_digest', 'stale'),
                              ('input_object_ids', ['other']), ('attempt_id', None)]:
            with self.subTest(field=field), self.assertRaises(GateError):
                validate_child({**child, field: wrong}, **kwargs)


    def test_profile_escape_refused_before_children_or_writes(self):
        profile = read(self.f.version / 'profile.json')
        profile['worker_profile'] = str(self.f.work / 'outside-worker.json')
        save(Path(profile['worker_profile']), read(self.f.worker))
        save(self.f.version / 'profile.json', profile)
        self.reject('selected profile paths/identity')

    def test_foreign_installer_data_root_refused(self):
        save(self.f.work / 'install/program/install.json', {'data_root': '/foreign/state'})
        self.reject('data root is not selected')

    def test_nonlocal_discovery_refused(self):
        path = self.f.work / 'install/data/runtime/discovery.json'; value = read(path)
        value['endpoint'] = 'https://external.invalid:443'; save(path, value)
        self.reject('local endpoint')

    def test_missing_active_realm_refused(self):
        path = self.f.work / 'install/data/runtime/discovery.json'; value = read(path)
        del value['active_realm']; save(path, value); self.reject('active realm required')

    def test_foreign_credential_path_refused_without_reading_token(self):
        path = self.f.work / 'install/data/runtime/discovery.json'; value = read(path)
        foreign = self.f.root / 'foreign.token'; foreign.write_text('unread private fixture')
        value['credential_file'] = str(foreign); save(path, value)
        self.reject('selected credentials')

    def test_unsafe_worker_environment_refused(self):
        worker = read(self.f.worker); worker['environment'] = {'PYTHONPATH': '/foreign'}
        save(self.f.worker, worker); self.reject('undeclared authority')

    def test_stale_pack_refused_before_stop(self):
        (self.f.work / 'install/canary-pack').mkdir(); self.f.freeze()
        with self.assertRaisesRegex(GateError, 'stale test pack'):
            self.f.context()
        self.assertFalse((self.f.task / 'logs/installed-canary').exists())

    def test_stale_contracts_refused_before_children(self):
        (self.f.work / 'contracts').mkdir(); self.reject('stale contracts')

    def test_installed_shipped_matrix_mismatch_refused(self):
        matrix = Path(read(self.f.worker)['capability_matrix']); matrix.write_text('{}')
        self.reject('installed shipped matrix differs')

    def test_incomplete_contract_receipt_refuses_qualification(self):
        ctx = self.f.context(); stages = []
        def process(argv, **kwargs):
            stage = 'cli' if '-m' in argv else argv[argv.index('--stage') + 1]; stages.append(stage)
            if stage == 'api':
                save(ctx.evidence / 'api.json', {'result': 'PASS', 'api_invocations': 0,
                     'checked': 31, 'fingerprint': fingerprint(ctx)})
            elif stage == 'identity':
                save(ctx.evidence / 'identity.json', {'result': 'PASS', 'fingerprint': fingerprint(ctx)})
            elif stage == 'contracts':
                save(ctx.evidence / 'contracts.json', {'fingerprint': fingerprint(ctx)})
            return types.SimpleNamespace(returncode=0)
        with patch('installed_canary.tool.subprocess.run', side_effect=process):
            with self.assertRaisesRegex(GateError, 'four composition contract receipts'):
                execute(ctx, 'qualify')
        self.assertEqual(stages, ['api', 'identity', 'cli', 'contracts'])
        self.assertFalse((ctx.evidence / 'qualification.json').exists())

    def test_cleanup_error_retains_primary_start_failure(self):
        ctx = self.f.context(); ctx.evidence.mkdir(parents=True)
        def fail_start():
            raise RuntimeError('primary start error')
        def fail_stop():
            raise OSError('cleanup stop error')
        with self.assertRaisesRegex(RuntimeError, 'primary start error'):
            restart_and_validate(ctx, self.f.worker, start=fail_start, verify=lambda: None,
                                 stop=fail_stop, list_capabilities=self.f.caps)
        self.assertIn('cleanup stop error', read(ctx.evidence / 'cleanup-error.json')['error'])
        self.assertTrue((ctx.evidence / 'restart-snapshots.json').exists())

    def test_partial_capability_pages_survive_transport_failure(self):
        ctx = self.f.context(); ctx.evidence.mkdir(parents=True)
        def fetch(*, cursor, limit):
            if cursor is None:
                return [{'capability_id': 'first-page-evidence'}], 'next'
            raise OSError('later page unavailable')
        result = restart_snapshot(ctx, self.f.worker, fetch)
        self.assertEqual(len(result['errors']), 1)
        retained = (ctx.evidence / 'canary-capability-pages.jsonl').read_text().splitlines()
        self.assertEqual(json.loads(retained[0])['rows'][0]['capability_id'], 'first-page-evidence')
        self.assertTrue(result['service_logs'])

    def test_final_service_capture_keeps_complete_logs(self):
        from installed_canary.evidence import final_snapshot
        ctx = self.f.context(); ctx.evidence.mkdir(parents=True)
        result = final_snapshot(ctx)
        self.assertFalse(result['errors']); self.assertTrue(result['service_logs'])
        self.assertEqual((ctx.evidence / 'final-service-logs/service-pack-host.log').read_text(),
                         'selected complete log\n' * 3)


    def test_valid_single_child_lock_alignment(self):
        agent = Path(self.f.sources['agent']['root'])
        path = agent / 'runtime.lock.json'; lock = read(path)
        lock['source_commit'] = '0' * 40; save(path, lock)
        self.f.commit_source('agent')
        self.f.sources['agent_base'] = copy.deepcopy(self.f.sources['agent'])
        lock['source_commit'] = self.f.sources['runtime']['sha']; save(path, lock)
        self.f.commit_source('agent')
        ctx = self.f.context()
        self.assertEqual(ctx.payload['sources']['agent_base']['sha'], self.f.git(agent, 'rev-parse', 'HEAD^'))

    def test_lock_alignment_cannot_change_product_file(self):
        agent = Path(self.f.sources['agent']['root'])
        (agent / 'product-change.py').write_text('# disallowed alignment')
        self.f.commit_source('agent'); self.reject('more than runtime.lock.json')

    def test_vendored_lock_bytes_and_hash_are_checked(self):
        runtime = Path(self.f.sources['runtime']['root']); agent = Path(self.f.sources['agent']['root'])
        (runtime / 'member.txt').write_text('shared contract bytes')
        self.f.commit_source('runtime')
        (agent / 'vendored.txt').write_text('shared contract bytes')
        path = agent / 'runtime.lock.json'
        save(path, {'source_commit': self.f.sources['runtime']['sha'], 'files': {
            'vendored.txt': {'from': 'member.txt', 'sha256': 'sha256:' + sha(runtime / 'member.txt')}}})
        self.f.commit_source('agent')
        self.f.sources['agent_base'] = copy.deepcopy(self.f.sources['agent']); self.f.freeze()
        self.assertTrue(self.f.context())
        lock = read(path); lock['files']['vendored.txt']['sha256'] = 'sha256:' + '0' * 64
        save(path, lock); self.f.commit_source('agent')
        self.f.sources['agent_base'] = copy.deepcopy(self.f.sources['agent']); self.reject('vendored Runtime bytes or pin')

    def test_tuple_change_after_api_blocks_subsequent_processes(self):
        ctx = self.f.context(); stages = []
        def process(argv, **kwargs):
            stages.append(argv[argv.index('--stage') + 1])
            save(ctx.evidence / 'api.json', {'result': 'PASS', 'api_invocations': 0,
                 'checked': 31, 'fingerprint': fingerprint(ctx)})
            self.f.value['matrix']['added_row']['evidence_reason'] += ' changed'
            self.f.freeze()
            return types.SimpleNamespace(returncode=0)
        with patch('installed_canary.tool.subprocess.run', side_effect=process):
            with self.assertRaisesRegex(GateError, 'driver/tuple changed'):
                execute(ctx, 'qualify')
        self.assertEqual(stages, ['api'])
        self.assertFalse((ctx.evidence / 'qualification.json').exists())


    def test_canary_log_alias_refused_before_child_writes(self):
        (self.f.work / 'install/canary.log').symlink_to(self.f.root / 'foreign.log')
        self.reject('alias refused')

    def test_malformed_declaration_reports_stage_path_and_underlying_error(self):
        path = self.f.version / 'services.json'; value = read(path)
        value['services'][0]['argv'] = []; save(path, value)
        self.reject('stage=inputs path=.*IndexError')

    def test_incomplete_cli_rejects_before_any_action(self):
        with self.assertRaises(SystemExit) as result:
            main(['inventory', '--tuple', str(self.f.tuple), '--task-dir', str(self.f.task)])
        self.assertEqual(result.exception.code, 2)
        self.assertFalse((self.f.task / 'logs/installed-canary').exists())
