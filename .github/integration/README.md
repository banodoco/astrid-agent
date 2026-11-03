# Local regression evidence contract

`int/regression-green` is evaluated on the orchestrator, not in GitHub Actions: the broker journal is local and is not transported to GitHub. The existing reusable hosted check is always red, including on dispatch and failed/skipped prerequisites. It must not be interpreted as the local result. The sealed finalizer must be invoked even if producers failed or were skipped; it currently refuses green as described below.

Install Python 3.11+, `jsonschema` and `PyYAML`. For a ledger and provenance diagnostic, run:

```sh
python scripts/ci/local_regression_green.py \
  --tuple tuple.json --inventory reviewed-inventory.json --coverage reviewed-coverage.json --ledger ledger.jsonl \
  --broker-root /home/user/merge/podq --hosted-root downloaded-artifacts \
  --freshness freshness.json --not-before 2026-10-31T23:10:39Z
```

All arguments are required. This diagnostic does not evaluate feature obligations and always exits nonzero; the sealed aggregate below also currently refuses `int/regression-green`. The tuple, reviewed **complete** inventory, ledger, freshness receipt, cutoff, broker journal, downloaded artifacts and API metadata are trusted inputs provided by M-INT; this program does not fetch or authenticate them. The cutoff is a timezone-bearing timestamp chosen for this evaluation, at least as late as the start of the required run; the evaluator also requires evidence no older than tuple creation and no later than the current time. A C-07 `{"check":"C-07","result":"pass","tuple_digest":"...","repo_shas":{...}}` receipt is required; M-INT must verify the actual C-07 job succeeded and supply the receipt only then. Local possession of a JSON receipt is not an authentication mechanism.

The separate reviewed coverage file is `{ "lanes": [{"repo":"runtime","lane":"unit-1","platform":"linux"}, ...] }`; its exact suite/shard set must match the test inventory. Both files must be reviewed against the approved coverage; the evaluator cannot detect the same omission from both. The inventory has the prior P-04 shape, extended with one `source` per lane. Its `lanes` array must enumerate **every** required suite, platform, shard and full test node ID, and mark protected journeys. The evaluator checks exactly this reviewed list; it cannot infer omitted suites or prove inventory completeness. C-05's approved inventory and sequential plan must be translated into this file and reviewed, including all eight Agent unit shards, all four integration shards, both release Python versions and hosted OS matrices. A synthetic example (not the production inventory):

```json
{"tuple_digest":"<digest>","lanes":[
  {"repo":"runtime","lane":"unit-1","platform":"linux","tests":[{"node_id":"tests/test_a.py::test_a","protected":false}],"source":{"kind":"broker","id":"CI-job-1","job_sha256":"<reviewed sha256 of exact job.yaml>","checkouts":["runtime"]}},
  {"repo":"agent","lane":"unit-1","platform":"windows","tests":[{"node_id":"tests/test_b.py::test_b","protected":false}],"source":{"kind":"hosted","id":"artifact-1","checkouts":["agent","runtime"],"run_id":123,"run_attempt":1,"job_id":456,"workflow_path":".github/workflows/integration.yml"}}
]}
```

Each source id is unique. The tuple digest is SHA-256 of UTF-8 JSON with sorted keys, compact separators and no ASCII escaping, excluding `digest`. Each manifest has `repo`, `lane`, `platform`, `tuple_digest`, the complete `repo_shas` map, `checkouts` (effective repository-to-SHA map exactly matching source.checkouts), `junit_sha256`, `created_at`, `result` and integer `test_exit_code`. A successful test command uses `success` and 0. A completed command with test failures uses `failure` and 1, and must contain corresponding JUnit failures, all matched by admitted ledger rows. Infrastructure errors, skips, collection errors, and contradictory exit/JUnit outcomes are red. A source's identity and checkout list, including every sibling actually used, must be reviewed; the evaluator cannot infer sibling use from arbitrary shell commands. The source job spec digest must be reviewed and obtained independently of the evidence submission. Spec text must include each declared repository URL and exact SHA. This binds the approved command but does not execute or prove arbitrary shell semantics.

