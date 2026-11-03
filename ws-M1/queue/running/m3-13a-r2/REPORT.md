# M3-13a r2 REPORT

Recorded 2026-11-03T12:17:15.206344+00:00.

**BLOCKING — review artifact, not an accepted candidate.** One C3 guard has a changed failure at the undeclared-Python import boundary; see ROOT-CAUSE.md and the final node comparison below. Two fix rounds and one root-cause diagnosis are exhausted. Local commit and verified bundle are retained for review. No push. Delegation: used 0 sub-workers.

## Exact identity and scope

- Base: `677089b882ccf789a8816482483a4685a686c9a0`; initial HEAD verified at this exact SHA on `lane/M-GPU/packs-M3-13a-implementation-r2`, clean working tree.
- Final candidate: `cb77f29913717effb49c811078473e4b34fbb97a` on `lane/M-GPU/packs-M3-13a-implementation-r2`, with `[skip ci]` tip message.
- Reference only: `4cf47a6a440580119ab3eee6ab828b149604e918`; no cherry-pick or earlier candidate branch reused.
- Runtime: `/home/user/astrid/ws-M1/queue/running/m3-13a/runtime` at `1e6d899efa77a2d6ec247c38250f9748330bb587`, clean.
- Git metadata: the workspace's protected `.git` cannot be written. Candidate commits/branch and bundle are owned by task-local `/home/user/astrid/ws-M1/queue/running/m3-13a-r2/sol-git` with work tree `/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree`. Use `git --git-dir=/home/user/astrid/ws-M1/queue/running/m3-13a-r2/sol-git --work-tree=/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree` to inspect the delivered branch; the protected original `.git` still names the frozen base.
- Bundle: `/home/user/astrid/ws-M1/queue/running/m3-13a-r2/M3-13a-r2.bundle`, incremental `677089b882ccf789a8816482483a4685a686c9a0..cb77f29913717effb49c811078473e4b34fbb97a`; verification log retained. Final patch: `/home/user/astrid/ws-M1/queue/running/m3-13a-r2/final.patch`.
- Implementation brief: `/home/user/astrid/ws-M1/queue/running/m3-13a-r2/implementation-brief.md`. Appending the manager-owned brief was refused: `PermissionError: [Errno 13] Permission denied: '/home/user/astrid/ws-M1/queue/running/m3-13a-r2/brief.md'`; the companion brief records the required spec change without replacing that file.
- Prior aborted source and preparation scripts were reviewed before selected mechanisms were reused. Fresh validation uses only `sol-*` logs and ledger entries. Other pre-existing logs are retained, not claimed as this worker's results.
- Frozen row resolved at `/home/user/repo/docs/merge-plan/handoff/M-GPU/PACKS-HANDOFF/rows-for-overflow/M3-13a-implementation.md`; the translated `/home/user/astrid/repo/...` path is absent.

Changed repository files:

- `astrid/core/execution/generic_host.py`
- `astrid/core/pack/canonical.py`
- `astrid/core/pack/discovery.py`
- `astrid/core/pack/loader.py`
- `astrid/core/pack/source_setup.py`
- `tests/core/generation/test_discovery_scope.py`
- `tests/packs/test_pack_discovery_single_scan.py`
- `tests/packs/test_selected_source_composition.py`
- `tests/packs/test_selected_source_reads.py`

No identity.py creation, local pack retirement, local asset edits, pyproject globs, catalog.ts, artifact_manifest.py, CLI consumer edits, or out-of-scope composition edits. Generic host changes are confined to `_quarantine_scope`.

## Mechanisms and owner decisions

Metadata/path validation and dependency closure precede descriptor and payload acquisition. All declarations are checked before the first resource byte read. Import checks move to dependency-ordered acquisition; a failed dependency re-closes pending candidates before their payloads are read. Full canonical validation still gates publication.

Selected managed files are checked against immutable Git blob identities without checkout-wide status or tree hashing. Default `active_source_inventory()` retains whole-checkout validation. An explicit scoped inventory helper preserves existing no-argument host/registry mocks without verification fallbacks. One source-record/pin authority remains in use. Managed revision-addressed identity is validated canonically, while API, requirements and release-pin checks remain in the pure compatibility validator.

Fingerprints first observe manifests, declared-path metadata and whole-tree link safety. Only admitted roots grant payload hashes. Visibility, missing paths, descriptor repairs, alternate manifests, links and selected content changes invalidate selection before payload acquisition. Project-local and default home layers remain. Default executable discovery retains v3-only compatibility, duplicate-ID refusal and required/fail-closed semantics.

Existing read-only canonical discovery and injected-scanner policies are preserved separately from execution admission. Both route through the same canonical path/descriptor/resource and dependency machinery. Canonical queries acquire no unrelated bundled-source payloads; source metadata is collected only when a declared dependency may require it. This preserves the two previously passing legacy/read-only v3-module cases; default executable discovery still quarantines legacy packs. Canonical projections do not reuse the executable query's cache namespace.

Projection cache keys use the same bound default project root as default discovery. Cold projections publish against the final selected fingerprint only after unchanged metadata and a repeated selected-fingerprint observation, so the first warm lookup both reuses the projection and verifies selected managed custody.

**C2 — owner-approved SPEC CHANGE.** Old fixture: schema-v2 managed checkout, quarantined, with an undeclared executor mutation. New fixture: an admitted SELECTED schema-v3 managed root with `executors/echo/executor.yaml` explicitly declared in resources and a discoverable generation action. Both packs/actions cases prove the root remains selected; the warm read must reopen the selected executor for custody verification. The original `with pytest.raises(source_setup.SourceSetupError, match="dirty")` refusal assertion is unchanged. Added cold/warm/tamper negative open, file-digest and hash-byte assertions for payloads and descriptors in hidden and dependency-quarantined managed roots. Hash auditing covers SHA-1 Git blobs and SHA-256. No existing assertion was removed or weakened. This is the only guard-module edit, and is also recorded in the implementation brief.

**C1 — base-first.** Three original selectors ran first unchanged on exact base: 0 passed, 8 failed. Final comparison by node ID: `{'DEFERRED base-identical': 8}`. The original v2 helper and original selector assertions remain unchanged; new tests use a separate v3 helper. Only matching failure messages qualify as DEFERRED. Comparison normalizes task-root paths, memory addresses, pytest-elided task path prefixes and traceback source-line positions; terminal exception text and assertion values are preserved.

