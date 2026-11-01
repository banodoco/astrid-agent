# C-06g-R4: tuple-batch coordinator contract

Status: design for M-INT review. This packet does not implement, deploy, or establish C-06 acceptance or a production-green receipt.

## Decision

Adopt one M-INT-deployed, M-CI-versioned coordinator on the orchestration host. It owns one durable record for each tuple batch, opens the required check before producers may start, seals all producer outcomes, invokes the trusted downloader/finalizer under one idempotency identity, and is the only process allowed to publish `int/regression-green`.

The current job relay and broker remain evidence sources; neither is promoted by assumption into a tuple coordinator. The current GitHub Actions workflow is not the publisher. Production activation requires the new host service, a source-pinned GitHub App check, and the implementation changes named below.

Pinned design inputs:

- Agent base: `int/r1` at `75ad0e6ea1e443a249df406f91379f88ad66ca3c`.
- Retained CI candidate: `lane/M-CI/C-06g-R1` at `59a8c6b4a81f8ac94b3b20ae70d3ba79063a9a9f`.
- QUAL interface: `lane/M-QUAL/R-36-v3` at `2f845c51c8a3f9af784868fcdd4678bc442deb90`; `scripts/journey_predicates.py` SHA-256 `6ef495007d3f815eaad52c61d2ec7e86f8abca9e4287a48321c64f119e7021f4`.
- P-04 authority: `/home/user/repo/docs/merge-plan/schemas/{tuple.schema.json,ledger.schema.json,README.md}`.

## Observed boundary and chosen seam

| Path | Owner | Observed behavior | Decision |
|---|---|---|---|
| `/home/user/merge/podq/watchdog.sh` | M-INT deployment | Relays task `runpod-job.yaml` files into individual broker job directories, mirrors job files, retries limited readiness failures, restarts a stale broker, and launches remote polling. | Keep as a job/broker watchdog. It is not the tuple coordinator. M-INT starts a separate coordinator watchdog beside it. |
| `/home/user/merge/podq/remote-poll.sh` | M-INT deployment | Discovers remote `*-runpod` branches and calls the remote evidence publisher. | No C-06 role. |
| `/home/user/merge/podq/publish-remote.sh` | M-INT deployment | Copies finished remote-job evidence into the merge-plan repository and commits it. | Evidence transport only; it MUST NOT publish `int/regression-green`. |
| `/home/user/merge/broker/podq_broker/broker.py` | M-GPU-origin implementation, M-INT deployment | Validates and executes one job, writes per-job state/result/download hashes, records pod custody, cancels/times out steps, and marks interrupted jobs failed on recovery. | Read-only evidence source. Do not add tuple or GitHub-check policy to the broker. |
| `scripts/ci/regression_finalizer.py`, `trusted_hosted_download.py`, `regression_aggregate.py`, `local_regression_green.py` at candidate `59a8c6b…` | M-CI | Trusted-evaluation candidate and focused fixtures. The finalizer currently executes a producer plan; the aggregate is CP1-fixed; no deployed caller or check publisher exists. | Retain the validation work, but replace the producer-execution interface with the sealed-result interface below. Never rerun completed producers during finalization. |
| `.github/workflows/regression-green.yml` at candidate `59a8c6b…` | M-CI | An uncalled reusable/manual workflow whose job is named `int/regression-green` and deliberately exits nonzero. | Not the required-check source. On activation, rename/remove its conflicting check name or leave it unrequired; branch protection accepts only the coordinator GitHub App as source. |

Selected implementation and deployment paths:

- M-CI versioned code in Agent: new `scripts/ci/regression_coordinator.py` and `scripts/ci/regression_check_publisher.py`; retained-and-adapted `scripts/ci/regression_finalizer.py`, `scripts/ci/trusted_hosted_download.py`, `scripts/ci/regression_aggregate.py`, and `scripts/ci/local_regression_green.py`.
- M-INT host deployment: an immutable, root-owned checkout at `/home/user/merge/podq/releases/astrid-agent/<approved-sha>/`; state at `/home/user/merge/podq/tuple-batches/state.sqlite3`; per-invocation data at `/home/user/merge/podq/tuple-batches/<batch_id>/`; local control socket `/home/user/merge/podq/tuple-coordinator.sock`.
- M-INT-owned launch/supervision seam: new `/home/user/merge/podq/run-tuple-coordinator.sh` and `/home/user/merge/podq/tuple-coordinator-watchdog.sh`. These are proposed paths, not observed files. They are outside M-CI's repository write authority and require M-INT deployment approval.
- GitHub seam: a `banodoco/astrid-agent` installation token available only to the publisher, with Checks write and metadata read. A separate read-only token is used by the hosted-artifact downloader. Branch protection requires `int/regression-green` from that exact App installation.

