# M3-13a REPORT — stopped under binding C2

Recorded 2026-11-03T05:18:17.494304+00:00. **DEFERRED contract question: implementation stopped after the unchanged C2 fixture was proven to require custody of an unselected managed root.** No production-composition acceptance is claimed. No repository source, generated output, test, or assertion was edited.

## Identity and delivery state

- Exact base: `677089b882ccf789a8816482483a4685a686c9a0`.
- Candidate branch: `lane/M-GPU/packs-M3-13a-implementation-r1` in `/home/user/astrid/ws-M1/queue/running/m3-13a/candidate-git`.
- Exact current candidate head: `677089b882ccf789a8816482483a4685a686c9a0` (unchanged base; zero implementation commits).
- Working clone: `/home/user/astrid/ws-M1/queue/running/m3-13a/tree`. Its original `.git` remains read-only and still names the placeholder branch. The exact requested checkout command failed there with `fatal: Unable to create '/home/user/astrid/ws-M1/queue/running/m3-13a/tree/.git/index.lock': Read-only file system`. A task-local copy of the Git metadata allowed creation of the required r1 candidate branch without changing repository files.
- `git diff --exit-code` and `git diff --exit-code --cached`: PASS, no changes. Candidate status is empty. `final.patch` is intentionally empty.
- No `[skip ci]` tip commit, incremental bundle, bundle verification or READY-TO-PUSH entry was produced: C2 required stopping before implementation, so there is no candidate to publish and `base..HEAD` is empty. READY-TO-PUSH was not written or attempted; there was no host refusal to quote. Nothing was pushed.
- Evidence package: `M3-13a-stopped-evidence.tar.gz`; logs, diagnostics, metadata, Runtime checkout and scratch are retained in the task folder.
- Delegation: used 1 sub-worker (Luna), read-only test mapping, brief `sub/1/brief.md`, result `sub/1/mapping.md`. Its static C2 BLOCKING/scope-update wording predates the concrete trace; this report applies the manager's required **DEFERRED contract question** classification. No scope update was requested or acted on.
- Fix rounds: 0. One root-cause trace was performed. No installer, provider, GPU, pod or credential service calls. No full suite. No rm commands. Free space stayed above 19 GB (threshold 2 GB).

## C1 / C2 / C3 outcomes

**DEFERRED — C1 baseline failures established.** The three required original selectors ran first, on exact base with no overlays or edits: 0 passed, 8 failed. All six parameters of the strict-manifest/path selector failed, the broken-source selector failed, and edits-on-every-call raised IndexError. No passing base case exists among these three. No head comparison was run after the C2 stop, so the required paired base-identical qualification is **unproven**, rather than reported as completed. The candidate files are byte-identical to base. Assertions remain unchanged.

**DEFERRED — C2 contract question (mandatory stop).** The unmodified test `tests/core/generation/test_discovery_scope.py::test_managed_checkout_is_verified_even_on_snapshot_hits[packs|actions]` passed (2 passed) with read-only observation. Both fixtures provision schema v2 `demo`, whose manifest declares no executor payload; closure quarantines `demo`, and discovery returns no selected rows on the first and warm reads. After the undeclared executor is changed, whole-checkout Git status reports `M executors/echo/executor.yaml` and the packs case raises `SourceSetupError: managed source checkout is dirty`. The actions case also observes the original dirty exception. Earlier reads open this unselected executor as bytes and run whole-checkout hashing. `C2-TRACE.md` and raw `logs/c2-events.jsonl` give the concrete trace. Detecting this change requires observing an unselected fact; the frozen R5 contract forbids scanning/hashing unselected inputs. A selected managed v3 hit could be verified without that conflict, but this unchanged fixture never selects one. The test was not edited and no exception or fallback was introduced.

**DEFERRED — C3 incomplete due C2 stop.** The entire approved guard list was launched on exact base, unmodified. It was explicitly interrupted after the C2 trace proved the stop condition (exit 130). The log has seven completed-pass progress marks, no failure marks and no final/JUnit summary; these are not presented as a completed guard result. Head guards and node-ID failure comparison were not run. The C2 two-parameter selector separately completed with 2 passed. No guard was edited.

**BLOCKING — production-composition acceptance not achieved.** There is no implementation or qualified candidate. This is the consequence of the manager-directed C2 stop, not a new head regression. New head failures are not measured.

## Pinned environment and tuple

The required `/home/user/astrid/ws-X/sources/runtime-1e6d899` is absent (DEFERRED environment limit). Reconstructed the source environment locally from `runtime.lock.json`: shared local clone from `/home/user/astrid/route-M3-08-r5b/runtime` into `/home/user/astrid/ws-M1/queue/running/m3-13a/runtime`, then detached at the exact locked Runtime SHA `1e6d899efa77a2d6ec247c38250f9748330bb587`. No network or installer was used. Runtime working tree is clean. All 16 lock-owned vendored/runtime files match their recorded SHA-256 values; see `logs/lock-integrity.json`.

