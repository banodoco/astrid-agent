import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/ci'))
import trusted_hosted_download as hosted
import regression_finalizer as finalizer
import local_regression_green as local


class TrustedDownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.repos = {'agent': {'url': 'https://github.com/banodoco/astrid-agent', 'sha': 'a' * 40}}
        self.source = {'kind': 'hosted', 'repo': 'agent', 'workflow_path': '.github/workflows/g3.yml',
                       'workflow_revision': 'b' * 40, 'run_id': 1, 'run_attempt': 1, 'job_id': 2,
                       'artifact_id': 3, 'artifact_name': 'receipt', 'head_sha': 'a' * 40}
        self.run = {'id': 1, 'run_attempt': 1, 'head_sha': 'a' * 40, 'status': 'completed', 'conclusion': 'success', 'path': self.source['workflow_path'], 'repository': {'html_url': self.repos['agent']['url']}}
        self.jobs = {'total_count': 1, 'jobs': [{'id': 2, 'run_id': 1, 'run_attempt': 1, 'status': 'completed', 'conclusion': 'success', 'started_at': '2026-10-31T23:01:00Z', 'completed_at': '2026-10-31T23:03:00Z'}]}
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as z:
            z.writestr('manifest.json', '{}')
            z.writestr('junit.xml', '<testsuite/>')
        self.raw = stream.getvalue()
        self.artifacts = {'total_count': 1, 'artifacts': [{'id': 3, 'name': 'receipt', 'expired': False, 'workflow_run': {'id': 1}, 'created_at': '2026-10-31T23:02:00Z', 'digest': 'sha256:' + hashlib.sha256(self.raw).hexdigest()}]}

    def fetch(self, path, binary=False):
        if binary:
            return self.raw
        if '/jobs?' in path:
            return self.jobs
        if '/artifacts?' in path:
            return self.artifacts
        return self.run

    def download(self):
        return hosted.download(self.source, self.repos, self.root / 'receipt', self.fetch)

    def test_verified_archive_and_metadata(self):
        metadata = self.download()
        self.assertEqual(metadata['source'], self.source)
        self.assertEqual(metadata['run']['job_id'], 2)
        self.assertEqual(metadata['archive_sha256'], hashlib.sha256(self.raw).hexdigest())
        self.assertEqual({x['path'] for x in metadata['files']}, {'manifest.json', 'junit.xml'})
        for entry in metadata['files']:
            local = (self.root / 'receipt' / entry['path']).read_bytes()
            self.assertEqual(hashlib.sha256(local).hexdigest(), entry['sha256'])

    def test_missing_wrong_stale_and_unassociated_rejected(self):
        for target, key, wrong in [(self.run, 'head_sha', 'b' * 40), (self.run, 'run_attempt', 2), (self.run, 'conclusion', 'failure'), (self.jobs, 'total_count', 2), (self.jobs['jobs'][0], 'conclusion', 'skipped'), (self.artifacts, 'total_count', 0), (self.artifacts['artifacts'][0], 'digest', 'sha256:' + '0' * 64), (self.artifacts['artifacts'][0], 'name', 'wrong')]:
            with self.subTest(key=key, wrong=wrong):
                old = target[key]
                target[key] = wrong
                with self.assertRaises(ValueError):
                    self.download()
                target[key] = old
        self.source['run_attempt'] = 2
        with self.assertRaises(ValueError):
            self.download()
        self.source['run_attempt'] = 1
        del self.source['workflow_revision']
        with self.assertRaisesRegex(ValueError, 'invalid hosted source binding'):
            self.download()
        self.assertFalse((self.root / 'receipt').exists())

    def test_duplicate_artifact_and_tampered_archive_rejected(self):
        self.artifacts['artifacts'].append(dict(self.artifacts['artifacts'][0]))
        self.artifacts['total_count'] = 2
        with self.assertRaisesRegex(ValueError, 'duplicate hosted artifact'):
            self.download()
        self.artifacts['artifacts'].pop()
        self.artifacts['total_count'] = 1
        self.raw += b'tampered'
        with self.assertRaisesRegex(ValueError, 'digest'):
            self.download()
        self.assertFalse((self.root / 'receipt').exists())

    def test_duplicate_or_unsafe_archive_members_rejected(self):
        for members, error in [(('manifest.json', 'junit.xml', '../escape'), 'unsafe'), (('manifest.json', 'manifest.json', 'junit.xml'), 'duplicate')]:
            with self.subTest(members=members), tempfile.TemporaryDirectory() as directory:
                stream = io.BytesIO()
                with zipfile.ZipFile(stream, 'w') as z:
                    for member in members:
                        z.writestr(member, '{}')
                self.raw = stream.getvalue()
                self.artifacts['artifacts'][0]['digest'] = 'sha256:' + hashlib.sha256(self.raw).hexdigest()
                with self.assertRaisesRegex(ValueError, error):
                    hosted.download(self.source, self.repos, Path(directory) / 'artifact', self.fetch)


