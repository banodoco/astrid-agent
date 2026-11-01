import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts/ci"))
import regression_finalizer as finalizer


class FinalizerBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def sealed(self):
        value = {"schema_version": finalizer.INPUT_VERSION, "check": finalizer.CHECK_NAME, "batch_id": "batch-1", "generation": 1,
                 "invocation_id": "invocation-1", "tuple_digest": "a" * 64, "target_sha": "b" * 40,
                 "tuple": {"repos": {"agent": {"sha": "b" * 40}}},
                 "producer_results": [{"producer_id": "producer-1", "outcome": "completed", "result": {"test_exit_code": 0}}]}
        value["sealed_results_digest"] = finalizer.digest(value["producer_results"])
        value["sealed_input_digest"] = finalizer.digest(value)
        return value

    def test_finalizer_evaluates_only_the_sealed_result_once(self):
        sealed = self.sealed()
        calls = []
        def evaluator(received):
            calls.append(received)
            self.assertNotIn("producer_plan", received)
            self.assertNotIn("producer_command", received)
            return {"result": "pass", "reasons": []}
        status = finalizer.run(sealed, self.root / "invocation", evaluator)
        self.assertEqual(status["result"], "pass")
        self.assertEqual(calls, [sealed])
        self.assertTrue((self.root / "invocation/started.json").is_file())

    def test_default_or_malformed_evaluator_is_unconditionally_red(self):
        sealed = self.sealed()
        status = finalizer.run(sealed, self.root / "default")
        self.assertEqual(status["result"], "fail")
        self.assertIn("unavailable", status["reasons"][0])
        malformed = self.sealed()
        status = finalizer.run(malformed, self.root / "malformed", lambda _: {"wrong": True})
        self.assertEqual(status["result"], "fail")
        self.assertIn("malformed", status["reasons"][0])

    def test_start_marker_prevents_replay_and_complete_status_is_bound(self):
        sealed = self.sealed()
        status_path = self.root / "status.json"
        status = finalizer.run(sealed, self.root / "interrupted", lambda _: {"result": "fail", "reasons": ["synthetic"]}, status_path)
        self.assertEqual(finalizer.read_complete_status(status_path, sealed)["result"], "fail")
        with self.assertRaises(finalizer.AlreadyStarted):
            finalizer.run(sealed, self.root / "interrupted", lambda _: {"result": "pass", "reasons": []})
        altered = dict(status)
        altered["tuple_digest"] = "f" * 64
        status_path.write_text(json.dumps(altered), encoding="utf-8")
        with self.assertRaisesRegex(finalizer.FinalizerError, "does not bind"):
            finalizer.read_complete_status(status_path, sealed)

    def test_malformed_sealed_input_still_reaches_aggregate_failure_path(self):
        malformed = self.sealed()
        malformed["sealed_input_digest"] = "0" * 64
        with mock.patch.object(finalizer.aggregate, "evaluate_sealed", wraps=finalizer.aggregate.evaluate_sealed) as aggregate_call:
            with self.assertRaisesRegex(finalizer.FinalizerError, "digest mismatch"):
                finalizer.run(malformed, self.root / "invalid")
        aggregate_call.assert_called_once_with(malformed, None)


if __name__ == "__main__":
    unittest.main()
