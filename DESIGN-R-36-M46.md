# M4-6 authenticated native context and external provenance contract

Disposition: **design complete; production capability unavailable; AWAITING_EVIDENCE**.
This packet identifies the missing capability rather than enabling admission. All
interfaces marked **proposal** require implementation and protected review. There
is no native specimen, qualifying receipt, checker change, or release claim here.

The answer to M-CI's 2026-11-03T02:06:53Z request is: **Q owns the source-fixed
native producer and its private host channel. M-CI joins an independently obtained
host execution record to API identity and exact downloaded bytes. No existing
source can currently mint that host execution record.** GitHub authenticates a
job, not this collector's process/image, private observations, or its particular
artifact upload. A provider accepting the present binding alone would manufacture
the missing authority. Keep the existing refusal.

## 1. Frozen inputs and the added result

All repository citations below refer to `banodoco/astrid-agent` at
`b54117c9312165e2799554ec27480665d5cad2b1`, tree
`3eaa0f3326ec85635f10368aefca76716499b437`, parents
`ef8df6eb25c5de1691c357c2a6d0e3b87f5074c2` and
`890ca2347bdc8c9377e178fd176a9eca3152e00f`. A shared-object task clone and local
branch `lane/M-QUAL/R-36-M46-TRUSTED-CONTEXT-CONTRACT-R1` remain at those bytes.
`git ls-remote origin refs/heads/int/r1` independently returned the required base.

The existing direct-session design already describes the fixed provider, typed
removal phases, and five output files. The keep-data seam report already explains
the action-14/data gaps. This packet adds the exact cross-domain keys, source
feasibility evidence, channel/terminal ordering, conditional trusted record, and
smallest capability experiment. It does not repeat the old claim that Option B
needs a direct-slot ruling: that ruling was given at 22:04:52Z.

The snapshotted product tuple is App `79f7e5397c044c4c97dc7e97b20ad96f787e5247`,
Agent `ab43bad818ef65ca1e929b973a2f61cd9fc4bf61`, Runtime
`2776ba973b6c8f3555b5629b0840d9691a98d3c2`, Pinokio
`3a6a98ae251ca126505245286e2d3e760f4348e9`, manifest
`b2584f0bb5a949890364004572a0182f2b568adcdba061e73b739e1d3d2d623f`.
This is an input snapshot, not a selected execution. Harness SHA, workflow SHA,
and candidate Agent SHA are distinct bindings. [Input snapshots](scratch/input-manifest.json)
record exact document bytes and hashes.

## 2. Verified facts, supported capabilities, and gaps

| Classification | Finding and source | Consequence |
| --- | --- | --- |
| Existing fact | Fixed provider always raises; binding/owner are expressly not origin authority. [S1] | No live direct session exists. |
| Existing fact | Public launcher refuses five explicit missing backends. Fixture `assert_binding` also refuses; private Python objects are not a security boundary. [S2] | A socketpair, exact class, `__file__`, HMAC, or fixture mint cannot open admission. |
| Existing fact | `_host_authentication` checks environment strings plus live OS build, then writes `authenticated=False`, `requires_admission_context=True`. [S3] | Environment and OS-build consistency are retained but insufficient. |
| Supported GitHub capability | OIDC documents issuer/audience/validity/JTI, numeric repository identities, workflow SHA, run/attempt and `check_run_id`. Public discovery returned these claims and RS256/JWKS location. [G1], [API] | A real verified token can authenticate job identity. Discovery is not a token or a verified job invocation. |
| Supported GitHub capability | Attempt-scoped jobs expose numeric job, `check_run_url`, head, runner ID/group/labels and terminal facts. [G2], [API] | Match OIDC `check_run_id` through the job's check-run URL; never assume it equals numeric job ID. |
| Verified gap | Artifact metadata exposes artifact ID/digest and workflow-run association; the inspected schema/specimen contains no producer-job/run-attempt/frozen-slot edge. [G3], [API], [S7] | Names, upload outputs, timing and receipt echoes do not fill that edge. |
| Verified gap | The documented OIDC claim set/specimen has no immutable collector-image, collector PID/birth, candidate tuple, or slot claim. [G1], [API] | A separately selected plan and protected host launch/export source remain necessary. No hosted collector-attestation API is asserted. |
| Existing host limitation | Hosted Linux/macOS grant passwordless sudo; hosted Windows runs as administrator with UAC disabled. [G4] | Merely placing a root parent beside candidate code on the default VM does not establish independent isolation. |
| Existing self-hosted evidence | Mac setup defines `astrid-runner`, a repo runner and label. R-30's protected helper/export was a proposal; the later raw-producer report still had no authentic specimen. [R2], [R3] | An accepted venue/profile label is not evidence that the protected context backend was installed. |
| Existing fact | M4-6 artifact checking requires external origin joins and separate post-check completion; those context types/releases are absent. [S7] | Native producer success cannot mint evaluator/checker admission. |

The actual content sources already implemented are below. Their *code paths* are
verified; no successful non-root real-host execution of them was performed here.

| OS | Native source at base | Trust and completeness limit |
| --- | --- | --- |
| Linux | Current UID/user, user systemd capability/version, unit/fragment/ExecStart/MainPID/InvocationID; `/proc/<pid>/stat` start time and `/proc/<pid>/exe`; executable-identity enumeration. [S4] | Kernel/user manager are content sources. Bind boot and PID namespace in a future host profile; PID/starttime alone is not cross-boot identity. Permission/race ambiguity must refuse. |
| macOS | Current UID, `launchctl` GUI domain/target/plist, libproc executable paths and PID list, `ps lstart`. [S4] | The current birth value has limited resolution; do not assert collision-free incarnation proof. Measure a stronger source or prove an exact-child handle/profile before admitting such a backend. Polling is not event history. |
| Windows | Current SID, fixed PowerShell 5.1, ScheduledTasks XML plus Scheduler COM instance GUID/EnginePID, CIM creation/executable identities. [S4] | Task instance, principal, action and process birth must agree. Missing paths/denied queries/multiple instances refuse. Default administrator execution does not meet P19's non-root-equivalent product-user proof. |
| Data/journal | Bounded canonical manifest and durable-journal reads. [S5] | The data manifest contains product-authored digest strings. `DataProbe` synthesizes equal before/after digests from them. It does not read four entities. Journal fields do not independently prove operation history. |