No existing tuple coordinator, check publisher, independent coordinator supervisor, or source-pinned required-check configuration was found in the inspected surfaces. The external deployment owner is M-INT. The remaining external authority is the `banodoco/astrid-agent` repository administrator who installs the App and changes branch protection; M-INT must name/engage that operator before activation.

## Authenticated batch contract

Only the root-owned M-INT dispatcher may submit on the local Unix socket; the coordinator checks Unix peer credentials and has no network listener. A batch is accepted only after the following bytes and identities are copied into its private directory, hashed, fsynced, and recorded in one SQLite transaction:

1. `batch_id` (coordinator-issued UUID), monotonic `generation`, target repository, and `target_sha`. The target MUST be `banodoco/astrid-agent`, and `target_sha` MUST equal `tuple.repos.agent.sha`.
2. Exact tuple bytes, P-04 schema validation, and the canonical tuple digest: SHA-256 of sorted, compact UTF-8 JSON excluding `digest`. A supplied `digest` MUST match. The complete repo URL/SHA map, generated bindings, creation time, workflow revisions, and installed identities become immutable batch inputs.
3. Independently reviewed coverage, complete reviewed inventory, admitted ledger, and C-07 receipt, each with path, SHA-256, authority revision, and reviewer identity. The coordinator authenticates the successful C-07 control-plane run and binds its run identity; a producer-supplied passing JSON is not authority.
4. The QUAL module path, commit and required SHA-256 above. Startup and every batch verify the deployed bytes. Missing/different bytes, a missing `check_<scenario>`, an exception, malformed return, `False`, or nonempty reasons are red. For `m4_6`, `candidate` is derived from the frozen tuple (including `generated.manifest_sha256` when selected), never from an artifact.
5. A closed producer plan. Every logical producer has a unique `producer_id`, repo/lane/platform, required checkout SHAs, deadline, evidence kind, reviewed command/workflow identity, and expected artifact names. The plan cannot grow after acceptance.

Before a producer starts, the trusted dispatcher binds its logical ID exactly once to an actual source:

- Broker: exact job-directory ID and SHA-256 of `job.yaml`. The coordinator later reads `/home/user/merge/podq/jobs/<id>/{state,result.json,job.yaml}` and `pods.jsonl` itself.
- Hosted: repository, workflow path/revision, run ID, run attempt, job ID, artifact ID/name, head SHA and dispatch correlation. The trusted downloader queries the GitHub run, job and artifact APIs and hashes archive and member bytes itself.

The binding is immutable and unique within and across active batches. Manifests MUST repeat `batch_id`, `generation`, tuple digest, complete checkout map, producer ID, attempt, artifact hashes, test exit code and collection status. Repetition detects mixups but does not confer trust. The local broker journal or GitHub API is authoritative for source identity; reviewed control-plane files are authoritative for expected identity; raw artifact bytes are authoritative only after the trusted downloader hashes them.

## Durable state machine

SQLite runs in WAL mode with full synchronous commits. A single process lock prevents split-brain publication. Transitions are append-audited and monotonic:

| State | Required durable fact | Next action |
|---|---|---|
| `REGISTERED` | Immutable batch inputs and closed producer plan recorded. | Create the check run. Producer starts are forbidden. |
| `CHECK_OPEN` | GitHub returned and a GET verified one `in_progress` `int/regression-green` check with expected App, head SHA, `external_id=batch_id:generation:tuple_digest`, and stored check-run ID. | Permit dispatch/binding; enter `COLLECTING`. |
| `COLLECTING` | Per-producer start/terminal events are recorded from trusted sources. | Wait until every producer is terminal, or batch cancel/deadline forces a terminal failure fact. |
| `SEALED` | Immutable result set and its digest recorded; every planned producer has exactly one terminal classification. | Insert the unique finalization row. |
| `FINALIZE_READY` | One `invocation_id` exists under a UNIQUE `batch_id` constraint. | Start/adopt the finalizer. |
| `FINALIZING` | Accepted-start marker and live lock for that invocation exist. | Wait for the atomic status or force failure on death/timeout. |
| `VERDICT_READY` | Strictly validated finalizer status, or coordinator-generated failure status, is stored and hashed. | Publish only if this is still the active generation. |
| `PUBLISHING` | Intended check update and attempt are durable. | Update the stored check-run ID and read it back. |
| `TERMINAL` | Remote check and local verdict agree, or publication is durably classified unverified/failed. | No more input, finalization or success publication. |

