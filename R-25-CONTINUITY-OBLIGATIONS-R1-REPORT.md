# R-25 continuity-obligations census — Release 1 Pinokio

Recorded 2026-11-02. Read-only source and contract census; no repository,
product, checker, scenario, contract, or proof changes were made. No release
acceptance or handoff is claimed.

## Decision-ready result

The exact Release-1 Pinokio obligations whose claims depend on continuous
custody are the four M4-6 failure-mode families, for each of the six
Pinokio-owned OS/channel slots: **24 obligation IDs**.

| OS | Continuous-custody obligation IDs (backend and fully-local) |
|---|---|
| Linux | `interruption-recovery:pinokio-backend-linux`, `interruption-recovery:pinokio-fully-local-linux`; `rollback:pinokio-backend-linux`, `rollback:pinokio-fully-local-linux`; `keep_data_uninstall:pinokio-backend-linux`, `keep_data_uninstall:pinokio-fully-local-linux`; `stop_safety:pinokio-backend-linux`, `stop_safety:pinokio-fully-local-linux` |
| macOS | `interruption-recovery:pinokio-backend-macos`, `interruption-recovery:pinokio-fully-local-macos`; `rollback:pinokio-backend-macos`, `rollback:pinokio-fully-local-macos`; `keep_data_uninstall:pinokio-backend-macos`, `keep_data_uninstall:pinokio-fully-local-macos`; `stop_safety:pinokio-backend-macos`, `stop_safety:pinokio-fully-local-macos` |
| Windows | `interruption-recovery:pinokio-backend-windows`, `interruption-recovery:pinokio-fully-local-windows`; `rollback:pinokio-backend-windows`, `rollback:pinokio-fully-local-windows`; `keep_data_uninstall:pinokio-backend-windows`, `keep_data_uninstall:pinokio-fully-local-windows`; `stop_safety:pinokio-backend-windows`, `stop_safety:pinokio-fully-local-windows` |

The literal removal-authorization family is the six
`keep_data_uninstall:<slot>` IDs. Its predicate claims removal of the program
root/selection/native service and final process absence while retaining data.
`rollback:<slot>` and `stop_safety:<slot>` also contain stop/termination and
cleanup claims that require custody to attribute the observed process to the
owned incarnation. The source does **not** define a separate
`authorize_exact_removal:<slot>` ID; none is invented here. `interruption-recovery`
requires custody for same-operation recovery and terminal identity, but is not
itself a removal predicate.

The six `fresh-install:<pinokio-...>` IDs are separate sampled lifecycle
obligations. Their current predicate is installed candidate, explicit Pinokio
owner, health/settlement, one Runtime and cleanup; the census found no source
wording that makes them a gap-free continuity claim. They still require the
closed R-36 evidence path and authentic receipts.

## Exact source and contract evidence

The Agent source under review is `banodoco/astrid-agent` at
`ab43bad818ef65ca1e929b973a2f61cd9fc4bf61` (parents
`fb222b3a54e9ffe2fa4eb2c382bc25033e773949` and
`0fed29efaf2dd009d68551d051e602c2e6bdca89`). The current tuple is
`App 79f7e539`, `Agent ab43bad8`, `Runtime 2776ba97`, `Pinokio 3a6a98ae`,
manifest digest `b2584f0b…`, full values in the receipt.

The matrix and obligation IDs are defined by the published Agent head:

- `scripts/journey_matrix.py:21-30` defines the nine Release-1 OS/channel
  rows; `:84-85` aliases M4-6 to that matrix.
- `scripts/journey_matrix.py:96-112` defines `fresh-install:<job_id>`;
  `:132-160` defines `interruption-recovery:<job_id>`,
  `rollback:<job_id>`, `keep_data_uninstall:<job_id>`, and
  `stop_safety:<job_id>` for every M4-6 row. Lines `:150-160` show the exact
  three latter predicate families and their M4-6 expansion.
- `scripts/journey_failure_modes.py:32-36` fixes the four scenario names;
  `:409-425` requires exactly those four scenarios and routes each to its
  checker. The field-level checks are `:252-295` (interruption), `:298-334`
  (rollback), `:337-363` (keep-data uninstall), and `:366-394` (stop safety).

The claims are custody-sensitive because the predicates depend on identity and
cleanup across a live operation, while the implementation only samples those
facts:

- `scripts/failure_mode_executor.py:807-823` obtains a point-in-time,
  birth-bound process identity and source/module digests.
- `:837-917` records interruption recovery and then checks cleanup after the
  operation; `:926-1016` records rollback before/after identities and cleanup;
  `:1026-1077` checks uninstall end state and process absence;
  `:1088-1172` checks refused/unknown stop byte invariants, identity pairs and
  cleanup.
- `scripts/check_journey_evidence.py:697-731` validates all nine local M4-6
  rows, but `:649-650` calls them local-only and never hosted proof.
  `:766-829` emits the end-state status and explicitly leaves a valid local
  M4-6 record `not-run` because hosted native-service evidence is required.

The Pinokio provider does not silently promote these samples:

- `scripts/m4_6_pinokio_custody_provider.py:1-21` says the source-shaped
  provider cannot produce live settlement or authorize custody.
- `:553-559` records a missing B observer as a permanent gap;
  `:635-666` marks a census untrusted and refuses authorization even when an
  observer is attached because it is polling-only.
- `:679-773` closes `custody_binding`, `register_start`, `observe`,
  `removal_census`, `terminate_owned_runtime`, `observe_absent`, and
  `remove_cleanup`; `:714-716` names the missing authenticated custody and
  gap-free observer.