For broker lanes the root contains `jobs/<id>/job.yaml`, `result.json`, and root-level `pods.jsonl` as produced by the broker. The job must download `manifest.json` and `junit.xml` under its declared download destination, and manifest must also contain `job_id` and `pod_id`. `result.json` must report successful broker steps and verified release, include sizes and SHA-256 for every download, and match a unique created/released pod with an intent in the shared journal. Test commands must capture their test exit code and still allow the broker step to complete, as directed by ORCHESTRATOR. Logs and all other listed downloads are also hash checked. The downloaded manifest must be produced inside the job during its recorded lifetime; the existing C-05b/c artifacts do not satisfy this contract.

For hosted lanes, the diagnostic expects trusted `run.json` metadata as described by `local_regression_green.py`. The current downloader returns a receipt instead and refuses without independently authenticated workflow revision; no adapter currently turns it into an authenticated diagnostic input.

JUnit must have at least one testcase, with a `node_id` property equal to the full reviewed test ID. Failure/error cases need `outcome`, `phase`, `fingerprint`, and `semantics_hash` when the ledger row requires it. Duplicate, unexpected, renamed, absent, skipped, malformed, protected-failing, and collection-error cases are red. Ledger rows retain P-04 schema and expiration semantics; a passing admitted case is flagged for retirement. The legacy `regression_green.py` function is the ledger/JUnit engine only; do not use its standalone `--evidence` interface as a provenance-aware aggregate.

## Sealed evaluator and finalizer

The production green path is **not available**. The fixed coverage lists 71 lanes but lacks feature coverage, and the reviewed inventory is incomplete. The CLI also lacks an authenticated bridge from coordinator source receipts to its index and feature bytes. It refuses even an otherwise complete input. Hashing caller-supplied data does not establish provenance. The pinned M4-6 predicate independently refuses success until the missing observation proofs and frozen bindings exist.

The orchestrator must arrange finalization after terminal producer results, including failures. The coordinator's `DeploymentAdapter.finalise` passes its sealed invocation to `regression_finalizer.run`; this payload has no ledger, authenticated index or feature snapshots, so the finalizer always returns red. Its legacy `evaluator` callback cannot authorize a pass. The finalizer never launches or replays producers and records a durable start marker to prevent replay. The sealed CLI is:

```sh
python3 scripts/ci/regression_finalizer.py --sealed-input /secure/sealed-input.json \
  --invocation-dir /secure/invocations/unique-id --status /secure/status.json
```

The input is the exact `c06-sealed-finalizer-input/v1` structure emitted by `Coordinator.begin_finalization`, with its canonical digests. A missing or malformed invocation is a failure; the outer caller must publish failure if no status can be written. Legacy `--producer-plan`, `--collection`, and broker-root finalizer invocations are unsupported and red. The legacy deployment CLI handoff is also red and required-check publication is deferred. No current caller should publish green.

The separate sealed aggregate CLI accepts the arguments shown by `python3 scripts/ci/regression_aggregate.py --help`. It verifies tuple, result and index identities, reviewed coverage and ledger constraints, then refuses because the index is not authenticated to source and feature bytes. Do not treat its index digest as source authentication. The old CP1 `--collection` command is not the sealed CLI contract.

## Feature and hosted evidence boundaries

A reviewed feature selects repository, lane, platform, scenario, slot, attempt, a candidate dictionary of the four frozen repository SHAs and artifact-relative JSON files. M4-6 additionally requires the tuple's frozen `generated.manifest_sha256`. After independently authenticating the source, attempt and index and checking exact byte lengths and hashes, a future bridge must pass parsed snapshots as `{path, sha256, json}`; duplicate JSON keys are rejected. The approved module is loaded as `scripts.journey_predicates` so its package imports work. The snapshot helper is not itself an authenticator and is not called by the sealed evaluator until that bridge exists.

`trusted_hosted_download.download(source, tuple_repos, destination, fetch=...)` returns a receipt and extracted files, not a `run.json`. It currently refuses all hosted downloads: the run head and workflow path do not independently establish the executed workflow revision. An accepted revision attestation and a source-to-index protocol are required before a receipt can be credited. Its archive and attempt checks remain in place; reruns require independent artifact-to-attempt/job attribution. Synthetic tests that bypass the revision guard exercise archive validation only and are not hosted provenance or qualification evidence.
