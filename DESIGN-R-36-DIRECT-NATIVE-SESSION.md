# R-36 direct native platform-session design

Status: **decision-ready S packet; no implementation, live provider, hosted run, or qualification is claimed.**

Canonical design-work receipt: [receipt.json](./receipt.json).

## 1. Decision in one paragraph

The smallest acceptable direct producer is one source-fixed, direct-only
`DirectCheckpointPlatformSession` implementation behind the existing
`m4_6_platform_session_provider.py` export. It reuses the live, platform-specific
manager/process/data collection already in `native_service_adapter.py`, the existing
two-phase `AuthenticatedPlatformSession` contract, the existing
`shared_journey_driver.py` action order, and the existing five-file M4-6 bundle. It
does not add another observer, receipt, aggregate, authority service, or dispatch
path. The bundle remains provisional until the M-CI trust-anchor process joins the
run/job/artifact identity and evaluates the existing R-36 predicate. This design is
not executable yet: there is no real immutable
`astrid-m4-6-product-driver/v1` command/config in the inspected sources, direct
checkpoint evidence is approved under the conditions in section 11, but the
published harness has a `macos-15`/`macos-14` provenance mismatch.

## 2. Frozen source and product identities

The remote published Agent harness ref was rechecked read-only:

- `refs/heads/int/r1` = `b54117c9312165e2799554ec27480665d5cad2b1`
- tree = `3eaa0f3326ec85635f10368aefca76716499b437`
- subject = `Merge pull request #166 from banodoco/lane/M-R1FIX/INST-SCHEMA-PIN-FIX`

All Agent source findings below are from that commit object, not the manager
checkout's detached HEAD. The current published product tuple is a different
identity and must not be conflated with the harness base:

| Identity | Value |
| --- | --- |
| App | `79f7e5397c044c4c97dc7e97b20ad96f787e5247` |
| Agent product pin | `ab43bad818ef65ca1e929b973a2f61cd9fc4bf61` |
| Runtime | `2776ba973b6c8f3555b5629b0840d9691a98d3c2` |
| Pinokio | `3a6a98ae251ca126505245286e2d3e760f4348e9` |
| manifest SHA-256 | `b2584f0bb5a949890364004572a0182f2b568adcdba061e73b739e1d3d2d623f` |
| published | `2026-11-02T17:55:27Z` |

`CURRENT-TUPLE.json` and every inspected source/document hash are recorded in the
canonical receipt. A later implementation must record both its harness commit and
the candidate product tuple; candidate Agent is not proof of the harness source.

## 3. What exists and what does not

### Existing executable seams

The lower-level route already exists:

```text
ci_journey.py --shared-executor m4-6-hosted \
  --slot-id SLOT --candidate CANDIDATE.json --fixture RELEASE0_FIXTURE \
  --output OUTPUT/journey-failure-result.json --work-root PRIVATE_ROOT \
  --app-repo APP --agent-repo AGENT --runtime-repo RUNTIME --pinokio-repo PINOKIO \
  --journey-driver-config DRIVER.json
```

`shared_journey_driver.py` is a strict client/orchestrator for
`astrid-m4-6-product-driver/v1`. It pins the candidate, executable, optional Python
script, working directory, timeout, and every named artifact. It executes the
required operations in source-fixed order and never accepts driver-supplied proof
as platform evidence.

`native_service_adapter.py` already supplies the direct owner, lifecycle, exact
removal request, final absence observation, four scenario observations, receipt
validation, and atomic five-file publication. It opens the platform session before
creating the private work root and before native registration.

### Existing live collector sources

For the three direct slots, the authentic content source is the live path through
`NativeOwner` plus `LiveProcessProbe`, `DataProbe`, and `DurableJournalProbe` in
`native_service_adapter.py`:

| Slot | Independently read platform sources |
| --- | --- |
| `direct-macos` | current UID/user, `launchctl` GUI domain and job state, canonical plist/action, `libproc` executable paths, `ps` birth values, exhaustive executable-identity process scan, durable journal, and run-owned data manifest |
| `direct-linux` | current UID/user, user systemd >=254, exact unit/fragment/`ExecStart`/`MainPID`/`InvocationID`, `/proc` birth and executable identities, exhaustive executable-identity process scan, durable journal, and run-owned data manifest |
| `direct-windows` | current SID, fixed Windows PowerShell 5.1, ScheduledTasks plus Task Scheduler COM/XML/instance PID state, CIM process creation/executable identities, exhaustive executable-identity process scan, durable journal, and run-owned data manifest |

