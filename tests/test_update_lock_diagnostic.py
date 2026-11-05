"""Run directly with Python and the runtime_protocol package on PYTHONPATH."""
import contextlib
import fcntl
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from astrid.installer import cli, service
from runtime_protocol import platform


class LockDiagnosticTests(unittest.TestCase):
    def records(self, stderr):
        return [json.loads(line.removeprefix("ASTRID_LOCK_DIAG "))
                for line in stderr.splitlines() if line.startswith("ASTRID_LOCK_DIAG ")]

    def test_real_cli_acquire_release_even_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"HOME": tmp}):
            root = Path(tmp) / "private-program"
            backend = SimpleNamespace(service=SimpleNamespace())
            checks = mock.Mock(side_effect=[None, ValueError("test failure")])
            stderr = io.StringIO()
            with mock.patch.object(cli, "Backend", return_value=backend), \
                 mock.patch.object(cli, "_provisional_roots", return_value=[]), \
                 mock.patch.object(cli, "_check_installer_state", checks), \
                 contextlib.redirect_stderr(stderr):
                self.assertEqual(cli.main(["--update", "--root", str(root)]), 1)
            records = self.records(stderr.getvalue())
            self.assertEqual([r["event"] for r in records], ["acquire_begin", "acquire_success", "release_begin", "release_success"])
            self.assertTrue(all(r["role"] == "updater" and r["pid"] == os.getpid() for r in records))
            self.assertTrue(all(r["lock_file"] == records[0]["lock_file"] for r in records))
            self.assertNotIn(tmp, "\n".join(json.dumps(r) for r in records))
            self.assertEqual([r["time_ns"] for r in records], sorted(r["time_ns"] for r in records))
            with (Path(tmp) / ".local/share/astrid-install.lock").open("a+b") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    def test_real_cli_contention_emits_no_release(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"HOME": tmp}):
            root = Path(tmp) / "private-program"
            backend = SimpleNamespace(service=SimpleNamespace(recorded=lambda: {}))
            lock_path = service.installer_lock_path(Path(tmp), sys.platform)
            lock_path.parent.mkdir(parents=True)
            stderr = io.StringIO()
            with lock_path.open("a+b") as holder:
                platform.lock_file(holder.fileno(), blocking=False)
                with mock.patch.object(cli, "Backend", return_value=backend), \
                     mock.patch.object(cli, "_provisional_roots", return_value=[]), \
                     mock.patch.object(cli, "_check_installer_state"), \
                     contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(cli.main(["--update", "--root", str(root)]), 1)
                platform.unlock_file(holder.fileno())
            records = self.records(stderr.getvalue())
            self.assertEqual([r["event"] for r in records], ["acquire_begin", "acquire_contended"])
            self.assertNotIn(tmp, "\n".join(json.dumps(r) for r in records))

    def test_generated_prelude_real_process_contention_then_free(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {"HOME": tmp}):
            path = service.installer_lock_path(Path(tmp), sys.platform)
            path.parent.mkdir(parents=True)
            script = service._launch_prelude_source() + '''\nfrom types import SimpleNamespace\nv = SimpleNamespace(managed_by=None)\nprint(activation_lock_held(Path("/unused"), v))\n'''
            def probe():
                result = subprocess.run([sys.executable, "-c", script], env=os.environ.copy(),
                                        text=True, capture_output=True, check=True)
                self.assertNotIn(tmp, result.stderr)
                return result.stdout.strip(), self.records(result.stderr)
            with path.open("a+b") as holder:
                platform.lock_file(holder.fileno(), blocking=False)
                held, blocked = probe()
                self.assertEqual(held, "True")
                self.assertEqual([r["event"] for r in blocked], ["acquire_begin", "acquire_contended"])
                st = os.fstat(holder.fileno())
                self.assertEqual(blocked[-1]["lock_file"], [st.st_dev, st.st_ino])
                self.assertEqual(blocked[-1]["home"], [os.stat(tmp).st_dev, os.stat(tmp).st_ino])
                self.assertTrue(blocked[-1]["home_relative_default"])
                platform.unlock_file(holder.fileno())
            free, acquired = probe()
            self.assertEqual(free, "False")
            self.assertEqual([r["event"] for r in acquired], ["acquire_begin", "acquire_success", "release_begin", "release_success"])
            self.assertEqual(acquired[0]["lock_file"], blocked[0]["lock_file"])
            self.assertNotEqual(acquired[0]["pid"], os.getpid())

    def test_trace_failure_does_not_escape(self):
        with mock.patch.object(service.os, "fstat", side_effect=OSError("sensitive")):
            service._lock_trace("candidate", "acquire_begin", Path("/private"), -1)


if __name__ == "__main__":
    unittest.main()
