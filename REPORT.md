# 17_E0_WAN2GP_NO_TOUCH-r3

E0's requested refusal/fix fallback is implemented. Default readiness on the existing pack `--wan-session` protocol exits before importing `shared.api` or calling upstream initialization, creating a spool, or copying config. It returns `wan2gp_init_requires_consent`, `status: blocked`, and an actionable fix with `scope: user_env`. **Successful real upstream initialization remains unproven.** This is a default refusal guarantee, not a claim that consented upstream initialization is read-only.

Base and worktree: `be06aed1e4046e62e3e4b69c3f6968704ecdb9dd` (`agent-contract`), confirmed before editing and again after validation. The base descends from frozen M candidate `3bac7302675cce3dc734639c29d1afaef62e1668` (`git merge-base --is-ancestor` succeeded). Source checkout remains clean at the same base. M's latest status at the final check still names that candidate and reports a new CPU acceptance rerun, not an accepted or replacement head. No integration, rebase, commit, push, GPU, pod, network download, dependency install, new environment, full installer, credential access, or access to `~/.codex` occurred.

The isolated worktree is `/home/user/astrid/ws-A/work/17-r3`, with its own administrative Git directory `/home/user/astrid/ws-A/work/17-r3-git` and read-only object alternates to the base checkout. No source administrative files were changed and no upstream checkout was copied. Patch: `/home/user/astrid/ws-A/patches/17_E0_WAN2GP_NO_TOUCH-r3.patch`; SHA-256 `d401416a32401fa15487f05d6d78fa37000d57aa321be456b2d42d68b13de3f7`.

Exact product files changed:

- `astrid/packs/wan2gp/src/driver.py`
- `tests/packs/wan2gp/test_standalone_driver.py`
- `tests/packs/wan2gp/test_no_touch.py` (new on this base)

The only shared operational record changed is the required append to `/home/user/astrid/ws-A/KNOWN-FAILURES.md` for the base-confirmed broken test environment. It is excluded from the product patch. The worktree and task-folder REPORT.md files contain this same report. Launchers, logs, environment inspection, and the sub-worker report are in `/home/user/astrid/ws-A/queue/running/17_E0_WAN2GP_NO_TOUCH-r3/`.

Prior art and caller boundary:

- Read ENGINES-PLAN E0, the base driver and standalone tests, both failed-task briefs/reports and sub-worker reports, the prior attempt's `test_no_touch.py`, and PLAYBOOK. The two prior early-failure checks are retained in expanded consented failure coverage. The pinned reference `/tmp/wan2gp-pinned` remains at `f3f204e50f6eeb73ce40d1dafc93bc97bfaa61e4`; it was not modified or executed.
- The base has no separate real probe command: its readiness boundary is the initial JSONL frame to `wan_session_main`. Fake persistent-runner liveness probes are unrelated. Root resolution only checks the explicit directory/API path and remains read-only.
- `GenericPackHost._wan_session_context` (`generic_host.py:8950`) extracts config/CLI options but no consent; `prepare_wan_session` (`:5906`) restricts init options; `_ManagedWanChildAdapter.start` (`:714`, frame at `:749`) sends only `birth`, `root`, and `init`. Its launcher still selects `generic_host_worker --wan-session`, which has no such dispatch path on this base. These existing host integration gaps were not expanded into E2 work.
- The gate is at `driver.py:844`, after read-only frame/root checks and before spool creation, scratch config, cwd changes, upstream import, and `api.init` (`:858-877`). Only top-level `allow_user_env` equal to JSON `true` in the first frame authorizes initialization. Missing/null/false/number/string/list/object values refuse. Subsequent frames and generation settings cannot authorize a refused session. The permission is not forwarded to upstream or persisted.
- The returned fix identifies the resolved root, explains FFmpeg writes/downloads and settings/model migration, requires caller consent, and provides `retry: {"allow_user_env": true}`. After obtaining consent for that root/session, the direct protocol caller must restart the same worker command and resend its original initialization frame with that field. This boolean records the caller's explicit decision; it does not authenticate an end-user conversation or introduce a new authority system.
- The current host does not forward consent and therefore gains no permission from this patch. If its dispatch is repaired separately, its unchanged frame will refuse. The host currently reports only the child error string rather than forwarding the structured fix; the string includes the remedy. A complete public host consent/fix journey remains outside this patch.
- The output-under-root check now runs before directory creation, preserving the prior root/output integrity check while preventing its earlier side effect.

Dependency decision and upstream proof:

`python3 -B /home/user/astrid/ws-A/queue/running/17_E0_WAN2GP_NO_TOUCH-r3/check-environments.py` inspected 15 existing interpreters using `-I -B` and `importlib.util.find_spec`, without importing Wan2GP. Inventory: system Python; the 11 existing environments under `ws-M/work/04`; `ws-P/.venv`; `/tmp/H07-runtime-env-check-1`; `/tmp/01-installed-venv`. None found `torch` or `mmgp`. Results, origins, and startup errors are recorded in `environment-inventory.json`. Package-directory placeholders were distinguished from usable module files; the prescribed environment has dangling dependency symlinks into removed `ws-M/work/03/agent-venv` files.

