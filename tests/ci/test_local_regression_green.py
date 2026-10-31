import datetime as dt
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts/ci'))
import regression_green as core
import local_regression_green as local


class LocalEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.p = Path(self.tmp.name)
        self.now = dt.datetime(2026, 10, 31, 23, 30, tzinfo=dt.timezone.utc)
        self.shas = {r: str(i) * 40 for i, r in enumerate(core.REPOS, 1)}
        self.tuple = {'version': 1, 'name': 'synthetic', 'repos': {r: {'url': 'https://github.com/banodoco/astrid-' + r, 'sha': sha} for r, sha in self.shas.items()}, 'generated': {}, 'toolchain': {}, 'created_at': '2026-10-31T23:00:00Z'}
        self.identity = core.digest(self.tuple)
        self.lanes = [{'repo': 'runtime', 'lane': 'unit-1', 'platform': 'linux', 'source': {'kind': 'broker', 'id': 'job-1', 'checkouts': ['runtime']}}, {'repo': 'agent', 'lane': 'unit-1', 'platform': 'windows', 'source': {'kind': 'hosted', 'id': 'artifact-1', 'checkouts': ['agent'], 'run_id': 1, 'run_attempt': 1, 'job_id': 2, 'workflow_path': '.github/workflows/ci.yml', 'artifact_id': 3}}]
        for lane in self.lanes:
            lane['tests'] = [{'node_id': 'test_a', 'protected': False}]
        self.write('tuple.json', self.tuple)
        self.write('freshness.json', {'check': 'C-07', 'result': 'pass', 'tuple_digest': self.identity, 'repo_shas': self.shas})
        (self.p / 'ledger.jsonl').write_text('')
        self.xml = b'<testsuite><testcase name="test_a"><properties><property name="node_id" value="test_a"/></properties></testcase></testsuite>'
        spec = {'stream': 'CI', 'steps': [{'run': 'git clone https://github.com/banodoco/astrid-runtime\ngit checkout ' + self.shas['runtime']}], 'download': [{'from': '/workspace/out/', 'to': str(self.p / 'download')}]}
        job = self.p / 'broker/jobs/job-1'
        job.mkdir(parents=True)
        (job / 'job.yaml').write_text(yaml.safe_dump(spec))
        self.lanes[0]['source']['job_sha256'] = local.sha(job / 'job.yaml')
        manifest = self.manifest(self.lanes[0])
        manifest.update(checkouts={'runtime': self.shas['runtime']}, pod_id='pod-1', job_id='job-1')
        self.write('download/manifest.json', manifest)
        (self.p / 'download/junit.xml').write_bytes(self.xml)
        downloads = [{'path': str(p), 'bytes': p.stat().st_size, 'sha256': local.sha(p)} for p in (self.p / 'download/manifest.json', self.p / 'download/junit.xml')]
        self.write('broker/jobs/job-1/result.json', {'ok': True, 'released': True, 'release_verified': True, 'exit_codes': [0], 'pod_id': 'pod-1', 'started': '2026-10-31T23:01:00Z', 'ended': '2026-10-31T23:03:00Z', 'downloads': downloads})
        events = [{'event': 'intent', 'name': 'podq-a', 'stream': 'CI', 'time': '2026-10-31T23:01:01Z'}, {'event': 'created', 'name': 'podq-a', 'stream': 'CI', 'pod_id': 'pod-1', 'time': '2026-10-31T23:01:02Z'}, {'event': 'released', 'name': 'podq-a', 'pod_id': 'pod-1', 'verified': True, 'time': '2026-10-31T23:04:00Z'}]
        (self.p / 'broker/pods.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in events))
        manifest = self.manifest(self.lanes[1])
        manifest.update(checkouts={'agent': self.shas['agent']}, run_id=1, run_attempt=1, job_id=2)
        self.write('hosted/artifact-1/manifest.json', manifest)
        (self.p / 'hosted/artifact-1/junit.xml').write_bytes(self.xml)
        self.write('hosted/artifact-1/run.json', {'id': 1, 'run_attempt': 1, 'job_id': 2, 'repository': self.tuple['repos']['agent']['url'], 'head_sha': self.shas['agent'], 'status': 'completed', 'conclusion': 'success', 'job_conclusion': 'success', 'workflow_path': '.github/workflows/ci.yml', 'run_started_at': '2026-10-31T23:01:00Z', 'updated_at': '2026-10-31T23:04:00Z', 'artifact': {'id': 3, 'name': 'artifact-1', 'run_id': 1, 'run_attempt': 1, 'job_id': 2}})
        self.save_inventory()
        self.write('coverage.json', {'lanes': [{k: lane[k] for k in ('repo', 'lane', 'platform')} for lane in self.lanes]})

    def manifest(self, lane):
        return {k: lane[k] for k in ('repo', 'lane', 'platform')} | {'tuple_digest': self.identity, 'repo_shas': self.shas, 'junit_sha256': hashlib.sha256(self.xml).hexdigest(), 'created_at': '2026-10-31T23:02:00Z', 'result': 'success', 'test_exit_code': 0}

    def write(self, name, obj):
        path = self.p / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj))

    def save_inventory(self):
        self.write('inventory.json', {'tuple_digest': self.identity, 'lanes': self.lanes})

    def check(self):
        return local.local_evaluate(self.p / 'tuple.json', self.p / 'inventory.json', self.p / 'coverage.json', self.p / 'ledger.jsonl', self.p / 'broker', self.p / 'hosted', self.p / 'freshness.json', '2026-10-31T23:00:00Z', now=self.now)[0]

    def mutate(self, name, **fields):
        path = self.p / name
        obj = json.loads(path.read_text())
        obj.update(fields)
        path.write_text(json.dumps(obj))

    def rehash_downloads(self):
        path = self.p / 'broker/jobs/job-1/result.json'
        result = json.loads(path.read_text())
        for entry in result['downloads']:
            file = Path(entry['path'])
            entry.update(bytes=file.stat().st_size, sha256=local.sha(file))
        path.write_text(json.dumps(result))

    def test_valid_complete_broker_and_hosted(self):
        self.assertEqual(self.check(), [])

    def test_missing_or_malformed_files(self):
        for name in ['broker/pods.jsonl', 'broker/jobs/job-1/job.yaml', 'broker/jobs/job-1/result.json', 'download/junit.xml', 'hosted/artifact-1/run.json', 'hosted/artifact-1/manifest.json', 'freshness.json', 'coverage.json']:
            with self.subTest(name=name):
                path = self.p / name
                saved = path.read_bytes()
                path.unlink()
                self.assertTrue(self.check())
                path.write_bytes(saved)
        (self.p / 'broker/jobs/job-1/result.json').write_text('{')
        self.assertTrue(self.check())

    def test_stale_and_wrong_hosted_identity(self):
        path = self.p / 'hosted/artifact-1/run.json'
        saved = path.read_text()
        for fields in ({'head_sha': 'f' * 40}, {'repository': 'wrong'}, {'run_started_at': '2026-10-30T23:01:00Z'}, {'conclusion': 'failure'}, {'job_conclusion': 'skipped'}, {'id': 99}, {'workflow_path': 'wrong'}, {'artifact': {'id': 99}}):
            with self.subTest(fields=fields):
                self.mutate('hosted/artifact-1/run.json', **fields)
                self.assertTrue(self.check())
                path.write_text(saved)

    def test_broker_provenance_and_download_integrity(self):
        result = self.p / 'broker/jobs/job-1/result.json'
        saved = result.read_text()
        for fields in ({'pod_id': 'wrong'}, {'ok': False}, {'release_verified': False}, {'exit_codes': [1]}, {'started': '2026-10-30T23:01:00Z'}):
            with self.subTest(fields=fields):
                self.mutate('broker/jobs/job-1/result.json', **fields)
                self.assertTrue(self.check())
                result.write_text(saved)
        (self.p / 'download/junit.xml').write_bytes(self.xml + b' ')
        self.assertTrue(self.check())

    def test_inventory_missing_duplicate_and_renamed(self):
        self.lanes.append(dict(self.lanes[0], lane='unit-2'))
        self.save_inventory()
        self.assertTrue(self.check())
        self.lanes.pop()
        self.lanes[0]['tests'][0]['node_id'] = 'renamed'
        self.save_inventory()
        self.assertTrue(self.check())
        self.lanes.append(self.lanes[0])
        self.save_inventory()
        self.assertTrue(self.check())

    def test_skipped_failed_wrong_tuple_and_junit(self):
        path = self.p / 'hosted/artifact-1/manifest.json'
        saved = path.read_text()
        for fields in ({'test_exit_code': 1}, {'result': 'skipped'}, {'tuple_digest': 'wrong'}, {'checkouts': {'agent': 'f' * 40}}):
            with self.subTest(fields=fields):
                self.mutate('hosted/artifact-1/manifest.json', **fields)
                self.assertTrue(self.check())
                path.write_text(saved)
        xml = b'<testsuite><testcase><properties><property name="node_id" value="test_a"/></properties><skipped/></testcase></testsuite>'
        (self.p / 'hosted/artifact-1/junit.xml').write_bytes(xml)
        self.mutate('hosted/artifact-1/manifest.json', junit_sha256=hashlib.sha256(xml).hexdigest())
        self.assertTrue(self.check())

    def test_admitted_failure_and_exit_code_are_consistent(self):
        xml = b'<testsuite><testcase><properties><property name="node_id" value="test_a"/><property name="outcome" value="failed"/><property name="phase" value="call"/><property name="fingerprint" value="known"/></properties><failure/></testcase></testsuite>'
        (self.p / 'download/junit.xml').write_bytes(xml)
        self.mutate('download/manifest.json', junit_sha256=hashlib.sha256(xml).hexdigest(), test_exit_code=1, result='failure')
        self.rehash_downloads()
        self.assertTrue(self.check())
        row = {'id': 'KF-1', 'repo': 'runtime', 'lane': 'unit-1', 'platform': 'linux', 'node_id': 'test_a', 'outcome': 'failed', 'phase': 'call', 'fingerprint': 'known', 'baseline': {'tuple_digest': 'parent', 'run': 'run'}, 'provenance': 'integration', 'owner': 'M-INT', 'rationale': 'known', 'status': 'admitted', 'expires': '2099-01-01'}
        (self.p / 'ledger.jsonl').write_text(json.dumps(row) + '\n')
        self.assertEqual(self.check(), [])
        self.mutate('download/manifest.json', test_exit_code=0, result='success')
        self.rehash_downloads()
        self.assertTrue(self.check())

    def test_omitted_required_lane_fails(self):
        self.lanes.pop()
        self.save_inventory()
        self.assertTrue(self.check())

    def test_spec_digest_and_required_sha(self):
        spec = self.p / 'broker/jobs/job-1/job.yaml'
        spec.write_text(spec.read_text().replace(self.shas['runtime'], 'f' * 40))
        self.lanes[0]['source']['job_sha256'] = local.sha(spec)
        self.save_inventory()
        self.assertTrue(self.check())


if __name__ == '__main__':
    unittest.main()