**C3 — unchanged guard list.** Exact base: 373 passed, 23 failed, 6 skipped across all 402 nodes. Final node-ID comparison: `{'unchanged': 379, 'DEFERRED base-identical': 22, 'BLOCKING new or different failure': 1}`. The approved C2 test is the sole fixture/spec exception; every other guard test is byte-for-byte unchanged. See `logs/sol-final-c3-comparison.json` for both normalized messages per node.

## Environment and exact tuple

| Component | Exact root / pin |
|---|---|
| Agent | `/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree` at `cb77f29913717effb49c811078473e4b34fbb97a` (base `677089b882ccf789a8816482483a4685a686c9a0`) |
| Runtime | `/home/user/astrid/ws-M1/queue/running/m3-13a/runtime` at `1e6d899efa77a2d6ec247c38250f9748330bb587` |
| Runtime producer version | `sha256:37a460109f3644e71010dd7626ea15cfbffe8e6e5705470d5ea56baa2b2a3576` |
| SDK | Agent-vendored `astrid_runtime_client`, lock-verified bytes from the Runtime pin |
| VibeComfy | `/home/user/astrid/route-M3-08-r5b/vibecomfy` at `2967033970cbb5884651c9565265778482d3b44e` |
| lifecycle | `/home/user/astrid/ws-R/src/runpod-lifecycle` at `96360a27a53c4608f780f7b370f747a2aa3c7ca8` |
| Python | `/home/user/astrid/ws-M1/venv/bin/python`, Python 3.13.14 |
| Distributions | pytest 9.0.2; wheel 0.46.3; build 1.4.0; httpx 0.28.1; astrid-runtime metadata ABSENT |

Pinned environment preflight: 18 checks passed, one selected node collected without execution (`sol-pinned-base`). All 16 lock-owned Agent/Runtime file pairs match recorded hashes (`logs/sol-lock-integrity.json`). No installers, provider, GPU, pod or external credential-service calls. Runtime daemon calls are local tests only. No full test suite and no rm commands. No reads of ~/.codex. Task-local TMPDIR and unique --basetemp for every pytest run. Disk remained above 2 GB; final reading is in the freeze log.

Exact-base overlay: a fresh `git archive 677089b882ccf789a8816482483a4685a686c9a0` source snapshot under `/home/user/astrid/ws-M1/queue/running/m3-13a-r2/sol-base`; new test files/helpers and the approved C2 fixture were overlaid only after candidate-alone selector execution. All 1,640 Agent/SDK production files were compared to frozen Git objects with zero mismatches (`logs/sol-base-source-integrity.json`). No candidate production code was overlaid onto base.

## Commands and counts

`commands.jsonl` holds exact argv, cwd, exit code, timestamp and log for each validation command. This report covers the fresh `sol-*` commands; older retained entries belong to the aborted attempt. Counts come from JUnit XML. The initial head and preview commands are explicitly intermediate, not final qualification. The mistyped action-projection selector produced exit 4 and zero tests; the corrected selector was rerun.

| Run | Passed | Failed | Errors | Skipped | Exit |
|---|---:|---:|---:|---:|---:|
| sol-c1-base | 0 | 8 | 0 | 0 | 1 |
| sol-pinned-base | — | — | — | — | 0 |
| sol-changed-base | 8 | 20 | 0 | 0 | 1 |
| sol-c3-base | 373 | 23 | 0 | 6 | 1 |
| sol-candidate-selectors | 1 | 8 | 0 | 0 | 1 |
| sol-v3-head | 4 | 6 | 0 | 0 | 1 |
| sol-overlay-base | 2 | 35 | 0 | 0 | 1 |
| sol-gen-capabilities | — | — | — | — | 0 |
| sol-gen-capabilities-diff | — | — | — | — | 0 |
| sol-gen-editors | — | — | — | — | 0 |
| sol-gen-elements | — | — | — | — | 1 |
| sol-gen-tools | — | — | — | — | 0 |
| sol-candidate-changed | 28 | 19 | 0 | 0 | 1 |
| sol-gen-elements-base | — | — | — | — | 1 |
| sol-fix1-preview | 22 | 6 | 0 | 0 | 1 |
| sol-fix1-boundaries | 0 | 0 | 0 | 0 | 4 |
| sol-fix2-boundaries | 27 | 4 | 0 | 0 | 1 |
| sol-c3-head | 372 | 24 | 0 | 6 | 1 |
| sol-final-gen-capabilities | — | — | — | — | 0 |
| sol-final-gen-capabilities-diff | — | — | — | — | 0 |
| sol-final-gen-tools | — | — | — | — | 0 |
| sol-final-gen-editors | — | — | — | — | 0 |
| sol-final-gen-elements | — | — | — | — | 1 |
| sol-generated-output-diff | — | — | — | — | 0 |
| sol-stage | — | — | — | — | 0 |
| sol-staged-check | — | — | — | — | 0 |
| sol-commit | — | — | — | — | 0 |
| sol-bundle | — | — | — | — | 0 |
| sol-bundle-verify | — | — | — | — | 0 |
| sol-freeze-diff | — | — | — | — | 0 |
| sol-freeze-cached-diff | — | — | — | — | 0 |
| sol-final-patch | — | — | — | — | 0 |
| sol-final-head | 37 | 20 | 0 | 0 | 1 |
| sol-head-comparisons | — | — | — | — | 0 |
| sol-final-c3-head | 373 | 23 | 0 | 6 | 1 |
| sol-c3-comparison | — | — | — | — | 0 |
| sol-final-state | — | — | — | — | 0 |

Exact commands, with working directories (each run also supplies task-local TMPDIR through sol_qualify.py/qualify.py):

### sol-c1-base (exit 1)

CWD: `/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree`

