import json
import hashlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


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

    def test_aggregate_calls_reviewed_evaluator_once_only_after_completed_results(self):
        calls = []
        result = aggregate.evaluate_sealed(self.sealed(), lambda value: calls.append(value) or {"result": "pass", "reasons": []})
        self.assertEqual(result, {"result": "pass", "reasons": []})
        self.assertEqual(len(calls), 1)

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

    def test_unapproved_qual_bytes_are_red(self):
        predicate = self.root / "predicate.py"
        predicate.write_text("def check_m4_6(evidence):\n    return True, []\n", encoding="utf-8")
        features = self.root / "features.json"
        features.write_text(json.dumps([]), encoding="utf-8")
        self.assertIn("unapproved", aggregate._validate_features(features, predicate, "0" * 64, {"generated": {}})[0])

    def test_qual_package_relative_import_and_frozen_m4_6_candidate_binding(self):
        predicate = self.root / "journey_predicates.py"
        support = self.root / "support.py"
        support.write_text(
            "def check(evidence):\n"
            "    candidate = evidence['candidate']\n"
            "    expected = {'app': 'a' * 40, 'agent': 'b' * 40, 'runtime': 'c' * 40, 'pinokio': 'd' * 40, 'manifest_sha256': 'e' * 64}\n"
            "    return candidate == expected, [] if candidate == expected else ['candidate differs']\n",
            encoding="utf-8",
        )
        predicate.write_text(
            "from .support import check\n\n"
            "def check_m4_6(evidence):\n"
            "    return check(evidence)\n",
            encoding="utf-8",
        )
        candidate = {"app": "a" * 40, "agent": "b" * 40, "runtime": "c" * 40,
                     "pinokio": "d" * 40, "manifest_sha256": "e" * 64}
        features = self.root / "features.json"
        features.write_text(json.dumps([{"scenario": "m4_6", "slot_id": "direct-linux", "candidate": candidate, "files": []}]), encoding="utf-8")
        frozen_tuple = {"repos": {name: {"sha": value} for name, value in candidate.items() if name != "manifest_sha256"},
                        "generated": {"manifest_sha256": candidate["manifest_sha256"]}}
        predicate_sha256 = hashlib.sha256(predicate.read_bytes()).hexdigest()
        with mock.patch.object(aggregate, "QUAL_PREDICATE_SHA256", predicate_sha256):
            self.assertEqual(aggregate._validate_features(features, predicate, predicate_sha256, frozen_tuple), [])
            candidate["agent"] = "f" * 40
            features.write_text(json.dumps([{"scenario": "m4_6", "slot_id": "direct-linux", "candidate": candidate, "files": []}]), encoding="utf-8")
            errors = aggregate._validate_features(features, predicate, predicate_sha256, frozen_tuple)
        self.assertIn("not derived", errors[0])


if __name__ == "__main__":
    unittest.main()