**Trust root:** the approved R-30 root is the existing orchestrator machine and
its controlled journal/API downloads, not a new signing service. [R1] For native
process truth it still needs an independently protected OS launch/observation/export
source. **No such production source is supplied in this base or inspected host
evidence.** Hostname, runner label, root UID, and API job identity cannot stand in
for it. Q owns the backend; M-VENUE owns available runner capability evidence;
M-INT/Hannah own separately administered host provisioning. M-CI owns the later
join, not a native generator.

## 3. Unchanged live API and conditional production binding

**Existing API:** `PLATFORM_SESSION_PROVIDER.open_authenticated_session(binding,
owner)` returns a live session. Its only subsequent calls are
`bind_exact_custody(custody, same_owner)`,
`authorize_exact_removal(request, same_owner)`,
`finalize_after_removal(observation, same_owner)`, and `abort(reason)`.
The four methods return `None`; no token, serialized capability, verdict or handle
may be returned. Authentication precedes private work-root creation and native
mutation. The loader remains source-fixed; no provider/endpoint/key/override
argument is introduced. [S6]

**Proposal, blocked on section 8:** the private backend must correlate the
existing `TrustedBinding` and `PlatformSessionBinding`, not reinterpret either as
authenticated. The exact binding product is:

| Group | Required closed fields and source |
| --- | --- |
| Plan | `plan_id, inventory_sha256, obligation_ids, nonce, issued_at, expires_at`; selected by orchestrator before work. `obligation_ids` is a sorted nonempty unique list of exact frozen R1 checkpoint IDs for this execution, never a producer-selected inventory. |
| GitHub identity | `repository_owner, repository_owner_id, repository, repository_id, workflow_id, workflow_ref, workflow_sha, head_sha, run_id, run_attempt, producer_job_id, check_run_id`; verified token plus concordant API facts. Repository owner ID/name and repository ID/name must all agree. Reusable workflow ref/SHA must also be selected/verified if used. |
| Selection | `family="m4_6", slot_id, mode, os, owner_kind, runner_image, shell`; exact frozen matrix/profile. Slot is one of the nine existing IDs, with no mode fallback. Expected runner environment, numeric runner ID/group and labels come from the selected profile/API, never receipt inference. |
| Code/product | `harness_sha, harness_tree, source_closure_sha256, candidate`; candidate is exactly `app, agent, runtime, pinokio, manifest_sha256`. Freeze each checkout path/SHA, Python interpreter, script/import closure, product protocol/command/cwd, all `PlatformArtifactBinding(role,path,sha256)` rows, and canonical owner principal/target from the existing platform binding. |
| Host execution | `host_profile_id, host_profile_sha256, host_id, boot_id, process_namespace_id, launcher_identity, collector_identity`; independently obtained by the unavailable host backend. Each process identity contains `pid, birth_id, parent_pid, parent_birth_id, executable_path, executable_sha256, principal`. A namespace ID is required where applicable; the profile supplies an explicit `not-applicable` literal elsewhere. |

For serialization the proposed `binding` has exactly five keys: `plan, origin,
selection, code, host`. Their closed field sets are:

```text
plan: plan_id, inventory_sha256, obligation_ids, nonce, issued_at, expires_at
origin: repository_owner, repository_owner_id, repository, repository_id,
        workflow_id, workflow_ref, workflow_sha, head_sha, run_id, run_attempt,
        producer_job_id, check_run_id, reusable_workflow_ref, reusable_workflow_sha
selection: family, slot_id, mode, os, owner_kind, runner_image, shell,
           runner_environment, runner_id, runner_group_id, runner_labels
code: harness_sha, harness_tree, source_closure_sha256, source_files, candidate,
      checkouts, collector_executable, product_protocol, product_command,
      product_command_cwd, artifacts, owner_principal, owner_target, os_build
host: host_profile_id, host_profile_sha256, host_id, boot_id,
      process_namespace_id, image_identity, launcher_identity, collector_identity
```

`source_files` entries are exactly `{path, sha256, bytes}`; `checkouts` entries
exactly `{name, path, sha}` for the four repositories; `collector_executable`
exactly `{path, sha256}`; `artifacts` entries exactly `{role, path, sha256}`.
`image_identity` is exactly `{source, immutable_id, sha256}` from the unavailable
host source, not `ImageVersion`. Process identities have the seven fields in the
table. Arrays have unique entries and deterministic ordering by path, name or
label as applicable; `product_command` preserves argv order. Reusable workflow
fields are both null when unused and otherwise exact nonempty ref/full SHA.
All remaining textual IDs/paths/roles are nonempty strings, not numeric coercions.
The selected plan/profile owns their allowed values. This closes the envelope;
it does not supply the missing host/image/data parser implementations.

These are constraints a caller cannot use to create a context. A production
backend must compare every present `*_claim` field in the
existing platform binding with independently obtained identity; the unchanged
receipt's `job_id=slot_id` is a logical matrix key, **not numeric API job ID**.
Current `collector_structural_sha256` hashes a class-name description; it is
not `source_closure_sha256`. [S3], [S6]

For this proposed record protocol, JSON objects have exactly their declared
fields; duplicate keys, unknown keys, NaN/Infinity and bool-as-int are rejected.
Numeric GitHub IDs, run attempt and process PIDs are positive JSON integers;
`runner_group_id` may be zero, as in the captured hosted API response. Digests are
lowercase full SHA-256; commit/tree fields are full lowercase 40-hex, while
`workflow_ref` retains its exact documented path/ref string. Timestamps are UTC
RFC3339 `Z`, nonce/session IDs 32 random bytes
encoded as 64 lowercase hex. Numeric strings in existing `TrustedBinding` are
converted only after full decimal validation and exact API equality. Canonical
bytes `C(x)` are UTF-8 JSON, sorted keys, compact separators, `ensure_ascii=True`,
`allow_nan=False`, no newline. The proposed `binding_sha256` is
`SHA256(b"astrid-m4-6-native-binding-v1\x00" + C(binding))`. It covers every group
above. The source-closure list is sorted by canonical path with unique
`{path, sha256, bytes}` rows and is hashed by the same canonical rule without a
domain prefix. Existing receipt hashes remain hashes of **raw bytes**, unchanged.