```sh
/home/user/astrid/ws-M1/venv/bin/python -m pytest -q -o addopts= -p no:cacheprovider --basetemp=/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tmp/sol-c1-base-basetemp --junitxml=/home/user/astrid/ws-M1/queue/running/m3-13a-r2/logs/sol-c1-base.xml tests/packs/test_pack_discovery_v3.py::test_broken_source_remains_strict_and_core_shell_is_not_a_pack tests/packs/test_pack_discovery_single_scan.py::test_scan_reuse_preserves_strict_manifest_and_path_guards tests/packs/test_pack_discovery_single_scan.py::test_discovery_observes_edits_on_every_call
```

### sol-pinned-base (exit 0)

CWD: `/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree`

```sh
bash /home/user/astrid/ws-M1/queue/running/m3-13a/pinned-env-retain.sh --python /home/user/astrid/ws-M1/venv/bin/python --agent-root /home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree --agent-pin 677089b882ccf789a8816482483a4685a686c9a0 --runtime-root /home/user/astrid/ws-M1/queue/running/m3-13a/runtime --runtime-pin 1e6d899efa77a2d6ec247c38250f9748330bb587 --sdk-root /home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree --sdk-pin 677089b882ccf789a8816482483a4685a686c9a0 --vibecomfy-root /home/user/astrid/route-M3-08-r5b/vibecomfy --vibecomfy-pin 2967033970cbb5884651c9565265778482d3b44e --lifecycle-root /home/user/astrid/ws-R/src/runpod-lifecycle --version wheel=0.46.3 --version build=1.4.0 --version pytest=9.0.2 --version httpx=0.28.1 --distribution astrid-runtime=ABSENT --node tests/packs/test_pack_discovery_v3.py::test_broken_source_remains_strict_and_core_shell_is_not_a_pack
```

### sol-changed-base (exit 1)

CWD: `/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree`

```sh
/home/user/astrid/ws-M1/venv/bin/python -m pytest -q -o addopts= -p no:cacheprovider --basetemp=/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tmp/sol-changed-base-basetemp --junitxml=/home/user/astrid/ws-M1/queue/running/m3-13a-r2/logs/sol-changed-base.xml tests/packs/test_pack_discovery_single_scan.py tests/packs/test_pack_discovery_v3.py
```

### sol-c3-base (exit 1)

CWD: `/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree`

```sh
/home/user/astrid/ws-M1/venv/bin/python -m pytest -q -o addopts= -p no:cacheprovider --basetemp=/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tmp/sol-c3-base-basetemp --junitxml=/home/user/astrid/ws-M1/queue/running/m3-13a-r2/logs/sol-c3-base.xml tests/core/pack/test_inventory_review_fix.py tests/core/generation/test_moved_data_composition.py tests/core/generation/test_discovery_scope.py tests/core/pack/test_source_setup.py tests/packs/test_pack_m1_14_manifest.py tests/packs/test_pack_quarantine.py tests/sdk/test_host_bootstrap_source_identity.py tests/sdk/test_host_recovery.py tests/stage1/test_host_bootstrap_manifest_luna.py tests/stage1/test_pack_host_interpreter_identity.py tests/packs/rendering/test_render_export_task_adapter.py
```

### sol-candidate-selectors (exit 1)

CWD: `/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree`

```sh
/home/user/astrid/ws-M1/venv/bin/python -m pytest -q -o addopts= -p no:cacheprovider --basetemp=/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tmp/sol-candidate-selectors-basetemp --junitxml=/home/user/astrid/ws-M1/queue/running/m3-13a-r2/logs/sol-candidate-selectors.xml tests/packs/test_pack_discovery_v3.py::test_broken_source_remains_strict_and_core_shell_is_not_a_pack tests/packs/test_pack_discovery_single_scan.py::test_scan_reuse_preserves_strict_manifest_and_path_guards tests/packs/test_pack_discovery_single_scan.py::test_discovery_observes_edits_on_every_call tests/packs/test_selected_source_composition.py::test_loader_discovery_runtime_catalog_cli_composition
```

### sol-v3-head (exit 1)

CWD: `/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree`

```sh
/home/user/astrid/ws-M1/venv/bin/python -m pytest -q -o addopts= -p no:cacheprovider --basetemp=/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tmp/sol-v3-head-basetemp --junitxml=/home/user/astrid/ws-M1/queue/running/m3-13a-r2/logs/sol-v3-head.xml tests/packs/test_pack_discovery_v3.py
```

### sol-overlay-base (exit 1)

CWD: `/home/user/astrid/ws-M1/queue/running/m3-13a-r2/sol-base`

```sh
/home/user/astrid/ws-M1/venv/bin/python -m pytest -q -o addopts= -p no:cacheprovider --basetemp=/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tmp/sol-overlay-base-basetemp --junitxml=/home/user/astrid/ws-M1/queue/running/m3-13a-r2/logs/sol-overlay-base.xml tests/packs/test_pack_discovery_v3.py::test_broken_source_remains_strict_and_core_shell_is_not_a_pack tests/packs/test_pack_discovery_single_scan.py::test_scan_reuse_preserves_strict_manifest_and_path_guards tests/packs/test_pack_discovery_single_scan.py::test_discovery_observes_edits_on_every_call tests/packs/test_selected_source_reads.py tests/packs/test_selected_source_composition.py tests/core/generation/test_discovery_scope.py::test_managed_checkout_is_verified_even_on_snapshot_hits
```

### sol-gen-capabilities (exit 0)

CWD: `/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree`

```sh
/home/user/astrid/ws-M1/venv/bin/python scripts/gen_capability_index.py
```

### sol-gen-capabilities-diff (exit 0)

CWD: `/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree`

```sh
git diff --exit-code -- astrid/packs/_core/docs/references/capabilities.md
```

### sol-gen-editors (exit 0)

CWD: `/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree`

```sh
/home/user/astrid/ws-M1/venv/bin/python scripts/gen_editor_extension_catalog.py --check
```

### sol-gen-elements (exit 1)

CWD: `/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree`

```sh
/home/user/astrid/ws-M1/venv/bin/python scripts/gen_element_catalog.py --check
```

### sol-gen-tools (exit 0)

CWD: `/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree`

```sh
/home/user/astrid/ws-M1/venv/bin/python scripts/gen_tool_catalog.py --check
```

### sol-candidate-changed (exit 1)

CWD: `/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree`

