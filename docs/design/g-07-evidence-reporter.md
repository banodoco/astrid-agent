# G-07: Evidence reporter design

## Goal

Provide one local command that gathers an existing Runtime task, its result
manifest and artifacts, and optional pod job evidence into a review packet for
CPU or GPU runs. The packet should let a reviewer identify what ran, inspect
the outputs, and see what is missing without consulting separate dashboards.

## Reuse and authority

- Runtime's existing task record is the run/task authority. Resolve it through
  the existing Runtime task read API and `RuntimePaths.current()`.
- Reuse `managed-generation-result.v1` (`result_manifest`) when the task links
  one. Its task/attempt/run ids, outcomes, warnings, inputs, outputs, producer
  evidence and transport evidence remain the detailed run record.
- Reuse the existing artifact refs and hashes. The reporter verifies bytes
  when it can resolve them, and links to refs when it cannot.
- For GPU transport, read the matching podq `result.json`, `log.txt`, job
  specification and release events. Confirm release from broker events and
  verified release state; a `released` field alone is not proof.
- CPU runs use the same Runtime and artifact path. Pod fields are `not
  applicable`; missing applicable data is `unknown`, never inferred.
- This is a read-only projection for review. It creates no task, ledger,
  manifest schema, authority, or artifact publication path.

## Proposed interface

`astrid evidence report <task-id> [--podq-job <job-dir>] [--out <dir>]`

`--podq-job` is optional and only needed when GPU job evidence is not already
linked from the Runtime task/result record. The reporter validates that the
task and job identifiers correlate before combining them. Default output is a
new directory under the current project's review area; `--out` allows an
explicit private directory. Existing output is never overwritten.

## Collection and checks

1. Read the Runtime task and linked result-manifest artifact through existing
   APIs; retain exact task, attempt, producer-run ids and terminal outcome.
2. Resolve each manifest input/output ref. Record filename, media type, byte
   count and SHA-256. Recompute hashes for accessible files and flag mismatches;
   distinguish verified bytes from unresolved references.
3. Record the execution tuple from manifest evidence: source/runtime pins,
   engine/entry, recipe/model-lock identity and available image/driver facts.
   Keep the canonical tuple ref and digest when present. Show unknown slots as
   unknown rather than reconstructing a different tuple.
4. Collect the exact invocation and command lines from task evidence and job
   scripts/logs. Preserve their order and source. If only a script is known,
   include the script ref and hash instead of inventing expanded commands.
5. For a pod job, include job id, GPU, datacenter, start/end time, exit codes,
   downloaded-file hashes, estimated cost, and release verification evidence.
   Mark absent spend or release facts unknown. CPU runs report no pod session.
6. Carry through Runtime and producer warnings, then add reporter findings
   such as missing refs, hash mismatch, uncorrelated job id, or unverified
   release. Do not turn warnings into a pass/fail judgment.
7. Redact credentials and signed query parameters from displayed commands and
   logs. Keep source files private in the project/workspace; never upload them
   to a public service.

## Packet

Emit one `review.json` projection with sections for identity/outcome, tuple,
commands, inputs, outputs, warnings, and optional pod evidence. Each item
includes its source ref/path and verification state (`verified`, `observed`,
`unknown`, or `mismatch`) where applicable. Include the source task/result/job
refs so a reviewer can trace every displayed fact. A concise `README.md` may
render the same fields for browsing, but it is only a view of `review.json`.

The packet is a derived review aid, not a new durable run record. Runtime and
`result_manifest` remain authoritative; podq remains authoritative for session
and release facts. A report with incomplete evidence is still emitted and
clearly identifies gaps rather than silently omitting them.

## Acceptance for an implementation

- A CPU task and a GPU task produce the same top-level sections; CPU pod fields
  are not applicable, while genuinely missing data is marked unknown.
- Hash verification detects a changed artifact and reports expected and
  observed hashes without altering source artifacts.
- A GPU report ties podq job/session evidence to the Runtime task and shows
  estimated spend plus verified or unknown release status.
- Failure, cancellation, partial output, and warning evidence survive in the
  report; the command does not require successful media publication.
- Redaction is tested with credential-like values and signed URLs; source logs
  and media remain private and unchanged.
