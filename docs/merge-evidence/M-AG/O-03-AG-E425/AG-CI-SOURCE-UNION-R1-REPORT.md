# AG-CI-SOURCE-UNION-R1 receipt

**Disposition: merged lane pushed; focused source identity and directly relevant helper tests pass.** The separate lifecycle-dependent collection failures remain and are not claimed as passing.

## Identity and provenance

- Repository: `banodoco/astrid-agent`.
- Lane: `lane/M-AG/AG-CI-SOURCE-UNION-R1`.
- Published base: `3566b56aa5a8d925c48847cd61af8f3bb48bce2b`.
- Candidate A: `005919e52a8f41aea0e70fe5245112a05de0810a`; Candidate B: `755424f7c540a912df848480416195c1e1fda01a`.
- Final head: `56d01856f65753a58e21538ae8f675a75455fe6a` (pushed to the named lane).
- Merge parents in order: Candidate A, then Candidate B. Both candidates have the published base as their sole parent. The candidate refs were verified with `git ls-remote` before merging and matched the exact SHAs above. `int/r1` was `3566b56aa5a8d925c48847cd61af8f3bb48bce2b` before and after the push.
- Merge command: `git merge --no-ff --no-edit 755424f7c540a912df848480416195c1e1fda01a` (inner exit **0**). No conflicts or manual resolutions.
- Runtime tuple stayed unchanged: Agent `runtime.lock.json` source commit `47e5949465cb6f3afdfd1cb8a0e90009fd9d4020`; dev dependency `runpod-lifecycle@db79345536985c701b7aff2eb5eec36667aae204`; Python 3.11.17; pytest 9.0.2. Runtime checkout was made at the lock source commit. No dependency pin was selected or edited.

## Change and coverage inventory

Relative to the published base, the only changed paths are the five helpers restored by Candidate A and Candidate B, plus Candidate A's directly related Timeline differential test repair:

- `scripts/pod_action_source.py` — restored helper.
- `scripts/pod_runtime_ready.py` — restored readiness helper.
- `scripts/pod_session.py` — restored pod-session helper; this resolves the prior focused test's missing import.
- `scripts/pod_storage_probe.py` — restored helper.
- `scripts/pod_source_identity.py` — restored source identity verifier from Candidate B.
- `tests/editing_conformance/test_clash_classifier_differential.py` — uses the active `retry._probe(session, request)` entry point; assertions retained.

Test inventory and results:

- `tests/scripts/test_pod_source_identity.py`: **34 passed**, inner exit **0**. This verifies the restored identity helper together with the restored pod-session module.
- Candidate A's recorded passing readiness/helper selectors, including the CUDA linkage case and Timeline differential test: **80 passed**, inner exit **0**. This covers `test_pod_runtime_ready.py`, `test_pod_storage_probe.py`, `test_clash_classifier_differential.py`, and `test_pod_session_cuda_string_is_derived_from_the_shared_list`.
- Lifecycle collection diagnostic: **571 collected, 1 deselected, 7 errors**, inner exit **2**. The collection errors are `test_pod_held_budget.py`, `test_pod_jobs.py`, `test_pod_progress_put.py`, `test_https_transport.py`, `test_workflow_endpoint_handoff.py`, `test_engine_endpoint_transport.py`, and `test_comfy_target.py`; they fail because the unchanged pinned lifecycle dependency lacks `runpod_lifecycle.https` / `runpod_lifecycle.pod_agent` imports. This diagnostic is separate from the passing focused test commands. No `-x` or `--maxfail` option was used.

## Commands and inner exit codes

Commands ran from this task's `agent/` checkout unless noted. Logs are stored in the task folder. Environment cache settings came from the shared environment (`PIP_CACHE_DIR=/home/user/merge/cache/pip`, `UV_CACHE_DIR=/home/user/merge/cache/uv`).

