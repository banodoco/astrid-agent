"""Focused local checks only; these are not a native L2 execution."""
import fcntl
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from scripts.l2_real_linux import check_data, lock_observation


class OrchestrationTests(unittest.TestCase):
    def test_lock_observation_fails_closed_and_reports_real_contention(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / 'owner.lock'
            lock.touch()
            self.assertFalse(lock_observation(lock, busy=False)['busy'])
            with self.assertRaises(RuntimeError):
                lock_observation(lock, busy=True)
            with lock.open('r+') as held:
                fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
                self.assertTrue(lock_observation(lock, busy=True)['busy'])
                with self.assertRaises(RuntimeError):
                    lock_observation(lock, busy=False)
            with self.assertRaises(FileNotFoundError):
                lock_observation(Path(directory) / 'missing', busy=False)

    def test_data_invariant_requires_every_field_and_migration(self):
        baseline = dict(project={'id': 'x'}, timeline={'content': 'y'}, preferences={'content': 'z'},
                        cas_sha256='abc', schema_version=1, target_schema_version=1)
        migrated = dict(baseline, schema_version=2, target_schema_version=2)
        check_data(baseline, migrated)
        for field in ('project', 'timeline', 'preferences', 'cas_sha256'):
            changed = dict(migrated)
            changed[field] = None
            with self.assertRaises(RuntimeError):
                check_data(baseline, changed)
            del changed[field]
            with self.assertRaises(RuntimeError):
                check_data(baseline, changed)
        with self.assertRaises(RuntimeError):
            check_data(baseline, dict(migrated, schema_version=1))

    def test_host_gate_rejects_local_execution_before_install(self):
        with tempfile.TemporaryDirectory() as directory:
            env = dict(os.environ, GITHUB_ACTIONS='false')
            proc = subprocess.run([sys.executable, 'scripts/l2_real_linux.py', '--app', directory,
                                   '--run-root', directory + '/run', '--output', directory + '/output'],
                                  env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.assertEqual(proc.returncode, 1)
            self.assertFalse(Path(directory, 'run').exists())
            self.assertIn('requires', Path(directory, 'output/result.json').read_text())

if __name__ == '__main__':
    unittest.main()
