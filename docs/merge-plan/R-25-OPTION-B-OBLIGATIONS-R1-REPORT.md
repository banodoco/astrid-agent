# R-25 Option B obligation classification — Pinokio R1

Recorded 2026-11-02. Read-only coordination census for M-QUAL. This task
changed no product, inventory, checker, scenario, proof, branch, or producer
source. It claims no qualification, acceptance, or hosted execution.

## Decision and disposition

The governing decision is Option B:

- `/home/user/merge/ws/M-PIN/ORCHESTRATOR.md:3709-3738` at
  `2026-11-02T20:00:23Z` says R1 uses harness-owned snapshots at action
  boundaries, bounded in-action polling, independent initial/final censuses,
  and data invariants. Gap-free custody/removal authorization is deferred to
  R1.x; snapshots must never be recorded as continuous custody.
- The same file `:3741-3753` at `2026-11-02T20:07:42Z` names the protected
  reconciliation: `CHECKPOINT` is R1-required; `CONTAINMENT` and
  `CONTINUOUS HISTORY` are `deferred-R1.x (decision 2026-11-02, option B)`.
- `/home/user/merge-docs/docs/merge-plan/DECISIONS.md:316-318` records the
  corresponding 20:00 and 20:07 decisions.

The actionable rule for the inventory/checker is therefore assertion-level:

| Class | R1 disposition | What it may establish | What it may not establish |
|---|---|---|---|
| `CHECKPOINT` | Required; must be green for an R1 row | Retained before/during/boundary snapshots; independently authenticated initial and final censuses; health, journal state, data invariants, and observed end states | No uninterrupted custody, no gap-free absence, no exact-removal authorization |
| `CONTAINMENT` | `deferred-R1.x`; visible in inventory, never passed | Future exact ownership of controller/service/runtime/descendants and exact transition/removal set | Cannot be inferred from a snapshot, equal snapshots, PID/birth fields, or a hash chain |
| `CONTINUOUS HISTORY` | `deferred-R1.x`; visible in inventory, never passed | Future loss-detecting event history with source sequence/loss information across the action | Cannot be inferred from bounded polling, a ledger, or initial/final equality |

`CHECKPOINT` is an evidence class, not a verdict. A row with missing or
unauthenticated evidence remains `not-run`/failed under the existing
fail-closed checker. `CONTAINMENT` and `CONTINUOUS HISTORY` must be represented
as deferred, not converted to green and not deleted.

## Exact Pinokio IDs

The six Pinokio slots are the Cartesian product of two channels and three OSes:

```text
pinokio-backend-linux       pinokio-fully-local-linux
pinokio-backend-macos       pinokio-fully-local-macos
pinokio-backend-windows     pinokio-fully-local-windows
```

They are the exact `M4_6_SLOT_IDS` expansion in the published Agent source
`ab43bad818ef65ca1e929b973a2f61cd9fc4bf61`,
`scripts/journey_matrix.py:21-30,80-85`.

### Six fresh-install checkpoint IDs

These are separate from the four M4–6 failure-mode families. Each is an R1
`CHECKPOINT` obligation, required but not passed by this census:

```text
fresh-install:pinokio-backend-linux
fresh-install:pinokio-fully-local-linux
fresh-install:pinokio-backend-macos
fresh-install:pinokio-fully-local-macos
fresh-install:pinokio-backend-windows
fresh-install:pinokio-fully-local-windows
```

The exact predicate is “installed candidate, owned service, health, settled
task, one Runtime, sentinel and cleanup” (`journey_matrix.py:96-111`). For
these six IDs, the R1 checkpoint evidence must bind the exact tuple and
authenticated hosted receipt, include action-boundary observations where the
producer supplies them, and independently establish the relevant initial and
final state/data invariants. This classification does not say that any of the
six receipts exists or passed.

### Twenty-four M4–6 IDs and split assertions

For every `S` in the six-slot set above, the following exact IDs are in scope.
The class mapping is identical for each OS/channel expansion; the assertions
below are the authoritative split.

