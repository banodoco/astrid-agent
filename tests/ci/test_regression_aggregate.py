import datetime as dt
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/ci'))
import local_regression_green as local
import regression_green as core
import regression_aggregate as aggregate


class AggregateTests(unittest.TestCase):
    def test_complete_synthetic_cp1_matrix_and_rejections(self):
        with tempfile.TemporaryDirectory() as directory:
            p = Path(directory)
            def write(path, data):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(data), encoding='utf-8')
            tup = {'version': 1, 'name': 'synthetic CP1', 'repos': {
                r: {'url': 'https://github.com/banodoco/astrid-' + r, 'sha': s}
                for r, s in aggregate.CP1.items()}, 'generated': {}, 'toolchain': {},
                'created_at': aggregate.CUTOFF}
            identity = core.digest(tup)
            write(p / 'tuple.json', tup)
            write(p / 'freshness.json', {'check': 'C-07', 'result': 'pass', 'tuple_digest': identity, 'repo_shas': aggregate.CP1})
            write(p / 'collection.json', {'collection': 'success', 'prerequisites': 'success'})
            (p / 'ledger').write_text('')
            coverage = aggregate.read(aggregate.COVERAGE)
            lanes, events = [], []
            xml = b'<testsuite><testcase><properties><property name="node_id" value="synthetic"/></properties></testcase></testsuite>'
            for n, row in enumerate(coverage['lanes']):
                repo = row['repo']
                source_id = f'evidence-{n}'
                source = {'kind': row['kind'], 'id': source_id, 'checkouts': [repo]}
                lane = {k: row[k] for k in ('repo', 'lane', 'platform')}
                lane.update(source=source, tests=[{'node_id': 'synthetic', 'protected': row['protected']}])
                manifest = {k: row[k] for k in ('repo', 'lane', 'platform')}
                manifest.update(tuple_digest=identity, repo_shas=aggregate.CP1, checkouts={repo: aggregate.CP1[repo]},
                                junit_sha256=hashlib.sha256(xml).hexdigest(), created_at='2026-10-31T23:38:00Z', result='success', test_exit_code=0)
                if row['kind'] == 'broker':
                    job = p / 'broker/jobs' / source_id
                    download = p / 'downloads' / source_id
                    download.mkdir(parents=True)
                    job.mkdir(parents=True)
                    spec = {'stream': 'CI', 'steps': [{'run': tup['repos'][repo]['url'] + ' ' + aggregate.CP1[repo]}], 'download': [{'to': str(download), 'from': '/workspace/out'}]}
                    (job / 'job.yaml').write_text(yaml.safe_dump(spec))
                    source['job_sha256'] = local.sha(job / 'job.yaml')
                    manifest.update(job_id=source_id, pod_id=source_id)
                    write(download / 'manifest.json', manifest)
                    (download / 'junit.xml').write_bytes(xml)
                    downloads = [{'path': str(f), 'bytes': f.stat().st_size, 'sha256': local.sha(f)} for f in (download / 'manifest.json', download / 'junit.xml')]
                    write(job / 'result.json', {'ok': True, 'released': True, 'release_verified': True, 'exit_codes': [0], 'pod_id': source_id, 'started': '2026-10-31T23:37:00Z', 'ended': '2026-10-31T23:39:00Z', 'downloads': downloads})
                    events.extend([{'event': 'intent', 'name': source_id, 'stream': 'CI', 'time': '2026-10-31T23:37:01Z'}, {'event': 'created', 'name': source_id, 'stream': 'CI', 'pod_id': source_id, 'time': '2026-10-31T23:37:02Z'}, {'event': 'released', 'name': source_id, 'pod_id': source_id, 'verified': True, 'time': '2026-10-31T23:39:01Z'}])
                else:
                    source.update(run_id=n+1, run_attempt=1, job_id=n+100, artifact_id=n+200, workflow_path='.github/workflows/g3.yml')
                    manifest.update(run_id=n+1, run_attempt=1, job_id=n+100)
                    host = p / 'hosted' / source_id
                    write(host / 'manifest.json', manifest)
                    (host / 'junit.xml').write_bytes(xml)
                    write(host / 'run.json', {'id': n+1, 'run_attempt': 1, 'job_id': n+100, 'repository': tup['repos'][repo]['url'], 'head_sha': aggregate.CP1[repo], 'status': 'completed', 'conclusion': 'success', 'job_conclusion': 'success', 'workflow_path': source['workflow_path'], 'run_started_at': '2026-10-31T23:37:00Z', 'updated_at': '2026-10-31T23:39:00Z', 'artifact': {'id': n+200, 'name': source_id, 'run_id': n+1, 'run_attempt': 1, 'job_id': n+100}})
                lanes.append(lane)
            (p / 'broker/pods.jsonl').write_text(''.join(json.dumps(e) + '\n' for e in events))
            review = p / 'review.json'
            row = next(row for row in lanes if row['source']['kind'] == 'hosted')
            source = row['source']
            host = p / 'hosted' / source['id']
            feature = {k: row[k] for k in ('repo', 'lane', 'platform')}
            feature.update(scenario='synthetic', slot_id='slot', candidate=dict(aggregate.CP1), files=['feature.json'], attempt=1)
            manifest = json.loads((host/'manifest.json').read_text())
            manifest.update(attempt=1, feature_bindings=[{'scenario': 'synthetic', 'slot_id': 'slot', 'attempt': 1}])
            write(host/'manifest.json', manifest)
            write(host/'feature.json', {'observed': True})
            run = json.loads((host/'run.json').read_text())
            run['files'] = [{'path': f.name, 'bytes': f.stat().st_size, 'sha256': local.sha(f)} for f in (host/'manifest.json', host/'junit.xml', host/'feature.json')]
            write(host/'run.json', run)
            coverage['features'] = [{k: feature[k] for k in ('repo', 'lane', 'platform', 'scenario', 'slot_id')}]
            coverage_path = p/'coverage.json'
            write(coverage_path, coverage)
            (p/'synthetic_predicates.py').write_text('def check_synthetic(evidence):\n    return (evidence["files"][0]["json"] == {"observed": True}, [])\n')
            reviewed = {'complete': True, 'features_complete': True, 'features': [feature], 'tuple_digest': identity, 'lanes': lanes}
            write(review, reviewed)
            args = type('Args', (), {'tuple': p/'tuple.json', 'ledger': p/'ledger', 'broker_root': p/'broker', 'hosted_root': p/'hosted', 'freshness': p/'freshness.json', 'collection': p/'collection.json', 'not_before': aggregate.CUTOFF})()
            original = local.local_evaluate
            def evaluate_at_fixed_time(*a, **kw):
                return original(*a, now=dt.datetime(2026, 10, 31, 23, 40, tzinfo=dt.timezone.utc), **kw)
            with mock.patch.object(aggregate, 'REVIEW', review), mock.patch.object(aggregate, 'COVERAGE', coverage_path), mock.patch.object(local, 'PREDICATE_MODULE', 'synthetic_predicates'), mock.patch.object(local, 'local_evaluate', side_effect=evaluate_at_fixed_time), mock.patch.object(sys, 'path', [str(p)] + sys.path):
                self.assertEqual(aggregate.evaluate(args), ([], []))
                reviewed['features'] = []
                write(review, reviewed)
                with self.assertRaisesRegex(ValueError, 'feature'):
                    aggregate.evaluate(args)
                reviewed['features'] = [feature]
                coverage['features'].append(dict(coverage['features'][0], slot_id='missing'))
                write(coverage_path, coverage)
                write(review, reviewed)
                with self.assertRaisesRegex(ValueError, 'feature inventory'):
                    aggregate.evaluate(args)
                coverage['features'].pop()
                write(coverage_path, coverage)
                reviewed['lanes'].pop()
                write(review, reviewed)
                with self.assertRaisesRegex(ValueError, 'coverage'):
                    aggregate.evaluate(args)
                write(p/'collection.json', {'collection': 'failure', 'prerequisites': 'success'})
                with self.assertRaisesRegex(ValueError, 'collection'):
                    aggregate.evaluate(args)

    def test_missing_arguments_always_write_red_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = Path(tmp) / 'status.json'
            proc = subprocess.run([sys.executable, str(ROOT/'scripts/ci/regression_aggregate.py'), '--status', str(status)], capture_output=True, text=True)
            self.assertNotEqual(proc.returncode, 0)
            self.assertEqual(json.loads(status.read_text())['result'], 'fail')

    def test_fixed_review_is_explicitly_incomplete(self):
        self.assertFalse(aggregate.read(aggregate.REVIEW)['complete'])
        self.assertEqual(len(aggregate.read(aggregate.COVERAGE)['lanes']), 71)


if __name__ == '__main__':
    unittest.main()
