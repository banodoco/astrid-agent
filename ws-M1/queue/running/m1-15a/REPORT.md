# M1-15a implementation report

Implemented the selected-root dependency map on the frozen composite. Final
candidate checks: **79 passed, 2 failed**. Both failures are base-identical
VibeComfy environment failures (DEFERRED); no new candidate failure remains.
The same final tests on the composite yield **78 passed, 3 failed**, with the
additional expected acceptance failure because the composite has no selected map.
Host-discovery root isolation is **DEFERRED to M1-15b**, explicitly not passing.
Delivery is **BLOCKING** only at the READY queue append: the target file is on a
read-only filesystem. Commit, patch, verified bundles and the exact queue line
are retained in the task folder.

## Exact composition and local commit

- Composite: `bc6cf2a703163bf09fb270e678719ec6918b5708`.
- Composite tree: `13bcee163648a13b578e60008553d87966b2b8ff` (verified before editing).
- Composite parents: `677089b882ccf789a8816482483a4685a686c9a0` and `2351d24cb0e265b6b71c8c329c095ad1301802d4`.
- Head: `d99d0d592df7b2e4683853dcb6601fb4b68b1680`; tree `b8f7f90d89eded531e81c129061227946e634e80`.
- Head parent: `bc6cf2a703163bf09fb270e678719ec6918b5708`; branch `lane/M-GPU/packs-M1-15a-selected-map`.
- Tip message: `Carry selected pack dependency maps through action workers and descendants [skip ci]`.
- All five required inputs passed `merge-base --is-ancestor` against the exact
  composite; see [composite-ancestry.json](composite-ancestry.json).
- K A6 was not composed. Cryptography stays `>=48.0.1,<49` at `pyproject.toml:33`.
- Pre-check read first: `/home/user/astrid/ws-M1/queue/running/m1-15-r2/design-precheck.md`.
  Its STOP applied only to ruling 2; the current split defers that work.
- Frozen row read at its original `/home/user/repo/docs/merge-plan/handoff/M-GPU/PACKS-HANDOFF/rows-for-overflow/M1-15-implementation.md`
  path after the supplied R alias did not contain that row. Current user scope
  overrides its older K A6 and imports.py instructions.

**DEFERRED environment limit:** the checkout's `.git` is read-only. The exact
branch creation refusal was:

```text
fatal: cannot lock ref 'refs/heads/lane/M-GPU/packs-M1-15a-selected-map': Unable to create '/home/user/astrid/ws-M1/queue/running/m1-15a/tree/.git/refs/heads/lane/M-GPU/packs-M1-15a-selected-map.lock': Read-only file system
```

Writable Git metadata lives at `/home/user/astrid/ws-M1/queue/running/m1-15a/git-metadata`, with `core.worktree` set to
`/home/user/astrid/ws-M1/queue/running/m1-15a/tree`. The original `.git` remains unchanged. Use
`git --git-dir=/home/user/astrid/ws-M1/queue/running/m1-15a/git-metadata ...` to inspect the local branch, or import the
verified transport bundle. No published commit was rewritten.

## Runtime and interpreter tuple

- Python: `/home/user/astrid/ws-M1/venv/bin/python`, CPython **3.13.14**.
- Runtime SHA: `1e6d899efa77a2d6ec247c38250f9748330bb587`.
- Source snapshot: `/home/user/astrid/ws-M1/queue/running/m1-15a/sources/runtime-1e6d899`.
- Tuple: contract `0.2.14`, protocol `workspace.v1`, schema digest
  `sha256:9628bb5fd6e1dbbec392dd39f2e01031622dcf52d7ccff7cd1f0ad10743dcb1c`.
- Component manifest: `sha256:7b74a8df08c86bbd8168d11272f13abd3e76becc2bcb4cbed799f11dc2f85f34`.
- Lock producer version: `sha256:37a460109f3644e71010dd7626ea15cfbffe8e6e5705470d5ea56baa2b2a3576`.
- All 16 Runtime-lock artifacts match both the pinned source snapshot and the
  candidate; see [runtime-evidence.json](runtime-evidence.json).

**DEFERRED environment limit:** the expected Runtime checkout was absent and the
local `/home/user/astrid-runtime` mirror did not contain the pinned SHA. Rebuilt a
source snapshot from the exact lock SHA, with no install or dependency change:

