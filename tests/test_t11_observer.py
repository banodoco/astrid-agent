"""Focused contract tests for the fail-closed T-11 observer."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


REPO_ROOT = Path(__file__).resolve().parents[1]
OBSERVER_PATH = REPO_ROOT / ".github/scripts/t11_observer.py"
PUBLISHED_AGENT_SHA = "ddd5d278796641901bd11508863a8ea21e6c9697"
PUBLISHED_RUNTIME_SHA = "1a275bb339ae897e52a5be5e1da99e846e402037"
PUBLISHED_APP_SHA = "7f6f99208100e895efebf39c70bc30af6cee0884"
PUBLISHED_PINOKIO_SHA = "89ed7aacae72bb48eb97ff9ff95c222bdd7e71e4"


def _observer():
    spec = importlib.util.spec_from_file_location("t11_observer_test", OBSERVER_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class T11ObserverTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.observer = _observer()

    def test_published_agent_has_the_exact_refusal_seams(self):
        evidence = self.observer.inspect_missing_product_seam(REPO_ROOT)
        self.assertEqual(
            evidence["schema_probe"]["sha256"],
            "e6f3834f470c532230473a17825baafe774a298b36a980a59343dfdea31da4b8",
        )
        self.assertEqual(
            evidence["host_worker_launch"]["sha256"],
            "30c10589c7305108639ef8dd483d54d3081d2c5bbb38b56171ca5205a6ce136c",
        )
        self.assertEqual(
            evidence["frame_worker"]["sha256"],
            "3b31d86c630c9f765f48d1c7c4f7880ea9caf2e1567ea3ac81c00d2c53124b85",
        )
        self.assertEqual(evidence["missing_seams"]["schema_consumption"]["status"], "unobserved")
        self.assertEqual(evidence["missing_seams"]["composition_execution"]["status"], "unobserved")

    def test_observer_writes_a_refusal_for_the_published_tuple(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data = root / "data"
            data.mkdir()
            profile = root / "profile.json"
            profile.write_text("{}\n", encoding="utf-8")
            example = root / "example.json"
            example.write_text(json.dumps({"render": {"run_id": "run-t11-focused"}}), encoding="utf-8")
            receipt = root / "observer.json"
            result = self.observer.main([
                "--agent-source", str(REPO_ROOT), "--data-root", str(data),
                "--profile", str(profile), "--example-receipt", str(example), "--receipt", str(receipt),
                "--agent-sha", PUBLISHED_AGENT_SHA, "--runtime-sha", PUBLISHED_RUNTIME_SHA,
                "--app-sha", PUBLISHED_APP_SHA, "--pinokio-sha", PUBLISHED_PINOKIO_SHA,
                "--schema-sha256", "a" * 64, "--composition-archive-sha256", "b" * 64,
            ])
            self.assertEqual(result, self.observer.EXIT_OBSERVATION_UNAVAILABLE)
            observed = json.loads(receipt.read_text(encoding="utf-8"))
            self.assertEqual(observed["status"], "refused-no-product-seam")
            self.assertFalse(observed["qualification"])
            self.assertEqual(observed["render_run_identity"]["run_id"], "run-t11-focused")

    def test_direct_event_contract_rejects_ambiguity(self):
        process = {"pid": 71, "start_ticks": 123, "boot_id": "boot"}
        events = [
            {
                "event": "timeline_schema_consumed", "run_id": "run-1",
                "observed_at_monotonic_ns": 100, "process": process,
                "schema": {"canonical_json_sha256": "a" * 64},
            },
            {
                "event": "composition_executed", "run_id": "run-1",
                "observed_at_monotonic_ns": 101, "process": process,
                "composition": {"archive_sha256": "b" * 64, "loaded_bundle_sha256": "c" * 64},
            },
        ]
        outcome = self.observer.validate_direct_events(
            events, run_id="run-1", schema_sha256="a" * 64,
            composition_archive_sha256="b" * 64, known_processes=[process],
        )
        self.assertEqual(outcome, {"schema_consumption": "observed", "composition_execution": "observed"})
        with self.assertRaises(self.observer.ObservationContractError):
            self.observer.validate_direct_events(
                events + [events[0]], run_id="run-1", schema_sha256="a" * 64,
                composition_archive_sha256="b" * 64, known_processes=[process],
            )


if __name__ == "__main__":
    unittest.main()