| Exact ID family (expand `S` over all six slots) | R1 `CHECKPOINT` assertions | Deferred `CONTAINMENT` assertions | Deferred `CONTINUOUS HISTORY` assertions |
|---|---|---|---|
| `interruption-recovery:S` | Controlled interruption is recorded; journal/phase and operation ID are retained; explicit recovery record is joined to the same operation; terminal state and terminal identity are independently observed; semantic data invariant is independently checked; before/during/boundary snapshots and independent initial/final censuses are retained | The controller, service, Runtime and descendants remain the exact owned incarnations through interruption/recovery; no unknown or replacement owner is admitted; recovery is attributable to the exact bound operation | No observer gap or missed start/exit/replacement from injection through terminal recovery; lossless source sequencing and loss counters |
| `rollback:S` | Failure phase and rollback result are retained; candidate/R0 identity and health observations are retained; journal terminal state, semantic data equality, and independent final census are checked; boundary snapshots cover the transition | Exact candidate/R0 process and service ownership, exact rollback/removal transition, and proof that an unowned or replacement process did not satisfy the transition | No candidate start/write/activation or replacement can occur between samples; lossless history of failure, rollback, and cleanup |
| `keep_data_uninstall:S` | Requested keep-data mode; program root/selection/service end-state fields; data root, workspace/CAS, semantic data, and unrelated sentinel preservation; zero owned Runtime at the observed final boundary; independent initial/final censuses and boundary snapshots | Exact owned removal set and single-use authorization; final absence is attributable to the bound owned incarnation and exact service/descendant set | No owned/replacement process or descendant existed between observations; no observer loss during removal/finalization; continuous cleanup history |
| `stop_safety:S` | Separate `refused` and `unknown` cases with reasons; at the observed boundaries, update admission, candidate write/download, selection, journal update, activation, data mutation, retry, and executable identity-pair invariants are unchanged; semantic data and initial/final censuses are retained | Exact controller/service/runtime ownership for the refused/unknown stop and exact protection of the bound transition | No unauthorized update/write/activation/retry or replacement occurred between samples; lossless refusal/unknown event history |

The exact 24 IDs, without a shorthand, are:

```text
interruption-recovery:pinokio-backend-linux
interruption-recovery:pinokio-fully-local-linux
interruption-recovery:pinokio-backend-macos
interruption-recovery:pinokio-fully-local-macos
interruption-recovery:pinokio-backend-windows
interruption-recovery:pinokio-fully-local-windows
rollback:pinokio-backend-linux
rollback:pinokio-fully-local-linux
rollback:pinokio-backend-macos
rollback:pinokio-fully-local-macos
rollback:pinokio-backend-windows
rollback:pinokio-fully-local-windows
keep_data_uninstall:pinokio-backend-linux
keep_data_uninstall:pinokio-fully-local-linux
keep_data_uninstall:pinokio-backend-macos
keep_data_uninstall:pinokio-fully-local-macos
keep_data_uninstall:pinokio-backend-windows
keep_data_uninstall:pinokio-fully-local-windows
stop_safety:pinokio-backend-linux
stop_safety:pinokio-fully-local-linux
stop_safety:pinokio-backend-macos
stop_safety:pinokio-fully-local-macos
stop_safety:pinokio-backend-windows
stop_safety:pinokio-fully-local-windows
```

The inventory predicates and source bindings are exact in
`scripts/journey_matrix.py:130-160`. The scenario names and required fields
are exact in `scripts/journey_failure_modes.py:32-40,252-295,298-334,337-394`:

- interruption: injected failure, durable journal state, same-operation
  recovery, terminal state/identity, and semantic data;
- rollback: failure injection, rollback selected, real R0 identity, candidate
  not activated, journal terminal state, executable identity pair, and data;
- keep-data uninstall: removal request, keep-data, program/selection/service
  removal, retained data/sentinel, zero Runtime, and semantic data;
- stop safety: both `refused` and `unknown`, explicit reasons, all seven
  unchanged mutation/data flags, identity pair, and semantic data.

The raw scenario validators require `status == "pass"`. Under Option B that
raw field cannot be used as a continuity verdict: M-QUAL must project only the
checkpoint assertions into the R1 gate and emit the custody-dependent
assertions as `deferred-R1.x`. A deferred class is not a passed assertion.

No separate `authorize_exact_removal:S` obligation ID exists in the inspected
inventory. The literal removal family is exactly
`keep_data_uninstall:S`; exact-removal authorization is its deferred
`CONTAINMENT` assertion, not a new identifier.

## Producer and checker evidence boundary

The existing R-25 producer evidence is useful but non-qualifying:

- `queue/done/R-25-SNAPSHOT-COLLECTORS-R1/REPORT.md` records Agent
  `a6b68d5b5478326a4db1971966d33a2cfe61d091` and Pinokio
  `95aafb5a029c3f53cb2268d8596909eab0deb3c4`. Its common contract is one
  `before`, one immediate `during`, bounded in-action polling, and one
  `boundary` observation for each of `start`, `ready`, `stop`, `update`,
  `rollback`, and `uninstall`, with closed-world receipt finalization.
- That report explicitly says it is component implementation evidence: no
  authentic hosted journey, no M4-6 receipt, and no qualification. It also
  records `qualification:false` and `m4_6_acceptance:false` in the producer
  receipt. The four macOS/Windows producer slots do not supply the two Linux
  slots.
