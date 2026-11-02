# AG-CI-LIFECYCLE-PIN-R1 receipt

## Disposition

**INCOMPLETE: exact requested base already contains the authorized lifecycle pin.** No in-scope file change or task commit could be made without producing a no-op or changing content outside the authorized scope. The requested lane ref was pushed at the exact base SHA so hosted CI can still be dispatched against the verified pin. Focused collection confirms the `pod_agent`/`https` imports are available. Relevant script tests pass, while other selected execution tests retain environment or consumer failures detailed below. Hosted CI has not run and is not claimed as passed.

**Manager action:** `ACTIONS: astrid-agent ci.yml lane/M-AG/AG-CI-LIFECYCLE-PIN-R1 {}`

## Identity and provenance

- Repository: `banodoco/astrid-agent`.
- Requested branch: `lane/M-AG/AG-CI-LIFECYCLE-PIN-R1`.
- Required base: `89750ad807ecf119114e1962cc40222ae8a8b848`.
- Pushed lane head: `89750ad807ecf119114e1962cc40222ae8a8b848` (same commit as base; no task commit).
- Base parents: `14ea1103f3105fca71317427c882572333bdb1e6` and `56d01856f65753a58e21538ae8f675a75455fe6a`.
- Remote head verified with `git ls-remote`: `89750ad807ecf119114e1962cc40222ae8a8b848`.
- Worktree is clean on the requested lane.
- The exact base's `pyproject.toml:62` already contains `runpod-lifecycle @ git+https://github.com/banodoco/runpod-lifecycle.git@96360a27a53c4608f780f7b370f747a2aa3c7ca8`. This contradicts the brief's stated prior pin (`db793455…`) for this base. The value was not edited because it already equals the authorized target.
- The authorized lifecycle commit exists in the task's local authority checkout: `96360a27a53c4608f780f7b370f747a2aa3c7ca8`, commit date `2026-10-30T13:09:20+01:00`. The dev-extra install also fetched and resolved this exact commit.
- Changed paths from required base to lane head: **none**. The sole permitted file, `pyproject.toml`, was not changed; its SHA-256 is `d58754328a77f008a964adfbb5dff486e8b26895c6a758f1fe475a0c6fae2350`.
- `git diff --check` exits 0. No forbidden or other paths changed.

## Commands and inner exits

All task shell commands were invoked through `/home/user/merge/kit/codex/env-safe.sh`. Unless stated, commands ran from the task-local `agent/` checkout. Scratch venv, Runtime clone, and logs are under the task folder; pip used the shared cache at `/home/user/merge/cache/pip`.