```sh
git init --bare /home/user/astrid/ws-M1/queue/running/m1-15a/runtime-git
GIT_TERMINAL_PROMPT=0 git -c credential.helper= --git-dir=/home/user/astrid/ws-M1/queue/running/m1-15a/runtime-git fetch https://github.com/banodoco/astrid-runtime 1e6d899efa77a2d6ec247c38250f9748330bb587
# Python tarfile extraction of: git --git-dir=/home/user/astrid/ws-M1/queue/running/m1-15a/runtime-git archive 1e6d899efa77a2d6ec247c38250f9748330bb587
```

No generator ran: no dependency/generated output is part of the patch.
No installer, pod, GPU, provider, credential, or push command ran. No `rm`
command ran. `~/.codex` was not read. Disk started at 18 GB free and remained
above 17 GB, well above the 2 GB stop threshold.

## Changed interfaces and acceptance evidence

Only five allowlisted files changed: `generic_host.py`, `generic_host_worker.py`,
`executor/runner.py`, `pack/imports.py`, and the new composition test file.
All read-only selected-source inputs, dependency pins, and Runtime lock are
byte-identical to the composite; recorded checks are in
[delivery-checks.json](delivery-checks.json).

| Acceptance clause | Implementation and evidence |
| --- | --- |
| Selected root plus transitive map | `GenericPackHost._selected_pack_map` (`generic_host.py:7025`) follows `dependencies.packs` through the shared inventory, including dependencies with no actions. Entries contain exact canonical roots and live digests. |
| Parent, command, Python grandchild | `admit()` carries `selected_pack_map`; runner installs one map-keyed finder; command controls carry `map`; both execution profiles receive selected namespace environment inputs. The real composition test asserts identical maps in the host parent, Python action worker, command, and plain Python grandchild, with four distinct PIDs. |
| Exact origins and no ambient shadowing | Every interpreter asserts the selected parent, direct dependency, transitive leaf, product facade, and Runtime origins. Same-ID external decoys poison ambient inputs; the selected roots still win. Pack roots and collection roots are absent from worker/command/grandchild `PYTHONPATH`. |
| Unrelated roots unread outside discovery | The final test configures an unrelated valid root, runs host discovery, then installs audit hooks rejecting `open`, `os.listdir`, or `os.scandir` under it and both same-ID decoy collections. Map construction and subsequent execution pass. No host-discovery non-read assertion is made. |
| Run-source invalidation | Editing the transitive leaf makes the old admission fail before execution; fresh admission updates its leaf digest while the parent's own digest stays unchanged. |
| Ruling 3, bundled/external same ID | `test_external_pack_sharing_bundled_id_is_refused[loader/namespace/host]`: all **3 pass on both base and candidate**. Asserts explicit raw-loader ValueError, namespace ImportError, and shared host PackValidationError for external `generation`. Existing real bundled import behavior also passes. |
| M2-05 resource construction seam | [M2-05-HANDOFF.md](M2-05-HANDOFF.md) documents `host.admit(...)[1]["selected_pack_map"]`, builder and validation/environment helpers. Resource readers remain untouched for M3-15. |

The new test is
`tests/core/execution/test_selected_root_map_composition.py::test_parent_command_python_grandchild_use_selected_map_and_exact_origins`.
It uses the proven explicit-product-root `-I` bootstrap and real inherited socket
pattern from the two named passing prior-art tests. It declares no resources.
All four new cases ran on the candidate alone before base comparison. The final
strengthened version also ran on the candidate alone before its base comparison.

**DEFERRED namespace limit (from the pre-check):** a plain Python descendant uses
the read-only `astrid.packs` namespace's collection-root semantics. It carries the
map environment but does not automatically install the raw-source finder or
verify map digests. Names in selected collections/personal namespace remain
subject to the existing collision policy, and this is not a claim that arbitrary
Python code cannot address siblings inside a selected collection. The audited
composition establishes exact selected origins and no reads of unrelated
collections for the exercised chain. Namespace-policy expansion is not included.

## Paired final checks

| Check group | Composite base | Candidate |
| --- | --- | --- |
| Frozen existing selectors plus selected-import regressions | 56 passed, 2 failed | 56 passed, 2 failed |
| New composition plus ruling-3 refusals | 3 passed, 1 failed (missing `selected_pack_map`) | 4 passed |
| Focused changed-code regressions (raw source, aliases, mutation, admission, symlink and legacy command paths) | 19 passed | 19 passed |
| Total final executions | **78 passed, 3 failed** | **79 passed, 2 failed** |