Consequently no real `shared.api.init` attempt was made, no disposable upstream copy was created, and no new OS-level upstream write/network-denial audit was performed. Prior reports also identify unguarded startup CUDA queries. The prior missing-torch audit was not repeated or presented as a successful-init proof. No before/after hash snapshot of the real pinned source is claimed for this attempt; only its revision was checked. Missing-FFmpeg and legacy-settings fixtures demonstrate refusal before those branches, not execution or isolation of the real branches.

Validation (final product patch was unchanged after these runs):

1. Required command, run once via task `launch-pytest.sh`:

   ```sh
   cd /home/user/astrid/ws-A/work/17-r3
   mkdir -p tmp pytest
   PATH=/home/user/astrid/ws-M/work/04/agent-venv/bin:$PATH TMPDIR=/home/user/astrid/ws-A/work/17-r3/tmp PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=. python -m pytest -q --basetemp=/home/user/astrid/ws-A/work/17-r3/pytest tests/packs/wan2gp/test_standalone_driver.py tests/packs/wan2gp/test_no_touch.py
   ```

   Exit 1 before collection; **0 tests executed**. Failing step: Python's pytest module entry point. Cause: `No module named pytest.__main__; 'pytest' is a package and cannot be directly executed`, caused by dangling symlinks in the prescribed environment. Startup also reports missing `_distutils_hack.add_shim`. Evidence: `pytest.log`.

2. Confirmed that environment failure once against the exact base using task `launch-base-env-check.sh`, with no product tests and no source changes:

   ```sh
   cd /home/user/astrid/updater-forks/agent
   PATH=/home/user/astrid/ws-M/work/04/agent-venv/bin:$PATH TMPDIR=/home/user/astrid/ws-A/work/17-r3/base-env-check/tmp PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=. python -m pytest --basetemp=/home/user/astrid/ws-A/work/17-r3/base-env-check/pytest --version
   ```

   Same exit 1, entry-point error, and startup errors; `base-env-check.log`. Recorded in KNOWN-FAILURES.md; not counted as a new product failure. No dependencies or environment files were repaired.

3. Used the already-existing usable `/tmp/01-installed-venv` (Python **3.11.17**, pytest **9.0.2**), once, via task `launch-pytest-existing.sh`, with fresh temp paths:

   ```sh
   cd /home/user/astrid/ws-A/work/17-r3
   mkdir -p tmp-existing pytest-existing
   PATH=/tmp/01-installed-venv/bin:$PATH TMPDIR=/home/user/astrid/ws-A/work/17-r3/tmp-existing PYTHONDONTWRITEBYTECODE=1 PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 PYTHONPATH=. python -m pytest -q --basetemp=/home/user/astrid/ws-A/work/17-r3/pytest-existing tests/packs/wan2gp/test_standalone_driver.py tests/packs/wan2gp/test_no_touch.py
   ```

   **23 passed, 1 warning in 3.96s**, exit 0 (`pytest-existing.log`). The warning is the existing unknown `timeout` option when plugin autoload is disabled. No tests were skipped, deleted, or weakened. All five existing standalone tests retain their assertions and explicitly consent through their fixture; one new session test verifies returned-fix retry, no forwarded permission, and refusal in a later unconsented process. Seventeen no-touch cases cover normal/missing-FFmpeg/legacy-settings default refusal, exact-boolean consent, later-frame rejection, misplaced consent, and consented path/config failures. Every no-touch case asserts matching file names/hashes/modes/symlink targets, an empty Python audit attempt list, one error frame, and no scratch config; default cases also assert no spool parent. Successful fake init/serial jobs/cancel/close/output checks were reached only against the handcrafted fixture, never real upstream or a human installation.

4. `cd /home/user/astrid/ws-A/work/17-r3 && PATH=/tmp/01-installed-venv/bin:$PATH python -m compileall -q astrid/packs/wan2gp/src/driver.py`: exit 0.
5. `cd /home/user/astrid/ws-A/work/17-r3 && git diff --check`: exit 0.
6. Generated the patch with `git diff --binary --full-index`; `git apply --reverse --check` passed against the edited worktree. A separate temporary index populated by `git read-tree HEAD` also passed `git apply --check --cached` against the exact base without copying a checkout or changing the main index.

Limitations: the fixture audit hook observes Python import/network events and lexical write paths; it does not provide OS isolation or comprehensive native/dirfd write auditing. Snapshot equality and refusal assertions independently cover the fixture tree. Pytest's existing `tmp_path_retention_policy=failed` removes passed fixture directories, so raw attempt files were not retained after the run; `test-evidence.json` states that limitation rather than claiming traces were saved. Real successful init, FFmpeg setup/download, settings/model/LoRA/other startup migrations, consented upstream failure cleanup and writes, actual inference, model-download consent/receipts, full host integration, and final merged-trunk acceptance remain unproven. No full suite, product integration test, or GPU test was run. Task-local temporary directories and compile bytecode were cleaned after validation; launchers/logs/report/patch were retained.

The requested Luna review was dispatched through `sub/1/brief.md` and `sub/1/tier`, kept read-only, and completed with `sub/1/exit = 0`. I waited for that exit and read `sub/1/REPORT.md` before finalizing the boundary. All design and product/test changes were made by the coordinator.

Delegation: used 1 sub-workers