```sh
/home/user/astrid/ws-M1/venv/bin/python -m pytest -q -o addopts= -p no:cacheprovider --basetemp=/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tmp/sol-candidate-changed-basetemp --junitxml=/home/user/astrid/ws-M1/queue/running/m3-13a-r2/logs/sol-candidate-changed.xml tests/packs/test_pack_discovery_single_scan.py tests/packs/test_selected_source_reads.py tests/packs/test_selected_source_composition.py tests/core/generation/test_discovery_scope.py::test_managed_checkout_is_verified_even_on_snapshot_hits
```

### sol-gen-elements-base (exit 1)

CWD: `/home/user/astrid/ws-M1/queue/running/m3-13a-r2/sol-base`

```sh
/home/user/astrid/ws-M1/venv/bin/python scripts/gen_element_catalog.py --check
```

### sol-fix1-preview (exit 1)

CWD: `/home/user/astrid/ws-M1/queue/running/m3-13a-r2/sol-fix1-preview`

```sh
/home/user/astrid/ws-M1/venv/bin/python -m pytest -q -o addopts= -p no:cacheprovider --basetemp=/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tmp/sol-fix1-preview-basetemp --junitxml=/home/user/astrid/ws-M1/queue/running/m3-13a-r2/logs/sol-fix1-preview.xml tests/packs/test_pack_discovery_v3.py tests/packs/test_selected_source_reads.py
```

### sol-fix1-boundaries (exit 4)

CWD: `/home/user/astrid/ws-M1/queue/running/m3-13a-r2/sol-fix1-preview`

```sh
/home/user/astrid/ws-M1/venv/bin/python -m pytest -q -o addopts= -p no:cacheprovider --basetemp=/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tmp/sol-fix1-boundaries-basetemp --junitxml=/home/user/astrid/ws-M1/queue/running/m3-13a-r2/logs/sol-fix1-boundaries.xml tests/packs/test_pack_discovery_v3.py tests/packs/test_selected_source_reads.py tests/core/generation/test_discovery_scope.py::test_managed_checkout_is_verified_even_on_snapshot_hits tests/core/generation/test_discovery_scope.py::test_action_projection_reuses_and_invalidates
```

### sol-fix2-boundaries (exit 1)

CWD: `/home/user/astrid/ws-M1/queue/running/m3-13a-r2/sol-fix1-preview`

```sh
/home/user/astrid/ws-M1/venv/bin/python -m pytest -q -o addopts= -p no:cacheprovider --basetemp=/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tmp/sol-fix2-boundaries-basetemp --junitxml=/home/user/astrid/ws-M1/queue/running/m3-13a-r2/logs/sol-fix2-boundaries.xml tests/packs/test_pack_discovery_v3.py tests/packs/test_selected_source_reads.py tests/core/generation/test_discovery_scope.py::test_managed_checkout_is_verified_even_on_snapshot_hits tests/core/generation/test_discovery_scope.py::test_action_projection_reuse_rediscovers_changes_and_ambiguity
```

### sol-c3-head (exit 1)

CWD: `/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree`

```sh
/home/user/astrid/ws-M1/venv/bin/python -m pytest -q -o addopts= -p no:cacheprovider --basetemp=/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tmp/sol-c3-head-basetemp --junitxml=/home/user/astrid/ws-M1/queue/running/m3-13a-r2/logs/sol-c3-head.xml tests/core/pack/test_inventory_review_fix.py tests/core/generation/test_moved_data_composition.py tests/core/generation/test_discovery_scope.py tests/core/pack/test_source_setup.py tests/packs/test_pack_m1_14_manifest.py tests/packs/test_pack_quarantine.py tests/sdk/test_host_bootstrap_source_identity.py tests/sdk/test_host_recovery.py tests/stage1/test_host_bootstrap_manifest_luna.py tests/stage1/test_pack_host_interpreter_identity.py tests/packs/rendering/test_render_export_task_adapter.py
```

### sol-final-gen-capabilities (exit 0)

CWD: `/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree`

```sh
/home/user/astrid/ws-M1/venv/bin/python scripts/gen_capability_index.py
```

### sol-final-gen-capabilities-diff (exit 0)

CWD: `/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree`

```sh
git diff --exit-code -- astrid/packs/_core/docs/references/capabilities.md
```

### sol-final-gen-tools (exit 0)

CWD: `/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree`

```sh
/home/user/astrid/ws-M1/venv/bin/python scripts/gen_tool_catalog.py --check
```

### sol-final-gen-editors (exit 0)

CWD: `/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree`

```sh
/home/user/astrid/ws-M1/venv/bin/python scripts/gen_editor_extension_catalog.py --check
```

### sol-final-gen-elements (exit 1)

CWD: `/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree`

```sh
/home/user/astrid/ws-M1/venv/bin/python scripts/gen_element_catalog.py --check
```

### sol-generated-output-diff (exit 0)

CWD: `/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree`

```sh
git diff --exit-code -- astrid/packs/_core/docs/references/capabilities.md astrid/tools/catalog.json astrid/packs/rendering/editor/catalog.ts astrid/packs/rendering/elements/catalog.ts
```

### sol-stage (exit 0)

CWD: `/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree`

```sh
git --git-dir=/home/user/astrid/ws-M1/queue/running/m3-13a-r2/sol-git --work-tree=/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree add -- astrid/core/execution/generic_host.py astrid/core/pack/canonical.py astrid/core/pack/discovery.py astrid/core/pack/loader.py astrid/core/pack/source_setup.py tests/core/generation/test_discovery_scope.py tests/packs/test_pack_discovery_single_scan.py tests/packs/test_selected_source_composition.py tests/packs/test_selected_source_reads.py
```

### sol-staged-check (exit 0)

CWD: `/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree`

```sh
git --git-dir=/home/user/astrid/ws-M1/queue/running/m3-13a-r2/sol-git --work-tree=/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree diff --cached --check
```

### sol-commit (exit 0)

CWD: `/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree`

```sh
git --git-dir=/home/user/astrid/ws-M1/queue/running/m3-13a-r2/sol-git --work-tree=/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree -c 'user.name=Astrid Sol' -c user.email=astrid-sol@example.invalid commit -m 'Implement selected-source custody and admission [skip ci]' -m 'Includes the owner-approved C2 selected v3 fixture spec change. Retains a BLOCKING changed C3 undeclared-Python import-check failure for review; exact evidence is in the task REPORT and ROOT-CAUSE artifacts.'
```