Exact tuple used:

| Component | Root / value | Pin |
|---|---|---|
| Agent | `/home/user/astrid/ws-M1/queue/running/m3-13a/tree` | `677089b882ccf789a8816482483a4685a686c9a0` |
| Runtime | `/home/user/astrid/ws-M1/queue/running/m3-13a/runtime` | `1e6d899efa77a2d6ec247c38250f9748330bb587` |
| Runtime contract producer version | `runtime.lock.json` | `sha256:37a460109f3644e71010dd7626ea15cfbffe8e6e5705470d5ea56baa2b2a3576` |
| SDK | Agent's lock-verified `astrid_runtime_client` | Agent `677089b882ccf789a8816482483a4685a686c9a0`, bytes from Runtime `1e6d899efa77a2d6ec247c38250f9748330bb587` |
| VibeComfy | `/home/user/astrid/route-M3-08-r5b/vibecomfy` | `2967033970cbb5884651c9565265778482d3b44e` |
| lifecycle | `/home/user/astrid/ws-R/src/runpod-lifecycle` | `96360a27a53c4608f780f7b370f747a2aa3c7ca8` |
| Python | `/home/user/astrid/ws-M1/venv/bin/python` | Python 3.13.14 |
| distributions | Existing shared dependency venv, no installs | pytest 9.0.2; wheel 0.46.3; build 1.4.0; httpx 0.28.1; astrid-runtime metadata ABSENT |

`qualify.py` defines exact PYTHONPATH, ASTRID_RUNTIME_CHECKOUT, PYTHONDONTWRITEBYTECODE and per-run TMPDIR. Every pytest command names a task-local --basetemp and retained logs. `pinned-env-retain.sh` is the supplied `/home/user/astrid/ops/pinned-env.sh` with only its scratch-removal EXIT trap omitted to honor the no-rm/retention requirement. All checks are unchanged. First invocation failed only the SDK-origin check because Python's working-directory priority loaded the vendored client from Agent instead of Runtime/packages/python. Re-running with the honest Agent-vendored SDK root/pin passed all checks and collected one named node. No code change was used to fix the environment check.

## Every validation command and counts

`commands.jsonl` is the machine-readable ledger. Every completed command ran on exact unchanged base. No changed-head tests were run. The only prospective C1 base comparison was required to precede implementation by the binding manager decision; candidate-alone/head qualification never began after the C2 stop.

### `pinned-base` — FAIL (17 checks passed, 1 origin check failed); 1 node collected, 0 tests executed

```sh
bash /home/user/astrid/ws-M1/queue/running/m3-13a/pinned-env-retain.sh --python /home/user/astrid/ws-M1/venv/bin/python --agent-root /home/user/astrid/ws-M1/queue/running/m3-13a/tree --agent-pin 677089b882ccf789a8816482483a4685a686c9a0 --runtime-root /home/user/astrid/ws-M1/queue/running/m3-13a/runtime --runtime-pin 1e6d899efa77a2d6ec247c38250f9748330bb587 --sdk-root /home/user/astrid/ws-M1/queue/running/m3-13a/runtime/packages/python --sdk-pin 1e6d899efa77a2d6ec247c38250f9748330bb587 --vibecomfy-root /home/user/astrid/route-M3-08-r5b/vibecomfy --vibecomfy-pin 2967033970cbb5884651c9565265778482d3b44e --lifecycle-root /home/user/astrid/ws-R/src/runpod-lifecycle --version wheel=0.46.3 --version build=1.4.0 --version pytest=9.0.2 --version httpx=0.28.1 --distribution astrid-runtime=ABSENT --node tests/packs/test_pack_discovery_v3.py::test_broken_source_remains_strict_and_core_shell_is_not_a_pack
```

Log: `/home/user/astrid/ws-M1/queue/running/m3-13a/logs/pinned-base.log`. Exit: 1. Head counterpart: NOT RUN.

### `pinned-base-vendored-sdk` — PASS (18 checks passed); 1 node collected, 0 tests executed

