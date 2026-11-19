"""Focused K2/K3 failure injections. No product tests are selected or changed."""
from __future__ import annotations
import copy
import datetime as dt
import importlib.util
import gzip
import os
import shutil
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("failure_ceiling", HERE / "failure_ceiling.py")
gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gate)
SHA = "a" * 40
REPO = "banodoco/" + HERE.parents[1].name
NOW = dt.datetime(2026, 11, 19, 5, tzinfo=dt.timezone.utc)


def fixture():
    check = {"workflow": "CI", "workflow_file": "ci.yml", "job": "unit", "kind": "tests"}
    nightly = {"workflow": "Nightly", "workflow_file": "nightly.yml", "job": "integration", "kind": "tests"}
    checks = {"version": 1, "repository": REPO, "merge": [check], "release": [check, nightly]}
    test = {"lane": "unit", "native_id": "tests/test_store.py::test_custody[owner]", "parameter": "[owner]", "subtest": None}
    entry = {**test, "owner": "R15-1", "release": 15, "admission": "explained-inherited", "stage": "call",
             "diagnostic": "FixtureError: old fixture cannot supply its declared owner"}
    for label, run in (("g1", 1), ("g2", 2)):
        entry[label] = {**test, "sha": str(run) * 40, "run_id": run, "job_id": 10 + run, "source": "https://github.com/proof/" + label,
                        "outcome": "failed", "stage": entry["stage"], "diagnostic": entry["diagnostic"]}
    ledger = {"schema": 1, "repository": REPO, "authority": "fixed two-record test ceiling", "entries": [entry], "retired": []}
    nightly_test = {**test, "lane": "integration", "native_id": "tests/test_integration.py::test_round_trip", "parameter": None}
    inventory = {"schema": 1, "repository": REPO, "tests": [test, nightly_test],
                 "tiers": {"merge": ["unit"], "release": ["unit", "integration"]}, "reconciliation": []}
    def job(workflow, name, row, failed, run):
        actual = {**row, "outcome": "failed" if failed else "passed", "stage": "call", "diagnostic": entry["diagnostic"] if failed else ""}
        return {"workflow": workflow, "job": name, "status": "completed", "conclusion": "failure" if failed else "success",
                "head": SHA, "tuple": {REPO: SHA}, "run_id": run, "job_id": run * 10, "attempt": 1,
                "started_at": "2026-11-19T03:00:00Z", "unexplained_failed_steps": [],
                "suites": [{"name": "actual-suite", "lane": row["lane"], "complete": True, "raw_exit": int(failed),
                            "collected": [row["native_id"]], "summary_count": 1, "tests": [actual]}]}
    report = {"schema": 1, "repository": REPO, "head": SHA, "tuple": {REPO: SHA}, "tier": "merge", "complete": True,
              "jobs": [job("CI", "unit", test, True, 30), job("Nightly", "integration", nightly_test, False, 40)]}
    trusted = (checks, ledger, inventory)
    return trusted, copy.deepcopy(trusted), report


