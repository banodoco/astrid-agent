"""Focused checks for Linux direct-service custody diagnostics."""

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import ci_journey as journey


class LinuxServiceOwnershipTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        run_root = Path(self.temp_dir.name) / "run"
        self.root = run_root / "program"
        self.data = run_root / "data"
        self.unit = run_root / "home" / ".config" / "systemd" / "user" / "astrid.service"
        self.unit.parent.mkdir(parents=True)
        self.unit.write_text("[Service]\n", encoding="utf-8")

    def _check(self, *, load_state="loaded", fragment_path=None,
               active_state="active", returncode=0):
        fields = {
            "LoadState": load_state,
            "FragmentPath": str(self.unit) if fragment_path is None else fragment_path,
            "ActiveState": active_state,
        }
        stdout = "".join(f"{key}={value}\n" for key, value in fields.items())
        completed = subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")
        with (patch.object(sys, "platform", "linux"),
              patch.object(journey, "program_root", return_value=self.root),
              patch.object(journey.subprocess, "run", return_value=completed)):
            return journey.assert_direct_service_ownership(self.root, self.data)

    def test_missing_unit_is_an_ownership_failure(self):
        self.unit.unlink()
        with self.assertRaisesRegex(RuntimeError, "did not install the owned astrid\\.service unit"):
            self._check()

    def test_missing_or_wrong_fragment_is_an_ownership_failure(self):
        cases = (("not-found", str(self.unit)), ("loaded", "/another/run/astrid.service"))
        for load_state, fragment_path in cases:
            with self.subTest(load_state=load_state, fragment_path=fragment_path):
                with self.assertRaisesRegex(RuntimeError, "not owned by the expected systemd user unit"):
                    self._check(load_state=load_state, fragment_path=fragment_path)

    def test_manager_query_failure_remains_fail_closed(self):
        with self.assertRaisesRegex(RuntimeError, "not owned by the expected systemd user unit"):
            self._check(returncode=1)

    def test_owned_but_inactive_service_has_distinct_fail_closed_diagnostic(self):
        with self.assertRaisesRegex(
                RuntimeError, r"systemd user service is inactive \(ActiveState=inactive\)"):
            self._check(active_state="inactive")

    def test_owned_active_service_passes_ownership_guard(self):
        self.assertEqual(self._check(), {
            "manager": "systemd-user",
            "service_path": str(self.unit),
            "foreground_fallback": False,
        })


if __name__ == "__main__":
    unittest.main()