The live entry point constructs these sources itself. Injected runners, process
probes, drivers, environments, or PowerShell paths switch the adapter to fixture
scope and are refused at the hosted entry point. Unit fixtures therefore test
shape and sequencing only; they are never evidence of source authenticity.

`m4_6_process_observer.py` is not the producer selected by this design. It declares
bounded polling and continuity unknown, always refuses authorization/finalization,
and lacks a complete macOS or Windows descendant census. Promoting it would either
weaken proof or create a second collector engine.

### Unavailable real driver

Both the published harness Agent tree and the current product-tuple Agent tree
contain the protocol/action strings only in the shared client and unit-test
fixtures. Exact searches of the current App and Runtime source commits found no
matching protocol/action implementation. No Pinokio repository is present in the
manager workspace. No published immutable driver config or executable was
identified by the R-39 diagnosis or this inspection.

Therefore the precise missing external input is:

> A real, immutable, per-platform executable plus config implementing
> `astrid-m4-6-product-driver/v1` against the installed candidate: `service-spec`,
> interruption-controller discovery and controlled interruption, same-operation
> recovery-controller completion, controlled failed-candidate rollback to the
> authentic R0 executable identity, refused/unknown stop cases, and keep-data
> product uninstall. It must return product state only and must not manufacture
> native-manager/process evidence.

M-R1FIX owns the underlying product update/recovery/fault-control surface under
R-11/R-15/R-18. M-QUAL owns only the thin pinned protocol adapter/config and
black-box conformance checks once that surface exists. M-OPS owns the separately
known authentic R0 artifact-location gap. Test fixture scripts do not satisfy this
dependency.

## 4. Trust boundaries

| Boundary | Trusted fact | Explicitly not trusted |
| --- | --- | --- |
| M-CI evaluator/orchestrator | externally observed producer repository/workflow/run/attempt/job result; fetched artifact identity/digest; selected tuple and harness SHA | JSON fields that merely repeat those values |
| Fixed Agent harness source | collector code, platform-session state machine, driver client, schema and predicates at one recorded harness commit | candidate Agent pin as a substitute for harness identity; alternate imports or registrars |
| Native platform collector | direct reads from launchd/systemd/Task Scheduler and OS process/data/journal sources | product-driver discovery as process proof; injected fixture sources; caller booleans |
| Product driver | commands the real installed product and reports its product protocol state | admission, manager/process custody, cleanup authority, or qualification verdicts |
| Serialized bundle | content-addressed scenario and native observations | self-authentication, continuity, external provenance, or final admission |

R-30's approved simplification is controlling: the orchestrator machine is the
trust anchor; it joins GitHub run/job identity and fetched artifacts and hashes them
into the per-tuple index. There is no PKI, signature protocol, trusted-launcher
supervisor, new authority service, or producer-created admission flag.

The live platform session authenticates that the collector is the fixed live
source path and enforces safe sequencing. It cannot turn GitHub environment claims
into externally authenticated provenance. External provenance is established only
by the M-CI join after artifact retrieval.

## 5. Minimal producer

### Scope

The provider accepts exactly:

- `direct-macos` / `mode=direct` / `owner_kind=native-service`
- `direct-linux` / `mode=direct` / `owner_kind=native-service`
- `direct-windows` / `mode=direct` / `owner_kind=native-service`

It rejects all six Pinokio slots. Their custody provider and ownership remain
separate R-25 work.

### Fixed source and live-type checks

`native_service_adapter._fixed_provider` remains the only loader. On open, the
implementation must require the exact live owner/probe/runner classes constructed
by the non-fixture entry point, the current OS matching the selected slot, the
fixed principal/manager target, a complete platform binding, full candidate and
checkout equality, a real collector executable hash, the exact product protocol,
and current hashes for every immutable driver/service artifact. The session freezes
a canonical binding hash and the owner object identity. Environment claims remain
structural inputs and never become an authority token.

No work root or product/native mutation may occur until open succeeds. Read-only
capability and initial-absence probes may precede open, as the current adapter does.

### One-shot state machine

