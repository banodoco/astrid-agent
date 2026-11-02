"""Exercise the actual embedded installed-wheel receipt generator in isolation."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import unittest

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/lanes.yml"
STEPS = ("checkout", "receipt_init", "c07_identity", "runtime_pin", "runtime_checkout", "refs", "python", "node",
         "media", "wheel", "install", "npm", "registry", "selector")
C07_STEPS = ("installed_agent_checkout", "c07_sidecar", "c07_app_checkout", "c07_pinokio_checkout", "c07_effective")
GOOD_XML = '<testsuites><testsuite tests="1" failures="0" errors="0" skipped="0"><testcase name="ok" /></testsuite></testsuites>'


def generator():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    block = workflow.split("      - name: Collect installed-wheel receipt\n", 1)[1]
    return textwrap.dedent(block.split("          python3 - <<'PY'\n", 1)[1].split("\n          PY\n", 1)[0])


class InstalledWheelReceiptTests(unittest.TestCase):
    def make_receipt(self, junit=GOOD_XML, sidecar="matching", selector_exit="0", selector_outcome="success", c07=False, c07_outcome="success"):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            receipt = root / "receipt"
            receipt.mkdir()
            if junit is not None:
                (receipt / "selector.junit.xml").write_text(junit, encoding="utf-8")
            if selector_exit is not None:
                (receipt / "selector.exit").write_text(selector_exit, encoding="utf-8")
            remotion = root / "remotion"
            remotion.mkdir()
            (remotion / ".astrid-registry-state.json").write_text('{}', encoding="utf-8")
            (remotion / "package.json").write_text(json.dumps({"dependencies": {"@banodoco/timeline-composition": "file:artifact.tgz"}}))
            tarball = remotion / "artifact.tgz"
            tarball.write_bytes(b"composition fixture")
            if sidecar is not None:
                digest = hashlib.sha256(tarball.read_bytes()).hexdigest() if sidecar == "matching" else sidecar
                (remotion / "artifact.tgz.sha256").write_text(digest + "\n")
            env = os.environ.copy()
            env.update({"ASTRID_INSTALLED_RECEIPT_DIR": str(receipt), "GITHUB_SHA": "harness-sha"})
            env.update({"OUTCOME_" + name.upper(): "success" for name in STEPS})
            env.update({"OUTCOME_" + name.upper(): c07_outcome if c07 else "skipped" for name in C07_STEPS})
            env["C07_MODE"] = "c07" if c07 else "normal"
            env["OUTCOME_SELECTOR"] = selector_outcome
            subprocess.run([sys.executable, "-c", generator()], cwd=root, env=env, check=True, capture_output=True, text=True)
            result = json.loads((receipt / "RECEIPT.json").read_text())
            sums = (receipt / "SHA256SUMS").read_bytes()
            self.assertEqual(hashlib.sha256(sums).hexdigest(), result["artifact_manifest"]["sha256"])
            entries = dict(line.split("  ", 1)[::-1] for line in sums.decode().splitlines())
            self.assertIn("artifacts/.astrid-registry-state.json", entries)
            for path, digest in entries.items():
                self.assertEqual(hashlib.sha256((receipt / path).read_bytes()).hexdigest(), digest)
            return result

    def test_matching_sidecar_and_valid_junit_are_green(self):
        receipt = self.make_receipt()
        self.assertEqual(receipt["status"], "green")
        self.assertTrue(receipt["tuple"]["composition"]["sidecar_matches"])
        self.assertEqual(receipt["installed_selector"]["junit"]["tests"], 1)

    def test_unavailable_invalid_or_zero_test_junit_fails_closed(self):
        for junit in (None, "", "<testsuite", '<testsuite tests="0" failures="0" errors="0" skipped="0"/>', '<testsuite tests="1" failures="0" errors="0" skipped="0"/>', '<other tests="1"/>', '<testsuite tests="1" failures="-1" errors="0" skipped="0"/>'):
            with self.subTest(junit=junit):
                receipt = self.make_receipt(junit=junit)
                self.assertEqual(receipt["status"], "failed")
                self.assertEqual(receipt["installed_selector"]["result"], "failed")

    def test_failures_errors_skips_and_contradictory_elements_fail_closed(self):
        for key in ("failures", "errors", "skipped"):
            with self.subTest(key=key):
                self.assertEqual(self.make_receipt(junit=GOOD_XML.replace(f'{key}="0"', f'{key}="1"'))["status"], "failed")
        self.assertEqual(self.make_receipt(junit=GOOD_XML.replace('<testcase name="ok" />', '<testcase><failure /></testcase>'))["status"], "failed")

    def test_selector_exit_and_outcome_are_required(self):
        for value, outcome in ((None, "success"), ("garbage", "success"), ("1", "success"), ("0", "failure")):
            with self.subTest(value=value, outcome=outcome):
                self.assertEqual(self.make_receipt(selector_exit=value, selector_outcome=outcome)["status"], "failed")

    def test_missing_or_mismatched_sidecar_fails_closed(self):
        for value in (None, "0" * 64, "", "not a digest"):
            with self.subTest(sidecar=value):
                receipt = self.make_receipt(sidecar=value)
                self.assertEqual(receipt["status"], "failed")
                self.assertFalse(receipt["tuple"]["composition"]["sidecar_matches"])

    def test_c07_skipped_or_failed_steps_fail_closed(self):
        for outcome in ("failure", "skipped", "cancelled"):
            with self.subTest(outcome=outcome):
                self.assertEqual(self.make_receipt(c07=True, c07_outcome=outcome)["status"], "failed")

    def test_upload_includes_hidden_manifest_entries(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        upload = workflow.split("      - name: Upload installed-wheel receipt\n", 1)[1]
        self.assertIn("include-hidden-files: true", upload)


if __name__ == "__main__":
    unittest.main()