Authentication means verifying the OIDC signature with pinned issuer/discovery
policy and supported algorithm/key, exact audience selected by the existing
orchestrator, `iss/sub/exp/nbf/iat/jti`, all required identity claims and API
concordance. Retain validation results and token hash/JTI in protected state,
never the bearer token. This defines verification requirements, not an installed
OIDC consumer or endpoint. OIDC possession alone cannot authorize a collector.

## 4. Private channel, replay and two finalization boundaries

**Proposal:** only the verified host backend may launch the fixed collector and
attach its exclusive inherited channel. The public open signature stays unchanged;
the child obtains no caller-supplied context argument or filesystem token. The
source-fixed provider can redeem the channel once after the host has matched
binding, exact process incarnation and owner. The parent retains its supervising
`TrustedCollectorContext`; it is not transferred into the child. A production
proxy/backend is currently absent. Reserve the static plan/nonce first; the host
may launch a read-only collector bootstrap held before admission, obtain the
dynamic exact-child identity, and complete the composite binding before releasing
open or any product/native mutation. Dynamic PIDs are observed, not predicted in
the preselected plan. Python object privacy cannot implement this
cross-process authentication by itself.

The existing fixture framing is a useful protocol, not a trust root: four-byte
big-endian frame length; `M4C1`; one-byte DATA=1/TERMINAL=2; eight-byte sequence;
32-byte binding digest; four-byte payload length; payload; HMAC-SHA256 over the
frame body before its MAC. Maximum payload 64 KiB. Production must bind the
composite digest above, with fresh per-launch key and nonce; the fixture's digest
of `TrustedBinding` alone does not cover the complete proposed binding. Never put
the key in argv, environment, product stdin, serialized evidence or logs. Prevent
product/descendant access to channel handles, memory and export state; otherwise
refuse. Fragment source bytes into bounded frames with a checked final byte count
and digest rather than increasing the frame limit. [S2]

The production bridge additionally needs bidirectional control, which the
existing receive-only fixture does not implement. **Proposal:** use independent
directional keys `HMAC-SHA256(master, b"child-to-parent")` and
`HMAC-SHA256(master, b"parent-to-child")`, and sequence counters starting at 1 in
each direction, to prevent reflection. DATA control payloads are canonical JSON:

```text
request:  protocol="astrid-m4-6-native-channel/v1", kind="request",
          request_id, method, arguments_sha256, arguments
response: protocol="astrid-m4-6-native-channel/v1", kind="response",
          request_id, method, arguments_sha256, outcome, reason_code
```

`request_id` is a positive integer incrementing once per request; at most one
request is outstanding. Methods are exactly `open, bind, authorize, finalize,
abort`, mapping to the existing provider/session calls. `arguments` is the
canonical typed binding/custody/request/absence body (or `{reason}` for abort),
and its digest must match. Responses echo the request identity/digest; outcome is
`accepted/rejected`, with null/nonempty reason code respectively. An accepted
response is only an acknowledgment of that one live phase. Accepted `open`
returns the live `AuthenticatedPlatformSession` proxy; its bind/authorize/
finalize/abort methods return `None`, never an admission flag. Reject
unknown/late/duplicate responses. The
child enforces the same local owner object across calls; the parent associates it
with independently observed canonical principal/target/registration/process
identity, not a cross-process Python object ID.

Source DATA payloads are a third exact variant:
`{protocol, kind="source-chunk", path, offset, total_bytes, sha256, bytes_base64}`.
The path is one allowed export path; integer byte offsets must be contiguous,
start at zero, never overlap and end at exact total. Base64 is canonical standard
padded encoding; decode/re-encode must match. Rehash assembled bytes. Control
requests exceeding the bound refuse rather than changing the wire format.
Session removal finalization leaves the launch channel open for remaining
source/export DATA. Only collector TERMINAL followed by exact reap/EOF ends it.
No existing fixture is asserted to implement this proposed bridge.

| Phase | Required ordering and failure result |
| --- | --- |
| Reserve/open | Orchestrator and protected host store atomically consume `(repository_id, run_id, run_attempt, producer_job_id, family, slot_id, nonce)` and JTI before mutation. Duplicate/resumed/restored/unreadable replay state refuses. No automatic clean-slate store. |
| Bind | One exact owner object and canonical custody binding; rehash definition/actions/command closure before registration. Mismatch aborts. |
| Observe | Initial census before registration; exact pre/post boundaries and polling during every operation. Denied, truncated, ambiguous or missing observation aborts, never means absent. |
| Authorize removal | Fresh complete removal census and current registration, exact authorized actions/incarnations and nonoverlapping remove/preserve sets; only then product uninstall. Single use, same session/owner/custody. |
| Session finalization | After product uninstall, independently reobserve all authorized incarnations/actions, manager/definition absence and preserved data. `finalize_after_removal` returns `None` only after these observations. This does **not** establish collector exit or GitHub job completion. |
| Collector finalization | After all five output files are validated and staged, require authenticated empty TERMINAL, exact child exit status 0 and bounded exact reap, then clean EOF with no trailing data. Parent seals the protected export. This is the launcher's separate `finalize()`, not the removal-session method. |
| Transport/admission | After upload and API terminal success, M-CI joins immutable artifact bytes and producer identity. A later independent boundary authenticates checker completed-success and exact checked-output digest before admission. |
| Abort/expiry | Permanently revoke; close/erase channel key; exact-child terminate/reap only. Preserve cleanup failure. No generic product cleanup authority, no passing export or retry with old context. |