| State | Accepted call | Required checks | Next state |
| --- | --- | --- | --- |
| `new` | `open_authenticated_session(binding, owner)` | direct slot, exact live types and OS, owner principal/target, tuple/checkouts, collector and immutable artifacts; recheck initial target absence | `opened` |
| `opened` | `bind_exact_custody(custody, same_owner)` | one call; slot/owner/principal/target equality; canonical definition and action equal frozen artifacts; exact keep-data removal plan; rehash artifacts | `custody-bound` |
| `custody-bound` | `authorize_exact_removal(request, same_owner)` | one call; request embeds the frozen session and custody values; product-uninstall/keep-data plan; exact current registration, live census, all observed incarnations/actions, preserved data identity, and no unaccounted action process; rehash artifacts | `removal-authorized` |
| `removal-authorized` | `finalize_after_removal(observation, same_owner)` | one call after product removal; exact slot/owner; independently collected target absence, every authorized incarnation absent, no owned action process, preserved data identity unchanged | `finalized` |
| any nonfinal state | `abort(reason)` | close once, record no verdict, authorize no cleanup | `aborted` |

All other orders, repeated calls, owner substitution, changed binding, changed file,
or calls after abort/finalize raise. Methods return `None`; a boolean, handle, token,
or verdict-shaped return remains forbidden.

The session does not duplicate the OS collector. `NativeSession` already constructs
`RemovalRequest` only after a fresh owner revalidation and full removal census, and
constructs `FinalRemovalObservation` only after `owner.observe_absent`. The fixed
provider validates and consumes those typed values while the adapter remains the
single collector/serializer. If implementation needs a second read at either
boundary, it must be a small public re-observation method on `NativeOwner` using the
same source and raw-observation ledger—not a new observer module or aggregate.

### Checkpoint scope and remaining conditions

M-INT ruled at 2026-11-02T22:04:52Z that Option B applies to all three
direct slots. Section 11 states the required evidence and deferred claims.
The existing provider remains fail-closed until an implementation validates those
requirements; the ruling itself does not provide a real driver or live evidence.

## 6. Driver seam and exact operation order

Keep `journey_driver_factory(config_path)` and the existing config schema unchanged:

```json
{
  "protocol": "astrid-m4-6-product-driver/v1",
  "candidate": {
    "app": "<40 hex>", "agent": "<40 hex>",
    "runtime": "<40 hex>", "pinokio": "<40 hex>",
    "manifest_sha256": "<64 hex>"
  },
  "product_command": {
    "argv": ["<absolute pinned executable>", "..."],
    "cwd": "<absolute directory>", "timeout_seconds": 300
  },
  "artifacts": {
    "<role>": {"path": "<absolute nonsymlink regular file>", "sha256": "<64 hex>"}
  }
}
```

Required artifact roles include the product command, `service-definition`, and the
service action executable. `service-spec` must bind the product-canonical definition,
principal, explicit action argv, working directory, and exact removal plan.

The source-fixed order remains:

1. `service-spec`; bind custody; product-owned native registration/start.
2. `interruption-recovery-before`; observe candidate Runtime.
3. `interruption-controller`; bind the durable operation and exact controller.
4. `interrupt-controller`; prove that controller incarnation absent.
5. Terminate only the owned Runtime through the native session.
6. `interruption-recovery-after`; observe a distinct recovered Runtime.
7. `recovery-controller`; require the same durable operation with a distinct controller.
8. `complete-recovery-controller`; prove recovery controller completion.
9. `failed-candidate-rollback`; observe the authentic R0 executable identity and unchanged data.
10. For each of `refused` and `unknown`: before/action observations; prove no update or data change.
11. `keep-data-uninstall-before`; observe and construct the exact removal census.
12. Platform session authorizes the exact removal request.
13. `remove-product`; the product, not the harness cleanup path, performs removal.
14. Independently observe absence/data preservation; finalize the platform session.

The driver may not reorder, merge, skip, or synthesize these actions. In particular,
a local exception is not controller interruption, a candidate executable is not R0
rollback, and a successful product response is not native removal evidence.

## 7. Evidence capture and existing checker integration

The producer keeps the existing atomic artifact tree exactly:

```text
journey-failure-result.json
evidence/interruption_recovery.json
evidence/failed_candidate_rollback.json
evidence/keep_data_uninstall.json
evidence/stop_safety.json
```

