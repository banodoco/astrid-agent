# M46-EXTERNAL-JOIN-R3

## Disposition: incomplete; admission remains vetoed

The published lane is `lane/M-CI/M46-EXTERNAL-JOIN-R3@73bf76fec0b4c40bf1770763d61a77f4b5fc7591`. It tightens a **diagnostic** Actions index; it does not implement the constrained workflow-construction binding, emit `external_checkpoint_join`, or admit M4-6 evidence. No integration handoff, hosted success, or complete acceptance evidence is claimed.

The exact published M-QUAL producer ref was fetched and rechecked: `lane/M-QUAL/R-36-M46-WORKFLOW-BINDING-R1@10daf020ccb117e717cec7f40ed6e5131266fbf7` (parent `8cc6881749edc6b2e998d3031f2220f45d5763de`). Its `.github/workflows/journey.yml` has Git blob `221dff2c375527287567ba4c29ee9cf904a24a40`, SHA-256 `6968888ed8cb0548dcd3f9141e4b7248159e8e7333906332e04a59635e435630`. The workflow names the matrix job `m4-6-local-${{ matrix.job_id }}` and its artifact `m4-6-local-${{ github.run_id }}-${{ github.run_attempt }}-${{ matrix.job_id }}`; the slot key includes OS. This inspection is not a completed structural proof that no other workflow job can produce the same artifact name. The matrix is dynamically sourced from code checked out using `github.workflow_sha`; the workflow invokes `--shared-executor m4-6`, which enforces `local-disposable` and writes `m4-6-local-result.json`. The workflow explicitly retains the fail-closed hosted native-service aggregate. The hosted predicate instead requires a hosted receipt and four machine-observation files. A positive hosted fixture under this pinned construction would therefore not establish that this workflow can produce the required bundle.

Do not equate this issue with a rejection of the 04:05:10Z ruling. A conforming producer and complete verifier could use that route. The route's stated limit is reliance on GitHub's run/artifact association and the pinned workflow, not a cryptographic job signature. This lane does not record `binding_method=workflow-construction@<harness_sha>` because it has not established the conditions.

## Interface and remaining work

`check_journey_evidence.py --m4-6-api-index --candidate FILE --slot-id SLOT --run-id ID --run-attempt N --job-id ID --artifact-id ID --harness-sha SHA` reads `GH_TOKEN` or `GITHUB_TOKEN`, fetches workflow contents at the SHA, run, attempt-scoped jobs, run artifacts, artifact metadata, and archive. It prints a diagnostic index with `admitted: false` on metadata success and exits 1 in all cases. The fixture seam `m4_6_actions_index` accepts caller-supplied observations and is not an authentication boundary. This revision requires one exact `m4-6-local-<slot>` job before comparing its numeric ID, rejects conflicting known runner labels, checks the run/attempt-scoped artifact name, and requires the listed artifact's metadata to match the fetched record. Archive size and SHA-256 are checked against API metadata as before.

Still required: a pinned producer that uploads the hosted bundle, complete structural uniqueness validation at the actual executed workflow and matrix revision, authenticated API/run/attempt/job/runner and artifact association, safe archive parsing, receipt-bound per-file digest checks, independently selected exact tuple and checkpoint-class checks, and a positive authenticated-construction fixture plus all required negatives. The current diagnostic checker does not validate workflow structure or parse the archive, and does not establish the receipt binding. It checks the selected runner label and records a runner name but does not independently establish OS from a hosted receipt. These gaps prevent admission.

The candidate tuple used in synthetic tests is app `33f1de3f33dfb1e9799ca745c5c3b8437173e06e`, agent `3cc84be5d8c565f1a4a500dcf713478498e34885`, runtime `a3e3f2ed95d6363e7f5b463b521da94221cf6f00`, pinokio `c5b42701641889ecb916c60cde09a717f83d62ff`, manifest SHA-256 `9999999999999999999999999999999999999999999999999999999999999999`. It is a fixture, not a published or hosted tuple. No real hosted artifact was fetched. The synthetic diagnostic archive SHA-256 is `74a06ea6113b7f98d70ba2286ddaa21a9a17e25ed2bb8736a823dbc46177eb44`.

## Revision, checks and coverage

Base and sole parent: `ae8415a3daaa56843ecf6353ebe5a4dc0858fc55`. Head tree `926743dc5099a1b449ac4ffad3d3aab0dc50c60d`. The producer ref was inspected but not merged. Changed paths from base are only `scripts/check_journey_evidence.py` and `tests/test_m4_6_actions_index.py`.

- Task clone command in the brief: exit 0. Branch creation, initial WIP push, final commit and push, and remote-head verification: exit 0.
- `PYTHONPATH=/home/user/merge/ws/M-CI/repos/astrid-runtime uv run --no-project --with pytest --with jsonschema --with pyyaml python -m pytest -q tests/test_m4_6_actions_index.py tests/test_journey_predicates.py`: inner exit 0, 52 passed, one existing unknown `timeout` configuration warning. Log and inner exit are retained here.
- `python3 -m py_compile scripts/check_journey_evidence.py tests/test_m4_6_actions_index.py tests/test_journey_predicates.py`: exit 0.
- `git diff --check ae8415a3daaa56843ecf6353ebe5a4dc0858fc55 HEAD`: exit 0. Changed-path audit and clean checkout: exit 0.

Synthetic tests cover wrong run and attempt, missing/duplicate/wrong-name job, wrong runner label or missing runner name, renamed/duplicate or prior-attempt artifact, artifact run/ID/size/API digest discrepancies, workflow-ref test seam, downloaded digest mismatch, wrong tuple, continuity relabel and bundle self-authority. The wrong-workflow test checks ref equality only, not structural workflow validation; the tests do not establish receipt/archive binding or a positive hosted construction. The existing positive case checks diagnostic metadata only and remains non-admitting.

## Bounded co-review request

PEER: M-QUAL: Please co-review exact head `73bf76fec0b4c40bf1770763d61a77f4b5fc7591` for diagnostic job/artifact name compatibility and the producer/hosted-bundle gap against your published head `10daf020ccb117e717cec7f40ed6e5131266fbf7`. Please identify a pinned workflow that uploads the hosted five-file bundle and supports the 04:05Z construction proof. This is a bounded source/interface review request, not an inventory, journey, or release co-sign. The manager's existing `STATUS.md` request asks for R3 co-review; no response is recorded here.