Retain the current 15-minute hard-lifetime bound, 5-second exact-child
finish/abort/reap and 0.25-second EOF bound. The proposed production idle cap is
60 seconds, matching the existing default; fixture idle parameters can differ.
Ordinary verified
DATA progress may renew idle only up to the hard deadline. Any longer profile
requires explicit reviewed code/policy change; a slow journey may honestly refuse.
Concurrent receives, sequence gap/duplicate, wrong MAC/digest, partial frame,
terminal payload, post-terminal byte, unknown exit, PID reuse, child/parent loss
and unauthorized product/descendant channel inheritance all abort. The selected
collector's sole inherited endpoint is required. Fork hooks in the fixture invalidate only a
forked copy; **parent-attempt revocation is missing**, so the production profile
must demonstrate it independently before using this route. [S2]

This supervision concerns integrity of the collector, not continuous custody of
the product process tree. It does not revive the deferred R1.x containment gate.

## 5. Exact producer export and M-CI join

The existing candidate artifact remains exactly:

```text
journey-failure-result.json
evidence/interruption_recovery.json
evidence/failed_candidate_rollback.json
evidence/keep_data_uninstall.json
evidence/stop_safety.json
```

Do not add self-authentication fields to these closed schemas or rewrite fetched
bytes. The current five files alone lack the new independent checkpoints/data
sources. The following is a **proposed protected host export into the existing
orchestrator index**, not a sixth qualifying producer receipt or a new authority
service. No authentic instance is emitted by this packet.

`NativeContextRecordV1` has exactly:

```text
kind = "m4-6-native-context", version = 1,
binding, binding_sha256, session_id,
started_at, ended_at, context_state = "finalized" | "aborted",
terminal_sequence, child_exit_code, child_reaped, clean_eof,
custody_sha256, removal_request_sha256, final_absence_sha256,
files, sources, failure_reasons
```

`binding` is the complete section 3 binding. For a finalized record, `files` is a
path-sorted list of exactly five `{path, bytes, sha256}` entries above, hashed from
unmodified bytes held by the protected export. `sources` is exactly three sorted entries with the
same field shape, naming `native-checkpoints.jsonl`, `native-censuses.json`,
`native-data.json`; these are separately retained protected source objects,
**not extra members silently accepted in the existing five-file bundle**.
`custody_sha256`, `removal_request_sha256` and `final_absence_sha256` hash canonical
typed dataclass payloads, not Python object representations. Specifically, use
the exact `CustodyBinding`, `RemovalRequest`, and `FinalRemovalObservation`
instances accepted in those phases; recursively convert dataclasses to their
declared field-name objects, tuples/lists to ordered JSON arrays, and mappings to
string-key JSON objects. Preserve tuple-of-pairs order rather than converting it
to a mapping. Reject Path, enum, callable, arbitrary object, non-string mapping
key or unsupported leaf; paths in these types are already strings. Hash `C` of
that conversion without a domain prefix. M-CI must reconstruct and compare these
payloads against independently retained phase/source records and the existing
removal identity/action/incarnation/data hashes, not trust hash equality alone.
Finalized records
require positive terminal sequence, exact integer exit 0, both booleans true,
nonempty valid source files, and empty `failure_reasons`. Aborted diagnostics
may contain only the retained subset of the same file/source allowlists, use null
for unavailable terminal/exit/digests, and have nonempty reasons;
they never qualify. No `pass`, `admitted` or `verified` producer flag is authority.

In all file rows, `bytes` is a nonnegative integer byte count, never a Python
bytes value or text encoding. Proposed source object contracts, each parsed with
duplicate-key/closed-schema checks, are:

| Source object | Exact record shape and content |
| --- | --- |
| `native-checkpoints.jsonl` | Each line has `sequence, binding_sha256, stage, step_id, action_sequence, action, edge, monotonic_ns, wall_time, principal, manager, processes, data_sha256`. `stage` is `initial`, `operation`, or `final`; product operation rows have positive action sequence, exact action vocabulary, edge `before/poll/after`; native operation rows use section 6's fixed step IDs and null product sequence. Initial/final have null action/sequence/edge. `manager` and `processes` are canonical native query/process payloads under the frozen OS parser. Sequence is collector order, not an OS event sequence. |
| `native-censuses.json` | Exactly `binding_sha256, initial, removal_authorization, final`. Initial/final are distinct endpoint observations with exactly `checkpoint_sequence, principal, targets, actions, processes, prior_incarnations, source_reads`; empty live process sets and absent targets are allowed. `removal_authorization` is exactly `{checkpoint_sequence, payload}`, where `payload` is the unmodified closed `_removal_census_payload(RemovalCensus)` mapping, retaining its required nonempty live census. Process rows use `RemovalProcessBinding`; endpoint prior-incarnation rows are exactly `{identity, absent}`, with identity that typed process payload and an independently observed boolean absence. Endpoint targets/actions/source-read payloads require a frozen OS parser. Endpoint censuses must come from independent enumeration, not discovery/history lists; every expected role/action/target and enumerated match is accounted for, with unexpected/unknown matches refusing. |
| `native-data.json` | Exactly `binding_sha256, reader_profile_sha256, observations`. Each observation has `checkpoint_sequence, data_root, data_root_identity, workspace_identity, manifest_sha256, program, selection, sentinel, entities`. `program` is exactly `{path, state, source_identity}`, with state `present/absent` and null source identity only for independently observed absence. `selection` is exactly `{path, bytes_sha256, uninstalled}`, retaining the recovery receipt and requiring boolean `uninstalled=true` after uninstall. Sentinel has exact `path, identity, sha256`; entities are exactly `project, document, preferences, cas`, each `{source_identity, canonical_bytes_sha256}`. Profile freezes actual installed-store readers/canonicalization and identity representations; no absence is inferred from exit code. |

Each checkpoint's `data_sha256` is `SHA256(C(data_observation))` for exactly one
`native-data.json` observation with the same `checkpoint_sequence`. Reject absent,
duplicate or cross-binding matches; require every data observation to have one
matching checkpoint. The three census `checkpoint_sequence` references must
likewise resolve uniquely to the correct initial, authorization-before-removal,
or final checkpoint. Raw source objects are addressed by their exact path and raw
digest in the orchestrator-controlled export namespace identified by the selected
plan/profile; an artifact-provided URL/path cannot select them.