Failure to create/verify the initial check seals the batch as `check_open_failed`; the absent required check remains non-green. `CANCEL_REQUESTED` is an audited fact, not a green terminal state. There is no transition from any failure to pass and no reopening a sealed batch. A retry is a new `batch_id` and higher generation.

## Producer and collection outcomes

Every planned producer receives one of: `completed`, `infrastructure_failed`, `unstartable`, `skipped`, `cancelled`, `timed_out`, or `missing_at_deadline`.

- `completed` means transport/job collection succeeded and authenticated artifacts are present. A captured test exit 1 with valid JUnit remains `completed`; the ledger evaluator decides whether its ordinary failures are admitted. Producer wrappers therefore capture the test exit and finish the transport step successfully.
- Every other classification is an unconditional red prerequisite. It is still passed to the finalizer so one complete reason set is produced.
- Missing JUnit, zero collection, malformed/duplicate/unsafe files, hash/size mismatch, wrong tuple/checkouts/run/attempt/job/artifact, unsuccessful broker release proof, hosted job failure/skip, or downloader/API ambiguity is `collection_failed` and red.
- New, expired, renamed-without-reviewed-mapping, missing-shard, collection-error, absent-JUnit and skipped/failing protected cases remain red under P-04. An admitted, unexpired ordinary failure may match; a now-passing admitted row is a retirement notice, not a failure.

## Exactly-once finalization

“Exactly once” means one accepted logical finalization and at most one evaluator body execution per `batch_id`; it does not mean producers run again.

1. Sealing inserts one `invocation_id` under a database uniqueness constraint. Duplicate producer events, repeated scans and restarts return that same row.
2. The coordinator starts `regression_finalizer.py` with only the sealed batch/result paths, invocation directory, tuple/authority paths and output path. The finalizer MUST NOT accept or execute a producer command plan.
3. Before downloading or evaluating, the finalizer obtains an exclusive invocation lock and creates `started.json` with `O_EXCL`, then fsyncs it. A duplicate process that sees the marker exits without evaluation. Status is written atomically and includes `schema_version`, check name, batch/generation/invocation IDs, tuple digest, target SHA, sealed-input digest, predicate SHA-256, artifact-index digest, result, reasons and timestamps.
4. On recovery: a live lock is adopted; a complete status is validated; no marker permits a safe start; a marker with no live lock and no complete status means the accepted worker died and becomes red. It is never rerun. An `exec`/permission error before the marker is `finalizer_unstartable` and red.
5. Nonzero exit, signal, operator cancellation, deadline, missing/malformed/stale status, unwritable output, or mismatch in any bound field produces a coordinator-generated failure verdict. The finalizer never receives the Checks token.

This closes the candidate's double-execution hazard: the coordinator observes already-run producers, while the finalizer performs trusted hosted download, broker-byte validation, strict parsing, predicates, ledger evaluation and aggregation once.

## Required-check publisher

The publisher is the sole holder of Checks write credentials and accepts only a validated `VERDICT_READY` row while holding the coordinator database lock.

- At registration it creates the `in_progress` check before producers can start. Whole-host loss therefore leaves a required check absent or pending, never green.
- At completion it updates the stored check-run ID, never searches by name and updates an arbitrary run. `success` is allowed only for finalizer `pass`; every other reason uses `completed/failure` with a concise reason inventory.
- Before any update it re-reads the active generation. Superseded, duplicate or late status is stored for audit and rejected. When a replacement batch targets the same SHA, the prior check is first completed as failure/superseded; the new generation then receives its own check.
- After update it GETs that exact check-run ID and verifies App identity, name, external ID, head SHA, status/conclusion and output tuple/batch identifiers. A 2xx response with mismatched fields, timeout, lost/ambiguous response, permission error, or failed read-back is not locally green. Recovery reconciles by exact check-run ID; it never creates a success from local memory alone.
- Branch protection MUST pin `int/regression-green` to this App. The Actions workflow's same-named job must not be an accepted source. Activation includes a branch-protection read-back and a negative test showing another App/Actions check with the same name cannot satisfy the requirement.

## Death, cancellation, timeout and recovery