These are selected checks, not the full suite. Every run has a distinct task-local
`TMPDIR` and `--basetemp`. The base is an exact `git archive` of the composite,
with only the new candidate test overlaid for comparison. No existing test or
assertion was edited, weakened, deselected, or skipped. No skip was reported.

### DEFERRED base-identical failures and root-cause diagnosis

Both `base-frozen` and `candidate-frozen` fail exactly these cases:

1. `tests/stage1/test_pack_host_interpreter_identity.py::test_selected_pack_host_python_uses_editable_vibecomfy_checkout`
   — `the test interpreter has no installed VibeComfy distribution`.
2. `tests/stage1/test_pack_host_interpreter_identity.py::test_selected_vibecomfy_checkout_survives_generic_host_worker_boundary`
   — `ASTRID_VIBECOMFY_CHECKOUT is not configured`.

The pinned packs Python lacks the optional editable VibeComfy installation and
no selected checkout is configured. The same messages and counts occur on the
composite before this patch. No installer or external provider was invoked to
change that environment. Logs: [base](logs/base-frozen.log), [candidate](logs/candidate-frozen.log).

### BLOCKING findings

**BLOCKING delivery:** appending the required queue entry to
`/home/user/astrid/ws-M1/READY-TO-PUSH.md` failed. Exact filesystem refusal:

```text
OSError: [Errno 30] Read-only file system: '/home/user/astrid/ws-M1/READY-TO-PUSH.md'
```

The target is outside this task's writable roots. No escalation is available in
this session. The exact entry is saved in [READY-TO-PUSH.line](READY-TO-PUSH.line),
and [ready-append-result.txt](ready-append-result.txt) records the refusal. The
queue file was not changed. A host with write access must append that retained
line for the authorized lanes-only publisher. No host push was attempted.

No unresolved new candidate test failure. The baseline composition test's
`KeyError: 'selected_pack_map'` is the original implementation gap, resolved by
this candidate (base-only failure, not a candidate regression).

Initial candidate-only fixture development is fully retained below. The first
run exposed a missing parent basetemp directory (4 setup errors; fixed in task
harness). Fixture fix round 1 removed a forbidden private SDK import from the
synthetic pack and retained its bridge assertion via the installed module.
Fixture fix round 2 completed the test bridge's `launched()` lifecycle and read
the host's actual persisted stdout tail. A newly added nested script literal
was escaped in that same fixture round; its intermediate failed run is retained.
No implementation change was needed after the initial code patch. Subsequent
passing coverage was strengthened to audit a configured unrelated collection.

## Every test command and result

| Run | Exit | Counts | Output |
| --- | --- | --- | --- |
| `candidate-new-r0` | `1` | 4 errors in 0.24s | [log](logs/candidate-new-r0.log) |
| `candidate-new-r0b` | `1` | 1 failed, 3 passed in 8.08s | [log](logs/candidate-new-r0b.log) |
| `candidate-new-r1` | `1` | 1 failed, 3 passed in 10.76s | [log](logs/candidate-new-r1.log) |
| `candidate-new-r2` | `1` | 1 failed, 3 passed in 3.50s | [log](logs/candidate-new-r2.log) |
| `candidate-new-r2-final` | `0` | 4 passed in 9.25s | [log](logs/candidate-new-r2-final.log) |
| `base-new` | `1` | 1 failed, 3 passed in 8.17s | [log](logs/base-new.log) |
| `candidate-frozen` | `1` | 2 failed, 56 passed in 68.47s (0:01:08) | [log](logs/candidate-frozen.log) |
| `base-frozen` | `1` | 2 failed, 56 passed in 73.14s (0:01:13) | [log](logs/base-frozen.log) |
| `candidate-new-final` | `0` | 4 passed in 9.01s | [log](logs/candidate-new-final.log) |
| `base-new-final` | `1` | 1 failed, 3 passed in 7.56s | [log](logs/base-new-final.log) |
| `base-focused` | `0` | 19 passed in 66.37s (0:01:06) | [log](logs/base-focused.log) |
| `candidate-focused` | `0` | 19 passed in 66.53s (0:01:06) | [log](logs/candidate-focused.log) |