### sol-bundle (exit 0)

CWD: `/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree`

```sh
git --git-dir=/home/user/astrid/ws-M1/queue/running/m3-13a-r2/sol-git --work-tree=/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree bundle create /home/user/astrid/ws-M1/queue/running/m3-13a-r2/M3-13a-r2.bundle 677089b882ccf789a8816482483a4685a686c9a0..HEAD
```

### sol-bundle-verify (exit 0)

CWD: `/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree`

```sh
git --git-dir=/home/user/astrid/ws-M1/queue/running/m3-13a-r2/sol-git --work-tree=/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree bundle verify /home/user/astrid/ws-M1/queue/running/m3-13a-r2/M3-13a-r2.bundle
```

### sol-freeze-diff (exit 0)

CWD: `/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree`

```sh
git --git-dir=/home/user/astrid/ws-M1/queue/running/m3-13a-r2/sol-git --work-tree=/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree diff --exit-code
```

### sol-freeze-cached-diff (exit 0)

CWD: `/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree`

```sh
git --git-dir=/home/user/astrid/ws-M1/queue/running/m3-13a-r2/sol-git --work-tree=/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree diff --cached --exit-code
```

### sol-final-patch (exit 0)

CWD: `/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree`

```sh
git --git-dir=/home/user/astrid/ws-M1/queue/running/m3-13a-r2/sol-git --work-tree=/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree diff --binary --full-index 677089b882ccf789a8816482483a4685a686c9a0 HEAD > /home/user/astrid/ws-M1/queue/running/m3-13a-r2/final.patch
```

### sol-final-head (exit 1)

CWD: `/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree`

```sh
/home/user/astrid/ws-M1/venv/bin/python -m pytest -q -o addopts= -p no:cacheprovider --basetemp=/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tmp/sol-final-head-basetemp --junitxml=/home/user/astrid/ws-M1/queue/running/m3-13a-r2/logs/sol-final-head.xml tests/packs/test_pack_discovery_single_scan.py tests/packs/test_pack_discovery_v3.py tests/packs/test_selected_source_reads.py tests/packs/test_selected_source_composition.py tests/core/generation/test_discovery_scope.py::test_managed_checkout_is_verified_even_on_snapshot_hits
```

### sol-head-comparisons (exit 0)

CWD: `/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree`

```sh
python3 /home/user/astrid/ws-M1/queue/running/m3-13a-r2/sol_collect_comparisons.py
```

### sol-final-c3-head (exit 1)

CWD: `/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree`

```sh
/home/user/astrid/ws-M1/venv/bin/python -m pytest -q -o addopts= -p no:cacheprovider --basetemp=/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tmp/sol-final-c3-head-basetemp --junitxml=/home/user/astrid/ws-M1/queue/running/m3-13a-r2/logs/sol-final-c3-head.xml tests/core/pack/test_inventory_review_fix.py tests/core/generation/test_moved_data_composition.py tests/core/generation/test_discovery_scope.py tests/core/pack/test_source_setup.py tests/packs/test_pack_m1_14_manifest.py tests/packs/test_pack_quarantine.py tests/sdk/test_host_bootstrap_source_identity.py tests/sdk/test_host_recovery.py tests/stage1/test_host_bootstrap_manifest_luna.py tests/stage1/test_pack_host_interpreter_identity.py tests/packs/rendering/test_render_export_task_adapter.py
```

### sol-c3-comparison (exit 0)

CWD: `/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree`

```sh
python3 /home/user/astrid/ws-M1/queue/running/m3-13a-r2/sol_compare.py sol-c3-base sol-final-c3-head sol-final-c3-comparison
```

### sol-final-state (exit 0)

CWD: `/home/user/astrid/ws-M1/queue/running/m3-13a-r2/tree`

```sh
python3 /home/user/astrid/ws-M1/queue/running/m3-13a-r2/sol_final_state.py
```

## Fix rounds and root-cause findings

Two fix rounds, no further implementation changes after the second. Initial joined production composition already passed, but extended checks found read-only-policy regressions, redundant canonical source scanning, and a projection cache key that did not match the query's bound default root. Round 1 restored existing read-only/injected-scanner compatibility without changing execution policy. Round 2 removed unrelated canonical source payload acquisition and corrected projection cache selection/publication. Its isolated boundary preview had 27 passed and four unchanged base failures; C2 packs/actions and action projection passed. Initial failures and the collection typo remain in the ledger for audit. The one remaining import-check failure was diagnosed by paired traceback/captured-warning evidence in ROOT-CAUSE.md: base propagates the forbidden-read observer assertion; candidate quarantines that same pack inside the new acquisition boundary and then fails the selected-renderer assertion. It is conservatively BLOCKING because the observed head failure differs, even though the underlying forbidden read exists on base. The out-of-scope import checker enumerates all Python files, while another unchanged guard requires undeclared action.py imports to be checked. No check was bypassed and no third fix was attempted.

Final changed-file run: 37 passed, 20 failed; all 29 new/approved-spec cases passed. All 20 original failures are base-identical. Final original-test comparison: `{'DEFERRED base-identical': 20, 'unchanged': 8}`. Final new-test/base-overlay comparison: `{'improved': 27, 'DEFERRED base-identical': 8, 'unchanged': 2}`. Full final 57-node comparison: logs/sol-final-complete-comparison.json.

**BLOCKING — unresolved final failures:**

- `tests/core/generation/test_moved_data_composition.py::test_explicit_renderer_inventory_never_reads_ambient_or_unselected_bytes[True]`: base `{'state': 'failed', 'message': "AssertionError: PosixPath('<basetemp>/test_explicit_renderer_invento0/selected/render_fixture/unselected.py')\nassert not True\n +  where True = any(<generator object test_explicit_renderer_inventory_never_reads_ambient_or_unselected_bytes.<locals>.observed_open.<locals>.<genexpr> at <address>>)", 'label': 'sol-c3-base'}`; head `{'state': 'failed', 'message': "AssertionError: assert [] == [{'id': 'rend...chosen.yaml'}]\n  \n  Right contains one more item: {'id': 'render_fixture.chosen', 'required_binaries': ['selected-renderer'], 'source': '/home/user/astrid/ws-M1/queue/r...3-13a-r2/tmp/sol-final-c3-head-basetemp/test_explicit_renderer_invento0/selected/render_fixture/rendering/chosen.yaml'}\n  Use -v to get more diff", 'label': 'sol-final-c3-head'}`.

