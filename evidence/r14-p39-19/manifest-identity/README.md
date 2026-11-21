# R14-P39-19 — actual manifest revision checkpoint

**BLOCKED.** Clean measured Agent `305943c7c5d835d78e0bb46335710656a9d8f76f`, tree `d043d1844b94a03e1f797ae27d5debababc54ce4`, with clean pinned Runtime `2537fed24385ec1c24f48a812e09af4bb63fb794`. Full original104 native/raw exit **1**, **268.35s**, summary **54 failed, 48 passed, 2 errors**. This is local engineering proof, not hosted qualification.

## Authorized source changes and chronology

The requested sibling migration is isolated in named commit `42be807df9053bcff55a0ef82e174b83fd9b6580`: one file, existing public capture_occurrence_place_shared, accepted popup-baseline canonical input, original assertions and Runtime command-claim guards retained. Its completed104 run and separate four-ID #1002 observation were preserved FIRST in evidence-only `df2e62e603a1e84f2d52695903e82ccc8ecee163`; see [capture checkpoint](../capture-migration/README.md).

[Lead7018497811](https://github.com/banodoco/astrid-dev/issues/796#issuecomment-7018497811) and [PR ruling7018486500](https://github.com/banodoco/astrid-agent/pull/442#issuecomment-7018486500) then authorized a separate revision-identity-only migration. Commit305943c7 changes only the manifest fixture file: capture actual accepted current revision; use it for pre-seed public read, closure, seed expected-head CAS and post-restart historical read; verify canonical parent/current head/both shared occurrences. The explicit media-baseline seed identity remains. No additional binding/content setup or product change was made.

## Complete104 current outcomes

Setup **102 PASS / 2 ERROR**; call **48 PASS / 54 FAIL / 2 NO-RESULT**; teardown **104 PASS**. All104 original/reference/execution/native/JUnit IDs match; no missing/extra IDs, duplicate phases, deselection, skip, xfail or subtest. The reference and execution manifests are byte-identical. [Every ID and phase](outcomes-by-id.json) includes its42be comparison: all per-ID phases remain equal; the two setup failures move from missing revision to missing binding.

| File in tests/editing_conformance | IDs | Setup pass | Setup error | Call pass | Call fail | Call no-result |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `test_commit_runtime_media_manifest_inverse.py` | 2 | 0 | 2 | 0 | 0 | 2 |
| `test_commit_runtime_occurrence_media.py` | 48 | 48 | 0 | 36 | 12 | 0 |
| `test_commit_runtime_parent_media_swap.py` | 24 | 24 | 0 | 12 | 12 | 0 |
| `test_commit_runtime_video_unplace.py` | 4 | 4 | 0 | 0 | 4 | 0 |
| `test_recipe_m2_auto_placement.py` | 6 | 6 | 0 | 0 | 6 | 0 |
| `test_recipe_m2_placement_create_unplace.py` | 4 | 4 | 0 | 0 | 4 | 0 |
| `test_recipe_m2_shared_auto_placement.py` | 6 | 6 | 0 | 0 | 6 | 0 |
| `test_recipe_m2_shot_pool.py` | 7 | 7 | 0 | 0 | 7 | 0 |
| `test_recipe_occurrence_media.py` | 3 | 3 | 0 | 0 | 3 | 0 |

## First remaining setup boundary: missing real text binding

Both original media-manifest parameters [add] and [swap] now pass the actual-revision closure, current-head and shared-owner checks. They fail at existing binding access, manifest_inverse.py:51, `shot['payload']['text_bindings'][0]['binding_id']`: **IndexError: list index out of range**. Argument evaluation stops before the public binding getter, all CAS-byte assertions, media-baseline seed, and the test call. These steps remain unexecuted, never counted as passing. Original binding/byte/history assertions are preserved.

Read-only source trace: occurrence_media.py:229 explicitly sets the fixture shot's text_bindings to [] before supported publication. This pre-existing assignment is unchanged by this packet. The common fixture's exact authored/public readback checks preserve that value. The current film-owned producer is native_text_binding.capture_text_binding_set -> text-binding.set; it requires exclusive occurrence ownership and exact Runtime-verified UTF-8 media, admitted through commit.py:1224 -> port.text_selection. New rows are immutable six-field descriptors. Existing binding_id/head/event_stream_id authority is retained only for the same authenticated pin; the command does not invent that legacy authority for a new descriptor. Runtime service.set_project_shot_text_binding is a separate direct producer, gated by _require_unplaced_project_shot at service.py:4335, so it cannot be invoked on this already-shared accepted shot.

This is a source trace, not a producer execution. No text, descriptor or binding was synthesized; no extra setup was added; no assertion was skipped or changed to accommodate the empty list. [BLOCKED7018665630](https://github.com/banodoco/astrid-dev/issues/796#issuecomment-7018665630) reports the first boundary and ownership constraints before proposing any further migration. Source byte witnesses are preserved in producer-source-witnesses.json. Approval covers revision identity only; further binding-path classification remains with Lead.

## Remaining executed call failures

| Observed boundary | Calls | Limits |
| --- | ---: | --- |
| Runtime command-claim refusal | 25 FAIL | 8 occurrence lifecycle assertions at occurrence_media.py:503 and17 direct refusals; representative preview at:367 reaches Runtime validate_claims:337; a common omitted field is not assumed |
| Canonical-document project-scope rejection | 23 FAIL | auto-placement6/create-unplace4/shared6/shot-pool7; first helpers auto_placement.py:165 or create_unplace.py:53 -> Agent commit._verify_document:94, raised:46 |
| HTTP Runtime import boundary | 2 FAIL | parent_media_swap.py:285 -> tests/conftest.py:186 -> helpers/runtime.py:124 -> Runtime daemon.py:99 -> astrid_runtime/update.py:28; INSTALL_RECORD_OWNER import, cause not attributed |
| #1002 inverse internal-payload guard | 4 FAIL | unchanged video-unplace image/video x keepFalse/True, existing undo at:284 -> Runtime guard:1979 |

All four #1002 cases retain their original invalid_records refusal. [The separate four-ID observation](../capture-migration/inverse-differences-by-id.json) proves stored order [clip-a,placed,clip-b] versus reconstructed [clip-a,clip-b,placed], first differing clip index1. All clip objects by id and every non-clips field are exactly equal. Public persisted payload and digest match the expected guard image for all four. Native restoration write0 (keepFalse) or write3 (keepTrue, after pool cleanup) first appends placed in the wrong restored position. The observer's source/full images/native writes/intermediate states remain in the capture checkpoint. No inverse contract change, ownership weakening or content substitution occurred. Producer-versus-authenticator responsibility remains for #1002 routing.

The current HTTP trace names /tmp/astrid-runtime-archive-0ebecxn6/astrid_runtime/paths.py; native artifacts retain it separately from the42be path. The earlier42be temporary paths.py was already absent during read-only inspection, so its bytes/hash remain unknown; the pinned paths.py independently defines INSTALL_RECORD_OWNER. No environment mutation or failure attribution was inferred from that comparison.

## Assertion preservation and custody

The requested sibling commit retains all392 original assertion ASTs and104 test-function ASTs across nine roster files. The separately approved manifest identity migration then retains391 assertion ASTs exactly, changes only the historical-read revision argument in one equality assertion, and adds three identity/shared-owner assertions. All104 test-function ASTs remain equal after that one approved identity substitution. No expected CAS bytes, binding checks, undo/retry/history expectations, test, timing budget or guard was weakened, skipped or deleted. The exact old/new equality and all file comparisons are in assertion-preservation.json. No Plan38/55-owned test was edited.

The unchanged staged-proof pin guard passes (1 PASS, exit0); Ruff and diff checks pass. Native gzip files preserve exact decompressed bytes; native-files.json/manifest.json record SHA256 and size. Invocation receipt includes exact command/selectors/source/tree/pinned Runtime/clean state/environment/raw exit. The repository isolated runner prepends this selected checkout; pre-existing editable package metadata elsewhere remains an explicit environment limit. No dependency installation was changed.

Final hosted qualification still waits for PR428 integrated into actual release/14/plan-39 and Lead-owned exact-head Lanes. Actual68e7 destination S01 phases remain unknown after its collection abort. Automatic PR jobs are unqualified checkpoint observations, including cancelled/nonterminal runs; no manual cancellation or duplicate full hosted run was requested. No destination merge/write or board edit occurred. Root performed engineering/judgment; helper collected evidence only; review remains Lead-owned. Routes used: Unknown.
