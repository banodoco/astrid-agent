import datetime as dt
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
import xml.etree.ElementTree as ET

MODULE = Path(__file__).resolve().parents[2] / "scripts/ci/regression_green.py"
spec = importlib.util.spec_from_file_location("regression_green", MODULE)
evaluator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(evaluator)


class RegressionGreenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.today = dt.date(2026, 10, 31)
        self.tuple = {"version": 1, "name": "synthetic", "repos": {r: {"url": "test", "sha": str(i) * 40} for i, r in enumerate(evaluator.REPOS, 1)}, "generated": {}, "toolchain": {}, "created_at": "2026-10-31T00:00:00Z"}
        self.identity = evaluator.digest(self.tuple)
        self.shas = {r: x["sha"] for r, x in self.tuple["repos"].items()}
        self.key = ("agent", "unit-1", "linux")
        self.inventory = {"tuple_digest": self.identity, "lanes": [{"repo": self.key[0], "lane": self.key[1], "platform": self.key[2], "tests": [{"node_id": "test_a", "protected": False}]}]}
        self.row = {"id": "KF-1", "repo": "agent", "lane": "unit-1", "platform": "linux", "node_id": "test_a", "outcome": "failed", "phase": "call", "fingerprint": "AssertionError: expected / test_a", "semantics_hash": "hash-a", "baseline": {"tuple_digest": "parent", "run": "run-1"}, "provenance": "release1-main", "owner": "M-AG", "rationale": "known", "status": "admitted", "expires": "2026-11-01"}
        self.write_json("tuple.json", self.tuple)
        self.write_json("inventory.json", self.inventory)
        self.write_json("freshness.json", {"check": "C-07", "result": "pass", "tuple_digest": self.identity, "repo_shas": self.shas})
        self.write_ledger([])
        self.junit()

    def write_json(self, name, data):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

    def write_ledger(self, rows):
        (self.root / "ledger.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))

    def junit(self, outcome="passed", node="test_a", phase="call", fingerprint=None, semantics="hash-a"):
        suite = ET.Element("testsuite")
        case = ET.SubElement(suite, "testcase", name=node)
        props = ET.SubElement(case, "properties")
        values = {"node_id": node}
        if outcome == "failed":
            ET.SubElement(case, "failure")
            values.update(outcome="failed", phase=phase, fingerprint=fingerprint or self.row["fingerprint"], semantics_hash=semantics)
        elif outcome == "skipped":
            ET.SubElement(case, "skipped")
        for key, value in values.items():
            ET.SubElement(props, "property", name=key, value=value)
        content = ET.tostring(suite)
        directory = self.root / "lanes" / evaluator.digest(list(self.key))
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "junit.xml").write_bytes(content)
        (directory / "manifest.json").write_text(json.dumps({"repo": self.key[0], "lane": self.key[1], "platform": self.key[2], "tuple_digest": self.identity, "repo_shas": self.shas, "junit_sha256": hashlib.sha256(content).hexdigest()}))

    def run_evaluator(self):
        return evaluator.evaluate(self.root / "tuple.json", self.root / "inventory.json", self.root / "ledger.jsonl", self.root / "lanes", self.root / "freshness.json", self.today)

    def assert_red(self):
        self.assertTrue(self.run_evaluator()[0])

    def test_new_failure_is_red(self):
        self.junit("failed")
        self.assert_red()

    def test_admitted_failure_is_green(self):
        self.junit("failed")
        self.write_ledger([self.row])
        self.assertEqual(self.run_evaluator(), ([], []))

    def test_expired_row_is_red(self):
        self.row["expires"] = "2026-10-30"
        self.junit("failed")
        self.write_ledger([self.row])
        self.assert_red()

    def test_expiration_date_is_inclusive(self):
        self.row["expires"] = self.today.isoformat()
        self.junit("failed")
        self.write_ledger([self.row])
        self.assertFalse(self.run_evaluator()[0])

    def test_rename_without_mapping_is_red(self):
        self.junit("failed", node="renamed")
        self.write_ledger([self.row])
        self.assert_red()

    def test_missing_shard_is_red(self):
        lane = dict(self.inventory["lanes"][0], lane="unit-2")
        self.inventory["lanes"].append(lane)
        self.write_json("inventory.json", self.inventory)
        self.assert_red()

    def test_skipped_protected_test_is_red(self):
        self.inventory["lanes"][0]["tests"][0]["protected"] = True
        self.write_json("inventory.json", self.inventory)
        self.junit("skipped")
        self.assert_red()

    def test_protected_failure_cannot_be_waived(self):
        self.inventory["lanes"][0]["tests"][0]["protected"] = True
        self.write_json("inventory.json", self.inventory)
        self.junit("failed")
        self.write_ledger([self.row])
        self.assert_red()

    def test_resolved_row_flagged_for_retirement(self):
        self.write_ledger([self.row])
        errors, retirement = self.run_evaluator()
        self.assertFalse(errors)
        self.assertIn("KF-1", retirement[0])

    def test_missing_inputs_fail_closed(self):
        for path in ("tuple.json", "inventory.json", "ledger.jsonl", "freshness.json"):
            with self.subTest(path=path):
                file = self.root / path
                saved = file.read_bytes()
                file.unlink()
                self.assert_red()
                file.write_bytes(saved)
        (self.root / "lanes" / evaluator.digest(list(self.key)) / "junit.xml").unlink()
        self.assert_red()

    def test_wrong_tuple_and_corrupt_evidence_are_red(self):
        self.write_json("freshness.json", {"check": "C-07", "result": "pass", "tuple_digest": "wrong", "repo_shas": self.shas})
        self.assert_red()

    def test_different_failure_identity_or_semantics_is_red(self):
        self.write_ledger([self.row])
        for kw in ({"phase": "setup"}, {"fingerprint": "other"}, {"semantics": "changed"}):
            with self.subTest(kw=kw):
                self.junit("failed", **kw)
                self.assert_red()

    def test_proposed_row_is_red(self):
        self.row["status"] = "proposed"
        self.write_ledger([self.row])
        self.junit("failed")
        self.assert_red()

    def test_missing_test_is_red(self):
        self.inventory["lanes"][0]["tests"].append({"node_id": "test_missing", "protected": False})
        self.write_json("inventory.json", self.inventory)
        self.assert_red()

    def test_exact_tuple_and_junit_hash_are_required(self):
        directory = self.root / "lanes" / evaluator.digest(list(self.key))
        manifest = json.loads((directory / "manifest.json").read_text())
        manifest["repo_shas"]["runtime"] = "f" * 40
        (directory / "manifest.json").write_text(json.dumps(manifest))
        self.assert_red()
        self.junit()
        (directory / "junit.xml").write_text("<testsuite/>")
        self.assert_red()

    def test_empty_inventory_and_failed_freshness_are_red(self):
        self.write_json("inventory.json", {"lanes": []})
        self.assert_red()
        self.write_json("inventory.json", self.inventory)
        self.write_json("freshness.json", {"check": "C-07", "result": "fail", "tuple_digest": self.identity, "repo_shas": self.shas})
        self.assert_red()

    def test_reviewed_rename_requires_new_exact_identity(self):
        self.inventory["lanes"][0]["tests"][0]["node_id"] = "renamed"
        self.write_json("inventory.json", self.inventory)
        self.row.update(node_id="renamed", renamed_from="test_a", admitted_by="M-INT")
        self.write_ledger([self.row])
        self.junit("failed", node="renamed")
        self.assertFalse(self.run_evaluator()[0])
        del self.row["admitted_by"]
        self.write_ledger([self.row])
        self.assert_red()

    def test_collection_failure_is_red(self):
        self.row["phase"] = "collection"
        self.write_ledger([self.row])
        self.junit("failed", phase="collection")
        self.assert_red()


if __name__ == "__main__":
    unittest.main()
