# UPD-LEGACY-COMBINED local exits

All commands below used Python 3.13.18, the task-local virtual environment and
scratch directory, the shared UV cache, `PYTHONDONTWRITEBYTECODE=1`, and the
task-local Runtime checkout at `2776ba973b6c8f3555b5629b0840d9691a98d3c2`.
No command used `-x` or `--maxfail`.

| Command/log | Inner exit | Terminal result |
| --- | ---: | --- |
| `foreign-root-controls.log` — the two reviewer-named foreign-root nodes | 0 | 2 passed in 1.09s |
| `installer-legacy-live-update.log` — `tests/test_installer_legacy_live_update.py` | 0 | 45 passed in 2.97s |
| `affected-installer-tests.log` — changed/affected installer selection | 0 | 582 passed, 4 configured lane deselections in 172.00s |
| `installer-installed-schema.log` — test file changed to align its producer fixture with the preserved lock | 0 | 10 passed in 88.01s |
| `git diff --check ab43bad..HEAD` | 0 | no whitespace errors |
| ancestry checks for published base and both requested candidates | 0 | all are ancestors of final head |
| final candidate push and remote-ref verification | 0 | remote equals `1080ef666025d5bfc317a58370ba593694c3dc45` |

The completed current-base worker's separate selection exited 1 (254 passed,
1 failed, 3 deselected) only because its installed-schema fixture expected the
superseded Runtime producer `54533664…` while its preserved published lock
contained `2776ba97…`.  That node was absent from `KNOWN-FAILURES.md`; it is
not carried as a green result.  The in-scope fixture correction and its own
non-fail-fast file run above resolve that stale expectation without changing
the Runtime lock.