Limits proposed for retained source objects are 8 MiB checkpoints, 1 MiB census,
4 MiB data; overall protected export including the five files at most 64 MiB.
Overflow/truncation fails. The OS payload parsers, data reader profile, source
read coverage and real store locations are **unresolved code/profile inputs**, not
declared available by these types. No synthetic semantic values may fill them.
Q must freeze their exact parser bytes/tests before implementation can pass.

**M-CI-owned post-job record:** an `ArtifactOriginJoinV1` in the trusted index has
exactly `kind="m4-6-artifact-origin", version=1, binding_sha256,
native_context_sha256, repository_id, run_id, run_attempt, producer_job_id,
check_run_id, slot_id, artifact_id, artifact_name, archive_sha256,
archive_size_bytes, producer_status, producer_conclusion, origin_evidence_sha256`.
IDs come from authenticated API/platform observations, not the five files.
`origin_evidence_sha256` identifies the independently retained protected
upload/export transaction proving that this exact context's bytes were uploaded
as this immutable artifact ID by this job/attempt. That transaction capability is
**missing today**. Repeating an upload-action output into the record is forbidden.
`native_context_sha256` is `SHA256(C(NativeContextRecordV1))`, excluding no fields
and with no self-hash field in that record. `origin_evidence_sha256` hashes the
exact retained transaction bytes; that transaction's source/parser profile must
be frozen before it can be accepted. This last parser/source is a named missing
capability, not an already implemented platform attestation.

The DAG is deliberately acyclic: selected binding → protected native record and
file/source hashes → upload transaction/artifact ID → API terminal job facts →
downloaded archive and file rehash → checked output → later checker completion.
The running producer need not predict its artifact ID/job completion time, and
the running checker must not predict its own completed-success. A raw receipt
never acquires a post-job freshness field. The R-30 Mac freshness schema-3
correction is a separate carrier contract, not this source. [R3], [S7]

M-CI's join algorithm is:

1. Start from the independently selected tuple/harness/inventory and exact slot
   registry. Load host records only from the orchestrator-controlled export/journal,
   never an artifact-supplied `trusted-context` path or producer JSON assertion.
2. Verify section 3 identity, replay reservation and finalized section 4 lifecycle.
   Require the exact artifact-origin transaction and successful API producer job.
   Join on `(repository_id, run_id, run_attempt, producer_job_id, check_run_id,
   family, slot_id, binding_sha256)` and immutable artifact ID. Artifact name is
   an additional equality constraint; it is not a join key.
3. Download by artifact ID. Verify API digest when available and actual archive
   size/hash; retain private no-follow descriptor custody and closed file sets.
   Rehash all five raw files and protected source files; reject unsafe paths,
   symlinks, duplicate members, races, extra/missing files and parser ambiguity.
4. Map the verified origin fields to the existing `_AuthenticatedArtifactOrigin`,
   API/download fields to `_AuthenticatedArtifactMetadata`, and independently
   selected expectations to `_FrozenRunBinding/_FrozenSlotBinding`. Their context
   types remain distinct from native collector context. The public checker APIs
   remain closed until a real private production mint/release exists. [S7]
5. Only then build the existing predicate input
   `{slot_id, candidate, files:[{path,sha256,json}]}` and evaluate the independently
   bound checkpoint/source evidence with Q's reviewed predicate extension. The
   base predicate still unconditionally refuses; the above three source objects
   require a separately reviewed input/parser integration, not an invented
   `ok=true` field. Do not simply remove its final refusal. [S8]
6. Preserve exact nine-slot closed aggregation, unique producer/artifact IDs,
   missing/skipped/cancelled/duplicate/stale refusal and all semantic predicates.
   After checker termination, require the independent post-check context binding
   exact checked-output hash to exact completed-success checker job. [S7]

The JSON-only `native_observation_errors()` also always appends a refusal
(`native_service_adapter.py:4370–4374`). It must remain incapable of admitting
serialized observations. A future trusted entry point must consume the real live
context and reviewed independent evidence; removing that error from the
JSON-only API is not an implementation of this contract.

## 6. Option B evidence and protected invariants

P13 requires checkpoints and data invariants on **all direct and six Pinokio
slots**. It explicitly defers containment and continuous history to `deferred-R1.x`,
never passed. P19 requires real OS/non-root product-user proof for identity,
ownership and service/process changes. [P]

The proposed checkpoint profile freezes a one-second sampling target and at most
five seconds between completed independent samples during any live operation.
Each operation requires before, actual in-step, and after samples. No short-step
exemption is inferred from P13; a profile unable to observe a short operation
honestly remains unavailable until its scheduling contract is resolved. The
five-second bound is a proposed measurable
checkpoint policy, not gap-free coverage. Freeze it with the inventory/profile
under Q/M-CI review before enabling; failure to sample within it refuses this
profile. A sample around a synchronous command cannot be called an in-step poll.
The current driver does not implement the required concurrent scheduling.

For direct slots the checkpoint step order is closed as follows. Product steps
are the numbered actions in the table below. The proposed checkpoint row's
`step_id` is `product/01` through `product/14`, or one of exactly
`native/register`, `native/start`, `native/interrupted-controller-complete`,
`native/runtime-stop`, `native/recovery-controller-complete`. Native rows have
null `action_sequence`, their exact final step component as `action`, and the
same before/poll/after edges. The total order is product/01 → native/register →
native/start → product/02–04 → native/interrupted-controller-complete →
native/runtime-stop → product/05–07 → native/recovery-controller-complete →
product/08–14. Initial/final rows use step IDs `initial/final` and null
action/sequence/edge. Stage is `initial/operation/final` as defined above. Native
manager-internal register/start boundaries need real instrumentation of those
existing collector methods, not an adjacent product snapshot. The six Pinokio
profiles must freeze their additional engine operation IDs/order against actual
engine evidence; that is an explicit missing profile input, not an open wildcard.

