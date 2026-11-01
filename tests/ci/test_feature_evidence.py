"""Synthetic evaluator-path tests; these files are never qualification receipts."""
import hashlib
import json
from pathlib import Path
import sys
from unittest import mock

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
        with mock.patch.object(local, 'load_predicates') as imported:
            self.assertTrue(self.feature_check())
            imported.assert_not_called()
        (self.base/'feature.json').write_text('{"observation":"pass"}')
        self.authenticate()
        (self.p/'freshness.json').unlink()
        with mock.patch.object(local, 'load_predicates') as imported:
            self.assertTrue(self.feature_check())
            imported.assert_not_called()

    def test_failed_predicate_and_invalid_return_are_red(self):
        self.configure('hosted')
        with mock.patch.object(local, 'load_predicates') as imported:
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
                with mock.patch.object(local, 'load_predicates') as imported:
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
        with mock.patch.object(local, 'load_predicates') as imported:
            imported.return_value.check_demo.side_effect = RuntimeError('broken predicate')
            errors = self.feature_check()
        self.assertTrue(any('feature demo/selected-slot: RuntimeError: broken predicate' in e for e in errors), errors)

    def test_predicate_import_exception_is_a_red_result(self):
        self.configure('broker')
        with mock.patch.object(local, 'load_predicates', side_effect=RuntimeError('broken import')):
            errors = self.feature_check()
        self.assertTrue(any('predicate module import failed: RuntimeError: broken import' in e for e in errors), errors)

    def test_missing_predicate_module_rejected(self):
        self.configure('hosted')
        with mock.patch.object(local, 'PREDICATE_MODULE', 'missing_qual_predicates'):
            self.assertTrue(any('predicate module import failed' in e for e in self.feature_check()))

    def test_published_predicate_runs_only_after_authenticated_provenance(self):
        self.configure('hosted')
        self.freeze_manifest_binding('a' * 64)
        self.feature.update(scenario='m4_6', candidate=dict(self.shas, manifest_sha256='a' * 64))
        manifest_path = self.base / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['feature_bindings'][0]['scenario'] = 'm4_6'
        manifest_path.write_text(json.dumps(manifest))
        self.authenticate()
        with mock.patch.object(local, 'PREDICATE_MODULE', 'scripts.journey_predicates'):
            errors = self.feature_check()
            self.assertTrue(any('independent section 5 observation proofs' in e for e in errors), errors)
            (self.base / 'feature.json').write_text('{"forged":true}')
            with mock.patch.object(local, 'load_predicates', wraps=local.load_predicates) as loader:
                self.assertTrue(any('hash or size mismatch' in e for e in self.feature_check()))
                loader.assert_not_called()

    def test_published_module_file_missing_is_red(self):
        self.configure('hosted')
        with mock.patch.object(local, 'PREDICATE_MODULE', 'scripts.journey_predicates'), \
             mock.patch.object(local, 'PREDICATE_PATH', self.p / 'absent.py'):
            self.assertTrue(any('predicate module import failed' in e for e in self.feature_check()))

    def test_missing_predicate_function_rejected(self):
        self.configure('hosted')
        with mock.patch.object(local, 'load_predicates', return_value=object()):
            self.assertTrue(any('missing predicate: demo' in e for e in self.feature_check()))

    def test_missing_receipt_rejected(self):
        self.configure('hosted')
        (self.base / 'manifest.json').unlink()
        self.assertTrue(self.feature_check())

    def test_skipped_receipt_rejected(self):
        self.configure('hosted')
        (self.base / 'junit.xml').write_bytes(b'<testsuite><testcase><skipped/></testcase></testsuite>')
        self.authenticate()
        self.assertTrue(any('feature job failed or skipped' in e for e in self.feature_check()))

    def test_forged_artifact_hash_rejected_before_predicate(self):
        self.configure('hosted')
        (self.base / 'feature.json').write_text('{"observation":"fail"}')
        with mock.patch.object(local, 'load_predicates') as imported:
            self.assertTrue(any('hash or size mismatch' in e for e in self.feature_check()))
            imported.assert_not_called()

    def test_duplicate_receipt_and_attempt_rejected(self):
        self.configure('hosted')
        manifest_path = self.base / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['feature_bindings'].append(dict(manifest['feature_bindings'][0]))
        manifest_path.write_text(json.dumps(manifest))
        self.authenticate()
        self.assertTrue(any('feature binding or attempt mismatch' in e for e in self.feature_check()))
        self.assertTrue(any('duplicate or missing feature obligation' in e for e in self.feature_check([self.feature, self.feature])))

    def test_wrong_tuple_receipt_rejected(self):
        self.configure('hosted')
        manifest_path = self.base / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['tuple_digest'] = 'f' * 64
        manifest_path.write_text(json.dumps(manifest))
        self.authenticate()
        self.assertTrue(any('wrong tuple/lane evidence' in e for e in self.feature_check()))

    def test_absent_required_evidence_rejected(self):
        self.configure('hosted')
        self.feature['files'].append('required.json')
        self.assertTrue(any('missing authenticated feature file: required.json' in e for e in self.feature_check()))

    def freeze_manifest_binding(self, value):
        self.tuple['generated']['manifest_sha256'] = value
        self.identity = local.core.digest(self.tuple)
        self.write('tuple.json', self.tuple)
        self.mutate('freshness.json', tuple_digest=self.identity)
        for name in ('download/manifest.json', 'hosted/artifact-1/manifest.json'):
            self.mutate(name, tuple_digest=self.identity)
        self.save_inventory()
        self.rehash_downloads()
        if self.transport == 'broker':
            self.authenticate_hosted()
        self.authenticate()

    def test_tuple_bound_manifest_hash_reaches_predicate(self):
        for transport in ('broker', 'hosted'):
            with self.subTest(transport=transport):
                self.configure(transport)
                self.freeze_manifest_binding('a' * 64)
                self.feature['candidate']['manifest_sha256'] = 'a' * 64
                with mock.patch.object(local, 'load_predicates') as imported:
                    imported.return_value.check_demo.return_value = (True, [])
                    self.assertEqual(self.feature_check(), [])
                    self.assertEqual(imported.return_value.check_demo.call_args.args[0]['candidate']['manifest_sha256'], 'a' * 64)

    def test_missing_wrong_or_malformed_manifest_binding_rejected(self):
        self.configure('hosted')
        self.feature['candidate']['manifest_sha256'] = 'a' * 64
        with mock.patch.object(local, 'load_predicates') as imported:
            self.assertTrue(any('frozen tuple manifest binding' in e for e in self.feature_check()))
            imported.assert_not_called()
        self.freeze_manifest_binding('b' * 64)
        self.assertTrue(any('unbound candidate fields' in e for e in self.feature_check()))
        self.freeze_manifest_binding('A' * 64)
        self.assertTrue(any('frozen tuple manifest binding' in e for e in self.feature_check()))
        self.freeze_manifest_binding('a' * 64)
        self.feature['candidate']['other'] = 'unbound'
        self.assertTrue(any('unbound candidate fields' in e for e in self.feature_check()))