- `queue/done/R-25-SPECIMEN-R1/REPORT.md` is explicitly synthetic and
  execution-free. It cannot qualify a row or substitute for the authentic
  six-slot receipts.
- The producer report says process/service snapshots do not establish
  candidate filesystem writes or semantic data preservation. At minimum,
  `stop_safety.cases.{refused,unknown}.candidate_downloaded_or_written` and
  `keep_data_uninstall.semantic_data` require a separate action/filesystem
  evidence join. These are still `CHECKPOINT` assertions when independently
  evidenced; snapshots alone leave them unresolved.

The published Agent source is fail-closed at the relevant boundaries:

- `scripts/m4_6_process_observer.py:1-13,812-845` defines bounded polling as
  non-authoritative and refuses authorization/finalization because it cannot
  prove gap-free continuity.
- `scripts/m4_6_pinokio_custody_provider.py:1-21,635-666,679-773` records an
  untrusted census and refuses custody, removal, and finalization without an
  authenticated Pinokio observer and approved gap-free source.
- `scripts/check_journey_evidence.py:649-731,766-829` validates local M4-6
  shape but keeps local-disposable evidence `not-run` for the hosted native
  service end-state inventory; it does not promote a snapshot into hosted
  proof.
- `scripts/m4_6_artifact_provenance.py:309-353,600-657` in the published
  source enforces the closed nine-slot origin/artifact shape. The available
  R-36 candidate `3005252e047d5c969d999b952df8500ea0587b00` changes the
  failure-mode source binding only; it does not add this Option B class split
  to the inventory/checker. It is a checker/provenance reference, not
  qualification evidence.

## Exact unresolved proof gaps for M-QUAL

1. The existing inventory has `evidence_kind: "m4-6"` and
   `route_state: "unimplemented"` for the 24 M4-6 IDs
   (`journey_matrix.py:130-160`). It has no explicit Option B
   `CHECKPOINT`/`CONTAINMENT`/`CONTINUOUS HISTORY` field. The protected
   reconciliation needs to add the class metadata and deferred disposition in
   the inventory/checker; this report does not make that change.
2. The six Pinokio fresh-install IDs are checkpoint-required, but the direct
   published checker requires authenticated lifecycle receipts and the
   component evidence is not such a receipt. No fresh-install ID is marked
   passed here.
3. No authentic six-slot M4-6 receipts, hosted run/job/attempt/artifact joins,
   independent finalization, or R-36 admission are present in the supplied
   evidence. The exact candidate tuple is the current tuple below; source or
   component hashes do not replace execution evidence.
4. A bounded final `runtime_processes_after: 0`, equal initial/final
   snapshots, birth IDs, receipt hash, or append-only ledger is only a
   checkpoint observation. It does not close containment or continuous history.
5. The deferred sources named by the decision are future R1.x work: Linux
   cgroup-v2 containment plus audit/eBPF history, Windows Job Objects plus ETW
   history, and `eslogger`/equivalent history on `hannah-mac`. Their design or
   component availability is not proof of implementation or execution.

## Frozen tuple and source identity

`/home/user/merge-docs/docs/merge-plan/CURRENT-TUPLE.json` SHA-256 is
`71a738081dba7f84bea0b882a975cd540b62684e8f6f1fa0d99f572e62908e50`:

```text
App      79f7e5397c044c4c97dc7e97b20ad96f787e5247
Agent    ab43bad818ef65ca1e929b973a2f61cd9fc4bf61
Runtime  2776ba973b6c8f3555b5629b0840d9691a98d3c2
Pinokio  3a6a98ae251ca126505245286e2d3e760f4348e9
Manifest b2584f0bb5a949890364004572a0182f2b568adcdba061e73b739e1d3d2d623f
```

The receipt records SHA-256 values for the two decision files, orchestration
file, tuple, R-30 interface, prior receipts/reports, and all inspected
published-Agent source files. The source checkout observed locally was
`b05ac03a5bcab278bccf99d85b49f4037b5dec10`; it was not used. All claims above
are from the immutable published Agent object `ab43bad...` or explicitly
identified producer/R-36 evidence.

## Commands and exits

This task performed read-only inspection only. No tests or hosted/native runs
were rerun. The exact command ledger, with shell/inner exits, is in
`canonical-receipt.json`; all inspection, hash, Git object, and `jq` checks
recorded there exited `0`; the two non-Git-worktree status probes are recorded
separately with exit `128`. Previously recorded producer checks are cited as
evidence, not repeated here; their reported inner exits remain unchanged.

Disposition: classification complete for M-QUAL review; qualification and
acceptance remain false.
