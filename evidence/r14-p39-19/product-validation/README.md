# R14-P39-19: product correction and newly exposed boundaries

**BLOCKED; not Done or landing-qualified.** The complete original 104-ID measurement ran on clean published `c1130e03b234763fb6c6d9e6fd4b29c275f80d42`, tree `9144ce956f5268c3c57322d1c526b8023b6ebe06`, with exact pinned Runtime `2537fed24385ec1c24f48a812e09af4bb63fb794`. Native finish: **4 failed, 100 errors**, exit **1**, **369.12 seconds**. Four calls executed and failed; 100 calls did not execute. Every teardown passed. There were no skips.

## Each original ID and phase

[outcomes-by-id.json](outcomes-by-id.json) enumerates all 104 original node IDs and their setup, call and teardown results. Raw phase reports, detailed traces, XML, logs, reference census and execution manifests are preserved in the compressed native files. Missing call events remain `NO-RESULT`.

| File in tests/editing_conformance | IDs | Setup pass | Setup error | Call pass | Call fail | Call no-result |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `test_commit_runtime_media_manifest_inverse.py` | 2 | 0 | 2 | 0 | 0 | 2 |
| `test_commit_runtime_occurrence_media.py` | 48 | 0 | 48 | 0 | 0 | 48 |
| `test_commit_runtime_parent_media_swap.py` | 24 | 0 | 24 | 0 | 0 | 24 |
| `test_commit_runtime_video_unplace.py` | 4 | 4 | 0 | 0 | 4 | 0 |
| `test_recipe_m2_auto_placement.py` | 6 | 0 | 6 | 0 | 0 | 6 |
| `test_recipe_m2_placement_create_unplace.py` | 4 | 0 | 4 | 0 | 0 | 4 |
| `test_recipe_m2_shared_auto_placement.py` | 6 | 0 | 6 | 0 | 0 | 6 |
| `test_recipe_m2_shot_pool.py` | 7 | 0 | 7 | 0 | 0 | 7 |
| `test_recipe_occurrence_media.py` | 3 | 0 | 3 | 0 | 0 | 3 |

The exact four executed calls are `tests/editing_conformance/test_commit_runtime_video_unplace.py::test_capture_commit_reload_undo_redo_and_restart_safe_retry` with parameters `[image-False]`, `[image-True]`, `[video-False]`, and `[video-True]`. Each has setup PASS, call FAIL, teardown PASS. All four fail at the existing line 284 `undo-unplace`, with Runtime `invalid_records`: **inverse stage materialization changes internal payload**. There is no redo or final integrity-check result beyond that refusal.

## Correction and regression proof