Exact commands, working directories and isolation variables follow. The task
harness `run_checks.py` executes each command and records it in `commands.jsonl`.

### candidate-new-r0

```sh
cwd=/home/user/astrid/ws-M1/queue/running/m1-15a/tree
TMPDIR=/home/user/astrid/ws-M1/queue/running/m1-15a/tmp/candidate-new-r0
ASTRID_RUNTIME_CHECKOUT=/home/user/astrid/ws-M1/queue/running/m1-15a/sources/runtime-1e6d899
PYTHONPATH=/home/user/astrid/ws-M1/queue/running/m1-15a/tree:/home/user/astrid/ws-M1/queue/running/m1-15a/sources/runtime-1e6d899
/home/user/astrid/ws-M1/venv/bin/python -m pytest -o addopts=--strict-markers -ra --tb=short --basetemp=/home/user/astrid/ws-M1/queue/running/m1-15a/pytest/candidate-new-r0 tests/core/execution/test_selected_root_map_composition.py
```

### candidate-new-r0b

```sh
cwd=/home/user/astrid/ws-M1/queue/running/m1-15a/tree
TMPDIR=/home/user/astrid/ws-M1/queue/running/m1-15a/tmp/candidate-new-r0b
ASTRID_RUNTIME_CHECKOUT=/home/user/astrid/ws-M1/queue/running/m1-15a/sources/runtime-1e6d899
PYTHONPATH=/home/user/astrid/ws-M1/queue/running/m1-15a/tree:/home/user/astrid/ws-M1/queue/running/m1-15a/sources/runtime-1e6d899
/home/user/astrid/ws-M1/venv/bin/python -m pytest -o addopts=--strict-markers -ra --tb=short --basetemp=/home/user/astrid/ws-M1/queue/running/m1-15a/pytest/candidate-new-r0b tests/core/execution/test_selected_root_map_composition.py
```

### candidate-new-r1

```sh
cwd=/home/user/astrid/ws-M1/queue/running/m1-15a/tree
TMPDIR=/home/user/astrid/ws-M1/queue/running/m1-15a/tmp/candidate-new-r1
ASTRID_RUNTIME_CHECKOUT=/home/user/astrid/ws-M1/queue/running/m1-15a/sources/runtime-1e6d899
PYTHONPATH=/home/user/astrid/ws-M1/queue/running/m1-15a/tree:/home/user/astrid/ws-M1/queue/running/m1-15a/sources/runtime-1e6d899
/home/user/astrid/ws-M1/venv/bin/python -m pytest -o addopts=--strict-markers -ra --tb=short --basetemp=/home/user/astrid/ws-M1/queue/running/m1-15a/pytest/candidate-new-r1 tests/core/execution/test_selected_root_map_composition.py
```

### candidate-new-r2

```sh
cwd=/home/user/astrid/ws-M1/queue/running/m1-15a/tree
TMPDIR=/home/user/astrid/ws-M1/queue/running/m1-15a/tmp/candidate-new-r2
ASTRID_RUNTIME_CHECKOUT=/home/user/astrid/ws-M1/queue/running/m1-15a/sources/runtime-1e6d899
PYTHONPATH=/home/user/astrid/ws-M1/queue/running/m1-15a/tree:/home/user/astrid/ws-M1/queue/running/m1-15a/sources/runtime-1e6d899
/home/user/astrid/ws-M1/venv/bin/python -m pytest -o addopts=--strict-markers -ra --tb=short --basetemp=/home/user/astrid/ws-M1/queue/running/m1-15a/pytest/candidate-new-r2 tests/core/execution/test_selected_root_map_composition.py
```

### candidate-new-r2-final

```sh
cwd=/home/user/astrid/ws-M1/queue/running/m1-15a/tree
TMPDIR=/home/user/astrid/ws-M1/queue/running/m1-15a/tmp/candidate-new-r2-final
ASTRID_RUNTIME_CHECKOUT=/home/user/astrid/ws-M1/queue/running/m1-15a/sources/runtime-1e6d899
PYTHONPATH=/home/user/astrid/ws-M1/queue/running/m1-15a/tree:/home/user/astrid/ws-M1/queue/running/m1-15a/sources/runtime-1e6d899
/home/user/astrid/ws-M1/venv/bin/python -m pytest -o addopts=--strict-markers -ra --tb=short --basetemp=/home/user/astrid/ws-M1/queue/running/m1-15a/pytest/candidate-new-r2-final tests/core/execution/test_selected_root_map_composition.py
```