| Execution span | Required independent evidence; existing semantics retained |
| --- | --- |
| Before first mutation / registration | Initial manager/action/process census, initial data and unrelated sentinel, exact principal and absent canonical target. Initial census must not be reconstructed from later product discovery. |
| Every product action | Before/poll/after around sequences 1–14: service-spec; interruption-recovery-before; interruption-controller; interrupt-controller; interruption-recovery-after; recovery-controller; complete-recovery-controller; failed-candidate-rollback; stop-safety-refused-before; stop-safety-refused; stop-safety-unknown-before; stop-safety-unknown; keep-data-uninstall-before; remove-product. Also bracket native registration/start, exact controller completion and Runtime stop boundaries executed between these calls. [S9] |
| Interruption/recovery | Preserve distinct interrupted/recovery controller PID/birth, same durable operation, admissible journal phases and terminal state, restored readiness and semantic data. Neither local exception nor process exit alone proves recovery. |
| Failed-candidate rollback | Preserve controlled failure phase, authentic R0 fixture/executable identity, selected rollback, real R0 readiness and unchanged four-entity data. |
| Both stop-safety cases | Preserve refused AND unknown results; no admission/download/write/selection/journal activation; same owned processes/data and surviving unrelated sentinel. |
| Before remove-product | Fresh complete census and exact typed one-use removal request; canonical definition included in removal set, observed data/manifest preserved, Pinokio action graph preserved. Snapshot policy does not permit unsafe removal. |
| After removal and export | Independent final census: definition/target absent, every authorized incarnation absent, zero owned action matches, program root absent, selection deactivated under existing retained-receipt semantics, same actual workspace/CAS/entity content and sentinel. Keep all nine existing scenario/phase instance correlations. [S10] |
| Containment / continuous history | Inventory retains IDs as `deferred-R1.x` with proposal pointer; no polling sequence, equal endpoints, zero local drops or channel HMAC is continuous history. |

The exact initial/final census currently implemented for removal covers recorded
actions and authorized incarnations; it is not automatically a complete initial
whole-owner census or an independently authenticated process-tree history.
The six Pinokio slots additionally need the R-30 engine/action settlement, real
busy/unknown injection and restoration facts. The current Pinokio provider's
`probe()` refuses before platform opening. This packet neither replaces it with
direct ownership nor converts its continuous observer's refusal into checkpoint
success. A reviewed checkpoint-specific implementation is required. [R1], [S11]

Protected checks remain conjunctive: exact tuple/checkouts/harness and R0 fixture;
fixed source/OS/principal/manager/action; exact interruption and journal identities;
canonical removal scope, all incarnations and adjacent raw authorization/absence
manager indices; all 12 keep-data fields and their strict leaf types; actual
project/document/preferences/CAS semantics; unrelated sentinel; no override,
foreground fallback or local-disposable promotion; atomic five-file publication;
closed inventory and all external provenance/checker-completion checks.
Independent polling must use its own protected source stream so it does not
silently insert queries between the existing adjacent raw-manager indices. [S10]

The existing CLI publishes its five-file tree before collector exit
(`native_service_adapter.py:4826–4893`). Those bytes remain nonqualifying until
the parent export seal and later M-CI joins. A future wrapper must enforce that
distinction and prevent an aborted collector's staged/published bytes from being
selected as finalized evidence; the base CLI does not already supply that seal.

An authenticated context does not repair the action-14 protocol mismatch or make
manifest claims into semantic data. The installer primitive at
`faa7c609d0732bbf67ac25eeccf14c52c7ff9063` supplies operation facts only; the prior
seam report's missing program/selection/sentinel/entity readers remain blockers.
The base's `macos-15` matrix versus failure-mode `macos-14` vocabulary mismatch is
also separate; no runner alias or hosted/self-hosted substitution is permitted.

## 7. Adversarial acceptance matrix

Each case must produce refusal/no passing publication; partial diagnostic output
may be retained outside admitted evidence.

| Attack or failure | Required rejection |
| --- | --- |
| Environment-only CI claim, copied Python object/`__file__`, fixture context, alternate module/provider | No authenticated mint; refuse before work root/native mutation. |
| Valid OIDC token from sibling job/repository/workflow/attempt; check-run ID confused with job ID | Exact issuer/identity/API join mismatch. |
| Valid job token plus invented slot, candidate, collector image or product checkout | Missing selected plan or protected launch evidence. |
| Reused nonce/JTI/session; replayed old boot/PID/binding; reset replay store | Atomic reservation/replay-state failure; never retry same context. |
| Writable/replaced interpreter, script, import, definition, action or checkout | Closure/phase hash mismatch; same path is insufficient. |
| Product can read channel key, duplicate handle, ptrace collector, replace export or elevate over supervisor | Host profile fails; HMAC/frame validity cannot repair source compromise. |
| Forked collector while parent remains active; missing lineage/child identity | Revoke parent attempt as well as child copy; unavailable revocation is a capability gap. |
| Missing census/poll, permission denial, PID reuse, respawned prior action, wrong principal/target | Unknown/incomplete source; refuse authorization or finalization. |
| Changed data bytes with identical product manifest, missing CAS/workspace, altered sentinel or selection | Independent data/profile observation differs or unavailable. |
| Duplicate/out-of-order authorization, verdict-shaped phase result, adapter cleanup relabelled product uninstall | API/order/ownership failure; preserve residue if authority unavailable. |
| Valid run-level artifact from earlier attempt or sibling slot/job; name/timestamp matching only | Missing independent artifact-origin transaction. First attempt and a narrow timestamp window alone do not supply M4-6's required edge. |
| Tampered archive or raw source, symlink/FIFO/traversal/extra member, duplicate JSON key | Byte custody/closed parsing failure before predicate credit. |
| Producer exits 0 but source incomplete; terminal without EOF/reap; checker still running or failed | Separate observation/lifecycle/completion failure. |
| One missing/skipped/cancelled slot, duplicate producer/artifact, null feature evidence, local smoke/continuous claim | Closed required inventory and evidence-class failure. |

## 8. Missing capabilities, experiment, and minimal implementation sequence