- Producer worker death/cancel/timeout is recorded from the broker/API as its non-green terminal classification; it cannot be converted to skipped success.
- Finalizer worker death after `started.json` is a terminal failure with no second evaluator execution. A finalizer timeout is terminated, its invocation lock is allowed to release, and the coordinator publishes failure.
- Coordinator clean cancellation first records cancellation, seals active batches and drives failure publication. SIGKILL/host loss cannot run cleanup; the independent coordinator watchdog restarts it. Recovery scans all nonterminal rows, reconciles exact broker/API/check identities, applies deadlines, adopts live finalizers, and publishes failure for dead/expired work.
- If both coordinator and watchdog/host are down, the pre-created remote check remains `in_progress`, which is fail-closed. On restart, expired batches become failure. No local process is permitted to publish success without the complete durable chain.
- Replayed socket requests are idempotent by request ID and payload digest. Same ID/digest returns the original response; same ID/different digest is rejected. Duplicate producer terminals must be byte-identical. Conflicts, late starts, post-seal artifacts and lower generations are audit events and red/rejected.

## Trust boundaries

- M-INT controls the host service account, release checkout, socket, SQLite state, reviewed inputs, broker journal access and service watchdog. Evidence producers have read-only/no access to these paths.
- M-CI owns the versioned coordinator, downloader, finalizer, evaluator and publisher adapter. Deployment uses one approved Agent SHA; mutable manager worktrees are forbidden.
- M-QUAL owns predicate semantics. M-CI verifies the pinned bytes and calls the interface only after provenance, attempt, tuple and artifact validation. Predicate output never authenticates input.
- The broker proves job/pod custody and downloaded byte hashes, not batch completeness or test success. GitHub APIs prove hosted run/job/artifact identity, not the semantic truth of artifact JSON.
- The Checks token is isolated to the publisher; the artifact token is read-only and isolated to the downloader; neither is exposed to producer jobs, finalizer subprocesses, logs or receipts.
- Repository administration is the authority for App installation and source-pinned branch protection. Until that read-back passes, production publication is disabled.

## Focused acceptance tests

Implementation approval requires deterministic tests through the real coordinator entry point and publisher adapter:

1. A fully valid synthetic batch reaches one finalizer invocation and one verified success check. Mark it synthetic; never treat it as a production receipt.
2. A captured ordinary failure with valid JUnit: admitted/unexpired is allowed; new or expired is red; a resolved admission emits retirement. Renamed without mapping, missing shard/JUnit, collection error and skipped/failing protected case are red.
3. Producer nonzero infrastructure result, dispatch rejection/unstartable, explicit skip, cancellation, timeout and never-started deadline each seal, finalize once and publish failure. Remaining producers are still collected.
4. Broker job/spec/tuple/release mismatch and hosted repo/head/workflow/run/attempt/job/artifact/digest mismatch are red. Duplicate and unsafe archive members and duplicate source binding are red before predicate import.
5. QUAL module missing/hash mismatch, missing function, exception, malformed return, false result and nonempty reasons are red. Verify `manifest_sha256` is derived from the frozen tuple. The published `check_m4_6` remains red while its deliberate section-5 refusal exists.
6. Duplicate terminal events and repeated recovery scans do not create a second finalization. Crash before the start marker safely starts once; crash after the marker publishes failure without rerun; finalizer exec failure, signal, timeout, cancellation, malformed/stale status and unwritable status are red.
7. Kill/restart the coordinator in every state. Confirm the initial pending check survives, active generation is recovered, no producer/result is silently dropped, and terminal publication is idempotent.
8. Late result from an older generation, wrong batch/tuple/invocation, post-seal artifact and conflicting duplicate are rejected and cannot update the check.
9. Simulate check create/update/read-back timeout, permission denial, wrong App/head/external ID and duplicate remote run. None yields local green. Verify source-pinned branch protection rejects a same-name Actions check.

## Activation and limitations

Activation order is: M-INT approves this contract; M-CI implements and runs focused fixtures; M-INT reviews the exact implementation SHA; the repository administrator installs/source-pins the App; M-INT deploys the immutable checkout and new watchdog; a synthetic end-to-end failure matrix runs; only then may a real tuple batch be registered.

This design does not authorize edits to `/home/user/merge/podq`, workflows, schemas, ledger admissions or product code. It does not make the retained candidate production-ready: its reviewed inventory is incomplete, its aggregate is CP1-fixed, its hosted downloader is limited to one-job attempt-1 runs, and its finalizer currently runs producer commands. The published QUAL `m4_6` predicate deliberately returns false. Real C-05 inventory/artifacts, authenticated C-07, reviewed ledger/coverage, a compliant publisher App and deployment evidence are still required. C-06 acceptance and production green are not claimed.