| Command/action | Inner exit and result |
| --- | --- |
| `df -h /home/user/merge/ws/M-AG` | 0; 8.7 GiB available before setup (above the 4 GiB floor). |
| `/home/user/merge/kit/codex/task-clone.sh astrid-agent /home/user/merge/ws/M-AG/queue/running/AG-CI-LIFECYCLE-PIN-R1/agent 89750ad807ecf119114e1962cc40222ae8a8b848` | 0; exact required base. |
| Initial `git status --short --branch`, `git rev-parse HEAD`, `git show -s --format='%H%n%P%n%s' HEAD`, selected-pin inspection | 0; clean exact base; selected pin was already `96360a27…`. |
| `git ls-remote https://github.com/banodoco/runpod-lifecycle.git 96360a27a53c4608f780f7b370f747a2aa3c7ca8` | 128; GitHub did not advertise the raw SHA as a ref (`not our ref`). The task-local lifecycle authority checkout verified the object and date, and pip later fetched and resolved it successfully. |
| `git -C /home/user/merge/ws/M-AG/queue/pending/AG-CI-COLLECT-FIX-R2/lifecycle-authority rev-parse HEAD` and `git show -s --format='%H%n%ad%n%s' --date=iso-strict 96360a27a53c4608f780f7b370f747a2aa3c7ca8` | 0; exact commit `96360a27…`, dated before the freeze. |
| `git switch -c lane/M-AG/AG-CI-LIFECYCLE-PIN-R1` | 0. |
| `python3.11 -m venv ../venv` | 0. |
| `../venv/bin/python -m pip install -e '.[dev]'` | 0; built/installed `runpod-lifecycle 0.3.1.dev0`, resolved from the exact authorized SHA. |
| Initial collection: `../venv/bin/pytest --collect-only -q tests/core/execution/test_generic_host_verified_preparation_gr03.py tests/core/execution/test_workflow_endpoint_handoff.py tests/core/generation/test_engine_endpoint_transport.py tests/packs/runpod/test_https_transport.py tests/scripts/test_pod_jobs.py tests/scripts/test_pod_progress_put.py` | 4; Agent conftest could not import `runtime_protocol` because Runtime was not yet installed. |
| `../venv/bin/python -m pip install --no-deps -e /home/user/merge/ws/M-AG/repos/astrid-runtime -e /home/user/merge/ws/M-AG/repos/astrid-runtime/packages/python` | 0; this shared Runtime checkout was older than the Agent's pinned Runtime and lacked `runtime_protocol.deadline`. |
| Collection with that older shared Runtime | 1; 255/281 collected, 26 deselected, HTTPS collection errored on missing `runtime_protocol.deadline`. |
| `/home/user/merge/kit/codex/task-clone.sh astrid-runtime /home/user/merge/ws/M-AG/queue/running/AG-CI-LIFECYCLE-PIN-R1/runtime 1e6d899efa77a2d6ec247c38250f9748330bb587` | 0; cloned the exact Runtime SHA in `runtime.lock.json`. |
| `../venv/bin/python -m pip install --no-deps -e ../runtime -e ../runtime/packages/python` | 0; reinstalled task venv editables at exact pinned Runtime revision. |
| Corrected collection: `env ASTRID_RUNTIME_CHECKOUT=../runtime ../venv/bin/pytest --collect-only -q tests/core/execution/test_generic_host_verified_preparation_gr03.py tests/core/execution/test_workflow_endpoint_handoff.py tests/core/generation/test_engine_endpoint_transport.py tests/packs/runpod/test_https_transport.py tests/scripts/test_pod_jobs.py tests/scripts/test_pod_progress_put.py` | 0; 262 collected, 29 lane-deselected, 0 collection errors. This includes the previously failing GR03 module and direct imports of both `runpod_lifecycle.pod_agent` and `runpod_lifecycle.https`. |
| Combined execution: `env ASTRID_RUNTIME_CHECKOUT=../runtime ../venv/bin/pytest -q tests/core/execution/test_generic_host_verified_preparation_gr03.py tests/core/execution/test_workflow_endpoint_handoff.py tests/core/generation/test_engine_endpoint_transport.py tests/packs/runpod/test_https_transport.py tests/scripts/test_pod_jobs.py tests/scripts/test_pod_progress_put.py` | Interrupted with Ctrl-C after progress stopped at 54%; no complete result. Rerun below used a 60-second per-test timeout, no `-x` or `--maxfail`. |
| `env ASTRID_RUNTIME_CHECKOUT=../runtime ../venv/bin/pytest -q --timeout=60 tests/core/execution/test_generic_host_verified_preparation_gr03.py` | 1; 73 passed, 1 failed because `vibecomfy` is not installed (`ModuleNotFoundError` in `test_private_dispatch_freezes_real_installed_producer_recipe`). Log: `gr03.log`. |
| `env ASTRID_RUNTIME_CHECKOUT=../runtime ../venv/bin/pytest -q --timeout=60 tests/core/generation/test_engine_endpoint_transport.py` | 1; 42 passed, 3 failed because `vibecomfy` is not installed. Log: `engine-endpoint.log`. |
| `env ASTRID_RUNTIME_CHECKOUT=../runtime ../venv/bin/pytest -q --timeout=60 tests/packs/runpod/test_https_transport.py` | 1; 4 passed, 3 failed because the current `scripts/claim_runpod.py` parser rejects `--transport https`; 3 integration tests were lane-deselected. Log: `https-transport.log`. |
| `env ASTRID_RUNTIME_CHECKOUT=../runtime ../venv/bin/pytest -q --timeout=60 tests/scripts/test_pod_jobs.py` | 0; 117 passed. Log: `pod-jobs.log`. |
| `env ASTRID_RUNTIME_CHECKOUT=../runtime ../venv/bin/pytest -q --timeout=60 tests/scripts/test_pod_progress_put.py` | 0; 19 passed. Log: `pod-progress.log`. |
| `env ASTRID_RUNTIME_CHECKOUT=../runtime ../venv/bin/pytest -q --timeout=60 -m integration tests/core/execution/test_workflow_endpoint_handoff.py` | 1; 7 passed, 3 failed and 16 setup errors, all tied to missing `vibecomfy`. Log: `workflow-handoff.log`. |
| `git diff --check 89750ad807ecf119114e1962cc40222ae8a8b848 HEAD`; `git diff --name-only 89750ad807ecf119114e1962cc40222ae8a8b848 HEAD`; `sha256sum pyproject.toml` | 0; no changed paths; `pyproject.toml` hash above. |
| `../venv/bin/python -c "import runpod_lifecycle.pod_agent, runpod_lifecycle.https; print(runpod_lifecycle.pod_agent.__file__); print(runpod_lifecycle.https.__file__)"` | 0; both modules import from the task venv installed from the authorized lifecycle source. |
| `git push origin HEAD:refs/heads/lane/M-AG/AG-CI-LIFECYCLE-PIN-R1` | 0; published the requested lane ref at the exact base commit. |
| Final `git ls-remote --heads origin lane/M-AG/AG-CI-LIFECYCLE-PIN-R1`, `git status --short --branch`, `git show -s --format='commit=%H%nparents=%P%nsubject=%s' HEAD` | 0; remote head matches base; clean lane; exact base parents recorded above. |

## Coverage and limitations

The original missing-module collection failure is cleared with the exact authorized lifecycle commit: the requested GR03 test module collected, and `pod_agent`/`https` consumer modules imported. The pod-jobs and progress-put execution suites passed (136 tests total). Other selected execution results are not green due to missing `vibecomfy` in the task-local environment and HTTPS claim-helper tests expecting a CLI option absent from this Agent base. The workflow handoff selector also had 16 `vibecomfy` setup errors. These are not attributed to the lifecycle pin.

The supplied exact base already has the target pin, so this lane has no code diff and no task commit. The hosted Actions run on the pushed lane remains necessary before acceptance; the manager action above is the requested dispatch. No hosted CI pass is claimed.