### base-new

```sh
cwd=/home/user/astrid/ws-M1/queue/running/m1-15a/base
TMPDIR=/home/user/astrid/ws-M1/queue/running/m1-15a/tmp/base-new
ASTRID_RUNTIME_CHECKOUT=/home/user/astrid/ws-M1/queue/running/m1-15a/sources/runtime-1e6d899
PYTHONPATH=/home/user/astrid/ws-M1/queue/running/m1-15a/base:/home/user/astrid/ws-M1/queue/running/m1-15a/sources/runtime-1e6d899
/home/user/astrid/ws-M1/venv/bin/python -m pytest -o addopts=--strict-markers -ra --tb=short --basetemp=/home/user/astrid/ws-M1/queue/running/m1-15a/pytest/base-new tests/core/execution/test_selected_root_map_composition.py
```

### candidate-frozen

```sh
cwd=/home/user/astrid/ws-M1/queue/running/m1-15a/tree
TMPDIR=/home/user/astrid/ws-M1/queue/running/m1-15a/tmp/candidate-frozen
ASTRID_RUNTIME_CHECKOUT=/home/user/astrid/ws-M1/queue/running/m1-15a/sources/runtime-1e6d899
PYTHONPATH=/home/user/astrid/ws-M1/queue/running/m1-15a/tree:/home/user/astrid/ws-M1/queue/running/m1-15a/sources/runtime-1e6d899
/home/user/astrid/ws-M1/venv/bin/python -m pytest -o addopts=--strict-markers -ra --tb=short --basetemp=/home/user/astrid/ws-M1/queue/running/m1-15a/pytest/candidate-frozen tests/core/execution/test_generic_host_actions_v3.py::test_installed_host_vibe_imports_use_only_attested_root tests/core/execution/test_generic_host_actions_v3.py::test_private_imports_add_no_repository_or_pack_pythonpath tests/core/execution/test_generic_host_actions_v3.py::test_private_child_command_preserves_entrypoint_and_original_bridge tests/stage1/test_pack_host_interpreter_identity.py tests/core/execution/test_external_pack_loading_m1_03.py
```

### base-frozen

```sh
cwd=/home/user/astrid/ws-M1/queue/running/m1-15a/base
TMPDIR=/home/user/astrid/ws-M1/queue/running/m1-15a/tmp/base-frozen
ASTRID_RUNTIME_CHECKOUT=/home/user/astrid/ws-M1/queue/running/m1-15a/sources/runtime-1e6d899
PYTHONPATH=/home/user/astrid/ws-M1/queue/running/m1-15a/base:/home/user/astrid/ws-M1/queue/running/m1-15a/sources/runtime-1e6d899
/home/user/astrid/ws-M1/venv/bin/python -m pytest -o addopts=--strict-markers -ra --tb=short --basetemp=/home/user/astrid/ws-M1/queue/running/m1-15a/pytest/base-frozen tests/core/execution/test_generic_host_actions_v3.py::test_installed_host_vibe_imports_use_only_attested_root tests/core/execution/test_generic_host_actions_v3.py::test_private_imports_add_no_repository_or_pack_pythonpath tests/core/execution/test_generic_host_actions_v3.py::test_private_child_command_preserves_entrypoint_and_original_bridge tests/stage1/test_pack_host_interpreter_identity.py tests/core/execution/test_external_pack_loading_m1_03.py
```

### candidate-new-final

```sh
cwd=/home/user/astrid/ws-M1/queue/running/m1-15a/tree
TMPDIR=/home/user/astrid/ws-M1/queue/running/m1-15a/tmp/candidate-new-final
ASTRID_RUNTIME_CHECKOUT=/home/user/astrid/ws-M1/queue/running/m1-15a/sources/runtime-1e6d899
PYTHONPATH=/home/user/astrid/ws-M1/queue/running/m1-15a/tree:/home/user/astrid/ws-M1/queue/running/m1-15a/sources/runtime-1e6d899
/home/user/astrid/ws-M1/venv/bin/python -m pytest -o addopts=--strict-markers -ra --tb=short --basetemp=/home/user/astrid/ws-M1/queue/running/m1-15a/pytest/candidate-new-final tests/core/execution/test_selected_root_map_composition.py
```

### base-new-final

