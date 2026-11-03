import json
from pathlib import Path
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/ci"))
import regression_aggregate as aggregate


class AggregateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    @staticmethod
    def sealed(outcome="completed"):
        return {"producer_results": [{"producer_id": "producer-1", "outcome": outcome, "result": {}}]}

    def test_aggregate_rejects_callback_without_authenticated_inputs(self):
        calls = []
        result = aggregate.evaluate_sealed(self.sealed(), lambda value: calls.append(value) or {"result": "pass", "reasons": []})
        self.assertEqual(result["result"], "fail")
        self.assertEqual(calls, [])

    def test_aggregate_is_unconditional_and_fail_closed_for_missing_or_unavailable_authority(self):
        calls = []
        result = aggregate.evaluate_sealed(self.sealed("missing_at_deadline"), lambda value: calls.append(value) or {"result": "pass", "reasons": []})
        self.assertEqual(result["result"], "fail")
        self.assertIn("missing_at_deadline", result["reasons"][0])
        self.assertEqual(calls, [])
        unavailable = aggregate.evaluate_sealed(self.sealed())
        self.assertEqual(unavailable["result"], "fail")
        self.assertIn("unavailable", unavailable["reasons"][0])

    def test_bad_cli_arguments_still_emit_a_red_host_status(self):
        status = self.root / "status.json"
        self.assertEqual(aggregate.main(["--status", str(status)]), 1)
        output = json.loads(status.read_text(encoding="utf-8"))
        self.assertEqual(output["result"], "fail")
        self.assertTrue(output["host_verdict"])

    def test_sealed_cli_rejects_incomplete_reviewed_inputs_and_unauthenticated_index(self):
        from unittest import mock
        identity, target = "a" * 64, "b" * 40
        coverage = aggregate.read(aggregate.COVERAGE)
        lanes = []
        for row in coverage["lanes"]:
            lanes.append({**{k: row[k] for k in ("repo", "lane", "platform")},
                          "source": {"kind": row["kind"]},
                          "tests": [{"node_id": "synthetic", "protected": row["protected"]}]})
        feature = {**{k: lanes[0][k] for k in ("repo", "lane", "platform")},
                   "scenario": "synthetic", "slot_id": "slot", "candidate": "fixture", "files": ["fixture.json"]}
        coverage["features"] = [{k: feature[k] for k in ("repo", "lane", "platform", "scenario", "slot_id")}]
        review = {"complete": True, "features_complete": True, "tuple_digest": identity,
                  "lanes": lanes, "features": [feature]}
        inventory = {"tuple_digest": identity, "lanes": lanes}
        sealed = {"batch_id": "batch", "generation": 1, "tuple_digest": identity,
                  "results": [{"producer_id": "producer", "state": "completed"}], "artifact_index": ["synthetic"]}
        def write(name, value):
            path = self.root / name
            path.write_text(json.dumps(value))
            return path
        coverage_path, review_path = write("coverage.json", coverage), write("review.json", review)
        tuple_path = write("tuple.json", {"repos": {"agent": {"sha": target}}})
        inventory_path = write("inventory.json", inventory)
        features_path = write("features.json", [feature])
        sealed_path = self.root / "sealed.json"
        status = self.root / "status.json"
        def run():
            write("sealed.json", sealed)
            args = ["--status", str(status)]
            values = {"sealed-results": sealed_path, "tuple": tuple_path, "inventory": inventory_path,
                      "ledger": self.root / "ledger", "evidence-root": self.root, "freshness": self.root / "freshness",
                      "tuple-schema": self.root / "tuple-schema", "ledger-schema": self.root / "ledger-schema",
                      "features": features_path, "predicate-path": self.root / "predicate", "predicate-sha256": "0" * 64,
                      "batch-id": "batch", "generation": "1", "invocation-id": "invocation", "tuple-digest": identity,
                      "target-sha": target, "sealed-input-digest": aggregate._canonical(sealed),
                      "artifact-index-digest": aggregate._canonical(sealed["artifact_index"])}
            for key, value in values.items():
                args.extend(["--" + key, str(value)])
            self.assertEqual(aggregate.main(args), 1)
            result = json.loads(status.read_text())
            self.assertEqual(result["result"], "fail")
            return " ".join(result["reasons"])
        with mock.patch.object(aggregate, "COVERAGE", coverage_path), mock.patch.object(aggregate, "REVIEW", review_path), \
             mock.patch.object(aggregate.ledger, "tuple_identity", return_value=(identity, {})):
            self.assertIn("not authenticated", run())
            write("inventory.json", {"tuple_digest": identity, "lanes": lanes[:-1]})
            self.assertIn("inventory differs", run())
            write("inventory.json", inventory)
            for bad in (None, [], [feature, feature], [dict(feature, slot_id="wrong")]):
                write("features.json", bad)
                self.assertRegex(run(), "feature")
            write("features.json", [feature])
            for bad in (None, [], {}):
                sealed["artifact_index"] = bad
                self.assertIn("artifact index", run())
            sealed["artifact_index"] = ["synthetic"]
            review["tuple_digest"] = "c" * 64
            write("review.json", review)
            self.assertIn("wrong tuple", run())

    def test_pinned_predicate_receives_frozen_candidate_and_checked_snapshots(self):
        import hashlib
        repos = {name: {"sha": char * 40} for name, char in zip(
            ("runtime", "agent", "app", "pinokio"), "abcd")}
        tup = {"repos": repos, "generated": {"manifest_sha256": "e" * 64}}
        candidate = {name: value["sha"] for name, value in repos.items()}
        candidate["manifest_sha256"] = "e" * 64
        feature = {"repo": "agent", "lane": "synthetic", "platform": "linux", "scenario": "m4_6",
                   "slot_id": "synthetic", "attempt": 1, "candidate": candidate, "files": ["receipt.json"]}
        path = self.root / "features.json"
        path.write_text(json.dumps([feature]))
        key = ("agent", "synthetic", "linux", "m4_6", "synthetic", 1)
        raw = b"{}"
        snapshots = {key: {"receipt.json": (hashlib.sha256(raw).hexdigest(), raw)}}
        predicate = ROOT / "scripts/ci/journey_predicates.py"
        def check():
            return aggregate._validate_features(path, predicate, aggregate.QUAL_PREDICATE_SHA256, tup, snapshots)
        errors = check()
        self.assertTrue(any("independent section 5" in item for item in errors), errors)
        self.assertFalse(any("import failed" in item or "candidate:" in item or "files[0]" in item for item in errors), errors)
        feature["candidate"] = dict(candidate, agent="f" * 40)
        path.write_text(json.dumps([feature]))
        self.assertIn("candidate tuple mismatch", check()[0])
        feature["candidate"] = candidate
        path.write_text(json.dumps([feature]))
        snapshots[key]["receipt.json"] = ("0" * 64, raw)
        self.assertIn("snapshot SHA-256 mismatch", check()[0])
        snapshots[key]["receipt.json"] = (hashlib.sha256(b'{"x":1,"x":2}').hexdigest(), b'{"x":1,"x":2}')
        self.assertIn("duplicate JSON key", check()[0])
        snapshots[key] = {}
        self.assertIn("receipt.json", check()[0])

    def test_unapproved_qual_bytes_are_red(self):
        predicate = self.root / "predicate.py"
        predicate.write_text("def check_m4_6(evidence):\n    return True, []\n", encoding="utf-8")
        features = self.root / "features.json"
        features.write_text(json.dumps([]), encoding="utf-8")
        self.assertIn("unapproved", aggregate._validate_features(features, predicate, "0" * 64, {"generated": {}})[0])


if __name__ == "__main__":
    unittest.main()


# Retain the published test suite alongside the sealed evaluator cases.
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


class PublishedAggregateTests(unittest.TestCase):
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
                    run = json.loads((host / 'run.json').read_text())
                    run['files'] = [{'path': f.name, 'bytes': f.stat().st_size, 'sha256': local.sha(f)} for f in (host / 'manifest.json', host / 'junit.xml')]
                    write(host / 'run.json', run)
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
                with mock.patch.object(local, 'PREDICATE_MODULE', 'missing_qual_predicates'):
                    errors, _ = aggregate.evaluate(args)
                    self.assertTrue(any('predicate module import failed' in e for e in errors), errors)
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