The original fixture-only checkpoint remains `de81bcd7e0518a8fc30b9d5ff17882240b6cb367`; its full 104 setup-error measurement is preserved in the parent evidence directory. [Lead decision 7017146954](https://github.com/banodoco/astrid-dev/issues/796#issuecomment-7017146954) separately authorized the bounded fps representation correction. Evidence-only commit `eb89a322e35a448201ba3c6957454f22ee5e3c5a` precedes named product commit `c1130e03b234763fb6c6d9e6fd4b29c275f80d42`.

The correction recognizes a retained typed fps slot only through canonical occurrence-local aliases with the same complete physical shot/internal identity, at least one existing occurrence owner and exact encoded authored fps. Missing/moved declarations and altered owners do not qualify. Original typed-location and rational-parity validation remain active; the general CC-3 rule registry is untouched.

| Focused run | Result | Raw exit |
| --- | --- | ---: |
| New shared-fps regression before product change | 4 provider positive cases FAIL; 14 negative controls PASS | 1 |
| After product correction | 62 PASS: 18 new cases plus 44 existing D12/D136/authenticated-inverse controls | 0 |
| Existing pin guard with failing-before proof staged | 1 PASS | 0 |
| Existing pin guard with completed product proof staged | 1 PASS | 0 |
| Source-neutral command-claim diagnostic on clean c113 | setup ERROR, call NO-RESULT, teardown PASS | 1 |

The four provider positives cover integer/rational fps, real provider acceptance/preview, exact persisted source revisions and inverse using the established detached Runtime ABI test harness. That focused provider proof does not replace the real Runtime integration outcomes above. Negative controls preserve refusal for removed/moved declarations, changed physical/occurrence owners, altered digests, arbitrary aliases, changed exact spelling and invalid fps.

The 62-case run used a recorded dirty source checkpoint. Its source-only patch sections and original complete patch hash are preserved. The sole subsequent code adjustment before publication was the local `set[str]` annotation; the exact c113 104-ID run used a clean checkout. Ruff and targeted mypy pass. The existing environment's editable package metadata points to a different checkout; the repository's isolated runner prepended this selected source checkout, as recorded in each invocation and native trace. No dependency installation was changed.

## Newly exposed setup boundary: 100 IDs

The occurrence-media helper submits a hand-built `occurrence.place-shared` command without a `created` claim. After fps validation succeeds, Runtime `timeline_commit_auth.validate_claims` at line 337 refuses it. A source-neutral trace of the unchanged image/add representative proves `core_matches=true`, `created_matches=false`, `inverse_restoration=false`, `track_creation=false`. The submitted core command fields match; its sealed command declares `created=['occurrence:occ-b']`, with the exact occurrence birth fact. Native diagnostic exit is 1, duration 14.28 seconds.

The observer also reopens the accepted pre-sibling parent, shot and internal revision and verifies the managed link and exact authored shot/internal payload. The parent has one owner at that point. This does not claim shared acceptance for these 100 cases. The source-neutral trace changes no request, object, assertion, exception or timing budget; its source and observed fields are preserved.

Reported before any further product/test change in [BLOCKED 7017505139](https://github.com/banodoco/astrid-dev/issues/796#issuecomment-7017505139) and [causal BLOCKED 7017535056](https://github.com/banodoco/astrid-dev/issues/796#issuecomment-7017535056). The proposed bounded migration is to use the existing capture helper on the accepted baseline; it has **not** been applied without Lead classification. Runtime authentication remains unchanged.

## Newly exposed call boundary: four IDs

The video-unplace helper already uses captured shared placement. All four setups pass the persisted parent/shot/internal readback, exact authored payload and shared ownership assertions before call-phase retry/inverse work. The calls accept unplace, preserve the sibling, reopen persisted state after restart, prove exact retry/history and reject a changed retry atomically before failing at undo.

Causal stack: Agent `commit.py:1481` to Runtime `timeline_commit_store.py:1545`, `timeline_commit_port.py:716`, `timeline_commit_auth.py:3055`, `:3384`, and `_inverse_stage_boundaries` at `:1979`. The refusal is **inverse stage materialization changes internal payload**. This distinct possible product defect was reported in [BLOCKED 7017596088](https://github.com/banodoco/astrid-dev/issues/796#issuecomment-7017596088). No inverse product/test change was made. There is no third failure signature in the complete roster.

## Preservation, custody and remaining gates

All **392 existing assert ASTs and 104 test-function ASTs across the nine files containing the roster** are unchanged from base 68e7; seven files remain byte-identical. The two fixture builders and new readback helper are the only existing-test-path changes. `original-104-preservation.json` records each comparison. No test/assertion deletion, weakening, skip, timing-budget change, pin change, workflow edit, Plan38/55-owned edit, destination write or board edit occurred.

`native-files.json` records the uncompressed byte sizes and SHA-256 hashes. `manifest.json` records stored-file hashes. Native artifacts are deterministic gzip containers with unchanged decompressed bytes. `run-summary.json` and individual invocation receipts preserve commands, selectors, source SHA/tree, clean/dirty state, Runtime source, environment and original exits. The original historical roster matches exactly; all 104 outcomes are accounted for.

Actual destination 68e7 Lanes 44462370783 has no S01 phase results because its wrapper aborts collection. Those results remain unknown; historical setup errors are not substituted. Final hosted qualification requires P39-13 PR #428 integrated into actual `release/14/plan-39`, then a merge of that destination into this topic and Lead-owned exact-head Lanes. No duplicate full run against the broken wrapper was dispatched. Automatic PR checks on synthetic merges are ancillary checkpoint evidence, not qualification. No workflow was manually canceled.

Routes used: Unknown; this ChatGPT packet has no configured model-usage record. Root performed all engineering and judgment; one helper collected native and remote evidence only. Review remains Lead-owned. PR: https://github.com/banodoco/astrid-agent/pull/442.