```sh
cwd=/home/user/astrid/ws-M1/queue/running/m1-15a/base
TMPDIR=/home/user/astrid/ws-M1/queue/running/m1-15a/tmp/base-new-final
ASTRID_RUNTIME_CHECKOUT=/home/user/astrid/ws-M1/queue/running/m1-15a/sources/runtime-1e6d899
PYTHONPATH=/home/user/astrid/ws-M1/queue/running/m1-15a/base:/home/user/astrid/ws-M1/queue/running/m1-15a/sources/runtime-1e6d899
/home/user/astrid/ws-M1/venv/bin/python -m pytest -o addopts=--strict-markers -ra --tb=short --basetemp=/home/user/astrid/ws-M1/queue/running/m1-15a/pytest/base-new-final tests/core/execution/test_selected_root_map_composition.py
```

### base-focused

```sh
cwd=/home/user/astrid/ws-M1/queue/running/m1-15a/base
TMPDIR=/home/user/astrid/ws-M1/queue/running/m1-15a/tmp/base-focused
ASTRID_RUNTIME_CHECKOUT=/home/user/astrid/ws-M1/queue/running/m1-15a/sources/runtime-1e6d899
PYTHONPATH=/home/user/astrid/ws-M1/queue/running/m1-15a/base:/home/user/astrid/ws-M1/queue/running/m1-15a/sources/runtime-1e6d899
/home/user/astrid/ws-M1/venv/bin/python -m pytest -o addopts=--strict-markers -ra --tb=short --basetemp=/home/user/astrid/ws-M1/queue/running/m1-15a/pytest/base-focused tests/core/execution/test_generic_host_actions_v3.py::test_static_discover_admit_dry_run_and_owned_child tests/core/execution/test_generic_host_actions_v3.py::test_relative_helpers_initializers_and_source_only_bytecode tests/core/execution/test_generic_host_actions_v3.py::test_whole_pack_mutation_invalidates_admission tests/core/execution/test_generic_host_actions_v3.py::test_admission_identity_is_required tests/core/execution/test_generic_host_actions_v3.py::test_symlink_escape_rejected tests/core/execution/test_generic_host_actions_v3.py::test_canonical_punctuation_paths_use_opaque_module_names tests/core/execution/test_generic_host_actions_v3.py::test_command_action_and_v2_command_harvest tests/core/execution/test_generic_host_actions_v3.py::test_legacy_child_command_and_ancestor_import_fence tests/core/execution/test_generic_host_actions_v3.py::test_legacy_orchestrator_child_command_route
```

### candidate-focused

```sh
cwd=/home/user/astrid/ws-M1/queue/running/m1-15a/tree
TMPDIR=/home/user/astrid/ws-M1/queue/running/m1-15a/tmp/candidate-focused
ASTRID_RUNTIME_CHECKOUT=/home/user/astrid/ws-M1/queue/running/m1-15a/sources/runtime-1e6d899
PYTHONPATH=/home/user/astrid/ws-M1/queue/running/m1-15a/tree:/home/user/astrid/ws-M1/queue/running/m1-15a/sources/runtime-1e6d899
/home/user/astrid/ws-M1/venv/bin/python -m pytest -o addopts=--strict-markers -ra --tb=short --basetemp=/home/user/astrid/ws-M1/queue/running/m1-15a/pytest/candidate-focused tests/core/execution/test_generic_host_actions_v3.py::test_static_discover_admit_dry_run_and_owned_child tests/core/execution/test_generic_host_actions_v3.py::test_relative_helpers_initializers_and_source_only_bytecode tests/core/execution/test_generic_host_actions_v3.py::test_whole_pack_mutation_invalidates_admission tests/core/execution/test_generic_host_actions_v3.py::test_admission_identity_is_required tests/core/execution/test_generic_host_actions_v3.py::test_symlink_escape_rejected tests/core/execution/test_generic_host_actions_v3.py::test_canonical_punctuation_paths_use_opaque_module_names tests/core/execution/test_generic_host_actions_v3.py::test_command_action_and_v2_command_harvest tests/core/execution/test_generic_host_actions_v3.py::test_legacy_child_command_and_ancestor_import_fence tests/core/execution/test_generic_host_actions_v3.py::test_legacy_orchestrator_child_command_route
```

## M1-15b open items

**DEFERRED — OPEN, NOT PASSING.** No `discovery.py` edit was made, and no host
scanner was duplicated. Anchors below are the frozen composite anchors from the
pre-check (discovery.py is unchanged):