There are two independently necessary platform capabilities, plus collector data
work; neither can be delegated to a producer-created JSON flag:

| Missing capability | Accountable owner and smallest next evidence |
| --- | --- |
| Protected exact collector launch, code/process/image identity, parent revocation and export outside candidate control while product uses its actual non-root native user session | Q backend owner with M-VENUE; host provisioning by M-INT/Hannah if a separately administered host is chosen. Supply a measured executable/profile and one real non-root capability specimen. Standard hosted root/admin access is a negative feasibility result until independently constrained. |
| Exact artifact ID → producer job/attempt/slot/context transaction | M-CI join owner with M-VENUE/platform transport support; Q supplies protected export hash. Obtain a real externally observed upload transaction or keep origin join unavailable. Public artifact REST plus OIDC currently does not provide it. |
| Complete checkpoint schedule/censuses and actual semantic-data/sentinel/program/selection readers; real protocol executable | Q native producer, M-PIN for six engine-owned slots, M-R1FIX for real operation surface. Supply installed-store reader/canonicalization and real operation artifacts; no fixtures promoted. |

**Next experiment, proposal only; do not dispatch from this packet:** Q asks
M-VENUE for one read-only Linux capability trial on a real systemd user-session
host, with the actual product user non-root and no fake `/proc`. Before any
Astrid install/removal, have the existing host administrator report the numeric
job/check-run/attempt identity, executable/import closure ownership, exact-child
kernel identity and proposed protected export location. As the product user,
attempt bounded access to a harmless collector canary/key surrogate and export
canary, handle inheritance, same-UID process access and elevation; record actual
denials or successes. Enumerate a harmless user service and known live/retired
child with the selected independent source before and after an ordinary child
exit, distinguishing denied from absent and retaining boot/namespace identity.
No real secrets, real product uninstall, unreviewed root daemon or fabricated
native receipt are needed. Measure parent-attempt revocation for a collector
fork/abnormal exit; if the backend cannot do it, stop with that exact missing
primitive. This experiment is about collector isolation, not deferred product
containment. Q owns its result, not M-CI.

The trial must retain raw commands, non-root UID/SID, OS/build, API facts, code
hashes, source reads, actual denial/results, inner exits and administrator-owned
export provenance in the task. A passing fixture or root-only read is insufficient
under P19. An ordinary hosted VM is expected to fail the independent-isolation
test because candidate elevation is available; measure, do not claim a pass.
No current machine is assumed provisioned for it.

After that source is real, the minimal reviewed sequence is:

1. Q implements the fixed backend behind `scripts/m4_6_trusted_launcher.py` and
   `scripts/m4_6_platform_session_provider.py`; preserve existing open signature.
   Extend `tests/test_m4_6_trusted_launcher.py` and
   `tests/test_m4_6_platform_session_provider.py` for real-mint-only, nonce/JTI
   reservation, composite binding, parent revocation, handle isolation and exact
   exit/reap/EOF. Existing fixture positives remain labelled structural only.
2. Q integrates boundary scheduling and protected source export in
   `scripts/native_service_adapter.py` / `scripts/shared_journey_driver.py`,
   retaining all removal/raw-index/type/schema checks. Extend
   `tests/test_native_service_adapter.py` and `tests/test_shared_journey_driver.py`
   for late/missing poll, denied census, respawn, same-manifest changed content,
   sentinel/selection/program absence, wrong operation result and no publication
   after abort. Resolve the separate real-driver/action-14 seam first.
3. M-PIN/Q implement a separate checkpoint route behind the fixed
   `scripts/m4_6_pinokio_custody_provider.py`; extend its existing tests for exact
   six-slot engine/action/terminal ownership and both refusal cases. Never remove
   a continuous-source refusal merely to reuse its result.
4. M-CI implements the real external transaction/context release behind
   `scripts/m4_6_artifact_provenance.py` with Q co-review; retain its distinct
   checker and later admission roles and exact nine slots. Extend
   `tests/test_m4_6_artifact_provenance.py` with sibling-job, stale-attempt,
   replayed-export, forged-upload-output, missing-host-source and checker-output
   substitution cases. Keep C-06g's complete reviewed inventory/features and
   fail-closed source-to-index bridge. [C]
5. Q/M-CI freeze the supplementary parser/profile and `journey_predicates.py`
   input integration and explicit P13 inventory mapping, retaining the current
   refusal until complete. Run focused predicate/evidence tests. Then obtain
   exact-head, real non-root native proof on each applicable OS and required slot,
   including P19 OS identity negatives, independent data checks and authenticated
   external join. Linux success does not prove macOS/Windows or six Pinokio slots.

**Source-backed alternatives if no host root is available:**

- Use the R-30 orchestrator-machine trust root to run/observe/export directly on
  a separately administered real user-session host, or implement its already
  proposed protected helper/export profile there. This needs actual isolation,
  administrator provisioning and measurements; the Mac runner setup alone is
  insufficient. The 01:35Z decision moved overnight Mac work back to hosted after
  its runner account became stranded; do not presume `hannah-mac` available. [R2], [P]
- R-30's existing broker journal can authenticate broker-owned Linux executions
  if its host meets the real native user-session requirements. Prior RunPod pods
  lacked systemd user sessions, so broker unit-suite receipts cannot satisfy
  this native journey. No new broker job is requested. [R1], [P]
- Otherwise retain explicit `not-run`/AWAITING_EVIDENCE for checkpoint obligations
  and the present provider refusal. A change to the required trust model would
  need a separate protected review; Option B only defers continuity/containment.
  It supplies no exception for synthetic data or unauthenticated provenance.

## 9. Source and claim catalogue

The canonical receipt hashes all sources, prior reports and API specimens.
Line ranges here refer to the frozen base, not a mutable manager checkout.

- **S1:** [fixed provider](agent/scripts/m4_6_platform_session_provider.py), lines 1–46.
- **S2:** [trusted launcher](agent/scripts/m4_6_trusted_launcher.py), 31–40 (bounds),
  127–238 (binding/child), 240–337 (framing), 342–460 (fixture trust), 462–681
  (lifecycle/fork), 728–743 (explicit production gaps).