```sh
bash /home/user/astrid/ws-M1/queue/running/m3-13a/pinned-env-retain.sh --python /home/user/astrid/ws-M1/venv/bin/python --agent-root /home/user/astrid/ws-M1/queue/running/m3-13a/tree --agent-pin 677089b882ccf789a8816482483a4685a686c9a0 --runtime-root /home/user/astrid/ws-M1/queue/running/m3-13a/runtime --runtime-pin 1e6d899efa77a2d6ec247c38250f9748330bb587 --sdk-root /home/user/astrid/ws-M1/queue/running/m3-13a/tree --sdk-pin 677089b882ccf789a8816482483a4685a686c9a0 --vibecomfy-root /home/user/astrid/route-M3-08-r5b/vibecomfy --vibecomfy-pin 2967033970cbb5884651c9565265778482d3b44e --lifecycle-root /home/user/astrid/ws-R/src/runpod-lifecycle --version wheel=0.46.3 --version build=1.4.0 --version pytest=9.0.2 --version httpx=0.28.1 --distribution astrid-runtime=ABSENT --node tests/packs/test_pack_discovery_v3.py::test_broken_source_remains_strict_and_core_shell_is_not_a_pack
```

Log: `/home/user/astrid/ws-M1/queue/running/m3-13a/logs/pinned-base-vendored-sdk.log`. Exit: 0. Head counterpart: NOT RUN.

### `c1-base` — FAIL: 0 passed, 8 failed

```sh
/home/user/astrid/ws-M1/venv/bin/python -m pytest -q -o addopts= -p no:cacheprovider --basetemp=/home/user/astrid/ws-M1/queue/running/m3-13a/tmp/c1-base-basetemp --junitxml=/home/user/astrid/ws-M1/queue/running/m3-13a/logs/c1-base.xml tests/packs/test_pack_discovery_v3.py::test_broken_source_remains_strict_and_core_shell_is_not_a_pack tests/packs/test_pack_discovery_single_scan.py::test_scan_reuse_preserves_strict_manifest_and_path_guards tests/packs/test_pack_discovery_single_scan.py::test_discovery_observes_edits_on_every_call
```

Log: `/home/user/astrid/ws-M1/queue/running/m3-13a/logs/c1-base.log`. Exit: 1. Head counterpart: NOT RUN.

### `c2-base-trace` — PASS: 2 passed, 0 failed (unchanged C2 test, observation plugin)

```sh
/home/user/astrid/ws-M1/venv/bin/python -m pytest -q -o addopts= -p no:cacheprovider --basetemp=/home/user/astrid/ws-M1/queue/running/m3-13a/tmp/c2-base-trace-basetemp --junitxml=/home/user/astrid/ws-M1/queue/running/m3-13a/logs/c2-base-trace.xml -p c2_observe tests/core/generation/test_discovery_scope.py::test_managed_checkout_is_verified_even_on_snapshot_hits
```

Log: `/home/user/astrid/ws-M1/queue/running/m3-13a/logs/c2-base-trace.log`. Exit: 0. Head counterpart: NOT RUN.

### `c3-base` — INTERRUPTED: exit 130; seven pass progress marks, no completed result; head not run

```sh
/home/user/astrid/ws-M1/venv/bin/python -m pytest -q -o addopts= -p no:cacheprovider --basetemp=/home/user/astrid/ws-M1/queue/running/m3-13a/tmp/c3-base-basetemp --junitxml=/home/user/astrid/ws-M1/queue/running/m3-13a/logs/c3-base.xml tests/core/pack/test_inventory_review_fix.py tests/core/generation/test_moved_data_composition.py tests/core/generation/test_discovery_scope.py tests/core/pack/test_source_setup.py tests/packs/test_pack_m1_14_manifest.py tests/packs/test_pack_quarantine.py tests/sdk/test_host_bootstrap_source_identity.py tests/sdk/test_host_recovery.py tests/stage1/test_host_bootstrap_manifest_luna.py tests/stage1/test_pack_host_interpreter_identity.py tests/packs/rendering/test_render_export_task_adapter.py
```

Log: `/home/user/astrid/ws-M1/queue/running/m3-13a/logs/c3-base.log`. Exit: 130. Head counterpart: NOT RUN.

Preflight source reconstruction commands (not tests):

```sh
git clone --shared --no-checkout /home/user/astrid/route-M3-08-r5b/runtime /home/user/astrid/ws-M1/queue/running/m3-13a/runtime
git -C /home/user/astrid/ws-M1/queue/running/m3-13a/runtime checkout --detach 1e6d899efa77a2d6ec247c38250f9748330bb587
```

Both passed. The initial direct branch creation failed on read-only `.git`; the repeated command with `--git-dir=<task>/candidate-git --work-tree=<task>/tree` passed. The frozen row was read at the original `/home/user/repo/docs/merge-plan/handoff/M-GPU/PACKS-HANDOFF/rows-for-overflow/M3-13a-implementation.md` because its translated `/home/user/astrid/repo/...` path is absent. The pre-check was read at its supplied task-local path.

### Original C1 failures on base