class FailureCeilingTests(unittest.TestCase):
    def setUp(self):
        self.trusted, self.candidate, self.report = fixture()

    def verdict(self, tier="merge"):
        self.report["tier"] = tier
        return gate.evaluate(self.trusted, self.candidate, self.report, repository=REPO, head=SHA,
                             tier=tier, expected_tuple={REPO: SHA}, now=NOW)

    def rejects(self, needle, tier="merge"):
        result = self.verdict(tier)
        self.assertNotEqual(result["verdict"], "PASS", result)
        self.assertIn(needle, "\n".join(result["errors"] + result["unknown"]), result)

    def test_complete_covered_run_keeps_failing_raw_exit(self):
        for tier in ("merge", "release"):
            result = self.verdict(tier)
            self.assertEqual(result["verdict"], "PASS", result)
            self.assertEqual(len(result["covered"]), 1)
            self.assertEqual(self.report["jobs"][0]["suites"][0]["raw_exit"], 1)
            self.assertEqual(self.report["jobs"][0]["conclusion"], "failure")

    def test_new_red_and_missing_id_cannot_cancel_by_count(self):
        suite = self.report["jobs"][0]["suites"][0]
        suite["collected"] = ["tests/test_store.py::test_other[owner]"]
        suite["tests"][0]["native_id"] = suite["collected"][0]
        self.rejects("new red")
        self.rejects("lost ID")

    def test_missing_job(self):
        self.report["jobs"] = []
        self.rejects("missing required job")

    def test_missing_entire_nightly(self):
        self.report["jobs"].pop()
        self.rejects("Nightly / integration", "release")

    def test_old_night_cannot_replace_tonights_missing_evidence(self):
        self.report["jobs"][1]["started_at"] = "2026-11-17T03:00:00Z"
        self.rejects("stale/missing nightly", "release")

    def test_cancelled_skipped_and_incomplete_jobs(self):
        for conclusion in ("cancelled", "skipped", "timed_out"):
            self.report["jobs"][0]["conclusion"] = conclusion
            self.rejects("incomplete job")

    def test_wrong_tuple(self):
        self.report["jobs"][0]["tuple"][REPO] = "b" * 40
        self.rejects("wrong job commit/dependency tuple")

    def test_empty_or_truncated_results(self):
        self.report["jobs"][0]["suites"][0]["complete"] = False
        self.rejects("incomplete results")
        self.report["jobs"][0]["suites"][0]["complete"] = True
        self.report["jobs"][0]["suites"][0]["tests"] = []
        self.rejects("selected/executed ID mismatch")

    def test_fake_result_count(self):
        self.report["jobs"][0]["suites"][0]["summary_count"] = 9
        self.rejects("truncated result count")

    def test_added_or_swapped_entry(self):
        for action in ("add", "swap"):
            _, self.candidate, _ = fixture()
            entry = copy.deepcopy(self.candidate[1]["entries"][0])
            entry["native_id"] += "-new"
            for label in ("g1", "g2"):
                entry[label]["native_id"] = entry["native_id"]
            if action == "add": self.candidate[1]["entries"].append(entry)
            else: self.candidate[1]["entries"][0] = entry
            self.rejects("ceiling entry added or substituted")

    def test_changed_diagnostic_cannot_self_approve(self):
        entry = self.candidate[1]["entries"][0]
        entry["diagnostic"] = "PersistenceError: accepted record was lost"
        for label in ("g1", "g2"): entry[label]["diagnostic"] = entry["diagnostic"]
        self.rejects("edited/swapped")

    def test_same_id_different_cause_sensitive_failure_rejected(self):
        self.report["jobs"][0]["suites"][0]["tests"][0]["diagnostic"] = "CustodyError: cleanup acknowledged before native owner released"
        self.rejects("same-ID different-cause/stage")

    def test_same_id_different_stage_rejected(self):
        self.report["jobs"][0]["suites"][0]["tests"][0]["stage"] = "teardown"
        self.rejects("same-ID different-cause/stage")

    def test_skip_or_xfail_is_not_coverage(self):
        suite = self.report["jobs"][0]["suites"][0]
        suite["raw_exit"] = 0
        self.report["jobs"][0]["conclusion"] = "success"
        for outcome in ("skipped", "xfailed"):
            suite["tests"][0]["outcome"] = outcome
            self.rejects("new skip/xfail/deselection")

    def test_required_job_and_expected_id_cannot_self_approve_removal(self):
        self.candidate[0]["merge"] = []
        self.rejects("candidate changed its required job inventory")
        self.trusted, self.candidate, self.report = fixture()
        self.candidate[2]["tests"] = []
        self.rejects("expected test removed")

    def pass_and_retire(self):
        job = self.report["jobs"][0]
        suite = job["suites"][0]
        suite["raw_exit"] = 0
        suite["tests"][0].update(outcome="passed", diagnostic="")
        job["conclusion"] = "success"
        entry = self.candidate[1]["entries"].pop()
        self.candidate[1]["retired"].append({**{key: entry[key] for key in ("lane", "native_id", "parameter", "subtest")},
          "receipt": {"head": SHA, "tuple": {REPO: SHA}, "run_id": job["run_id"], "job_id": job["job_id"], "attempt": 1, "outcome": "passed"}})

    def test_retirement_requires_exact_passing_receipt(self):
        self.pass_and_retire()
        self.assertEqual(self.verdict()["verdict"], "PASS")
        self.assertEqual(len(self.verdict()["retired"]), 1)
        self.candidate[1]["retired"][0]["receipt"]["job_id"] = 1
        self.rejects("exact passing receipt")

    def test_current_run_selector_emits_exact_retirement_receipt(self):
        self.pass_and_retire()
        entry = self.candidate[1]["retired"][0]
        entry["receipt"] = {"source": "qualifying-run", "workflow": "CI", "job": "unit"}
        verdict = self.verdict()
        self.assertEqual(verdict["verdict"], "PASS", verdict)
        actual = verdict["retirement_receipts"][0]
        self.assertEqual((actual["head"], actual["run_id"], actual["job_id"], actual["attempt"], actual["outcome"]), (SHA, 30, 300, 1, "passed"))
        entry["receipt"]["job"] = "another-job"
        self.rejects("exact passing receipt")

    def test_passing_entry_must_be_retired(self):
        self.report["jobs"][0]["suites"][0]["tests"][0].update(outcome="passed", diagnostic="")
        self.report["jobs"][0]["suites"][0]["raw_exit"] = 0
        self.report["jobs"][0]["conclusion"] = "success"
        self.rejects("passing entry must be retired")

    def test_removed_failure_is_rejected(self):
        self.pass_and_retire()
        self.report["jobs"][0]["conclusion"] = "failure"
        self.report["jobs"][0]["suites"][0]["raw_exit"] = 1
        self.report["jobs"][0]["suites"][0]["tests"][0].update(outcome="failed", diagnostic="old failure")
        self.rejects("retired failure reintroduced")

    def test_parent_pass_cannot_retire_failing_subtest(self):
        self.pass_and_retire()
        self.report["jobs"][0]["suites"][0]["tests"][0]["subtests_failed"] = 1
        self.rejects("parent pass has failing subtests")

    def test_unknown_reconciliation_stays_unknown(self):
        for inventory in (self.trusted[2], self.candidate[2]):
            inventory["reconciliation"].append({"id": "missing-g1", "status": "unknown", "reason": "no log"})
        result = self.verdict()
        self.assertEqual(result["verdict"], "NO-RESULT")
        self.assertIn("missing-g1", result["unknown"][0])

    def test_new_passing_id_must_be_retained_for_future_loss_checks(self):
        suite = self.report["jobs"][0]["suites"][0]
        row = {**suite["tests"][0], "native_id": "tests/test_new.py::test_new", "parameter": None,
               "outcome": "passed", "diagnostic": ""}
        suite["collected"].append(row["native_id"])
        suite["tests"].append(row)
        suite["summary_count"] += 1
        self.rejects("observed ID must be retained")
        self.candidate[2]["tests"].append({key: row[key] for key in ("lane", "native_id", "parameter", "subtest")})
        self.assertEqual(self.verdict()["verdict"], "PASS")

    def test_native_id_cannot_swap_source_declaration(self):
        for inv in (self.trusted[2], self.candidate[2]):
            inv["tests"][0]["source"] = {"file": "tests/test_store.py", "name": "custody owner"}
        self.report["jobs"][0]["suites"][0]["tests"][0]["source"] = {"file": "tests/other.py", "name": "custody owner"}
        self.rejects("native ID source binding changed")

    def test_dependency_revision_must_match_declared_job_tuple(self):
        for checks in (self.trusted[0], self.candidate[0]):
            checks["merge"][0]["dependency_tuple"] = {"banodoco/dependency": "b" * 40}
        self.report["jobs"][0]["tuple"]["banodoco/dependency"] = "c" * 40
        self.rejects("wrong job commit/dependency tuple")

    def test_merge_retirement_requires_release_only_job(self):
        for checks in (self.trusted[0], self.candidate[0]):
            checks["release"][1]["commands"] = [{"lane": "integration"}]
        entry = self.trusted[1]["entries"][0]
        later = self.report["jobs"][1]["suites"][0]["tests"][0]
        for record in (entry, entry["g1"], entry["g2"]):
            for key in ("lane", "native_id", "parameter", "subtest"):
                record[key] = later[key]
        self.candidate = copy.deepcopy(self.trusted)
        self.candidate[1]["entries"].clear()
        job = self.report["jobs"][1]
        self.candidate[1]["retired"].append({**{k: later[k] for k in ("lane", "native_id", "parameter", "subtest")},
            "receipt": {"head": SHA, "tuple": {REPO: SHA}, "run_id": job["run_id"], "job_id": job["job_id"], "attempt": 1, "outcome": "passed"}})
        self.report["jobs"][0]["conclusion"] = "success"
        self.report["jobs"][0]["suites"][0]["raw_exit"] = 0
        self.report["jobs"][0]["suites"][0]["tests"][0].update(outcome="passed", diagnostic="")
        self.assertEqual(self.verdict()["verdict"], "PASS", self.verdict())
        self.report["jobs"].pop()
        self.rejects("missing required job/nightly")

    def test_trusted_cli_never_imports_candidate_checker_or_pythonpath(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            trusted = root / "trusted"
            candidate = root / "candidate"
            marker = root / "candidate-executed"
            for target, policy in ((trusted, self.trusted), (candidate, self.candidate)):
                (target / "ci").mkdir(parents=True)
                for filename, data in zip(("required-checks.json", "failure-ceiling.json", "test-inventory.json.gz"), policy):
                    raw = json.dumps(data).encode()
                    (target / "ci" / filename).write_bytes(gzip.compress(raw, mtime=0) if filename.endswith(".gz") else raw)
                (target / "scripts/ci").mkdir(parents=True)
            shutil.copyfile(HERE / "failure_ceiling.py", trusted / "scripts/ci/failure_ceiling.py")
            payload = f"from pathlib import Path; Path({str(marker)!r}).write_text('executed'); raise RuntimeError('candidate code imported')"
            (candidate / "scripts/ci/failure_ceiling.py").write_text(payload)
            (candidate / "sitecustomize.py").write_text(payload)
            (root / "report.json").write_text(json.dumps(self.report))
            (root / "tuple.json").write_text(json.dumps({REPO: SHA}))
            result = subprocess.run([sys.executable, "-I", str(trusted / "scripts/ci/failure_ceiling.py"),
                "--candidate", str(candidate), "--report", str(root / "report.json"), "--repository", REPO,
                "--head", SHA, "--tier", "merge", "--tuple", str(root / "tuple.json"), "--output", str(root / "verdict.json")],
                cwd=candidate, env={**os.environ, "PYTHONPATH": str(candidate)}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertFalse(marker.exists())

    def test_unexplained_failed_setup_is_not_covered_by_red_test(self):
        self.report["jobs"][0]["unexplained_failed_steps"] = ["install dependency"]
        self.rejects("uncovered non-test/earlier-stage failure")


if __name__ == "__main__":
    unittest.main()
