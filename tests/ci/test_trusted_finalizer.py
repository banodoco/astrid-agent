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


class TrustedDownloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.source = {'kind': 'hosted', 'id': 'receipt', 'run_id': 1, 'run_attempt': 1, 'job_id': 2, 'artifact_id': 3, 'workflow_path': '.github/workflows/g3.yml', 'checkouts': ['agent']}
        self.lane = {'repo': 'agent', 'lane': 'unit', 'platform': 'windows', 'source': self.source}
        self.tup = {'repos': {'agent': {'url': 'https://github.com/banodoco/astrid-agent', 'sha': 'a' * 40}}}
        self.run = {'id': 1, 'run_attempt': 1, 'head_sha': 'a' * 40, 'status': 'completed', 'conclusion': 'success', 'path': self.source['workflow_path'], 'repository': {'html_url': self.tup['repos']['agent']['url']}, 'run_started_at': '2026-10-31T23:01:00Z', 'updated_at': '2026-10-31T23:03:00Z'}
        self.jobs = {'total_count': 1, 'jobs': [{'id': 2, 'run_id': 1, 'run_attempt': 1, 'status': 'completed', 'conclusion': 'success'}]}
        stream = io.BytesIO()
        with zipfile.ZipFile(stream, 'w') as z:
            z.writestr('manifest.json', '{}')
            z.writestr('junit.xml', '<testsuite/>')
        self.raw = stream.getvalue()
        self.artifacts = {'total_count': 1, 'artifacts': [{'id': 3, 'name': 'receipt', 'expired': False, 'workflow_run': {'id': 1}, 'digest': 'sha256:' + hashlib.sha256(self.raw).hexdigest()}]}

    def fetch(self, path, binary=False):
        if binary:
            return self.raw
        if '/jobs?' in path:
            return self.jobs
        if '/artifacts?' in path:
            return self.artifacts
        return self.run

    def download(self, lanes=None):
        return hosted.download(self.tup, [self.lane] if lanes is None else lanes, self.root, self.fetch)

    def test_verified_archive_and_metadata(self):
        self.download()
        metadata = json.loads((self.root / 'receipt/run.json').read_text())
        self.assertEqual(metadata['artifact']['job_id'], 2)
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

    def test_duplicate_artifact_and_tampered_archive_rejected(self):
        self.artifacts['artifacts'].append(dict(self.artifacts['artifacts'][0]))
        self.artifacts['total_count'] = 2
        with self.assertRaises(ValueError):
            self.download()
        self.artifacts['artifacts'].pop()
        self.artifacts['total_count'] = 1
        self.raw += b'tampered'
        with self.assertRaisesRegex(ValueError, 'digest'):
            self.download()

    def test_duplicate_or_unsafe_archive_members_rejected(self):
        for members in [('manifest.json', '../escape'), ('manifest.json', 'manifest.json'), ('manifest.json', 'run.json')]:
            with self.subTest(members=members), tempfile.TemporaryDirectory() as directory:
                stream = io.BytesIO()
                with zipfile.ZipFile(stream, 'w') as z:
                    for member in members:
                        z.writestr(member, '{}')
                self.raw = stream.getvalue()
                self.artifacts['artifacts'][0]['digest'] = 'sha256:' + hashlib.sha256(self.raw).hexdigest()
                with self.assertRaises(ValueError):
                    hosted.download(self.tup, [self.lane], directory, self.fetch)


class FinalizerTests(unittest.TestCase):
    def test_failed_producer_still_invokes_aggregate(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp)
            plan = p / 'plan.json'
            plan.write_text(json.dumps([[sys.executable, '-c', 'import sys; sys.exit(3)']]))
            args = ['--status', str(p / 'status'), '--tuple', str(p / 'tuple'), '--ledger', str(p / 'ledger'), '--broker-root', str(p / 'broker'), '--freshness', str(p / 'freshness'), '--producer-plan', str(plan)]
            def check_aggregate(arguments):
                receipt = json.loads(Path(arguments[arguments.index('--collection') + 1]).read_text())
                self.assertEqual(receipt, {'collection': 'failure', 'prerequisites': 'failure'})
                return 1
            with mock.patch.object(finalizer.aggregate, 'main', side_effect=check_aggregate) as invoked:
                self.assertEqual(finalizer.main(args), 1)
                invoked.assert_called_once()

    def test_missing_arguments_writes_failed_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = Path(tmp) / 'status.json'
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/ci/regression_finalizer.py'), '--status', str(status)], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(json.loads(status.read_text())['result'], 'fail')
