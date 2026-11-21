# R14-P39-19 / Plan39 S01 proof checkpoint

**Historical failing-before checkpoint.** The managed-shot-edge fixture repair is published at `de81bcd7e0518a8fc30b9d5ff17882240b6cb367` on `p39/media-shot-links`, from exact destination `68e7aa8593d13eaeb6a55da1d97bed8b494df15b`. It is not a completed packet. The required 104 calls have not executed.

## Results by source and phase

| Source | S01 IDs selected | Setup | Calls | Teardown | Original inner exit |
| --- | ---: | --- | --- | --- | --- |
| Historical Lanes 44454822934 at `086845def6ec28216384163d835f47d8cebbae81` | 104 | 104 ERROR | 104 NO-RESULT | 104 PASS | 1 on shards 2 and 3 |
| Exact destination Lanes 44462370783 at `68e7aa8593d13eaeb6a55da1d97bed8b494df15b` | 104 | 104 NO-RESULT | 104 NO-RESULT | 104 NO-RESULT | 4 per shard; later XML summary 1 |
| Local focused candidate `de81bcd7e0518a8fc30b9d5ff17882240b6cb367` | 104 | 104 ERROR | 104 NO-RESULT | 104 PASS | 1 |

The historical roster is 78 IDs from shard 2 and 26 from shard 3. All 104 original failures have the managed-edge refusal at `_doc_patch.py:372`. All 104 local candidate setup failures have the later shared-placement typed-fps refusal at `validation.py:247`. **No newly exposed call failure can be reported, because no call ran.** Each ID and all three phase records are in [outcomes-by-id.json](outcomes-by-id.json). Reference selection is not execution evidence.

| File in tests/editing_conformance | Selected | Setup ERROR | Calls executed | Call NO-RESULT |
| --- | ---: | ---: | ---: | ---: |
| `test_commit_runtime_media_manifest_inverse.py` | 2 | 2 | 0 | 2 |
| `test_commit_runtime_occurrence_media.py` | 48 | 48 | 0 | 48 |
| `test_commit_runtime_parent_media_swap.py` | 24 | 24 | 0 | 24 |
| `test_commit_runtime_video_unplace.py` | 4 | 4 | 0 | 4 |
| `test_recipe_m2_auto_placement.py` | 6 | 6 | 0 | 6 |
| `test_recipe_m2_placement_create_unplace.py` | 4 | 4 | 0 | 4 |
| `test_recipe_m2_shared_auto_placement.py` | 6 | 6 | 0 | 6 |
| `test_recipe_m2_shot_pool.py` | 7 | 7 | 0 | 7 |
| `test_recipe_occurrence_media.py` | 3 | 3 | 0 | 3 |

The exact-destination execution used `/home/runner/work` as its root and attempted sibling dependency collection. Every shard had 75 collection errors, including an Agent/Runtime `tests.conftest` import mismatch, before any S01 phase report. This is the separate P39-13 wrapper boundary. The canonical `baseline-diff.py` refuses this empty destination comparison with exit 2; no pass/fail parity or no-new-failure claim follows.

## What changed

One test-only `authored_shot_patch` builder preserves the exact accepted presence/value of `internal_timeline_revision_id`, `shot_id` and `revision_id`, and takes only authored fields from the requested fixture payload. Both stale fixture paths use it. Existing full authored/shared-closure checks remain, and the fixture now includes public persisted parent, shot and internal-revision readback before test retry/inverse work. All 51 original assert ASTs, every original test-body AST, and every existing `pytest.fail` call in the two edited files are unchanged. Product, pins, workflows and Plan38/55-owned tests are unchanged.

## Newly exposed product-path refusal

The unmodified occurrence image/add case was observed once with a source-neutral Python trace. It does not alter product/test objects, requests, exceptions, assertions, selection, or timing budgets. Its native outcome remains setup ERROR / call NO-RESULT / teardown PASS, exit 1.

Before adding the sibling, public persisted reads verified the accepted shot's managed link, the exact authored shot payload and the exact authored internal payload: all equal. The parent had one owner at this point. The observer emitted the same state three times because the multiline call revisited its first line; these are not three runs.

When `occurrence.place-shared` adds the second placement, `commit.py:1433–1441` adapts only the candidate to local per-occurrence shot keys. The baseline fps path names `chapter-shot`; candidate fps paths name two `shot-placement-...` local keys. All values remain 24. Both candidate source mappings preserve the same physical shot revision, internal revision and digests as the baseline. `_validate_fps` at `validation.py:245–247` compares the local-key-derived path strings and refuses the candidate. The preserved observer receipt records the maps and exact exception.

This is a distinct possible product defect in the shared-placement validation path. No numeric fps migration, validator relaxation, fixture reordering around the failure, or product correction has been made. The accepted single-owner readback does not prove shared ownership: sibling acceptance, retry and inverse remain open. Lead disposition was requested before any such change.

## Custody and reproduction

Native JSONL, XML, logs, reference and execution manifests are retained as deterministic gzip files. `native-files.json` names each original-byte SHA-256 and size. The 104 local selectors exactly equal the historical roster; the unchanged repository runner performed a reference census and validated execution evidence. `*-invocation.json` records the exact argv, source SHA/tree, clean/dirty state, pinned Runtime checkout, environment and original return code.

The first local before run used the exact destination, three representatives, and exited 1 with the original managed-edge refusal. The first three-case after run used a dirty fixture checkpoint; its tracked patch and untracked helper bytes are retained separately. The all-104 measurement and source-neutral diagnostic used clean published `de81bcd7e0518a8fc30b9d5ff17882240b6cb367`. Neither is hosted qualification. The existing environment's installed editable package metadata points to another checkout; the repository's isolated pytest entrypoint prepended this selected checkout, and native traces identify these selected source paths. No package or dependency installation was changed.

Historical artifact ZIP digests were independently matched against live GitHub metadata and extracted native members. The exact-destination artifacts were verified the same way. The original shard 3 outer Actions shell exit remains unknown; its native child exit 1 is explicit. No missing result is filled by inference. Dependency references in the historical provenance remain in the original artifacts and canonical declarations; their literals are not copied into this proof.

Canonical refs: [packet](https://github.com/banodoco/astrid-dev/blob/f5c2014e8483aac1ea1051b0011bee986bcea0f9/releases/14/packets/R14-P39-19.md), [original run](https://github.com/banodoco/astrid-agent/actions/runs/44454822934), [destination run](https://github.com/banodoco/astrid-agent/actions/runs/44462370783), [PR #442](https://github.com/banodoco/astrid-agent/pull/442), [causal BLOCKED](https://github.com/banodoco/astrid-dev/issues/796#issuecomment-7017066664).

## Subsequent disposition

After this checkpoint, [Lead DECISION 7017146954](https://github.com/banodoco/astrid-dev/issues/796#issuecomment-7017146954) classified the refusal as a product defect and authorized this packet to correct only the authenticated shared-placement validation path in a separate named commit. The complete 104-ID measurement here preceded that revision. This document preserves the failing-before results; it does not describe subsequent candidate outcomes. Hosted qualification still requires P39-13 collection repair integrated into the actual destination. No merge or board edit was performed.

Routes used: Unknown — this ChatGPT packet has no configured model-usage record. Root performed the engineering and judgment; one helper only recovered and reconciled native evidence. No independent review was commissioned; review remains Lead-owned.