class FinalizerTests(unittest.TestCase):
    def sealed(self, results):
        value = {'schema_version': finalizer.INPUT_VERSION, 'check': finalizer.CHECK_NAME,
                 'batch_id': 'batch-1', 'generation': 1, 'invocation_id': 'invocation-1',
                 'tuple_digest': 'a' * 64, 'target_sha': 'b' * 40, 'tuple': {},
                 'producer_results': results}
        value['sealed_results_digest'] = finalizer.digest(results)
        value['sealed_input_digest'] = finalizer.digest(value)
        return value

    def assert_failed_results_reach_aggregate(self, results, expected):
        sealed = self.sealed(results)
        evaluator = mock.Mock(return_value={'result': 'pass', 'reasons': []})
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(finalizer.aggregate, 'evaluate_sealed', wraps=finalizer.aggregate.evaluate_sealed) as invoked:
            status = finalizer.run(sealed, tmp, evaluator)
            invoked.assert_called_once_with(sealed, evaluator)
            evaluator.assert_not_called()
            self.assertEqual(status['result'], 'fail')
            for item in expected:
                self.assertTrue(any(item in reason for reason in status['reasons']))
            self.assertEqual(json.loads((Path(tmp) / 'status.json').read_text()), status)

    def test_failed_producer_still_invokes_aggregate(self):
        self.assert_failed_results_reach_aggregate([{'producer_id': 'producer', 'outcome': 'infrastructure_failed'}], ['infrastructure_failed'])

    def test_failed_producer_does_not_skip_successful_collection(self):
        self.assert_failed_results_reach_aggregate([{'producer_id': 'producer', 'outcome': 'infrastructure_failed'}, {'producer_id': 'collection', 'outcome': 'completed'}], ['producer producer: infrastructure_failed'])

    def test_unstartable_producer_does_not_skip_remaining_work(self):
        self.assert_failed_results_reach_aggregate([{'producer_id': 'first', 'outcome': 'unstartable'}, {'producer_id': 'second', 'outcome': 'collection_failed'}], ['producer first: unstartable', 'producer second: collection_failed'])

    def test_completed_worker_death_invokes_aggregate_without_replaying_producers(self):
        self.assert_failed_results_reach_aggregate([{'producer_id': 'worker-1', 'outcome': 'infrastructure_failed', 'result': {'state': 'worker-death'}}], ['worker-1: infrastructure_failed'])

    def test_failed_producer_writes_failed_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            marker = p / 'ran'
            (p / 'plan').write_text(json.dumps([[sys.executable, '-c', 'from pathlib import Path; Path(%r).touch()' % str(marker)]]))
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/ci/regression_finalizer.py'), '--status', str(p / 'status'), '--producer-plan', str(p / 'plan')], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(json.loads((p / 'status').read_text())['result'], 'fail')
            self.assertFalse(marker.exists())

    def test_missing_arguments_writes_failed_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = Path(tmp) / 'status.json'
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/ci/regression_finalizer.py'), '--status', str(status)], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(json.loads(status.read_text())['result'], 'fail')


from test_local_regression_green import LocalEvidenceTests


class HostedEvaluatorIntegrationTests(LocalEvidenceTests):
    def test_downloaded_hosted_evidence_reaches_evaluator_and_tampering_fails(self):
        base = self.p / 'hosted/artifact-1'
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as archive:
            archive.writestr('manifest.json', (base / 'manifest.json').read_bytes())
            archive.writestr('junit.xml', (base / 'junit.xml').read_bytes())
        raw = stream.getvalue()
        source = {'kind': 'hosted', 'repo': 'agent', 'workflow_path': '.github/workflows/ci.yml',
                  'workflow_revision': 'b' * 40, 'run_id': 1, 'run_attempt': 1, 'job_id': 2,
                  'artifact_id': 3, 'artifact_name': 'artifact-1', 'head_sha': self.shas['agent']}
        run = {'id': 1, 'run_attempt': 1, 'head_sha': self.shas['agent'], 'status': 'completed', 'conclusion': 'success', 'path': source['workflow_path'], 'repository': {'html_url': self.tuple['repos']['agent']['url']}}
        jobs = {'total_count': 1, 'jobs': [{'id': 2, 'run_id': 1, 'run_attempt': 1, 'status': 'completed', 'conclusion': 'success', 'started_at': '2026-10-31T23:01:00Z', 'completed_at': '2026-10-31T23:04:00Z'}]}
        artifacts = {'total_count': 1, 'artifacts': [{'id': 3, 'name': 'artifact-1', 'expired': False, 'workflow_run': {'id': 1}, 'created_at': '2026-10-31T23:02:00Z', 'digest': 'sha256:' + hashlib.sha256(raw).hexdigest()}]}
        def fetch(path, binary=False):
            if binary:
                return raw
            if '/jobs?' in path:
                return jobs
            if '/artifacts?' in path:
                return artifacts
            return run
        for path in base.iterdir():
            path.unlink()
        base.rmdir()
        receipt = hosted.download(source, self.tuple['repos'], base, fetch)
        self.assertEqual(receipt['source'], source)
        # The downloaded receipt is not a legacy run.json or an authenticated
        # bridge to the evaluator. Collection alone must not make this green.
        self.assertTrue(any('run.json' in error for error in self.check()))
        manifest = next(item for item in receipt['files'] if item['path'] == 'manifest.json')
        local.checked_bytes(base / 'manifest.json', manifest['bytes'], manifest['sha256'])
        (base / 'manifest.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'hash or size mismatch'):
            local.checked_bytes(base / 'manifest.json', manifest['bytes'], manifest['sha256'])
        self.assertTrue(any('run.json' in error for error in self.check()))