- **DEFERRED (base failure; paired comparison unproven)** `tests/packs/test_pack_discovery_v3.py::test_broken_source_remains_strict_and_core_shell_is_not_a_pack`: `Failed: DID NOT RAISE <class 'astrid.core.pack._common.PackValidationError'>`.
- **DEFERRED (base failure; paired comparison unproven)** `tests/packs/test_pack_discovery_single_scan.py::test_scan_reuse_preserves_strict_manifest_and_path_guards[source]`: `Failed: DID NOT RAISE <class 'astrid.core.pack._common.PackValidationError'>`.
- **DEFERRED (base failure; paired comparison unproven)** `tests/packs/test_pack_discovery_single_scan.py::test_scan_reuse_preserves_strict_manifest_and_path_guards[local]`: `Failed: DID NOT RAISE <class 'astrid.core.pack._common.PackValidationError'>`.
- **DEFERRED (base failure; paired comparison unproven)** `tests/packs/test_pack_discovery_single_scan.py::test_scan_reuse_preserves_strict_manifest_and_path_guards[hidden]`: `Failed: DID NOT RAISE <class 'astrid.core.pack._common.PackValidationError'>`.
- **DEFERRED (base failure; paired comparison unproven)** `tests/packs/test_pack_discovery_single_scan.py::test_scan_reuse_preserves_strict_manifest_and_path_guards[alternate]`: `Failed: DID NOT RAISE <class 'astrid.core.pack._common.PackValidationError'>`.
- **DEFERRED (base failure; paired comparison unproven)** `tests/packs/test_pack_discovery_single_scan.py::test_scan_reuse_preserves_strict_manifest_and_path_guards[path]`: `Failed: DID NOT RAISE <class 'astrid.core.pack._common.PackValidationError'>`.
- **DEFERRED (base failure; paired comparison unproven)** `tests/packs/test_pack_discovery_single_scan.py::test_scan_reuse_preserves_strict_manifest_and_path_guards[symlink]`: `Failed: DID NOT RAISE <class 'astrid.core.pack._common.PackValidationError'>`.
- **DEFERRED (base failure; paired comparison unproven)** `tests/packs/test_pack_discovery_single_scan.py::test_discovery_observes_edits_on_every_call`: `IndexError: tuple index out of range`.

## Generators

All four requested generators are **DEFERRED / NOT RUN**, because C2 required stopping before code changes and freeze. No generated output changed. The following commands remain unproven:

```sh
python scripts/gen_capability_index.py
git diff --exit-code -- astrid/packs/_core/docs/references/capabilities.md
python scripts/gen_tool_catalog.py --check
python scripts/gen_editor_extension_catalog.py --check
python scripts/gen_element_catalog.py --check
```

## Frozen acceptance clause → evidence / gaps

| Frozen clause | Evidence | Status |
|---|---|---|
| Real loader/discovery/catalog/CLI/host production composition | Read-only r6/base mapping in `sub/1/mapping.md`; fourth composition selector not ported or run | **BLOCKING: unproven** |
| Selected and unselected managed/external roots | C2 concrete trace establishes current base opens/hashes an unselected managed root | **BLOCKING: target behavior not implemented** |
| Dependency closure | Base C2 closure rejects schema v2 demo before returning empty rows; no candidate dependency-ordered acquisition proof | **BLOCKING: unproven** |
| Symlink/visibility/content transitions | Six strict selector parameters fail on exact base; production transitions not implemented | **DEFERRED base failures / BLOCKING target proof missing** |
| Warm caches | C2 unchanged packs/actions tests pass, but trace shows whole-checkout custody on unselected input | **DEFERRED contract question** |
| Reject before opening/hashing unselected declared payloads | Trace shows current base byte opens of undeclared executor in quarantined root; no selected-only implementation | **BLOCKING: acceptance unproven** |
| Fingerprints do not hash unselected roots | Current base `_tree_fingerprint` recursively hashes the managed tree; C2 requires observing an unselected mutation | **DEFERRED contract question / BLOCKING implementation missing** |
| Preserve strict discovery coverage | No test or assertion edits; exact original C1 selectors run, 8 failures | **DEFERRED baseline failures; head comparison unproven** |
| Run generators and verify generated outputs by Git | No code/output edits; generators stopped before freeze under C2 | **DEFERRED: not run** |

## Remaining unproven items and required contract ruling

**DEFERRED contract question:** whether discovery must detect undeclared-file changes in a managed checkout that its own admission rejects, despite the frozen rule to scan/hash only selected inputs. The supplied fixture explicitly requires that behavior. This report requests no permission to edit the test; C2's NO is preserved. A manager contract ruling is needed before implementation can resume.

**BLOCKING:** candidate-alone four-selector acceptance, new-test base overlay, head C1 comparison, complete C3 base/head comparison, changed-file tests, all generator checks, final implementation commit and verified incremental bundle are unproven. No base-identical head classification or acceptance claim is made. No further fix rounds were spent after the mandatory stop.
