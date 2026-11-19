"""Replay retained native receipts while varying only Actions workflow state."""
from __future__ import annotations
import copy
import datetime as dt
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tarfile
import tempfile
import unittest
from unittest import mock
import zipfile


HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("ceiling_hosted", HERE / "ceiling_hosted.py")
hosted = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hosted)


class RecordedActions:
    """Read-only API fixture backed by the packet's actual executed native proof."""
    def __init__(self):
        source = HERE.parents[1] / "ci/evidence/p39-k2-k3/native-executed-proof.tar.gz"
        with tarfile.open(source) as archive:
            files = {member.name.removeprefix("executed-fixture/"): archive.extractfile(member).read()
                     for member in archive.getmembers() if member.isfile()}
        before = json.loads(files["g1-observed.json"])
        current = json.loads(files["g2-observed.json"])
        self.repository, self.head = current["repository"], current["head"]
        self.now = max(dt.datetime.fromisoformat(job["started_at"]) for job in current["jobs"]) + dt.timedelta(seconds=1)
        self.runs, self.jobs, self.artifacts, self.archives, self.requests, self.trees = [], {}, {}, {}, [], {}
        checks = {"version": 1, "repository": self.repository, "merge": [], "release": []}
        inventory = {"schema": 1, "repository": self.repository, "tests": [], "reconciliation": [],
                     "tiers": {"merge": ["CI/native"], "release": ["CI/native", "Nightly/native"]}}
        ledger = {"schema": 1, "repository": self.repository, "authority": {"fixture_only": True}, "entries": [], "retired": []}
        for index, job in enumerate(current["jobs"]):
            workflow, run_id = job["workflow"], job["run_id"]
            suite = job["suites"][0]
            check = {"workflow": workflow, "workflow_file": workflow.lower() + ".yml", "job": "native",
                     "kind": "tests", "job_id": "native", "matrix_index": "0",
                     "commands": [{"id": "native", "name": "native", "kind": "tests", "lane": suite["lane"], "invocations": 1}]}
            checks["release"].append(check)
            if not index:
                checks["merge"].append(check)
            self.runs.append({"id": run_id, "run_attempt": 1, "name": workflow, "path": ".github/workflows/" + check["workflow_file"],
                              "head_sha": self.head, "event": "push", "created_at": job["started_at"],
                              "status": "completed", "conclusion": "failure"})
            self.jobs[run_id] = [{"id": job["job_id"], "name": "native", "run_attempt": 1, "started_at": job["started_at"],
                                 "status": "completed", "conclusion": "failure",
                                 "steps": [{"name": "native", "status": "completed", "conclusion": "failure"}]}]
            buffer = io.BytesIO()
            prefix = f"runs/{run_id}/failure-ceiling/"
            with zipfile.ZipFile(buffer, "w") as archive:
                for name, content in sorted(files.items()):
                    if name.startswith(prefix):
                        archive.writestr(zipfile.ZipInfo(name.removeprefix(prefix)), content)
            raw = buffer.getvalue()
            self.archives[index + 1] = raw
            self.artifacts[run_id] = [{"id": index + 1, "name": "failure-ceiling-native-0-1", "expired": False,
                                      "digest": "sha256:" + hashlib.sha256(raw).hexdigest()}]
            for row in suite["tests"]:
                identity = {key: row.get(key) for key in ("lane", "native_id", "parameter", "subtest")}
                inventory["tests"].append(identity)
                if row["outcome"] != "failed":
                    continue
                entry = {**identity, "owner": "fixture-owner", "release": 15, "admission": "explained-inherited",
                         "stage": row["stage"], "diagnostic": row["diagnostic"]}
                for label, measurement in (("g1", before), ("g2", current)):
                    witness = measurement["jobs"][index]
                    actual = next(t for t in witness["suites"][0]["tests"] if hosted.core.identity(t) == hosted.core.identity(row))
                    entry[label] = {**identity, "stage": actual["stage"], "diagnostic": actual["diagnostic"], "outcome": actual["outcome"],
                                    "sha": self.head, "run_id": witness["run_id"], "job_id": witness["job_id"], "source": label + "-observed.json"}
                ledger["entries"].append(entry)
        self.policy = (checks, ledger, inventory)

    def pages(self, path, key):
        self.requests.append(path)
        if key == "workflow_runs":
            return copy.deepcopy(self.runs)
        run_id = path.split("/")[2]
        return copy.deepcopy(self.jobs[run_id] if key == "jobs" else self.artifacts[run_id])

    def get(self, path):
        return self.archives[int(path.split("/")[2])]

    def evaluate(self, tier="release", wait_seconds=0):
        with tempfile.TemporaryDirectory() as directory:
            report = hosted.collect(self, self.policy[0], self.repository, self.head, tier,
                                    HERE.parents[1], Path(directory), wait_seconds)
        verdict = hosted.core.evaluate(self.policy, copy.deepcopy(self.policy), report,
                                       repository=self.repository, head=self.head, tier=tier,
                                       expected_tuple={self.repository: self.head}, now=self.now)
        return report, verdict


class HostedWorkflowTests(unittest.TestCase):
    def test_completed_failure_workflows_keep_complete_receipts_eligible(self):
        for tier, covered in (("merge", 1), ("release", 2)):
            with self.subTest(tier=tier):
                api = RecordedActions()
                report, verdict = api.evaluate(tier)
                self.assertEqual(verdict["verdict"], "PASS", verdict)
                self.assertEqual(len(verdict["covered"]), covered)
                self.assertTrue(all(job["suites"][0]["raw_exit"] == 1 for job in report["jobs"]))

    def test_cancelled_workflow_rejects_the_same_complete_receipts(self):
        for workflow, tier in (("CI", "merge"), ("CI", "release"), ("Nightly", "release")):
            with self.subTest(workflow=workflow, tier=tier):
                api = RecordedActions()
                archives = copy.deepcopy(api.archives)
                run = next(r for r in api.runs if r["name"] == workflow)
                run["conclusion"] = "cancelled"
                report, verdict = api.evaluate(tier)
                self.assertNotEqual(verdict["verdict"], "PASS", verdict)
                self.assertFalse(report["complete"])
                self.assertIn("cancelled", " ".join(report["collection_errors"]))
                self.assertEqual(api.archives, archives)
                self.assertNotIn(f"actions/runs/{run['id']}/attempts/1/jobs", api.requests)

    def test_deadline_rejects_nonterminal_workflows_despite_complete_receipts(self):
        for state in ("queued", "in_progress", "pending", "waiting", "requested"):
            with self.subTest(state=state):
                api = RecordedActions()
                api.runs[0].update(status=state, conclusion=None)
                with mock.patch.object(hosted.time, "monotonic", side_effect=(0, 2)), mock.patch.object(hosted.time, "sleep") as sleep:
                    report, verdict = api.evaluate(wait_seconds=1)
                self.assertNotEqual(verdict["verdict"], "PASS", verdict)
                self.assertFalse(report["complete"])
                self.assertIn(state, " ".join(report["collection_errors"]))
                sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
