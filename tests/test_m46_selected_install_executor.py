"""Focused admission and evidence tests; fixtures are not native install proof."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from scripts import m46_selected_install_executor as subject


class SelectedInstallExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.agent = Path(subject.__file__).resolve().parent.parent
        self.pin = self.root / 'pinokio'
        (self.pin / 'pinokio').mkdir(parents=True)
        self.output = self.root / 'output'
        self.output.mkdir(mode=0o700)
        self.roots = {'agent': self.agent, 'pinokio': self.pin}
        self.request = {'schema': subject.REQUEST_SCHEMA, 'runner_head': 'a'*40,
                        'request_id': 'b'*32, 'action': 'install', 'mode': 'backend',
                        'checkouts': {key: '/x/' + key for key in (*subject.REFS, 'runner')},
                        'output_directory': str(self.output)}
        # The real collector starts with these roles, then rejects this dynamic helper.
        for action in ('start', 'stop', 'update'):
            (self.pin / 'pinokio' / (action + '.js')).write_text("require('./lib')\n")
        (self.pin / 'pinokio/lib.js').write_text("async function x() { return import('./dynamic.mjs'); }\n")

    def test_request_is_closed_and_exact(self):
        subject.validate_shape(self.request)
        for key, value in [('action', 'start'), ('mode', 'local'), ('runner_head', 'A'*40),
                           ('request_id', 'x'*32)]:
            old = self.request[key]; self.request[key] = value
            with self.assertRaises(subject.Refusal): subject.validate_shape(self.request)
            self.request[key] = old
        self.request['unknown'] = True
        with self.assertRaises(subject.Refusal): subject.validate_shape(self.request)
        with self.assertRaises(subject.Refusal):
            json.loads('{"action":"install","action":"start"}', object_pairs_hook=subject.pairs_no_duplicates)

    def test_real_collector_refusal_exit_and_raw_evidence_hashes(self):
        command, code, files = subject.capture_graph(self.roots, self.output)
        self.assertEqual(command[-1], str(self.pin))
        self.assertEqual(code, 3)
        self.assertIn(b'dynamic action/helper import cannot be bound', (self.output / 'graph.stderr').read_bytes())
        for name, metadata in files.items():
            raw = (self.output / name).read_bytes()
            self.assertEqual(metadata, {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)})

    def test_refusal_does_not_become_action_exit_or_observer_binding(self):
        with mock.patch.object(subject, 'validate_request', return_value=(self.roots, self.output, {})):
            self.assertEqual(subject.run(self.request), 2)
        result = json.loads((self.output / 'result.json').read_bytes())
        self.assertEqual(result['reason'], 'agent-action-graph-refused')
        self.assertEqual(result['graph_check']['exit_code'], 3)
        self.assertFalse(result['action_invoked'])
        self.assertIsNone(result['action_exit'])
        self.assertIsNone(result['readback'])
        self.assertEqual(result['settlement'], 'unknown')
        self.assertEqual(result['observer_binding'], 'not-produced')
        self.assertEqual(result['slot_receipt'], 'not-produced')

    def test_successful_noninstall_graph_is_still_blocked(self):
        (self.pin / 'pinokio/lib.js').write_text("module.exports = {}\n")
        with mock.patch.object(subject, 'validate_request', return_value=(self.roots, self.output, {})):
            self.assertEqual(subject.run(self.request), 2)
        result = json.loads((self.output / 'result.json').read_bytes())
        self.assertEqual(result['graph_check']['exit_code'], 0)
        self.assertEqual(result['reason'], 'install-not-covered-by-agent-action-graph')
        self.assertFalse(result['action_invoked'])

    def test_graph_error_or_timeout_never_dispatches(self):
        for code in (None, 1, -9):
            with self.subTest(code=code), mock.patch.object(subject, 'validate_request', return_value=(self.roots, self.output, {})), \
                 mock.patch.object(subject, 'capture_graph', return_value=(['python'], code, {})):
                self.assertEqual(subject.run(self.request), 2)
                result = json.loads((self.output / 'result.json').read_bytes())
                self.assertEqual(result['reason'], 'agent-action-graph-check-failed')
                self.assertFalse(result['action_invoked'])
                (self.output / 'result.json').unlink()

    def test_pin_tree_and_parent_are_checked_after_exact_checkout_refs(self):
        roots = {}
        for name in (*subject.REFS, 'runner'):
            path = self.root / name
            path.mkdir(exist_ok=True)
            roots[name] = path
        script = roots['runner'] / 'scripts/m46_selected_install_executor.py'
        script.parent.mkdir(); script.write_text('fixture')
        manifest = roots['app'] / 'public/install-manifest.json'
        manifest.parent.mkdir(); manifest.write_text(json.dumps({'agent': {'sha': subject.REFS['agent']}, 'runtime': {'sha': subject.REFS['runtime']}}))
        self.request['checkouts'] = {name: str(path) for name, path in roots.items()}
        hashes = {('app', 'public/install-manifest.json'): hashlib.sha256(manifest.read_bytes()).hexdigest()}
        def fake_git(root, *args):
            if args[0] == 'rev-parse': return '837855e0c3aee7148f76d43faa020a3d01bd0959'
            if root == roots['runner']: return self.request['runner_head'] + ' ' + subject.BASE
            return subject.REFS['pinokio'] + ' 969c2810412b7df34334cf10e589454c4a9c4f93'
        with mock.patch.object(subject, 'validate_checkout') as checkout, \
             mock.patch.object(subject, 'git', side_effect=fake_git), \
             mock.patch.object(subject, 'SOURCE_HASHES', hashes):
            subject.validate_request(self.request, script=script)
            checkout.assert_any_call(roots['pinokio'], 'f8605a3877ac76c0d7f65e4b013bc112b8c96992')
        with mock.patch.object(subject, 'validate_checkout'), \
             mock.patch.object(subject, 'git', return_value='wrong'), \
             mock.patch.object(subject, 'SOURCE_HASHES', hashes):
            with self.assertRaises(subject.Refusal): subject.validate_request(self.request, script=script)

    def test_closed_schemas_and_exact_tuple(self):
        for kind in ('request', 'result'):
            schema = json.loads((self.agent / 'scripts' / f'm46-selected-install-executor-{kind}.schema.json').read_bytes())
            self.assertFalse(schema['additionalProperties'])
            self.assertEqual(set(schema['properties']), set(schema['required']))
        schema = json.loads((self.agent / 'scripts/m46-selected-install-executor-result.schema.json').read_bytes())
        self.assertEqual({k: v['const'] for k, v in schema['properties']['tuple']['properties'].items()}, subject.REFS)
        self.assertEqual(schema['properties']['action']['const'], 'pinokio/install.js')
        self.assertEqual(schema['properties']['settlement']['const'], 'unknown')


if __name__ == '__main__':
    unittest.main()
