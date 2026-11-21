# R14-P39-19 — accepted sibling capture checkpoint

**Measured source: `42be807df9053bcff55a0ef82e174b83fd9b6580`, tree `5f1b08b1afc8033a0e60c57ef2b6014f776a8657`. Not Done or hosted-qualified.** This named fixture-only commit follows [Lead7017788634](https://github.com/banodoco/astrid-dev/issues/796#issuecomment-7017788634). It changes one file, five added/three removed lines: use the public capture_occurrence_place_shared helper on the accepted popup-baseline document in place of the hand-built sibling command. All existing assertions/test bodies and Runtime command-claim checks are retained. The original authored parent/occurrence, shot/internal payload, managed-link, shared-owner and public persisted readback checks execute before the later boundaries below.

## Complete original 104-ID run

Clean Agent42be and clean pinned Runtime2537fed24385ec1c24f48a812e09af4bb63fb794. Native/raw exit **1**, **275.97s**, summary **54 failed, 48 passed, 2 errors**. Setup102 PASS/2 ERROR; call48 PASS/54 FAIL/2 NO-RESULT; teardown104 PASS. All original/reference/execution/native/JUnit IDs match exactly104; no missing/extra IDs, duplicate phases, skips, xfails or subtests. Reference and execution manifests are byte-identical. [Every ID and phase](outcomes-by-id.json) also retains its original c113 phase comparison.

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

## Newly reached boundaries

| Boundary | Phase/count | First causal location |
| --- | --- | --- |
| Literal popup-baseline revision lookup | 2 setup ERROR | manifest_inverse fixture:40 -> Runtime service:2993, NotFoundError parent composition revision not found |
| Provider executed a different command | 25 call FAIL | Runtime timeline_commit_auth.validate_claims:337; 8 occurrence lifecycle assertions see this instead of selected-media-catalog refusal, 17 direct Runtime rejections |
| INSTALL_RECORD_OWNER import | 2 call FAIL | parent_media_swap:285 -> tests/conftest:186 -> helpers/runtime:124 -> Runtime daemon:99 -> astrid_runtime/update:28 |
| Inverse stage materialization changes internal payload | 4 call FAIL | Existing video_unplace:284 undo -> Runtime _inverse_stage_boundaries:1979; separate #1002 |
| Project scope belongs on the canonical document | 23 call FAIL | Agent commit._verify_document:94, raised:46; recipe auto-placement6/create-unplace4/shared6/shot-pool7 |

The 25 command-claim calls comprise occurrence12, parent-media10 and recipe-occurrence3. Repeated error text does not prove that these later commands share the corrected sibling omission. No further command, assertion or guard was changed. Reports: [new setup boundary7018438706](https://github.com/banodoco/astrid-dev/issues/796#issuecomment-7018438706), [call boundaries7018496355](https://github.com/banodoco/astrid-dev/issues/796#issuecomment-7018496355).

The HTTP import trace names `/tmp/astrid-runtime-archive-lreryndx/astrid_runtime/paths.py`. That temporary file no longer existed during the read-only provenance check, so its bytes/hash/symbol contents are UNKNOWN. The pinned Runtime paths.py is4633 bytes, SHA256 d7fc9509c24cbc07154acb2f94a80cd8d500a4f28a86479230dc0632b1807e6e; it defines INSTALL_RECORD_OWNER at19 and uses it at47. No environment-versus-product attribution, environment modification or targeted rerun follows from this observation.

## #1002: earliest stored/reconstructed difference, all four IDs

The four unchanged video_unplace test_capture_commit_reload_undo_redo_and_restart_safe_retry parameters [image-False], [image-True], [video-False], [video-True] each retain setup PASS/call FAIL/teardown PASS in the ordinary run. The original c113 outcomes remain immutable in the parent product-validation proof. A separate source-neutral four-ID diagnostic on42be preserves the same invalid_records refusal, raw exit1, four call failures, **88.61s**.

[Per-ID differences](inverse-differences-by-id.json) and the compressed observer stream retain the full expected/reconstructed images, native writes, source step, intermediate states and public stored readback. In every case, the expected image exactly matches the publicly persisted internal payload and content_digest.

- Stored clip order: **clip-a, placed, clip-b**.
- Reconstructed clip order: **clip-a, clip-b, placed**.
- First differing clip index: **1**, stored id placed versus reconstructed id clip-b.
- Clip objects keyed by id are exactly equal; every non-clips payload field is exactly equal. The final observed mismatch is ordering only.
- keep=False: native restoration write index0 changes [clip-a,clip-b] to [clip-a,clip-b,placed].
- keep=True: writes0–2 remove the pool/empty app ancestors without changing clip order; restoration write3 then appends placed.

This identifies the earliest native restoration step yielding the wrong restored position; it does not assign responsibility between producer/capture and authenticator contracts. The trace does not modify source, requests, objects, exception handling, assertions or timing budgets. Public readback happens after the failed call, before fixture teardown. Runtime's inverse guard and Agent inverse code are unchanged. [Lead report7018464874](https://github.com/banodoco/astrid-dev/issues/796#issuecomment-7018464874), [issue1002](https://github.com/banodoco/astrid-dev/issues/1002).

## Preservation and subsequent authorization

All392 original assert ASTs and104 test-function ASTs across the nine roster files remain unchanged from68e7; seven files byte-identical. The audit is assertion-preservation.json. Native gzip files preserve decompressed bytes; native-files.json and manifest.json record hashes. Invocation receipts preserve exact argv, selectors, source/tree, clean state, pinned Runtime, environment and raw exit. The ordinary104 measurement and four-case observer are separate runs. Existing editable package metadata points elsewhere; the repository isolated runner prepends this selected checkout, as recorded in the native traces. No installation was changed.

After this completed measurement, [Lead7018497811](https://github.com/banodoco/astrid-dev/issues/796#issuecomment-7018497811) and [detailed PR ruling7018486500](https://github.com/banodoco/astrid-agent/pull/442#issuecomment-7018486500) authorize a separate revision-identity-only migration for the two manifest parameters. This checkpoint preserves42be before that further change. The real text binding/CAS bytes, both shared occurrences, explicit media-baseline seed identity and undo/retry/history checks must survive. A missing binding must be reported before expanding setup.

Actual destination remains68e7 and PR428 is unmerged. Its original Lanes outcomes remain unknown after collection abort. Final hosted proof waits for integrated428, actual destination and Lead-owned dispatch. Automatic PR checks are unqualified checkpoint observations; cancellations are not product passes. No manual cancellation, full hosted dispatch, destination merge/write, board edit, inverse-contract change, Plan38/55-owned edit, assertion weakening, skip or budget change. Root performed engineering and judgment; helper collected evidence only; review remains Lead-owned. Routes used: Unknown.
