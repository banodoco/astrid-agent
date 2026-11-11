> Historical 131-pair draft, superseded by [RESUME-118.md](RESUME-118.md). Do not use its branch or scope for the current proposal.

# A2 comparison resume: preparation only

No ref was changed and no CI was started for this design. Await the explicit provenance/coordination decision before preparing or pushing any triggering ref. This plan assumes the independently reported verification of the five named artifacts from cancelled run [43023353123](https://github.com/banodoco/astrid-agent/actions/runs/43023353123); direct artifact download returned 403 in this session; the embedded files were subsequently validated against the exact independently committed receipt bytes, rather than downloaded again from the artifact service. Do not infer receipt validity from artifact names alone.

The five embedded JSON files are copied from the committed astrid-dev `origin/main` evidence for PR #472. Local byte hashes match the frozen values below, and all five receipts record complete passing outcomes. This verifies those committed bytes; it does not resolve the provenance of the triggering push.

| Receipt | SHA-256 |
|---|---|
| r10-019 | `0d0aa5ec4497608117ee1efda6d880ace1022282d6e0fef35b354c185d1c3444` |
| r11-019 | `bedf88c1f4305b9dd940d878340853c03019166fcb91c961f0550d124f631bc7` |
| base-019 | `783769392cc4c3e943895aef7e815cdf25c7b6632d0f23c29c47fbfded02394d` |
| candidate-019 | `d0253725bc612dd83aeb1a7a470abe1c911306b6f5d3384c7723d29442a9558e` |
| r10-020 | `82b28a74b88297e56e0cb0b547a43bcea04cfdd75215594f17ab2f55bdfe7d21` |

The run was `push` attempt 1 at `fdf95fa5abfe894bb9954c93631367726e391bf1` on `scratch/r12-a2-full-019-052-20261111`, started 12:47:14 UTC on 2026-11-11, and ended cancelled at 12:48:26 UTC. All four first-range jobs were cancelled; later ranges were skipped. Its five listed artifacts are group 019 for r10, r11, base and candidate, plus group 020 for r10. Preserve and independently verify their exact receipt hashes, full known IDs/statuses, pin tuple, manifest digest, toolchain and census/seal results; retain the original run/artifact links and record any existing accounted failure. An incomplete current group, absent receipt, partial log or cancelled step is not evidence. Do not assume the other 020 sides completed merely because a job started.

## Exact remaining scope

| Side | First range | Second range | Third range | New group receipts |
|---|---|---|---|---:|
| r10 | 021–030 | 031–041 | 042–052 | 32 |
| r11 | 020–030 | 031–041 | 042–052 | 33 |
| base | 020–030 | 031–041 | 042–052 | 33 |
| candidate | 020–030 | 031–041 | 042–052 | 33 |

That is 131 new side/group pairs; the five verified pairs above complete the frozen 136 pairs for groups 019–052. The four chains preserve same-side range order, with at most one range for a side active at once and four jobs active overall. Group 020 aligns the prior r10 receipt with fresh r11/base/candidate receipts only after the owner accepts their environment and provenance comparability. Different GitHub-hosted VMs have the same requested image but are not proven identical to each other or to the prior local host. Group boundaries restart session fixtures and can miss cross-group effects. Preserve existing 001–018 evidence and all open 005, 015 and 017 potential regressions; no cancelled or partial run changes their status.

## Draft implementation, not authorized to run

Use a fresh exact scratch ref such as `scratch/r12-a2-resume-020-052-20261111`, checked absent immediately before any separately authorized creation. At the read-only check for this document it was absent. A single creation push would require the exact repository, ref and frozen control digest, zero before-SHA, non-forced and non-deletion event, attempt 1, no `workflow_dispatch`, no retry, and no force push. The implementation prohibits 019 for every side and 020 for r10 in both slot mapping and runner control, rather than relying on operators to skip those pairs. Every group driver validates all five embedded receipt JSON files by their frozen SHA-256 and exact group, side, pin, manifest, source workflow, ID and outcome checks before running. The files are the exact committed bytes from astrid-dev origin/main, PR #472; all five show complete passing results. Re-run the artifact verification and examine all relevant queued/in-progress jobs and other launch mechanisms immediately before any coordinated push; the current read-only query returned no A2-named queued or in-progress run, but is not a future concurrency guarantee. The repo-wide workflow concurrency guard is an additional control, not a substitute for this check.

Bind all allowed pairs to the same frozen 52-group manifest SHA-256 `885b264862a0e6e90698ed8a76a6b5b30e2dbeb1f3846ea96b356913b1a9e771` and the same exact four Agent/Runtime/VibeComfy/lifecycle pins, Python 3.11.17, Node 22.22.0, esbuild 0.27.7 and verified binary hashes already specified by the harness. Keep the same module selectors, `-vv --tb=line`, `-rA`, browser exclusion, build-hook ignore, and unchanged individual test timeouts. Use fresh per-group checkout, venv, home, temp, cache and basetemp with a bounded before/after same-UID process census. An unknown census, new surviving process, failed setup, invalid seal, incomplete ID coverage or failed upload stops that side's later work. Do not claim that process census proves a fresh kernel, no detached race or immunity to same-UID evidence tampering.

Upload each completed sanitized receipt to its fixed literal slot path before advancing; identify its actual group and side inside the receipt, since r10's first new slot is 021 while the others begin at 020. Freeze and verify the 131-pair allowlist and test that no pair is duplicated, omitted, cross-ranged or mapped to an already verified receipt. A completely accounted pytest failure is retained as a failure and may continue; propagate its fixed failure flag across that side's ranges and make the final side red. Seed that flag from the verified existing receipts where applicable, rather than silently clearing prior failures. A 0-ID side/group must produce a validated explicit absence/no-results receipt. Prior successfully uploaded receipts survive later cancellation subject to artifact availability/retention; the currently interrupted group can be missing or incomplete and requires separately coordinated work. Raw logs and failure text are not uploaded; sanitized summaries alone cannot establish exact failure-message equivalence or the A2 gate.

Proposed bound: at most 12 runner jobs, each capped at 360 minutes, for 4,320 runner-minutes (72 runner-hours) plus provider rounding. The ceiling can interrupt a group or leave later groups unrun; it does not shorten individual test timeouts or promise completion. Current free quota, price, usage period and runner availability are unverified; earlier read-only billing API calls were unavailable to these credentials. Do not execute on an assumption of unpaid capacity. Historical timing cannot produce a reliable estimate: 019–030 contains 169.09 seconds of known per-side test duration but 362–949 unknown IDs per side; 031–052 has zero known duration and 1,664–1,774 unknown IDs per side, before setup costs. Stage output and artifact service availability also remain subject to live service behavior.

Before proposing a trigger, independently review the exact allowlist/ref changes, fixed artifact paths and receipt validation; run lightweight offline guard tests only under a coordinated host lease. Verify any current GitHub usage/cost ceiling, provenance and no overlapping run immediately before a new branch creation. This document authorizes neither a push nor CI.