OS/channel coverage of the current observer is explicit in
`scripts/m4_6_process_observer.py`:

- `:1-13` defines bounded polling snapshots as non-authoritative and says they
  cannot authorize removal/finalization.
- Linux uses `linux-bounded-proc-systemd-poll` at `:358-359`, with bounded
  `/proc` and systemd observations at `:377-394`, `:420-475`, and `:506-533`.
- macOS uses `macos-bounded-launchd-libproc-poll` at `:536-540`; the source
  explicitly says it is polling-only at `:568-571` and refuses incomplete
  descendant census at `:610-614`.
- Windows uses `windows-bounded-task-cim-poll` at `:617-620`; the fixed
  PowerShell/CIM/Task Scheduler observation is bounded at `:628-671` and
  refuses incomplete owner/descendant census.
- The common observer records `continuity: not-proven` at `:712-720`, labels
  missing event sequencing at `:759-779`, permits sampled `initial`/`poll`
  records at `:781-810`, and unconditionally refuses authorization/finalization
  for bounded polling at `:812-845`.

## Contract and R-36 boundary

The approved R-30 interface is
`/home/user/merge-docs/docs/merge-plan/handoff/inputs/M-QUAL-R-30-verifier-interface.md`,
SHA-256 `2f685c8aec21bef116f95645920717e4b12ef33c1ca820fe6185eb01ffea115d`.
Its exact requirements are:

- §2.3, line 68: removal order is authenticate → bind exact custody → observe
  owned objects → authorize the exact removal set → execute once → independently
  observe final absence/restoration/data invariants → finalize/reap; it rejects
  PID reuse, unknown descendants, definition drift and changed removal sets.
- §2.3, line 70: observer loss, overflow, lineage loss, channel/timeout/exit/
  reap failures and collector drift abort the grant.
- §2.3, line 72: continuous custody requires demonstrable event completeness,
  source sequence/loss counters, and independent initial/final censuses. A hash
  chain or two equal snapshots is insufficient; polling-only observers are
  non-authoritative.
- §4, lines 245-246: live-session/custody includes exact incarnations,
  listener/roots/locks, removal plan, loss information and final census; no
  native-service fallback satisfies a Pinokio ownership row.
- §4, lines 254-255 and 273: exact custody bind, single-use exact-removal
  authorization, and independent finalization are session operations; serialized
  snapshots cannot authorize removal.
- §10, line 341: approved loss-detecting OS source APIs/profiles, process and
  manager binding, exact-child lifecycle, and independent finalization probes
  are still open inputs; current polling primitives do not satisfy them.

The accessible current R-36 source is Agent object
`010e8f25c70109f0a93a6fccb0a16f7f40e06739` (`lane/M-QUAL/R-36-v3`, parent
`2f845c51c8a3f9af784868fcdd4678bc442deb90`), not the requested Agent
candidate. Its `scripts/m4_6_artifact_provenance.py:41-50` freezes the exact
nine slots and `:321-352` enforces closed-world count/order; `:631-657` rejects
missing/duplicate origin and artifact rows. Its `check_journey_evidence.py:611-670`
only validates the nine local-disposable records and labels them never-hosted;
there is no current R-36 source here that supplies the missing gap-detecting
observer or exact-removal session. This is a checker/provenance reference, not
qualification evidence.

R-25’s current collector evidence is component-only: Agent
`a6b68d5b5478326a4db1971966d33a2cfe61d091`, Pinokio
`95aafb5a029c3f53cb2268d8596909eab0deb3c4`, R-36 reference above. Its report
(`queue/done/R-25-SNAPSHOT-COLLECTORS-R1/REPORT.md:58-66,125-136,172-189`)
requires action-bound before/during/boundary snapshots and bounded polling but
states that they do not prove gap-free absence, and that six authentic Pinokio
receipts remain required. The specimen report repeats this limitation and is
explicitly synthetic/non-qualifying.

## Sampled end state versus continuity

| Evidence currently supports | Evidence does not support |
|---|---|
| A manager/service/process state at a retained boundary; PID/birth/path fields where the source returns them; before/after byte or semantic digests; a zero-process observation at the sampled cleanup boundary; closed receipt-file shape and tuple/artifact joins when independently authenticated. | No short-lived process or replacement between samples; no uninterrupted controller/descendant custody; no lossless event sequence; no exact removal authorization; no proof that an observed final absence was continuously true; no candidate-write/data-preservation fact from process/service snapshots alone. |

Bounded polling limits are explicit: the collector samples at configured
boundaries and bounded in-action intervals; a missed start/exit/replacement can
occur between samples, and a process can be PID-reused unless birth identity is
independently rechecked. Increasing polling frequency, hashing snapshots,
retaining a ledger, or comparing equal initial/final snapshots does not close
that gap under R-30.

## Gaps and decision requested

1. To keep the current Release-1 promise, the 24 IDs above need a reviewed,
   authenticated, loss-detecting observer and session-scoped exact custody /
   removal-finalization path on Linux, macOS and Windows. The current provider
   correctly refuses admission until those inputs exist.
2. If Hannah decides Release 1 will not make those continuity/removal promises,
   M-INT must record an explicit scope decision narrowing the affected IDs and
   their acceptance predicates. That is a release-policy decision, not a
   checker relaxation, and is outside this read-only census.
3. No current source identifies a separate `authorize_exact_removal` obligation
   ID. The six `keep_data_uninstall` IDs are the exact literal removal family;
   rollback/stop cleanup remains custody-dependent but is reported as such,
   without inventing identifiers.

This report is the routing notice to M-QUAL and M-INT requested by the task.
It claims neither release acceptance nor a handoff.
