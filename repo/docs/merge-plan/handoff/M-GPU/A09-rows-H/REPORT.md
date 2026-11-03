# H-31 — A09 H3 and approved C0 row-file

## Handoff

- Row-file: [`row-file.json`](row-file.json) — SHA-256 `689073e50d6531d3c573ecad32fa37a663ea06c118f525fbeaed7ba92f846f26`.
- Relay request: `NEEDS: orchestrator relay a09-rows /home/user/astrid/proof-codex/queue/running/31_A09_H3_ROWS/row-file.json`
- The file contains exactly two unique assertion rows: `A09-H3-public-generate` and `A09-H3-C0-approved`.

## Sources and format

- Prior A09 draft and review: `/home/user/astrid/proof-codex/queue/failed/30_H3_CP2_PREVIEW/A09-H3-rows.yaml` and `REPORT.md`.
- Accepted machine-readable row format reference: `/home/user/astrid/ws-M/queue/done/29_A09_ROWS/row-file.json` and `REPORT.md`; additive artifact reference: `/home/user/astrid/ws-M/patches/29_A09_ROWS.patch`.
- A09 scope: `/home/user/astrid/ops/specs/_v51/projects/A.md`, §A09. C0 requirements: `/home/user/astrid/ops/specs/_v51/projects/V.md`, §V05.
- C0 metadata/hash provenance cited by task 30: H-08 C0 request and asset-map metadata under `/home/user/astrid/proof-codex/manager/H-08-hc-gpu/inputs/c0/`, and `h18-plan-digest/evidence/final-input-integrity.json`. The four expected hashes in the row match the corresponding integrity metadata entries. The approved input is identified by digest only; no media bytes were read.
- Task 30 also cites `/home/user/astrid/proof-codex/work/26/H-08-hc-gpu/h26/STATUS.md` for the approved 48-frame, 1024x576, 24 fps, audio-present tuple.

The task-29 format uses structured case IDs, status, assertion objects, required evidence fields, unresolved dependencies, readiness, and GPU state. The requested `docs/merge-plan/handoff/M-GPU/A09-rows-M` directory was not present at that stated location in the available workspace; task 29's accepted row-file/report and patch were used as the available format artifacts, as also listed in the task brief.

## Validation

- `python3 -m json.tool /home/user/astrid/proof-codex/queue/running/31_A09_H3_ROWS/row-file.json`: passed.
- Focused structural check: passed. It confirmed exactly two unique requested IDs, the required C0 digest/media tuple and four source/mask/request/asset-map hash assertions, playable-output and output-hash/readback assertions, and explicit pending render/setup/cost/release/readback evidence. Both rows are marked blocked, execution `not_run`, and GPU `not run`.
- The structural check also confirmed that the row-file contains no private media payload, media-file references, or `agent_journeys.json` reference. No `agent_journeys.json` or existing A09 row file was edited.
- Concise command results are retained in `validation.log`.

## Limits

This is an assertion handoff only. No generation, GPU/render, setup, cost, output hash, output readback, or release was run or observed; these remain explicit unresolved dependencies. C0 hash values are assertions sourced from the cited integrity metadata, not claims that media bytes were independently rehashed during this task. No private media content is included.
