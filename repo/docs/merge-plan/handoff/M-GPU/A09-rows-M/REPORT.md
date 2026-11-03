# 29_A09_ROWS report

## Handoff

- Standalone rows: `/home/user/astrid/ws-M/queue/running/29_A09_ROWS/row-file.json` — SHA-256 `674bb9c9dec424eaf92e748de50504afd657ee0b1c1b4ce04920d3a820b57396`.
- Unified additive assertion patch: `/home/user/astrid/ws-M/patches/29_A09_ROWS.patch` — SHA-256 `4869d6ee30afe4901df7368030333fa52bb50d810114904278e29160f5286801`.
- Patch adds only `tests/fixtures/a09_scenario_assertions.json` to the isolated candidate tree. It does not touch `tests/fixtures/generation/agent_journeys.json`.
- Relay request: `NEEDS: orchestrator relay a09-rows <path>`

## Source and compatibility

Used the accepted cumulative source at `/home/user/astrid/ws-M/queue/done/28_V08_ROUND1B_REFRESH/work/package/`. Source digests:

- `manifest.json`: `46559d4e781e77ce50acea50a32b75b7ad35ffdd72d960249c0bb69db465b840`
- `CASE-CONTRACTS.md`: `7a6d931857af8c6b28aa3635e5112eb4f6b1bfd72c597fb81b92ab61d1be1f2c`
- `case-commands.json`: `a3890cf1af191cb9939a2bce506a8016789f5cffaa08855c661599948fc79542`
- `cases/v08-rc/README.md`: `6fea88f8a785f949c39b6f034e716452307958dcb2e321f081e14a2c4dd54379`

Rechecked the R05/R06 gap narrowly against the cited task 025 main and subreports and local row/submission/R05/R06 filenames in recent W and proof-codex queue task areas. No new Pallando row submission, exact format, lane command, receipt schema, or assertion interface was found. The package's placeholder references do not define producer contracts. Accordingly, the rows and patch are portable/reviewable only and make no exact-compatibility claim.

The four cases are exactly `human-image`, `managed-missing-model`, `workflow-input`, and `failure-owned-release`. All four remain `blocked`, each with explicit assertion objects, required evidence fields, unresolved dependency IDs, named owners, and `gpu_status: not run`, reflecting the accepted manifest. Package-only synthetic adapters are identified as synthetic; no installed, GPU, repair, release, or producer-backed success is claimed.

The A09 C0 gate is recorded separately because these four requested rows do not establish the H3/C0 media journey. Its approved input SHA-256 remains `null` pending V09 custody approval, not a guessed digest. It retains the 48-frame requirement or an approved replacement digest/count, playable and fully decoded hash-checked 1024x576 output at 24 fps with audio, and setup/cost/release evidence.

## Checks and results

- `python3 -m json.tool .../row-file.json`: passed.
- `python3 -m json.tool` on the patch's added JSON artifact: passed.
- Focused structural check: passed; four unique requested IDs, all blocked, owner/dependency/assertion/evidence fields present, C0 approval slot unresolved with required media fields.
- Against an isolated shared-object clone at candidate base `cfa0f054bf5b7f070ae222248cd0b74885209096`, `git apply --check /home/user/astrid/ws-M/patches/29_A09_ROWS.patch`: passed. Patch applied in that clone; reverse check passed.
- `git diff --check` after marking the added artifact intent-to-add in the isolated clone: passed. The sole changed path was `tests/fixtures/a09_scenario_assertions.json` (448 added lines).
- No product tests, installations, network/provider calls, pod operations, or GPU work were run.

The isolated verification clone at `/home/user/astrid/ws-M/work/29/astrid-agent` was removed after checks. No task-created basetemp, TMPDIR, review copy, or scratch clone remains. The source checkout was not modified.

## Remaining evidence

A09/A08 still needs to bind these rows to the shared journey contract and supply approved human-server, workflow, missing-model, and failure fixtures. P must supply supported installed target identity/readiness interfaces; A07 must supply an exact supported repair and consent contract; G-08 must supply candidate-installed generation/readback and release operation; F07/R04/R06 must supply owned release readback and measured VRAM method. V09 must supply approved C0 input identity/digest and custody evidence. Until those producer inputs and their execution evidence arrive, every row and the C0 gate remain blocked.
