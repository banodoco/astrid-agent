# A2 remaining 80 pairs — design only

This is a design and draft implementation, not trigger authorization or a gate decision. The reviewed parser source is `b4ddfb7dd9766d19526286e53cbc8768170178a6`. This handoff contains draft workflow source for the 80-pair shape; its proposed scratch ref has not been authorized for creation. Do not push a triggering ref, start CI, alter the candidate or treat an old receipt as newly generated. A later implementation and trigger require separate review and coordination.

## Frozen prior evidence and exact gap

[DESIGN-080-hashes.json](DESIGN-080-hashes.json) pins all 56 prior receipt JSON byte hashes, their immutable astrid-dev source commit `26041afe13b30e4b5d6b9520a9977def490f6c80`, source paths, workflow SHA, classification, exit, counts and field-generation markers. The JSON also contains the exact 80-pair remaining allowlist. Its SHA-256 at preparation is `064a69064ebe8edb36bfb2597b78c31b9ec806ad2065bdaae193b755b32e7ede`. All 56 local receipt bytes matched the committed source at that commit, with 56 distinct side/group pairs; read-only inspection matched manifest and pins, per-side expected/absent IDs, outcome counts and clear process census.

| Side | Existing group receipts | Missing pairs | Proposed same-side chains |
|---|---|---:|---|
| R10 | 019–040 | 041–052: 12 | 041; 042–052 |
| R11 | 019–034 | 035–052: 18 | 035–041; 042–052 |
| base | 019–027 | 028–052: 25 | 028–034; 035–041; 042–052 |
| candidate | 019–027 | 028–052: 25 | 028–034; 035–041; 042–052 |

Total coverage is 56 preserved plus 80 missing = 136 pairs in 019–052. Original 001–018 evidence stays intact. Missing group 028 base/candidate, 035 R11 and 041 R10 lack valid receipts; their raw output was not retained. The reviewed parser's synthetic counterexamples show formats it can safely handle, but do not establish that those were the live failure causes. No result is inferred from a cancelled or incomplete job.

Each side has preserved nonpassing outcomes: R10 114 across 10 groups, R11 106 across 9, base 21 across 4 and candidate 21 across 4. Counts are outcome counts, not a regression verdict. All four chains must seed and preserve `prior_failure=true`; completion cannot turn a side green merely because later groups pass. In preserved groups 023–027, every candidate failed/error ID also has a failed/error status on base; this status-only observation does not establish matching messages, causation or gate disposition. The 005, 015 and 017 potential regressions remain open.

## Receipt compatibility is a prerequisite

All 56 historical receipts declare schema 1, but their fields vary: the first 18 do not have `incomplete_reasons`; the newer 38 have it; none has the reviewed parser's later `additional_error_after_outcome_ids` field. A later implementation must identify each prior receipt by its exact fixed byte hash and source workflow, validate the known generation's original fields and exact frozen IDs/statuses, and preserve the original bytes. Do not add missing fields, rewrite old receipts, run the new parser against guessed old output, or reconstruct unavailable raw logs. Immutable hash verification does not settle source-run provenance, trust or cross-run comparability. Recheck the committed bytes and source revision when implementation is reviewed.

## Candidate bounds and controls

The 10-job shape in the table caps one attempt at 10 × 360 = 3,600 runner-minutes (60 runner-hours), plus provider rounding, with at most one live range per side and four concurrent runner jobs. Separate options are possible: eight side/range jobs would lower the ceiling to 48 runner-hours but place 14 groups in each base/candidate first job, increasing timeout and lost-progress risk. Neither shape guarantees completion; per-group setup repeats, historical durations are sparse, and a 360-minute ceiling can leave a group or later groups unrun.

Any later executable workflow must bind a fresh exact one-shot scratch ref, immutable fixed 80-pair allowlist and code revision, zero before-SHA, first run attempt and non-forced, non-deletion event. No automatic rerun or retry; recheck all relevant active/queued runs and the ref immediately before separately authorized creation. Carry exact frozen manifest SHA-256 `885b264862a0e6e90698ed8a76a6b5b30e2dbeb1f3846ea96b356913b1a9e771`, four Agent/Runtime/VibeComfy/lifecycle pins, Python 3.11.17, Node 22.22.0 and esbuild 0.27.7 hashes, original selectors and unchanged individual test timeouts. Freeze pair-to-slot mapping and reject every previously preserved pair; deterministic offline checks must prove each of the 80 appears exactly once and nothing else can run.

Use per-group fresh checkout, venv, home, tmp, cache and basetemp; the bounded same-UID process census must fail closed on residue or unavailable census. Explicitly retain the limits: sequential groups share VM/kernel and can leave undetected external or detached state; same-UID adversarial tampering is not ruled out, and comparability with the prior host or other runner jobs is not automatic. Require separate literal-path, sealed sanitized artifact upload before advancing; incomplete, absent, unsealed or failed-upload evidence stops that side. Fully accounted test failures continue with the failure flag retained; NO_RESULTS needs verified absence IDs. Cancellation can lose the current group, while earlier successfully uploaded artifacts survive subject to service and retention limits. Sanitized receipts omit raw failure text and cannot alone settle exact message comparison or A2.

Before any future triggering push, independently review exact code, prior-byte/schema verification, failure-flag propagation, fixed upload paths, reusable-workflow behavior and no-overlap checks. Confirm provenance, authoritative available free quota and provider rounding; quota, billing, capacity and actual cost are currently unverified. This document, its JSON, and its source-only handoff do not authorize execution.