## DEFERRED base-identical failures

- **DEFERRED** `tests/packs/test_pack_discovery_single_scan.py::test_discovery_observes_edits_on_every_call` — `IndexError: tuple index out of range`
- **DEFERRED** `tests/packs/test_pack_discovery_single_scan.py::test_scan_reuse_preserves_strict_manifest_and_path_guards[alternate]` — `Failed: DID NOT RAISE <class 'astrid.core.pack._common.PackValidationError'>`
- **DEFERRED** `tests/packs/test_pack_discovery_single_scan.py::test_scan_reuse_preserves_strict_manifest_and_path_guards[hidden]` — `Failed: DID NOT RAISE <class 'astrid.core.pack._common.PackValidationError'>`
- **DEFERRED** `tests/packs/test_pack_discovery_single_scan.py::test_scan_reuse_preserves_strict_manifest_and_path_guards[local]` — `Failed: DID NOT RAISE <class 'astrid.core.pack._common.PackValidationError'>`
- **DEFERRED** `tests/packs/test_pack_discovery_single_scan.py::test_scan_reuse_preserves_strict_manifest_and_path_guards[path]` — `Failed: DID NOT RAISE <class 'astrid.core.pack._common.PackValidationError'>`
- **DEFERRED** `tests/packs/test_pack_discovery_single_scan.py::test_scan_reuse_preserves_strict_manifest_and_path_guards[source]` — `Failed: DID NOT RAISE <class 'astrid.core.pack._common.PackValidationError'>`
- **DEFERRED** `tests/packs/test_pack_discovery_single_scan.py::test_scan_reuse_preserves_strict_manifest_and_path_guards[symlink]` — `Failed: DID NOT RAISE <class 'astrid.core.pack._common.PackValidationError'>`
- **DEFERRED** `tests/packs/test_pack_discovery_v3.py::test_broken_source_remains_strict_and_core_shell_is_not_a_pack` — `Failed: DID NOT RAISE <class 'astrid.core.pack._common.PackValidationError'>`
- **DEFERRED** `tests/core/generation/test_discovery_scope.py::test_changed_schema_is_readmitted` — `Failed: DID NOT RAISE <class 'astrid.core.pack._common.PackValidationError'>`
- **DEFERRED** `tests/core/generation/test_discovery_scope.py::test_changed_tree_is_readmitted_and_invalid_inputs_fail_closed[alternate]` — `Failed: DID NOT RAISE <class 'astrid.core.pack._common.PackValidationError'>`
- **DEFERRED** `tests/core/generation/test_discovery_scope.py::test_changed_tree_is_readmitted_and_invalid_inputs_fail_closed[duplicate_yaml]` — `Failed: DID NOT RAISE <class 'astrid.core.pack._common.PackValidationError'>`
- **DEFERRED** `tests/core/generation/test_discovery_scope.py::test_changed_tree_is_readmitted_and_invalid_inputs_fail_closed[resource]` — `Failed: DID NOT RAISE <class 'astrid.core.pack._common.PackValidationError'>`
- **DEFERRED** `tests/core/generation/test_discovery_scope.py::test_changed_tree_is_readmitted_and_invalid_inputs_fail_closed[symlink]` — `Failed: DID NOT RAISE <class 'astrid.core.pack._common.PackValidationError'>`
- **DEFERRED** `tests/core/generation/test_discovery_scope.py::test_every_layer_and_same_size_manifest_edits_are_observed` — `AssertionError: assert [('bundled', 'source')] == [('bundled', ...rnal', 'env')]
  
  Right contains 3 more items, first extra item: ('local', 'local')
  Use -v to get more diff`
- **DEFERRED** `tests/core/generation/test_discovery_scope.py::test_literal_project_root_does_not_alias_expanded_home` — `AssertionError: assert [] == ['local']
  
  Right contains one more item: 'local'
  Use -v to get more diff`
- **DEFERRED** `tests/core/generation/test_discovery_scope.py::test_model_registry_rediscovers_added_changed_and_duplicate_backends` — `ValueError: models[0].modes['t2i'].backends['studio']: unknown backend key; available backend ids: cloud, codex, local, vibecomfy-h3-workflow, wavespeed`
- **DEFERRED** `tests/core/generation/test_discovery_scope.py::test_symlinked_external_root_tracks_target_contents` — `IndexError: tuple index out of range`
- **DEFERRED** `tests/core/pack/test_source_setup.py::test_local_git_v2_source_is_staged_once_and_shared` — `StopIteration`
- **DEFERRED** `tests/core/pack/test_source_setup.py::test_real_filtered_command_root_cf_pin_preserves_all_four_inventory_operations` — `AssertionError: Traceback (most recent call last):
    File "<basetemp>/test_real_filtered_command_roo0/public_inventory.py", line 3, in <module>
      from astrid.core.pack.source_setup import active_source_inventory
    File "<worktree>/astrid/core/pack/source_setup.py", line <source-line>, in <module>
      from astrid.core.foundation.atomic_io import read_json, write_json_atomic
    File "<worktree>/astrid/core/foundation/atomic_io.py", line <source-line>, in <module>
      from runtime_protocol import platform
  ModuleNotFoundError: No module named 'runtime_protocol'
  
assert 1 == 0
 +  where 1 = wait(timeout=0)
 +    where wait = <Popen: returncode: 1 args: ['/home/user/astrid/ws-M1/venv/bin/python', '-I'...>.wait`