Each scenario file is generated by the adapter from the validated observation and
contains the execution scope, slot, collector structural binding, scenario fields,
applicable native instances, and sanitized manager command metadata. Raw argv,
stdout, and stderr are never published; their lengths and SHA-256 digests are. The
receipt binds all four evidence-file hashes, candidate, R0 fixture, live checkouts,
runner structure, native registration/instances/controller interruption/removal,
data identity, and product outcome. Publication remains an atomic rename and never
overwrites an existing output directory.

No extra producer verdict or platform-session artifact is needed: initial manager
absence, all action-boundary manager/process reads, the removal census hashes, final
absence, and data identity already flow into the existing receipt/evidence. A
successful fixed state machine is bound by the externally recorded harness commit
and provider blob. The provider must not serialize an `authenticated=true` field.

Checker flow:

1. M-CI, as the sole evaluator, selects one exact tuple/slot/harness identity.
2. It externally records producer repo/workflow/run ID/attempt/job/result and fetched
   artifact ID/name/digest, hashes the five files, and writes those facts into the
   existing per-tuple evidence index.
3. The existing `journey_predicates.check_m4_6` validates the five parsed files,
   cross-file hashes, receipt, scenario/native equivalence, slot, and tuple. It must
   cease returning its unconditional section-5 refusal only for an externally joined
   direct checkpoint obligation class approved by M-INT.
4. Existing `check_failure_receipts.py` remains the inner exact-nine content check:
   all dependencies successful, exactly one receipt per slot, no missing/skipped/
   malformed/duplicate/unknown/wrong-tuple row, and exact evidence bytes.
5. The M-CI evaluator, not either producer-side checker, emits the release verdict.

This extends the single proof system. It does not revive the superseded PKI/private
release mechanism in `m4_6_artifact_provenance.py`, add a second aggregate engine,
or allow the serialized bundle to admit itself.

The direct inventory must keep two distinct obligation classes under the approved Option B: `checkpoint` is R1-required; `containment/continuous-custody` is
`deferred-R1.x` and never reported as passed. A snapshot/checkpoint record containing
`custody_continuity`, `gap_free_custody`, or equivalent success must be rejected.

## 8. Refusal and cleanup semantics

| Failure point | Required result |
| --- | --- |
| Missing driver/config/artifact, wrong tuple/checkout, wrong slot/OS/source type, unsafe/symlinked/changed artifact, pre-existing manager target | Refuse before work-root creation or native mutation; publish nothing |
| Session opens, then custody/service spec differs | Abort session; no registration; publish nothing |
| Registration or any scenario fails before exact removal authorization | Abort; do not issue generic cleanup; preserve residue and report that it may remain; publish no passing bundle |
| Collector source is denied, ambiguous, malformed, over-broad, or loses an exact identity | Refuse the operation; absence is not inferred; publish no passing bundle |
| Product removal invoked without the exact one-shot authorization | Refuse; product command is not called |
| Removal fails or an authorized incarnation/action remains | Abort/refuse finalization; no receipt; do not relabel adapter cleanup as product uninstall |
| Final target/data observation differs | Refuse finalization and publication, even if product files are already absent |
| Validation fails after finalization | No passing publication; final product removal is not rolled back by the harness |
| Atomic publication fails | Delete only the private publication stage; never overwrite or expose a partial bundle |

`abort()` closes the session; it is not cleanup authority. Hosted `safe_finalize()`
continues to refuse cleanup without exact platform authorization. Product removal is
the normal, authorized final cleanup. Any later manual cleanup is a separate owner
operation and cannot repair the failed evidence run.

## 9. Independently testable implementation pieces

### A. Direct provider state machine — M-QUAL

Focused tests in `tests/test_m4_6_platform_session_provider.py`:

- `test_direct_provider_accepts_only_exact_live_direct_binding`
- `test_direct_provider_rejects_fixture_owner_wrong_os_and_pinokio_slots`
- `test_direct_session_binds_custody_once_before_removal_authority`
- `test_direct_session_rejects_changed_artifact_owner_or_request_binding`
- `test_direct_session_authorizes_exact_checkpoint_request_once`
- `test_direct_session_finalizes_only_exact_absence_then_closes`
- `test_direct_session_abort_never_authorizes_cleanup_or_returns_verdict`

