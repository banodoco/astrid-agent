import datetime as dt
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/ci"))
import regression_green as evaluator


SCHEMAS = Path("/home/user/repo/docs/merge-plan/schemas")


class RegressionGreenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.today = dt.date(2026, 11, 1)
        self.tuple = {
            "version": 1, "name": "synthetic", "repos": {
                repo: {"url": "https://github.com/banodoco/astrid-" + repo, "sha": char * 40}
                for repo, char in zip(evaluator.REPOS, "abcd")
            }, "generated": {}, "toolchain": {}, "created_at": "2026-11-01T00:00:00Z",
        }
        self.identity = evaluator.canonical_digest(self.tuple)
        self.shas = {repo: value["sha"] for repo, value in self.tuple["repos"].items()}
        self.key = ("agent", "unit-1", "linux")
        self.inventory = {"tuple_digest": self.identity, "lanes": [{"repo": self.key[0], "lane": self.key[1], "platform": self.key[2],
                                                                         "tests": [{"node_id": "test_a", "protected": False}]}]}
        self.row = {"id": "KF-1", "repo": "agent", "lane": "unit-1", "platform": "linux", "node_id": "test_a",
                    "outcome": "failed", "phase": "call", "fingerprint": "AssertionError: expected", "semantics_hash": "hash-a",
                    "baseline": {"tuple_digest": "parent", "run": "run-1"}, "provenance": "integration", "owner": "M-CI",
                    "rationale": "synthetic", "status": "admitted", "expires": "2026-11-02"}
        self.write_json("tuple.json", self.tuple)
        self.write_json("inventory.json", self.inventory)
        self.write_json("freshness.json", {"check": "C-07", "result": "pass", "tuple_digest": self.identity, "repo_shas": self.shas})
        self.write_ledger([])
        self.junit()

    def write_json(self, name, value):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding="utf-8")
        return path

    def write_ledger(self, rows):
        (self.root / "ledger.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")

    def junit(self, outcome="passed", node="test_a", phase="call", fingerprint=None, semantics="hash-a"):
        suite = ET.Element("testsuite")
        case = ET.SubElement(suite, "testcase", name=node)
        properties = ET.SubElement(case, "properties")
        values = {"node_id": node}
        if outcome == "failed":
            ET.SubElement(case, "failure")
            values.update(outcome="failed", phase=phase, fingerprint=fingerprint or self.row["fingerprint"], semantics_hash=semantics)
        elif outcome == "skipped":
            ET.SubElement(case, "skipped")
        for key, value in values.items():
            ET.SubElement(properties, "property", name=key, value=value)
        xml = ET.tostring(suite)
        target = self.root / "evidence" / evaluator.canonical_digest(list(self.key))
        target.mkdir(parents=True, exist_ok=True)
        (target / "junit.xml").write_bytes(xml)
        self.write_json("evidence/" + evaluator.canonical_digest(list(self.key)) + "/manifest.json", {
            "repo": self.key[0], "lane": self.key[1], "platform": self.key[2], "tuple_digest": self.identity,
            "repo_shas": self.shas, "junit_sha256": hashlib.sha256(xml).hexdigest(),
        })

    def run_evaluator(self):
        return evaluator.evaluate(self.root / "tuple.json", self.root / "inventory.json", self.root / "ledger.jsonl", self.root / "evidence",
                                  self.root / "freshness.json", SCHEMAS / "tuple.schema.json", SCHEMAS / "ledger.schema.json", self.today)

    def assert_red(self):
        self.assertTrue(self.run_evaluator()[0])

    def test_missing_new_and_expired_evidence_are_red(self):
        self.junit("failed")
        self.assert_red()  # new failure
        self.row["expires"] = "2026-10-31"
        self.write_ledger([self.row])
        self.assert_red()
        (self.root / "evidence" / evaluator.canonical_digest(list(self.key)) / "junit.xml").unlink()
        self.assert_red()

    def test_admitted_failure_and_resolved_retirement(self):
        self.junit("failed")
        self.write_ledger([self.row])
        self.assertEqual(self.run_evaluator(), ([], []))
        self.junit()
        errors, retirement = self.run_evaluator()
        self.assertFalse(errors)
        self.assertIn("KF-1", retirement[0])

    def test_renamed_skipped_protected_and_collection_are_red(self):
        self.junit("failed", node="renamed")
        self.write_ledger([self.row])
        self.assert_red()
        self.inventory["lanes"][0]["tests"][0]["protected"] = True
        self.write_json("inventory.json", self.inventory)
        self.junit("skipped")
        self.assert_red()
        self.junit("failed", phase="collection")
        self.assert_red()

    def test_missing_shard_wrong_tuple_and_corrupt_junit_are_red(self):
        self.inventory["lanes"].append({"repo": "agent", "lane": "unit-2", "platform": "linux", "tests": [{"node_id": "test_a", "protected": False}]})
        self.write_json("inventory.json", self.inventory)
        self.assert_red()
        self.inventory["lanes"].pop()
        self.write_json("inventory.json", self.inventory)
        self.write_json("freshness.json", {"check": "C-07", "result": "pass", "tuple_digest": "0" * 64, "repo_shas": self.shas})
        self.assert_red()
        self.write_json("freshness.json", {"check": "C-07", "result": "pass", "tuple_digest": self.identity, "repo_shas": self.shas})
        target = self.root / "evidence" / evaluator.canonical_digest(list(self.key)) / "junit.xml"
        target.write_text("<testsuite>", encoding="utf-8")
        self.assert_red()

    def test_non_green_c07_receipt_is_red(self):
        self.write_json("freshness.json", {"check": "C-07", "result": "fail", "tuple_digest": self.identity, "repo_shas": self.shas})
        self.assert_red()


if __name__ == "__main__":
    unittest.main()