| Command/action | Inner exit |
| --- | ---: |
| `/home/user/merge/kit/codex/task-clone.sh astrid-agent .../agent 005919e52a8f41aea0e70fe5245112a05de0810a` | 0 |
| Verify clean checkout/head and `git ls-remote` for base, both candidate lanes, and unchanged `int/r1` | 0 |
| `/home/user/merge/kit/codex/task-clone.sh astrid-runtime .../runtime 47e5949465cb6f3afdfd1cb8a0e90009fd9d4020` | 0 |
| `python3.11 -m venv .../venv` | 0 |
| `../venv/bin/python -m pip install -e '.[dev]'` | 0 (`pip-dev-install.log`) |
| `../venv/bin/python -m pip install --no-deps -e ../runtime -e ../runtime/packages/python` | 0 (`runtime-install.log`) |
| `ASTRID_RUNTIME_CHECKOUT="$PWD/../runtime" ../venv/bin/python -m pytest -q tests/scripts/test_pod_source_identity.py` | 0 (`source-identity-tests.log`) |
| `ASTRID_RUNTIME_CHECKOUT="$PWD/../runtime" ../venv/bin/python -m pytest -q tests/scripts/test_pod_runtime_ready.py tests/scripts/test_pod_storage_probe.py tests/editing_conformance/test_clash_classifier_differential.py tests/scripts/test_cuda_default_sites.py::test_pod_session_cuda_string_is_derived_from_the_shared_list` | 0 (`candidate-a-passing-tests.log`) |
| `ASTRID_RUNTIME_CHECKOUT="$PWD/../runtime" ../venv/bin/python -m pytest --collect-only -q tests/scripts/test_pod_*.py tests/scripts/test_cuda_default_sites.py tests/scripts/test_claim_runpod_5090_backup.py tests/packs/runpod/test_https_transport.py tests/core/execution/test_workflow_endpoint_handoff.py tests/core/generation/test_engine_endpoint_transport.py tests/core/generation/test_engine_custody.py tests/core/generation/backends/test_comfy_target.py tests/core/test_action_cli_installed.py tests/editing_conformance/test_clash_classifier_differential.py` | 2 (`collection.log`) |
| `git diff --check 3566b56aa5a8d925c48847cd61af8f3bb48bce2b HEAD` and allowed-path inventory review | 0 |
| `git push origin HEAD:refs/heads/lane/M-AG/AG-CI-SOURCE-UNION-R1` | 0 |
| Post-push `git ls-remote` verification of lane head and unchanged `int/r1` | 0 |

## SHA-256 inventory

| Path | SHA-256 |
| --- | --- |
| `scripts/pod_action_source.py` | `6c92648842cda48597b205105eca70f54f63e182afb9dfad0f7678ccd0083577` |
| `scripts/pod_runtime_ready.py` | `2f4be9199b2a2f710e1ab39b79b9f542f434d5bce19d0671d9c0d8d09031a870` |
| `scripts/pod_session.py` | `06cc919056a074347dc95faeb70f9eed192f8fa67b5e1bbde94bc09a334c4b98` |
| `scripts/pod_source_identity.py` | `4877ffe9d693e45c022d0cea62829656d58117e7114a1ef10fc72d45015dbf53` |
| `scripts/pod_storage_probe.py` | `c123670b8b0f0af70510521a94d10bdd295bfc728826ae916fe07853dafa5236` |
| `tests/editing_conformance/test_clash_classifier_differential.py` | `b51fc44df19b74541270b827e1c0ff1a38b3080dcf5f47f72b72121635020646` |
| `tests/scripts/test_pod_source_identity.py` (unchanged by this lane) | `3636337d93bc45e20de7c655b065c724dbc5dc0fba920aa6c3cfd9aac325e722` |

## Scope and limitations

The lane contains both exact candidate histories with a two-parent merge and is pushed only to `lane/M-AG/AG-CI-SOURCE-UNION-R1`. `runtime.lock.json`, generated Runtime outputs, workflows, installer files, Q-owned fixtures/harnesses, other repos, and `int/r1` were not changed. The source identity focused test and directly relevant passing helper selectors are green. The seven separate lifecycle-dependent collection errors remain under the recorded dependency pin; no CI collection pass or full CI pass is claimed.