- `astrid/core/pack/discovery.py:396`: active managed source inventory verification.
- `astrid/core/pack/discovery.py:512`: bundled pack scan.
- `astrid/core/pack/discovery.py:534`: managed-root scan.
- `astrid/core/pack/discovery.py:538`: personal `~/Astrid/packs` scan.
- `astrid/core/pack/discovery.py:539-546`: ambient `ASTRID_PACKS_PATH` scans.
- `astrid/core/execution/generic_host.py:6926` on the composite (candidate `:6944`):
  host call into shared discovery. It still performs those scans.
- `astrid/core/pack/discovery.py:548-553`: compatibility/duplicate-ID seam.
  M1-15b must preserve the ruling-3 fail-closed refusal while isolating roots.

The other pre-check host-discovery/quarantine/collection-folder reads are also
not proven isolated here. K A6 owns the scan-semantic gate and remains deferred.
The post-discovery audit in the new test deliberately does not turn any of these
open discovery reads into a passing claim.

## Local delivery and bundle transport

- Implementation patch: [M1-15a.patch](M1-15a.patch), against `bc6cf2a703163bf09fb270e678719ec6918b5708` only.
- Composite-range bundle: [M1-15a-composite-range.bundle](M1-15a-composite-range.bundle).
  Created with the exact requested `bc6cf2a703163bf09fb270e678719ec6918b5708..HEAD`
  range and verified locally. Its prerequisite is the local composite.
- Host transport: [M1-15a.bundle](M1-15a.bundle). Includes the composite merge
  because it is absent from the GitHub-backed mirror. Verified successfully in
  **both** the task metadata repository and `/home/user/astrid-agent`.
- Final transport prerequisites (all present in the mirror):
  `751547766957f0a907871617d9261318a1c8cdb8`,
  `68efb14bef5f9aefa29b6c45b8a9b6119f6d1a33`,
  `677089b882ccf789a8816482483a4685a686c9a0`.
- Only transport packaging was widened to `677089b882ccf789a8816482483a4685a686c9a0..HEAD`;
  the acceptance base and patch remain the exact composite, without rewriting it.

```sh
git --git-dir=/home/user/astrid/ws-M1/queue/running/m1-15a/git-metadata bundle create /home/user/astrid/ws-M1/queue/running/m1-15a/M1-15a.bundle bc6cf2a703163bf09fb270e678719ec6918b5708..HEAD
git --git-dir=/home/user/astrid/ws-M1/queue/running/m1-15a/git-metadata bundle verify /home/user/astrid/ws-M1/queue/running/m1-15a/M1-15a.bundle
cp /home/user/astrid/ws-M1/queue/running/m1-15a/M1-15a.bundle /home/user/astrid/ws-M1/queue/running/m1-15a/M1-15a-composite-range.bundle
git --git-dir=/home/user/astrid/ws-M1/queue/running/m1-15a/git-metadata bundle create /home/user/astrid/ws-M1/queue/running/m1-15a/M1-15a.bundle 677089b882ccf789a8816482483a4685a686c9a0..HEAD
git --git-dir=/home/user/astrid/ws-M1/queue/running/m1-15a/git-metadata bundle verify /home/user/astrid/ws-M1/queue/running/m1-15a/M1-15a.bundle
git -C /home/user/astrid-agent bundle verify /home/user/astrid/ws-M1/queue/running/m1-15a/M1-15a.bundle
```

The literal READY line is retained in [READY-TO-PUSH.line](READY-TO-PUSH.line).
**Queue append: BLOCKING**, read-only filesystem; exact refusal is recorded in
the BLOCKING findings above. No queue entry was appended and no push was performed
by this worker. This is a local filesystem refusal, not a host publication
rejection; no host publication refusal has been received.

Artifact SHA256:

- `M1-15a.bundle`: `b11f2fa44815c4d7e655b8521aca41897c5dd75d4c4aa5258ecc4ef75f84d0f7`
- `M1-15a-composite-range.bundle`: `fa8ff3bf59f00eefef2b17f4245507ce604d9e96487e3f7e12d0404d96830d6c`
- `M1-15a.patch`: `84be38e20ef7638cd50ef2f6b123f054742b5078288b7247524c35bfe2d81481`

Delegation: used 0 sub-workers