- **DEFERRED** `tests/core/pack/test_source_setup.py::test_real_outer_command_inventory_route_retains_sibling_creator_waits_and_graph` — `AssertionError: Traceback (most recent call last):
    File "<basetemp>/test_real_outer_command_invent0/public_inventory.py", line 3, in <module>
      from astrid.core.pack.source_setup import active_source_inventory
    File "<worktree>/astrid/core/pack/source_setup.py", line <source-line>, in <module>
      from astrid.core.foundation.atomic_io import read_json, write_json_atomic
    File "<worktree>/astrid/core/foundation/atomic_io.py", line <source-line>, in <module>
      from runtime_protocol import platform
  ModuleNotFoundError: No module named 'runtime_protocol'
  
assert 1 == 0
 +  where 1 = wait(timeout=0)
 +    where wait = <Popen: returncode: 1 args: ['/home/user/astrid/ws-M1/venv/bin/python', '-I'...>.wait`
- **DEFERRED** `tests/packs/rendering/test_render_export_task_adapter.py::test_forced_caption_uses_server_owned_executable_routing_and_isolation` — `AssertionError: the pinned provisioned registry is required
assert False
 +  where False = is_file()
 +    where is_file = PosixPath('<worktree>/remotion/src/effects.generated.ts').is_file`
- **DEFERRED** `tests/packs/rendering/test_render_export_task_adapter.py::test_render_export_adapter_writes_real_mp4_and_is_callable` — `astrid.packs.rendering.actions.render.task_adapter.RenderExportRefused: render_export executor failed: executor exited with returncode 1`
- **DEFERRED** `tests/packs/test_pack_quarantine.py::test_approve_regenerates_lock_and_keeps_quarantined_approvals` — `AssertionError: assert ['badpack.snap'] == ['badpack.sna...oodpack.echo']
  
  Right contains one more item: 'goodpack.echo'
  Use -v to get more diff`
- **DEFERRED** `tests/packs/test_pack_quarantine.py::test_doctor_section_lists_quarantine_with_error_and_fix` — `AssertionError: assert 'keywords.1' in 'Pack manifest needs an explicit schema upgrade. Migrate to v3 and add it again.'`
- **DEFERRED** `tests/packs/test_pack_quarantine.py::test_invalid_keyword_quarantines_only_that_pack` — `AssertionError: assert [] == ['goodpack']
  
  Right contains one more item: 'goodpack'
  Use -v to get more diff`
- **DEFERRED** `tests/packs/test_pack_quarantine.py::test_invoking_a_quarantined_capability_is_typed_unavailable` — `AssertionError: assert 'keywords.1' in 'Pack manifest needs an explicit schema upgrade. Migrate to v3 and add it again.'`
- **DEFERRED** `tests/packs/test_pack_quarantine.py::test_quarantine_owner_matches_declared_bare_labels` — `AssertionError: assert QuarantinedPack(pack_id='goodpack', pack_dir=PosixPath('<elided-basetemp>/packs/goodpack/pack.yaml'), error='Pack manifest needs an explicit schema upgrade. Migrate to v3 and add it again.') is None
 +  where QuarantinedPack(pack_id='goodpack', pack_dir=PosixPath('<elided-basetemp>/packs/goodpack/pack.yaml'), error='Pack manifest needs an explicit schema upgrade. Migrate to v3 and add it again.') = quarantined_pack_for_capability('goodpack.echo', roots=(PosixPath('<basetemp>/test_quarantine_owner_matches_0/packs'),))`
- **DEFERRED** `tests/packs/test_pack_quarantine.py::test_unapproved_capability_is_unavailable_without_stopping_host` — `astrid.core.pack._common.PackValidationError: manifest not found: <basetemp>/test_unapproved_capability_is_0/astrid/core/model_catalog/models.yaml`
- **DEFERRED** `tests/stage1/test_pack_host_interpreter_identity.py::test_selected_pack_host_python_uses_editable_vibecomfy_checkout` — `Failed: the test interpreter has no installed VibeComfy distribution`
- **DEFERRED** `tests/stage1/test_pack_host_interpreter_identity.py::test_selected_vibecomfy_checkout_survives_generic_host_worker_boundary` — `Failed: ASTRID_VIBECOMFY_CHECKOUT is not configured`
- **DEFERRED** `tests/packs/test_pack_discovery_single_scan.py::test_different_roots_keep_project_scan_and_do_not_leak_source_local` — `AssertionError: assert [call(PosixPa...lidate=False)] == [call(None, i... strict=True)]
  
  At index 0 diff: call(PosixPath('<basetemp>/test_different_roots_keep_proj0/installed/astrid/packs'), strict=False, _validate=False) != call(None, include_hidden=False, strict=True)
  Use -v to get more diff`
- **DEFERRED** `tests/packs/test_pack_discovery_single_scan.py::test_local_eligibility_is_observational[absent]` — `AssertionError: assert [] == ['alpha']
  
  Right contains one more item: 'alpha'
  Use -v to get more diff`
- **DEFERRED** `tests/packs/test_pack_discovery_single_scan.py::test_local_eligibility_is_observational[hidden]` — `AssertionError: assert [] == ['alpha']
  
  Right contains one more item: 'alpha'
  Use -v to get more diff`
- **DEFERRED** `tests/packs/test_pack_discovery_single_scan.py::test_local_eligibility_is_observational[manifestless]` — `AssertionError: assert [] == ['alpha']
  
  Right contains one more item: 'alpha'
  Use -v to get more diff`
- **DEFERRED** `tests/packs/test_pack_discovery_single_scan.py::test_managed_identity_guard_is_unchanged` — `AssertionError: assert [] == [('local', 'local')]
  
  Right contains one more item: ('local', 'local')
  Use -v to get more diff`
