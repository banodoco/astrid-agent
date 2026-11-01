"""Synthetic evaluator-path tests; these files are never qualification receipts."""
import hashlib
import importlib.util
import os
import json
from pathlib import Path
import sys
from unittest import mock, skipUnless

from test_local_regression_green import LocalEvidenceTests
import local_regression_green as local


class FeatureEvidenceTests(LocalEvidenceTests):
    def setUp(self):
        super().setUp()
        self.module = self.p / 'synthetic_feature_predicates.py'
        self.module.write_text('def check_demo(evidence):\n    f = evidence["files"][0]\n    return (f["path"] == "feature.json" and f["json"] == {"observation": "pass"}, [])\n')
        self.patch_path = mock.patch.object(sys, 'path', [str(self.p)] + sys.path)
        self.patch_path.start()
        self.addCleanup(self.patch_path.stop)
        self.patch_module = mock.patch.object(local, 'PREDICATE_MODULE', 'synthetic_feature_predicates')
        self.patch_module.start()
        self.addCleanup(self.patch_module.stop)
        self.addCleanup(lambda: sys.modules.pop('synthetic_feature_predicates', None))

    def configure(self, transport):
        lane = self.lanes[0 if transport == 'broker' else 1]
        source = lane['source']
        feature = {k: lane[k] for k in ('repo', 'lane', 'platform')}
        feature.update(scenario='demo', slot_id='selected-slot', candidate=dict(self.shas), files=['feature.json'], attempt=1)
        self.feature = feature
        if transport == 'broker':
            source['attempt'] = 1
            result_path = self.p / 'broker/jobs/job-1/result.json'
            result = json.loads(result_path.read_text())
            result['attempt'] = 1
            result_path.write_text(json.dumps(result))
            base = self.p / 'download'
        else:
            base = self.p / 'hosted/artifact-1'
        self.base = base
        self.transport = transport
        manifest = json.loads((base / 'manifest.json').read_text())
        manifest.update(attempt=1, feature_bindings=[{'scenario': 'demo', 'slot_id': 'selected-slot', 'attempt': 1}])
        (base / 'manifest.json').write_text(json.dumps(manifest))
        (base / 'feature.json').write_text('{"observation":"pass"}')
        self.save_inventory()
        self.authenticate()

    def authenticate(self):
        if self.transport == 'broker':
            path = self.p / 'broker/jobs/job-1/result.json'
            record = json.loads(path.read_text())
            record['downloads'] = [{'path': str(f), 'bytes': f.stat().st_size, 'sha256': local.sha(f)} for f in (self.base/'manifest.json', self.base/'junit.xml', self.base/'feature.json')]
        else:
            path = self.base / 'run.json'
            record = json.loads(path.read_text())
            record['files'] = [{'path': f.name, 'bytes': f.stat().st_size, 'sha256': local.sha(f)} for f in (self.base/'manifest.json', self.base/'junit.xml', self.base/'feature.json')]
        path.write_text(json.dumps(record))

    def feature_check(self, features=None):
        return local.local_evaluate(self.p/'tuple.json', self.p/'inventory.json', self.p/'coverage.json', self.p/'ledger.jsonl', self.p/'broker', self.p/'hosted', self.p/'freshness.json', '2026-10-31T23:00:00Z', now=self.now, features=[self.feature] if features is None else features)[0]

    def test_both_transports_import_predicate_after_authentication(self):
        for transport in ('broker', 'hosted'):
            with self.subTest(transport=transport):
                self.configure(transport)
                self.assertEqual(self.feature_check(), [])

    def test_missing_forged_and_duplicate_evidence(self):
        for transport in ('broker', 'hosted'):
            with self.subTest(transport=transport):
                self.configure(transport)
                path = self.base/'feature.json'
                original = path.read_bytes()
                path.unlink()
                self.assertTrue(self.feature_check())
                path.write_bytes(original + b' ')
                self.assertTrue(self.feature_check())
                path.write_bytes(original)
                self.feature['files'] = ['feature.json', 'feature.json']
                self.assertTrue(self.feature_check())
                self.feature['files'] = ['feature.json']
                record_path = self.p/'broker/jobs/job-1/result.json' if transport == 'broker' else self.base/'run.json'
                record = json.loads(record_path.read_text())
                entries = record['downloads' if transport == 'broker' else 'files']
                entries.append(entries[-1])
                record_path.write_text(json.dumps(record))
                self.assertTrue(self.feature_check())
                self.authenticate()
                self.assertEqual(self.feature_check(), [])

    def test_wrong_tuple_attempt_skipped_predicate_and_strict_json(self):
        self.configure('hosted')
        self.feature['candidate']['agent'] = 'f' * 40
        self.assertTrue(self.feature_check())
        self.feature['candidate']['agent'] = self.shas['agent']
        self.feature['attempt'] = 2
        self.assertTrue(self.feature_check())
        self.feature['attempt'] = 1
        self.feature['scenario'] = 'absent'
        manifest_path = self.base/'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['feature_bindings'][0]['scenario'] = 'absent'
        manifest_path.write_text(json.dumps(manifest))
        self.authenticate()
        self.assertTrue(any('missing predicate' in e for e in self.feature_check()))
        self.feature['scenario'] = 'demo'
        manifest['feature_bindings'][0]['scenario'] = 'demo'
        manifest_path.write_text(json.dumps(manifest))
        (self.base/'feature.json').write_text('{"observation":"pass","observation":"pass"}')
        self.authenticate()
        self.assertTrue(any('duplicate JSON key' in e for e in self.feature_check()))
        (self.base/'feature.json').write_text('{"observation":"pass"}')
        (self.base/'junit.xml').write_bytes(b'<testsuite><testcase><skipped/></testcase></testsuite>')
        self.authenticate()
        self.assertTrue(self.feature_check())

    def test_predicate_does_not_run_before_authentication_or_ledger(self):
        self.configure('hosted')
        (self.base/'feature.json').write_text('{"observation":"forged"}')
        with mock.patch.object(local.importlib, 'import_module') as imported:
            self.assertTrue(self.feature_check())
            imported.assert_not_called()
        (self.base/'feature.json').write_text('{"observation":"pass"}')
        self.authenticate()
        (self.p/'freshness.json').unlink()
        with mock.patch.object(local.importlib, 'import_module') as imported:
            self.assertTrue(self.feature_check())
            imported.assert_not_called()

    def test_failed_predicate_and_invalid_return_are_red(self):
        self.configure('hosted')
        with mock.patch.object(local.importlib, 'import_module') as imported:
            imported.return_value.check_demo.return_value = (False, ['skipped'])
            self.assertTrue(self.feature_check())
            imported.return_value.check_demo.return_value = (True, ['contradiction'])
            self.assertTrue(self.feature_check())
            imported.return_value.check_demo.return_value = (True, 'not a reasons list')
            self.assertTrue(self.feature_check())

    def test_duplicate_obligations_and_missing_module_fail(self):
        self.configure('broker')
        self.assertTrue(self.feature_check([self.feature, self.feature]))
        with mock.patch.object(local, 'PREDICATE_MODULE', 'module_that_does_not_exist'):
            self.assertTrue(self.feature_check())

    def test_predicate_receives_authenticated_snapshot_and_hash(self):
        for transport in ('broker', 'hosted'):
            with self.subTest(transport=transport):
                self.configure(transport)
                with mock.patch.object(local.importlib, 'import_module') as imported:
                    imported.return_value.check_demo.return_value = (True, [])
                    self.assertEqual(self.feature_check(), [])
                    imported.return_value.check_demo.assert_called_once_with({
                        'slot_id': 'selected-slot', 'candidate': self.shas,
                        'files': [{'path': 'feature.json',
                                   'sha256': hashlib.sha256(b'{"observation":"pass"}').hexdigest(),
                                   'json': {'observation': 'pass'}}],
                    })

    def test_predicate_exception_is_a_red_result(self):
        self.configure('hosted')
        with mock.patch.object(local.importlib, 'import_module') as imported:
            imported.return_value.check_demo.side_effect = RuntimeError('broken predicate')
            errors = self.feature_check()
        self.assertTrue(any('feature demo/selected-slot: RuntimeError: broken predicate' in e for e in errors), errors)

    def test_predicate_import_exception_is_a_red_result(self):
        self.configure('broker')
        with mock.patch.object(local.importlib, 'import_module', side_effect=RuntimeError('broken import')):
            errors = self.feature_check()
        self.assertTrue(any('predicate module import failed: RuntimeError: broken import' in e for e in errors), errors)

    @skipUnless(os.environ.get('C06E_QUAL_PREDICATE'), 'unpublished QUAL module not supplied')
    def test_unpublished_qual_predicate_refuses_authenticated_synthetic_input(self):
        # Optional integration check for the separately owned, unpublished module.
        spec = importlib.util.spec_from_file_location(
            'scripts.journey_predicates', os.environ['C06E_QUAL_PREDICATE'])
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.configure('hosted')
        self.feature['scenario'] = 'm4_6'
        self.feature['slot_id'] = 'direct-linux'
        self.feature['candidate']['manifest_sha256'] = 'a' * 64
        manifest_path = self.base / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['feature_bindings'][0].update(scenario='m4_6', slot_id='direct-linux')
        manifest_path.write_text(json.dumps(manifest))
        self.authenticate()
        with mock.patch.object(local.importlib, 'import_module', return_value=module):
            errors = self.feature_check()
        self.assertTrue(any('independent section 5 observation proofs' in e for e in errors), errors)
