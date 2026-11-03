"""The retained caller tests exercise the sealed interface, not legacy plans."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]


class FinalizerCallerTests(unittest.TestCase):
    def test_legacy_caller_is_red_and_does_not_run_plan(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            marker = root / "executed"
            plan = root / "plan.json"
            plan.write_text(json.dumps([[sys.executable, "-c", f"open({str(marker)!r}, 'w').close()"]]))
            status = root / "status.json"
            result = subprocess.run([sys.executable, str(ROOT / "scripts/ci/regression_finalizer.py"),
                                     "--status", str(status), "--producer-plan", str(plan)], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(json.loads(status.read_text())["result"], "fail")
            self.assertFalse(marker.exists())

    def test_missing_arguments_writes_red_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            status = Path(tmp) / "status.json"
            result = subprocess.run([sys.executable, str(ROOT / "scripts/ci/regression_finalizer.py"),
                                     "--status", str(status)], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(json.loads(status.read_text())["result"], "fail")
