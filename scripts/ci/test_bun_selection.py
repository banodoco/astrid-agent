"""Replay the original Bun receipts and reject lost or changed selection.

These tests do not grant any production inventory/omission allowance. The
unchanged comparator must still refuse the original non-execution outcomes.
"""
from __future__ import annotations

from collections import Counter
import copy
import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import tarfile
import unittest
from unittest import mock
from xml.etree import ElementTree as ET
import zipfile


ROOT = Path(__file__).resolve().parents[2]
EVIDENCE = ROOT / "evidence/r14-p39-30"
spec = importlib.util.spec_from_file_location("ceiling_hosted", ROOT / "scripts/ci/ceiling_hosted.py")
hosted = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hosted)
REPOSITORY = "banodoco/astrid-agent"
HEAD = "b1e53baf5facbb340dfc42b1688f7152c255e351"
EVIDENCE_ERROR = (hosted.core.InvalidEvidence, hosted.adapters.core.InvalidEvidence)


class BunSelectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        archive = EVIDENCE / "original-launchers-14581372377.zip"
        self.assertEqual(hashlib.sha256(archive.read_bytes()).hexdigest(),
                         "d09886c2c17d4215f829ee15aad20aa097023547815ccda9297f50abcde5a315")
        with zipfile.ZipFile(archive) as source:
            source.extractall(self.root)
        checks = json.loads((ROOT / "ci/required-checks.json").read_text())
        self.job_spec = next(row for row in checks["merge"] if row["job"] == "windows-launchers")
        self.raw = json.loads((EVIDENCE / "original-launchers-job.json").read_text())
        self.run = {"id": 44626225586, "run_attempt": 1}

    def expected(self, step):
        return next(row for row in self.job_spec["commands"] if row["id"] == step)

    def suite(self, step="step-04", raw_exit=0):
        directory = self.root / step
        expected = self.expected(step)
        command = hosted.trusted_bun_command(self.job_spec, expected, directory)
        return hosted.adapters.bun_junit(directory / "bun-results.xml", directory / "command.log",
                                          expected["lane"], step, raw_exit, command)

    def job(self):
        return hosted.normalized_job(self.job_spec, self.raw, self.run, [self.root],
                                     REPOSITORY, HEAD, ROOT, {REPOSITORY: HEAD})

    def xml(self, change):
        path = self.root / "step-04/bun-results.xml"
        tree = ET.parse(path)
        change(tree.getroot())
        tree.write(path, encoding="utf-8", xml_declaration=True)

    def log(self, change):
        path = self.root / "step-04/command.log"
        path.write_text(change(path.read_text()))

    def test_original_35_identities_and_every_native_outcome_are_preserved(self):
        ledger = json.loads((EVIDENCE / "per-id-ledger.json").read_text())
        for step, counts in (("step-03", (24, 23, 1, 0)), ("step-04", (1, 1, 0, 10))):
            with self.subTest(step=step):
                suite = self.suite(step)
                native = ledger["steps"][step]["cases"]
                self.assertEqual(suite["collected"], [row["native_id"] for row in native])
                outcomes = {"pass": "passed", "skipped": "skipped", "fail": "failed"}
                self.assertEqual([row["outcome"] for row in suite["tests"]],
                                 [outcomes[row["junit_outcome"]] for row in native])
                selection = suite["selection"]
                self.assertEqual(tuple(len(selection[key]) for key in
                                       ("selected", "executed", "declared_skips", "deliberately_unselected")), counts)
                self.assertEqual(suite["native_summary_count"], counts[0])
                self.assertEqual(suite["summary_count"], len(native))
                self.assertEqual(suite["raw_exit"], 0)
                self.assertEqual(selection["command_sha256"], ledger["steps"][step]["command_receipt"]["command_sha256"])
                hosted.core.verify_suite(suite, suite["lane"])

    def test_original_complete_job_normalizes_without_hiding_filtered_ids(self):
        job = self.job()
        self.assertTrue(job["commands_complete"])
        self.assertEqual(job["unexplained_failed_steps"], [])
        self.assertEqual([len(suite["tests"]) for suite in job["suites"]], [24, 11])
        self.assertEqual(sum(row["selection_status"] == "deliberately-unselected"
                             for suite in job["suites"] for row in suite["tests"]), 10)

    def test_filtered_receipt_needs_trusted_selection(self):
        directory = self.root / "step-04"
        with self.assertRaises(EVIDENCE_ERROR):
            hosted.adapters.bun_junit(directory / "bun-results.xml", directory / "command.log", "lane", "step", 0)

    def test_changed_command_cannot_authorize_its_own_filter(self):
        for replacement in ('-t "model"', '-t "never matches"', ''):
            with self.subTest(replacement=replacement):
                path = self.root / "step-04/command.txt"
                original = path.read_text()
                path.write_text(original.replace('-t "model discovery allows sequential OMP startup"', replacement))
                receipt_path = path.with_name("command.json")
                receipt = json.loads(receipt_path.read_text())
                receipt["command_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
                receipt_path.write_text(json.dumps(receipt))
                with self.assertRaisesRegex(EVIDENCE_ERROR, "differs from trusted workflow selection"):
                    self.job()
                path.write_text(original)

    def test_command_digest_mismatch_still_rejects(self):
        path = self.root / "step-04/command.txt"
        path.write_text(path.read_text() + " ")
        with self.assertRaisesRegex(EVIDENCE_ERROR, "digest mismatch"):
            self.job()

    def test_missing_trusted_workflow_fails_closed(self):
        with mock.patch.object(hosted, "TRUSTED_ROOT", self.root):
            with self.assertRaisesRegex(EVIDENCE_ERROR, "snapshot unavailable"):
                self.suite()

    def test_unsupported_regex_is_not_approximated(self):
        directory = self.root / "step-04"
        with self.assertRaisesRegex(EVIDENCE_ERROR, "unsupported trusted Bun selection"):
            hosted.adapters.bun_junit(directory / "bun-results.xml", directory / "command.log", "lane", "step", 0,
                                       'bun test file.test.ts -t "model.*"')

    def test_missing_or_duplicate_terminal_finish_rejects(self):
        path = self.root / "step-04/command.log"
        original = path.read_text()
        for text in (original[:original.index("Ran ")], original + "Ran 1 test across 1 file. [1s]\n"):
            path.write_text(text)
            with self.assertRaisesRegex(EVIDENCE_ERROR, "terminal summary"):
                self.suite()

    def test_missing_or_wrong_selected_terminal_id_rejects(self):
        path = self.root / "step-04/command.log"
        original = path.read_text()
        for text in ("\n".join(line for line in original.splitlines() if not line.startswith("(pass)")),
                     original.replace("model discovery allows", "a different test allows")):
            path.write_text(text)
            with self.assertRaisesRegex(EVIDENCE_ERROR, "selected/terminal ID"):
                self.suite()

    def test_each_lost_junit_identity_rejects_without_a_count_rewrite(self):
        path = self.root / "step-04/bun-results.xml"
        original = path.read_bytes()
        for index in range(11):
            with self.subTest(index=index):
                tree = ET.fromstring(original)
                suite = tree.find("testsuite")
                suite.remove(suite.findall("testcase")[index])
                path.write_bytes(ET.tostring(tree))
                with self.assertRaises(EVIDENCE_ERROR):
                    self.suite()

    def test_terminal_filtered_and_outcome_counts_cannot_hide_loss(self):
        path = self.root / "step-04/command.log"
        original = path.read_text()
        for text in (original.replace("10 filtered out", "9 filtered out"),
                     original.replace("10 filtered out", ""),
                     original.replace("1 pass", "2 pass"), original.replace("0 fail", "1 fail")):
            path.write_text(text)
            with self.assertRaises(EVIDENCE_ERROR):
                self.suite()

    def test_unselected_case_cannot_be_reported_as_executed(self):
        self.xml(lambda root: root.find("testsuite").findall("testcase")[1].remove(
            root.find("testsuite").findall("testcase")[1].find("skipped")))
        with self.assertRaisesRegex(EVIDENCE_ERROR, "unselected Bun case has an executed outcome"):
            self.suite()

    def test_zero_selected_tests_cannot_qualify_even_with_consistent_counts(self):
        def remove_selected(root):
            suite = root.find("testsuite")
            suite.remove(suite.find("testcase"))
            root.set("tests", "10")
            suite.set("tests", "10")
        self.xml(remove_selected)
        self.log(lambda text: "\n".join(line for line in text.splitlines() if not line.startswith("(pass)"))
                 .replace("1 pass", "0 pass").replace("Ran 1 test", "Ran 0 tests") + "\n")
        with self.assertRaisesRegex(EVIDENCE_ERROR, "no selected tests"):
            self.suite()

    def test_failure_and_its_diagnostic_and_exit_remain_real_red(self):
        self.xml(lambda root: ET.SubElement(root.find("testsuite/testcase"), "failure", {"type": "AssertionError"}))
        self.log(lambda text: text.replace("(pass)", "error: fixture retained failure\n(fail)").replace("1 pass", "0 pass").replace("0 fail", "1 fail"))
        suite = self.suite(raw_exit=1)
        self.assertEqual(suite["tests"][0]["outcome"], "failed")
        self.assertIn("fixture retained failure", suite["tests"][0]["diagnostic"])
        self.assertEqual(suite["raw_exit"], 1)
        hosted.core.verify_suite(suite, suite["lane"])
        suite["raw_exit"] = 0
        with self.assertRaisesRegex(EVIDENCE_ERROR, "successful raw exit"):
            hosted.core.verify_suite(suite, suite["lane"])

    def test_nonzero_or_interrupted_exit_without_failure_still_rejects(self):
        for raw_exit in (1, 137):
            suite = self.suite(raw_exit=raw_exit)
            with self.assertRaises(EVIDENCE_ERROR):
                hosted.core.verify_suite(suite, suite["lane"])

    def test_existing_comparator_still_refuses_unapproved_nonexecution(self):
        job = self.job()
        for tier in ("merge", "release"):
            policy = hosted.core.load_policy(ROOT, REPOSITORY)
            report = {"schema": 1, "repository": REPOSITORY, "head": HEAD, "tier": tier,
                      "tuple": {REPOSITORY: HEAD}, "complete": True, "jobs": [job]}
            verdict = hosted.core.evaluate(policy, copy.deepcopy(policy), report, repository=REPOSITORY,
                                            head=HEAD, tier=tier, expected_tuple={REPOSITORY: HEAD},
                                            now=dt.datetime.fromisoformat(job["started_at"].replace("Z", "+00:00")))
            self.assertEqual(verdict["verdict"], "FAIL")
            self.assertTrue(any("new skip/xfail/deselection" in message for message in verdict["errors"]))
            self.assertTrue(any("missing required job" in message for message in verdict["unknown"]))

    def test_unfiltered_linux_control_preserves_existing_duplicate_refusal(self):
        with zipfile.ZipFile(EVIDENCE / "original-trusted-refusal-14566167356.zip") as source:
            prefix = "artifact-14560192158/files/step-06/"
            for name in ("bun-results.xml", "command.log", "command.txt"):
                (self.root / name).write_bytes(source.read(prefix + name))
        suite = hosted.adapters.bun_junit(self.root / "bun-results.xml", self.root / "command.log",
                                          "Chat service/chat", "step-06", 0, (self.root / "command.txt").read_text())
        self.assertEqual(Counter(row["outcome"] for row in suite["tests"]), {"passed": 208, "skipped": 2})
        self.assertEqual(len(suite["collected"]), 210)
        self.assertEqual(len(set(suite["collected"])), 209)
        with self.assertRaisesRegex(EVIDENCE_ERROR, "duplicate collected IDs"):
            hosted.core.verify_suite(suite, suite["lane"])

    def test_archived_native_failure_retains_exact_identity_diagnostic_and_exit(self):
        with tarfile.open(ROOT / "ci/evidence/p39-k2-k3/native-adapter-probes.tar.gz") as source:
            for name in ("bun-results.xml", "command.log", "exit.json", "normalized.json"):
                (self.root / name).write_bytes(source.extractfile("native-current/bun/" + name).read())
        original = json.loads((self.root / "normalized.json").read_text())
        suite = hosted.adapters.bun_junit(self.root / "bun-results.xml", self.root / "command.log",
                                          original["lane"], original["name"], original["raw_exit"])
        self.assertEqual(suite["raw_exit"], 1)
        self.assertEqual([{key: value for key, value in row.items() if key != "selection_status"}
                          for row in suite["tests"]], original["tests"])
        self.assertEqual(Counter(row["outcome"] for row in suite["tests"]), {"passed": 1, "failed": 1})
        hosted.core.verify_suite(suite, suite["lane"])

    def test_trusted_snapshot_exports_the_original_workflow(self):
        source = (ROOT / ".github/workflows/failure-ceiling.yml").read_text()
        self.assertIn('git archive "$base" ci scripts/ci .github/workflows', source)


if __name__ == "__main__":
    unittest.main()
