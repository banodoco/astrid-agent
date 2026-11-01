"""Receipt and workflow tests that prevent a T-11 false green."""

from __future__ import annotations

import json
from pathlib import Path
import unittest

import jsonschema
import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
SCHEMA_PATH = REPO_ROOT / ".github/scripts/t11_receipt_schema.json"
HARNESS_PATH = REPO_ROOT / ".github/scripts/t11_harness.py"
WORKFLOW_PATH = REPO_ROOT / ".github/workflows/t11.yml"
TUPLE = {
    "agent": "ddd5d278796641901bd11508863a8ea21e6c9697",
    "runtime": "1a275bb339ae897e52a5be5e1da99e846e402037",
    "app": "7f6f99208100e895efebf39c70bc30af6cee0884",
    "pinokio": "89ed7aacae72bb48eb97ff9ff95c222bdd7e71e4",
}


def _refusal_receipt() -> dict:
    return {
        "status": "not-qualified",
        "qualification": False,
        "phase": "live-observation-unavailable",
        "observations": {
            "requested_tuple": dict(TUPLE),
            "schema_consumption": "unobserved",
            "composition_execution": "unobserved",
            "same_process_observer": {
                "status": "refused-no-product-seam",
                "qualification": False,
                "same_process_observation": {
                    "schema_consumption": {"status": "unobserved"},
                    "composition_execution": {"status": "unobserved"},
                },
            },
        },
    }


class T11HarnessContractTests(unittest.TestCase):
    def setUp(self):
        self.schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
        jsonschema.Draft202012Validator.check_schema(self.schema)
        self.validator = jsonschema.Draft202012Validator(self.schema)

    def test_refusal_receipt_is_valid(self):
        self.validator.validate(_refusal_receipt())

    def test_unobserved_consumption_can_never_be_qualified(self):
        receipt = _refusal_receipt()
        receipt["status"] = "qualified"
        receipt["qualification"] = True
        with self.assertRaises(jsonschema.ValidationError):
            self.validator.validate(receipt)

    def test_harness_requires_the_observer_and_all_four_tuple_members(self):
        source = HARNESS_PATH.read_text(encoding="utf-8")
        self.assertIn("'pinokio'", source)
        self.assertIn("t11_example_render.py", source)
        self.assertIn("t11_observer.py", source)
        self.assertIn("same-process observer did not fail closed", source)
        self.assertIn("return 2", source)

    def test_workflow_wires_the_pinokio_identity_and_observer_artifact(self):
        document = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
        dispatch = document.get("on", document.get(True))
        inputs = dispatch["workflow_dispatch"]["inputs"]
        self.assertIn("pinokio_sha", inputs)
        rendered = WORKFLOW_PATH.read_text(encoding="utf-8")
        self.assertIn("PINOKIO_SHA", rendered)
        self.assertIn("t11-observer-receipt", rendered)


if __name__ == "__main__":
    unittest.main()
