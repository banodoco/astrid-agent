# GPU journey evidence contract

The inventory is `GPU_MATRIX` in `scripts/journey_matrix.py`. Each entry is a
scenario with `runner=runpod`, `shell=sh`, `mode` and named checks/exits. The
RunPod lane uses `journey-result.json`, the existing `evaluate` checks, and
`check_journey_evidence.py --aggregate-gpu`; this is an additional aggregate,
not a substitute for the other release aggregates. It requires all twelve rows
exactly once. Duplicate JSON members at any depth are rejected.

The candidate file is validated before the GPU aggregate: `app`, `agent`,
`runtime`, `pinokio` must be full lowercase 40-hex commits; `manifest_sha256`
must equal the SHA-256 of the App checkout's `public/install-manifest.json`;
App HEAD must equal `app`; all three bootstrap `artifacts` hashes must match
`public/install`, `public/install.sh`, `public/install.ps1`; manifest agent and
runtime SHAs must equal the candidate. These are actual bytes and git checks,
not statements inferred from a row.

| Receipt field | Meaning | Gate check |
| --- | --- | --- |
| `schema_version` | Receipt version | integer 2 |
| `receipt_kind` | GPU installed journey | `gpu-installed-journey` |
| `scenario` | Inventory key | one R1–R10, PRIMARY, SECONDARY; exactly one per required key, no unknowns |
| `mode` | Execution topology | `attached` for PRIMARY; `pod` for all others |
| `status` | Overall row outcome | `pass` |
| `runner_image` | Executor label | `runpod` |
| `runner_provenance.RUNNER_OS`, `.ImageOS`, `.ImageVersion` | Executor OS, image family and observed image version | `Linux`, `runpod`, nonblank string respectively |
| `os_build` | Observed OS/build | string starting `Linux-` |
| `shell` | Invoking shell | `sh` |
| `source_override_test_mode` | Source override admitted | must be boolean false |
| `final_cleanup` | Cleanup outcome | `pass` |
| `manifest_sha256` | Manifest actually used | 64 lowercase hex, equal frozen candidate digest |
| `candidate_checkouts.{app,agent,runtime,pinokio}.sha` | Actual checked-out candidate revisions | each full 40-hex and equal frozen candidate |
| `sources.app.sha` | Installed App source | equals frozen candidate app |
| `sources.{agent,runtime}` | Actual installed source descriptors | each exactly equals corresponding manifest object and its `sha` equals candidate |
| `installed_tuple.{app,agent,runtime}` | Installed component revisions | each equals frozen candidate |
| `installed_tuple.schema_digest` | Installed Runtime schema digest | equals nonempty manifest `runtime.schema_digest` |
| `installed_tuple.tools` | Installed tool versions | exactly `{name: version}` for manifest `tools` entries with a nonempty version |
| `import_origins` | Installed import locations | truthy value; capture a nonempty map of package to actual installed path |
| `checks.{task_settled,workspace_content,one_runtime,unrelated_sentinel}` | Standard installed journey assertions | each `pass` |
| `checks.<scenario assertion>` | Assertions specific to that GPU row | every named assertion in `GPU_MATRIX` is `pass` |
| `inner_exits.<name>` | Exit of each required named step | every name in `GPU_MATRIX` exists and is integer 0, not boolean |

For expected negative behavior, such as R2 low-disk refusal, `inner_exits`
records the exit of the assertion/probe that verifies the expected refusal;
retain the underlying nonzero product exit and logs as evidence. An assertion
must be based on the named behavior, not just a wrapper reporting success.
R4 requires both image sizes and R6 requires missing-model, repair and rerun.
PRIMARY requires the completed task, local Runtime, and file assertions.

The emitter is `emit_gpu_receipt(evidence_dir, scenario_id, variant=None)` in
`journey_matrix.py`. A live runner writes observed receipt fields to
`journey-capture.json`; the emitter returns those fields, with absent ones null.
Write the returned object as `<results>/<scenario>/journey-result.json`. It also
reads historical PRIMARY evidence, a replay `out/rows-final.json` with explicit
`variant` (`fixed`, `rc0`, or `shakedown`), and a selected R4/R6 historical
receipt directory (use its filename as `variant` if ambiguous). Historical
facts do not establish fields that were not recorded; nulls fail the gate.
Replay receipt paths and hashes are checked against the index. No runner should
copy expected candidate values into observed fields without measuring them.

Run the release gate with `python scripts/check_journey_evidence.py
--aggregate-gpu --candidate CANDIDATE.json --app-checkout APP --results RESULTS`.
It validates the frozen tuple first and has no subset or override option. The
Python evaluator's `required` argument is for focused diagnostics only; a
focused success is not an aggregate release verdict.