These are contract tests only. They must say so and cannot claim a live source.

### B. Existing adapter integration — M-QUAL

Retain and extend the focused adapter tests around:

- `test_authenticated_session_opens_before_private_scratch_creation`
- `test_two_phase_session_authorizes_before_removal_and_finalizes_before_return`
- `test_removal_census_accounts_for_every_recorded_process_incarnation`
- `test_two_phase_session_returns_no_removal_when_final_reobservation_fails`
- `test_failure_without_exact_instance_preserves_residue_instead_of_unsafe_remove`
- `test_atomic_publication_helper_publishes_complete_tree_without_admission_claim`

Add parameterized live-boundary contract cases for all three direct slot definitions,
artifact mutation at every phase, duplicate/out-of-order session calls, ambiguous
absence, and no publication after abort. OS parser fixtures remain unit fixtures.

### C. Product-driver conformance — M-R1FIX input, M-QUAL black-box contract

- `test_product_driver_service_spec_matches_canonical_installed_service`
- `test_product_driver_recovery_uses_same_durable_operation_and_new_controller`
- `test_product_driver_failed_candidate_returns_authentic_r0_identity`
- `test_product_driver_refused_and_unknown_stop_leave_identity_and_data_unchanged`
- `test_product_driver_keep_data_uninstall_matches_frozen_removal_plan`

These must execute the real product control surface. The fixture driver in
`test_shared_journey_driver.py` is not acceptance for this piece.

### D. Predicate/evaluator — M-QUAL + M-CI co-sign

- `test_direct_checkpoint_bundle_is_structurally_valid_but_not_self_authenticating`
- `test_direct_bundle_rejects_wrong_harness_run_attempt_job_artifact_or_tuple`
- `test_direct_checkpoint_never_satisfies_continuity_obligation`
- existing missing/skipped/duplicate/malformed/evidence-hash aggregate tests

One native smoke per direct OS may follow only after A-C and the required inventory co-sign.
It must run the existing `m4-6-hosted` route and remain provisional until D joins its
artifact. There is no design-time or unit-test substitution for those executions.

## 10. Dependencies and ownership

| Dependency | Owner | Exact completion needed |
| --- | --- | --- |
| Direct checkpoint/continuity scope ruling | M-INT | decided 22:04:52Z; M-CI co-sign of inventory change outstanding |
| Real product recovery/fault/uninstall control | M-R1FIX | immutable `astrid-m4-6-product-driver/v1`-capable surface for all required actions |
| Thin driver executable/config | M-QUAL after M-R1FIX input | pinned per-OS adapter and artifact roles; no evidence authority |
| Authentic R0 predecessor artifact | M-OPS | resolvable artifact/provenance satisfying the existing R0 fixture/identity contract |
| `macos-15` provenance mismatch | M-QUAL with M-CI co-sign | align `RELEASE1_MATRIX` and `RUNNER_PROVENANCE`; honor the accepted self-hosted Mac profile if still current |
| External run/job/artifact join and sole verdict | M-CI | existing evaluator/per-tuple index accepts exact direct bundle and rejects substitution |
| Provider, predicate and direct collector implementation | M-QUAL | pieces A, B and D under section 11; inventory co-sign required |

Workflow/venue dispatch is not part of this packet. No hosted or cloud run is
needed to decide the producer contract, and none was performed.

## 11. Direct checkpoint evidence ruling

M-INT decided at 2026-11-02T22:04:52Z (rule 36 P13) that Option B applies
to all three direct slots. R1 removal and continuity obligations require
step-boundary snapshots, in-step polling, independent initial and final censuses,
and data kept/removed invariants. The inventory must mark containment and
continuous-history claims `deferred-R1.x`; they never count as passed. M-CI must
co-sign the inventory change. This ruling does not by itself authorize unsafe
removal, establish the missing real driver, or supply an authenticated run.

## 12. Completion boundary

This design identifies the minimal executable producer, existing driver seam,
collector sources, trust boundaries, operation order, artifacts, checker path,
refusals, cleanup, owners, dependencies, and focused tests. It also identifies the
precise unavailable product input and the resolved protected decision. It deliberately provides
no branch, code, workflow edit, dispatch, integration handoff, live-provider claim,
or qualification claim.
