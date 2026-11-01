"""Synthetic rejection coverage for the installed-product receipt evaluator."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("installed_smoke", ROOT / "scripts/ci/installed_smoke.py")
assert SPEC and SPEC.loader
smoke = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(smoke)


IDENTITIES = {
    "runtime": "47e5949465cb6f3afdfd1cb8a0e90009fd9d4020",
    "agent": "af5ae8afdb9829beb2bba4f811dbe97765bd72b2",
    "vibecomfy": "3bebae39655eb460454885040be4d3aefaf2ef31",
    "app": "a55bec9ff384b5bbfee5535962880084ab2adb1b",
    "pinokio": "89ed7aacae72bb48eb97ff9ff95c222bdd7e71e4",
}


class InstalledSmokeReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.receipt = self._receipt()

    def tearDown(self):
        self.tmp.cleanup()

    def _put(self, relative: str, text: str) -> dict[str, str]:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return {"path": relative, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}

    def _manifest(self) -> str:
        return json.dumps({
            "schema_version": 1,
            "agent": {"sha": IDENTITIES["agent"], "version": "0.1.0"},
            "runtime": {"sha": IDENTITIES["runtime"], "schema_digest": "sha256:test"},
            "vibecomfy": {"sha": IDENTITIES["vibecomfy"]},
            "tools": {},
        }, sort_keys=True) + "\n"

    def _receipt(self) -> dict:
        artifacts = {
            "requested_manifest": self._put("install-manifest.json", self._manifest()),
            "installed_manifest": self._put("installed/manifest.json", self._manifest()),
        }
        commands = {}
        for name in smoke.REQUIRED_COMMANDS:
            expected = "astrid/0.1.0\n" if name == "version" else None
            record = self._put(f"commands/{name}.log", expected or "ok\n")
            commands[name] = {"argv": ["astrid", name], "exit_code": 0, **record}
            if expected:
                commands[name]["expected_stdout"] = expected
        return {
            "format": smoke.FORMAT,
            "identities": dict(IDENTITIES),
            "runner_checkouts": {
                "agent": "c" * 40,
                "runtime": IDENTITIES["runtime"],
                "app": IDENTITIES["app"],
                "pinokio": IDENTITIES["pinokio"],
            },
            "tuple_sha256": hashlib.sha256(
                json.dumps(IDENTITIES, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "environment": {"PYTHONPATH": "unset"},
            "artifacts": artifacts,
            "commands": commands,
            "VERDICT": "PASS",
        }

    def errors(self):
        return smoke.evaluate_receipt(self.receipt, self.root, IDENTITIES)

    def test_passes_complete_receipt(self):
        self.assertEqual(self.errors(), [])

    def test_missing_artifact_is_rejected(self):
        (self.root / "installed/manifest.json").unlink()
        self.receipt["VERDICT"] = "FAIL"
        self.assertTrue(any("installed manifest artifact is missing" in item for item in self.errors()))

    def test_stale_tuple_is_rejected(self):
        self.receipt["identities"]["runtime"] = "a" * 40
        self.receipt["VERDICT"] = "FAIL"
        self.assertTrue(any("receipt runtime identity is stale" in item for item in self.errors()))

    def test_wrong_artifact_identity_is_rejected(self):
        bad = json.loads(self._manifest())
        bad["agent"]["sha"] = "b" * 40
        self.receipt["artifacts"]["installed_manifest"] = self._put(
            "installed/manifest.json", json.dumps(bad, sort_keys=True) + "\n"
        )
        self.receipt["VERDICT"] = "FAIL"
        self.assertTrue(any("installed manifest has wrong agent artifact identity" in item for item in self.errors()))

    def test_failed_inner_command_is_rejected(self):
        self.receipt["commands"]["generate_list"]["exit_code"] = 1
        self.receipt["VERDICT"] = "FAIL"
        self.assertTrue(any("generate_list inner command failed with exit 1" in item for item in self.errors()))