- **DEFERRED** `tests/packs/test_pack_discovery_single_scan.py::test_same_authority_reuses_full_scan_only_with_builtin_scanner[False-checkout]` — `astrid.core.pack._common.PackValidationError: duplicate pack id 'alpha': <basetemp>/test_same_authority_reuses_ful0/checkout/astrid/packs/alpha and <basetemp>/test_same_authority_reuses_ful0/env/alpha`
- **DEFERRED** `tests/packs/test_pack_discovery_single_scan.py::test_same_authority_reuses_full_scan_only_with_builtin_scanner[False-site-packages]` — `astrid.core.pack._common.PackValidationError: duplicate pack id 'alpha': <basetemp>/test_same_authority_reuses_ful1/site-packages/astrid/packs/alpha and <basetemp>/test_same_authority_reuses_ful1/env/alpha`
- **DEFERRED** `tests/packs/test_pack_discovery_single_scan.py::test_same_authority_reuses_full_scan_only_with_builtin_scanner[True-checkout]` — `astrid.core.pack._common.PackValidationError: duplicate pack id 'alpha': <basetemp>/test_same_authority_reuses_ful2/checkout/astrid/packs/alpha and <basetemp>/test_same_authority_reuses_ful2/env/alpha`
- **DEFERRED** `tests/packs/test_pack_discovery_single_scan.py::test_same_authority_reuses_full_scan_only_with_builtin_scanner[True-site-packages]` — `astrid.core.pack._common.PackValidationError: duplicate pack id 'alpha': <basetemp>/test_same_authority_reuses_ful3/site-packages/astrid/packs/alpha and <basetemp>/test_same_authority_reuses_ful3/env/alpha`
- **DEFERRED** `tests/packs/test_pack_discovery_v3.py::test_layer_priority_and_managed_identity_are_preserved` — `AssertionError: assert ['managed', 'extra', 'env'] == ['source', 'l...extra', 'env']
  
  At index 0 diff: 'managed' != 'source'
  Right contains 2 more items, first extra item: 'extra'
  Use -v to get more diff`
- **DEFERRED** `tests/packs/test_pack_discovery_v3.py::test_v2_and_v3_discover_one_declared_skill[2]` — `ValueError: not enough values to unpack (expected 1, got 0)`
- **DEFERRED** `tests/packs/test_pack_discovery_v3.py::test_v2_and_v3_discover_one_declared_skill[3]` — `ValueError: not enough values to unpack (expected 1, got 0)`

## Generators and unproven items

- Capability index: generated with no --check, reported unchanged; required targeted git diff --exit-code passed.
- Tool catalog --check and editor-extension catalog --check passed.
- **DEFERRED, base-identical environment failure:** element catalog --check raises `ValueError: cannot verify missing rendering components: installed public Composition package is missing at <worktree>/remotion/node_modules/@banodoco/timeline-composition/package.json` on both exact base and candidate. No installer, local asset change, catalog.ts edit or check bypass was used. Full element output verification remains unproven.
- Generated outputs did not change; final generated-output diff and repository freeze checks are retained in the command ledger.
- Existing failed strict-discovery cases remain DEFERRED under C1; no assertion was changed to force green. New strict/required/symlink tests provide targeted additional evidence, not a claim that all baseline failures are repaired.
- Six C3 skips retain their existing skip conditions. They are not claimed as passing coverage.
- Provider/GPU execution and actual action execution by the composition host were outside this test; the production composition selector registers/catalogs/admits local fixture actions without executing them.
- Performance was not benchmarked. Intermediate full guard and joined-test durations are retained; warm scanner/projection reuse is asserted functionally.

## Acceptance clause → evidence map

| Frozen clause | Concrete evidence | Limit |
|---|---|---|
| Real loader/discovery/catalog/CLI/host composition | `test_loader_discovery_runtime_catalog_cli_composition`: real pinned Runtime daemon, WorkspaceClient catalog, GenericPackHost discovery/register, rendering CLI and task CLI refusal then repair/admission | Fixture actions are cataloged/admitted, not executed |
| Selected/unselected managed and external roots | real managed cold/warm/default-custody test; extra/env/managed dependency repair matrix; C2 selected executor plus hidden/quarantined sentinels | Manifests and path/link metadata remain necessary admission inputs |
| Dependency closure | optional descriptor/payload/cycle tests; external I/O-failure dependent closure; cross-root C3 inventory tests | Base-identical failures are listed rather than hidden |
| Symlink/visibility/content transitions | selected-source read tests cover missing/overlap/descriptor repairs, opaque resource directories, content edits with restored mtime, undeclared link retarget/remove, local visibility and eligibility transitions | Existing contradictory strict selectors remain DEFERRED |
| Warm caches | scan-count assertions, selected managed payload reopen on hits, first action projection reuse and repair invalidation | No performance claim |
| Refuse before unselected declared payload open/hash | audited builtins/io/os opens, file_digest streams and hash chunks; hidden/quarantined/dependent resources remain absent; dependency resource read precedes dependent resource | Descriptor bytes needed to decide descriptor validity are admission inputs; BLOCKING: the import checker still attempts an undeclared Python read in a selected root |
| Fingerprints avoid unselected payload hashing | two-stage admission metadata/selected-root fingerprints; selected-vs-undeclared/authoring sentinel audits on cold and warm calls | Manifest, declaration/path and safety-link metadata are fingerprinted |
| Preserve strict discovery | original tests and assertions retained; base/head node comparisons; new strict/required refusal and strict source link tests | C1 baseline eight failures remain DEFERRED |
| Generators then Git verification | capability generation and targeted diff, tool/editor checks, element check on base/head, final generated diff | Element verification DEFERRED due missing installed package |

## Delivery and READY state

Local commit, patch and verified incremental bundle are retained. No push was performed. READY append result is recorded below after the final attempt; if refused, its error text is preserved verbatim and work stops.

Final freeze receipt: source/index clean against task-local candidate metadata; Runtime clean; free disk 17,757,753,344 bytes. Bundle SHA-256 `0ffea3f69c9921e8291e4ef78ed75dabe05742b3de215e9356928a085f2e7530`; final.patch SHA-256 `3a160c98d7c93c908ec0d81cdc4eafb2537a504d93b48577038884233d7c2210`. Final guard: 373 passed / 23 failed / 6 skipped; 22 failures base-identical, one changed BLOCKING failure. All 29 new/approved-spec cases pass.


**BLOCKING — READY append REFUSED.** Exact attempted line:

```text
lane/M-GPU/packs-M3-13a-implementation-r2 | cb77f29913717effb49c811078473e4b34fbb97a | 677089b882ccf789a8816482483a4685a686c9a0 | /home/user/astrid/ws-M1/queue/running/m3-13a-r2/M3-13a-r2.bundle | BLOCKING changed C3 import-read guard; all 29 new/C2 cases pass; verified bundle; no push
```

Verbatim refusal:

```text
OSError: [Errno 30] Read-only file system: '/home/user/astrid/ws-M1/READY-TO-PUSH.md'
```

Stopped immediately after recording this refusal as instructed. No push was performed. Local review commit, patch and verified bundle remain available.