- **S3:** [native adapter](agent/scripts/native_service_adapter.py), 2456–2522
  (host claims), 4500–4640 (live entry/open), 4754–4782 (logical job/structural hashes).
- **S4:** same adapter, 387–649 (process source), 1294–1549 (manager/principal),
  1633–1746 (removal census), 1811–1989 (live identity/final absence).
- **S5:** same adapter, 268–384 (manifest and journal readers).
- **S6:** same adapter, 724–785 (artifact/platform/custody fields), 899–947
  (removal/session interface), 968–1034 (fixed loader/authenticate before work root).
- **S7:** [artifact provenance](agent/scripts/m4_6_artifact_provenance.py), 1–79
  (missing join), 124–265 (private payloads), 309–489 (identity checks), 491–720
  (safe archive/closed slots), 758–947 (checked hash and distinct admission).
- **S8:** [predicate](agent/scripts/journey_predicates.py), 1–23, 67–151.
- **S9:** [shared driver](agent/scripts/shared_journey_driver.py), 769–853,
  979–1058; [ordering fixture](agent/tests/test_shared_journey_driver.py), 739–754.
- **S10:** native adapter, 2247–2380 (two-phase removal/residue), 2605–2705
  (nine instances), 2987–3031 (12 keep-data fields), 3449–3534 (removal schema),
  4329–4349 (raw adjacency), 4374–4485 (five-file publication).
- **S11:** [Pinokio provider](agent/scripts/m4_6_pinokio_custody_provider.py),
  553–666, 712–720; [polling observer](agent/scripts/m4_6_process_observer.py),
  536–614, 759–825. Neither is authenticated checkpoint authority.
- **R1:** [R-30 disposition](../../done/R-30/M-INT-DISPOSITION.md), approved
  simplification; [R-30 design](../../done/R-30/DESIGN.md) §5 semantic checks,
  with superseded signatures/control-plane machinery excluded.
- **R2:** [R-30 self-hosted design](../../done/R-30-SELF-HOSTED-PROFILE-DESIGN-R1/DESIGN.md)
  §2.3–4; [Mac setup snapshot](scratch/MAC-RUNNER-SETUP.md), 11–62.
- **R3:** [raw-producer report](../../done/R-30-MAC-RAW-PRODUCER-R1/REPORT.md),
  [freshness report](../../done/R-30-MAC-FRESHNESS-BINDING-R1/REPORT.md);
  [keep-data seam report](../../done/R-36-KEEP-DATA-NATIVE-SEAM-R1/REPORT.md)
  and its canonical receipt; [prior direct design](../../done/R-36-DIRECT-NATIVE-SESSION-DESIGN-R1/DESIGN.md) §11.
- **P:** [operating snapshot](scratch/MERGE-OPERATING.md), P13/P19 at 190/196;
  [decisions snapshot](scratch/DECISIONS.md), 316–319, 338 and final overnight Mac
  entry; [orchestrator snapshot](scratch/ORCHESTRATOR.md), 11776–11781 and
  15467–15469; 23:10:39Z no-systemd RunPod decision.
- **C:** [M-CI integrity review](../../done/R-36-C06G-PROOF-INTEGRITY-REVIEW-R1/REPORT.md)
  F1/F2; [successor review](../../done/R-36-C06G-R3-REVIEW-R1/REPORT.md), repair
  closes bypass but deliberately leaves provenance bridge red. No owner proof is
  imported as fresh acceptance here.
- **G1:** [GitHub OIDC reference](https://docs.github.com/en/actions/reference/security/oidc)
  and [public discovery](https://token.actions.githubusercontent.com/.well-known/openid-configuration).
- **G2:** [GitHub workflow-jobs REST reference](https://docs.github.com/en/rest/actions/workflow-jobs).
- **G3:** [GitHub artifacts REST reference](https://docs.github.com/en/rest/actions/artifacts).
- **G4:** [GitHub-hosted runner privileges](https://docs.github.com/en/actions/reference/runners/github-hosted-runners#administrative-privileges).
- **API:** [captured API summary](scratch/platform-summary.json),
  [requests/statuses/hashes](scratch/http-reads.jsonl). Repository ID `1223136945`;
  run `41562148461`, attempt 1, jobs `125675015209` and `125675642700`, artifacts
  `13198296245` and `13197896468`. These public HTTPS reads are **schema/identity
  specimens only**, from `merge-suite.yml` at `096f92a13047e0a10f0e873da84f7063b13c7f5a`,
  not an M4-6 run or evidence for the selected harness/product tuple. No OIDC bearer
  was requested, retained or verified.

[S1]: agent/scripts/m4_6_platform_session_provider.py
[S2]: agent/scripts/m4_6_trusted_launcher.py
[S3]: agent/scripts/native_service_adapter.py
[S4]: agent/scripts/native_service_adapter.py
[S5]: agent/scripts/native_service_adapter.py
[S6]: agent/scripts/native_service_adapter.py
[S7]: agent/scripts/m4_6_artifact_provenance.py
[S8]: agent/scripts/journey_predicates.py
[S9]: agent/scripts/shared_journey_driver.py
[S10]: agent/scripts/native_service_adapter.py
[S11]: agent/scripts/m4_6_pinokio_custody_provider.py
[R1]: ../../done/R-30/M-INT-DISPOSITION.md
[R2]: ../../done/R-30-SELF-HOSTED-PROFILE-DESIGN-R1/DESIGN.md
[R3]: ../../done/R-36-KEEP-DATA-NATIVE-SEAM-R1/REPORT.md
[P]: scratch/MERGE-OPERATING.md
[C]: ../../done/R-36-C06G-R3-REVIEW-R1/REPORT.md
[API]: scratch/platform-summary.json
[G1]: https://docs.github.com/en/actions/reference/security/oidc
[G2]: https://docs.github.com/en/rest/actions/workflow-jobs
[G3]: https://docs.github.com/en/rest/actions/artifacts
[G4]: https://docs.github.com/en/actions/reference/runners/github-hosted-runners#administrative-privileges
