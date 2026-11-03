# Independent durable authority for R18 connected delivery

Status: **PROPOSED DESIGN; source diagnosis verified; no implementation or protected approval.**

## 1. Decision for M-RT

Agent owns the independent durable delivery-attempt authority, as an extension of its existing installation transaction authority. Runtime owns the live broker, original kernel custody, authenticated recipient channel, and the single send. Before Runtime can queue any descriptor, Agent must durably record **may_have_delivered** for the exact attempt in storage outside Runtime support and all candidate/data rollback scopes. Only the uninterrupted initial reservation exchange can create a process-local first-send permit. A recorded reservation, query result, replayed receipt, reconstructed session, or missing file never creates that permit.

The authority records an attempted transport effect; it does not record successful delivery, native admission, readiness, commit, or a second terminal outcome. The existing Agent `update-transaction.json` remains the lifecycle decision journal. The two Runtime files remain subordinate transport/recovery records. Losing both Runtime records leaves the Agent reservation occupied and cannot cause a resend.

This is a proposed **new private API and durable registry**, not a capability demonstrated in either inspected repository. Runtime R2 must retain its refusals until the Agent producer, Runtime consumer and required tests exist and receive direct protected review. This packet does not authorize implementation or change the frozen bridge, migration policy or qualification requirements.

## 2. Verified source and evidence limits

| Input | Exact identity and finding |
|---|---|
| Runtime candidate | `lane/M-RT/R18-RT-CONNECTED-DELIVERY-R2@96c0391d286b150dbb87ebfe3e5f1f9d7aa08b78`, sole parent `2776ba973b6c8f3555b5629b0840d9691a98d3c2`; inspected through a task-clone checkout. |
| Published Agent base | `b54117c9312165e2799554ec27480665d5cad2b1`; independently inspected task-clone checkout, not a proposed product tuple. |
| Critical request | Manager `ORCHESTRATOR.md:4602–4604`, 2026-11-03T06:18:19Z: M-RT requests the independent API after the both-records-lost failure. The relay's “fails only” statement is not an independently verified claim about all other candidate behavior. |
| Lifecycle design | `queue/done/R18-LIFECYCLE-TRANSFER-DESIGN/DESIGN.md`, especially §§3–4 and §9: one Agent lifecycle transaction; separate Runtime live custody; stable query/reentry; terminal release-pending; unknown/replacement preservation. |
| Issuer-barrier design | The brief's `queue/done/R18-WIRE-ISSUER-BARRIER/DESIGN.md` does not exist. Its REPORT/receipt identify the actual artifact: Agent `a58f76ebcf1a655aed2fe42911d8114a4abcb5e6:DESIGN-R18-LIFECYCLE-WIRE-CONTRACT.md`, SHA-256 `2a75a696ef5517938b3adc791e9b94c8f78d7e4c407630d04284ebeb1c8c83a9`. Read that exact Git object. Its proposed v3 barriers and native feasibility gates are inherited, not proven here. |
| M-RT full review | Not present at `docs/merge-plan/handoff/inputs/M-RT-R18-CONNECTED-DELIVERY-R2-REVIEW.md` in inspected docs HEAD `edb233602abdf7f0fb7800266a9a4b586a73f759`. `/home/user/merge/ws/M-RT` and its outbox are inaccessible/nonexistent here. No review acceptance is inferred. |
| Requested subworker | `sub/1/REPORT.md`, completed exit 0: independent read-only candidate analysis agrees with the failure and authority boundary. It supplies no protected approval or test result. |

Exact Runtime source diagnosis:

- `astrid_runtime/lifecycle_transfer.py:329–340,377–388`: missing ledger returns `None`; query then returns `observed` and no requests.
- `runtime_protocol/_lifecycle_transfer_delivery.py:20–36`: missing marker returns `None`; existing equal bytes are occupied/uncertain; changed bytes report binding_changed.
- `lifecycle_transfer.py:567–606`: absent marker plus initial phase/sequence creates and fsyncs the marker, records recovery-required in the ledger, checks the marker again, then calls `send`. Both files are under the same runtime-support directory. No independent transaction producer participates.
- `runtime_protocol/lifecycle_transfer_control.py:168–192`: opt-in provisional delivery authenticates the controller and supplies recipient identity; it calls that send seam. This is provisional descriptor transport, not writer admission.
- `runtime_protocol/lifecycle_transfer_broker.py:134–158`: one `sendmsg(SCM_RIGHTS)`; short/error delivery is uncertain and never retried. `:161–189` closes aliases without `LOCK_UN`.
- `tests/test_lifecycle_transfer_delivery.py:88–205`: three tests cover replay/restart, lost claim reply and pre-send crash, including missing-marker-with-ledger and marker replacement; there is no both-records-lost case in this file.

Exact Agent source diagnosis:

- `astrid/installer/core.py:68–87`: absent JSON returns `{}`; writes fsync a temporary file and use Runtime's durable replacement. This general helper is not the new authority validator or a create-only allocator.
- `core.py:377–397,424–461`: a stable journal operation ID is created for the transaction; `transaction_context` is saved in staged before stop intent; C2 job linkage is written separately to `install.json`. This is not a cross-file atomic transaction and none of these fields independently burns a delivery attempt today.
- `core.py:90–98,291–303`: current pending/recovery logic short-circuits on legacy terminal steps. The inherited release-pending behavior still needs product implementation; this design cannot rely on those current short-circuits.
- `astrid/installer/local_update.py:519–533` and `core.py:524–534`: existing installation/operation lock ordering must be preserved. `service.py:131–144` derives a per-user installer lock outside removed program/data files. That location motivates the separate control-store proposal below; the current code has no such registry.
- Runtime `runtime_protocol/platform.py:184–223`: POSIX directory fsync and durable replacement exist; Windows uses its native write-through move. That is not proof of a portable multi-record commit or of native Windows transfer.

Local verification: the exact candidate's three delivery tests passed. A task-local probe used the candidate's actual broker/SCM_RIGHTS helpers, closed the first aliases, stopped the broker, removed both Runtime records, restarted with the same binding, and received both aliases again for the identical request. Its no-redelivery assertion failed as expected. This ran as UID 0 on Linux with synthetic transaction/source fixtures and real kernel sockets/locks; it is diagnosis, not non-root installed/native admission proof. Logs, command exits and the probe are retained in the task packet. An initial probe collection failed because the external test lacked the Runtime pytest config; the corrected command explicitly supplies `-c <runtime>/pyproject.toml`. No full suite ran.

## 3. Owner, storage and authority lifecycle

**Owner:** M-R1FIX implements the Agent producer in the installer transaction layer, reviewed directly by M-AG. M-RT implements the Runtime consumer and live first-send guard, reviewed by the designated Runtime protected reviewer. Agent owns durable reservation and its association with its existing operation; Runtime cannot initialize or reset the store on its own. Existing Pinokio lifetime custody remains separate.

**Proposed storage:** derive `control_parent = installer_lock_path(home, system).parent` using the installed adapter, then use `control_parent/.astrid-connected-delivery/<installation_epoch>/registry/` containing immutable, contiguous `revision-<20-digit-U64>.json` snapshots. The implementation must authenticate the private authority directory, principal and installation association through pinned no-follow/native handles. Do not create it under Runtime support, a version directory, a workspace, a migration backup, or any root that update rollback/recovery replaces. If configured roots overlap that control location, choose and durably enroll an independent owner-private control location before enabling transfer; otherwise refuse the transfer path. Do not silently change protected data backup contents to make the location fit.

This provides independence from deletion of the two named Runtime records and from ordinary candidate/data restore. It is **not** an independent disk, a remote service, or protection against deletion/rollback of all local authority. The precise loss model must be stated in acceptance evidence.

One versioned authoritative registry per installation keeps all reserved attempt keys and their immutable digests. Each revision is an immutable full snapshot with its predecessor digest; revision 0 has an empty history and null predecessor. Open/recovery reads a bounded complete directory inventory and verifies the contiguous revision/digest chain, canonical filenames and journal references. An incomplete/unknown inventory, hole, partial revision or unexpected successor refuses; never select an older valid revision to ignore a bad newer object. No mutable head pointer or in-place registry overwrite is needed; the existing journal holds its exact registry/entry reference in `transaction_context.lifecycle_transfer.delivery_authority`. The registry has no competing complete/rolled_back decision. It stores occupied attempts and evidence references for their reconciliation. Retain occupied keys/tombstones through terminal completion, selection change, rollback, reinstall using the same enrolled installation, broker restart and Runtime-support cleanup. No R1 garbage collection or automatic reset is defined. A bounded full registry refuses with recovery_required; it never evicts replay history. Explicit removal of an installation/control store is a separate destructive lifecycle policy and cannot make replayed old requests legitimate.

The registry's closed top-level record is:

```text
Registry = {version:"astrid.connected-delivery-authority/v1",
            installation_epoch:ID, principal:Principal,
            installation:ObjectIdentity, authority_directory:ObjectIdentity,
            revision:U64, prior_revision_digest:Digest|null,
            active_operation:Token|null, entries:[Reservation]}
Reservation = {key:DeliveryKey, binding_digest:Digest, request_digest:Digest,
               base_journal_digest:Digest, predecessor:Digest|null,
               broker:NativePeer, recipient:NativePeer, channel_nonce:ID,
               objects:[directory:ObjectIdentity,owner:ObjectIdentity],
               state:"may_have_delivered", settlement:Settlement|null}
DeliveryKey = {journal_operation_id:Token, update_operation_id:Token|null,
               attempt_id:ID, transfer_id:ID, generation:ID,
               role:Role, delivery_index:U64, request_id:ID, sequence:U64}
Settlement = {kind:"closed"|"detached", evidence_digest:Digest,
              journal_snapshot_digest:Digest, resulting_revision:U64}
```

Use the existing design's closed ID/Digest/Principal/ObjectIdentity/NativePeer encodings, actual file IDs and authenticated native birth/image/containment identity, not path/PID claims alone. IDs allocated for this new API are 32 lowercase hex characters; journal/outer operation tokens preserve existing exact values. Serialize canonically, reject duplicate keys, unknown fields, unsupported versions, wrong ownership/ACL, links, size overflow, torn data and contradictory records. Native peer identity includes the OS boot/session identity where needed to prevent birth reuse across reboot; unavailable identity refuses. No API accepts a caller-selected authority path. Create-only revisions preserve an encountered replacement instead of overwriting it. Pinned-parent revalidation detects a changed path but does not provide an OS atomic compare-and-change for arbitrary external editors. The inherited replacement-safe mutation boundary still applies to Agent journal/native mutations: the implementation must establish the required exclusive mutation scope or native ownership/no-overwrite guard; pre/post samples alone do not prove race-free mutation. If that scope cannot be established, refuse before effects or retain recovery after effects. No adversarial same-user deletion/rollback protection is claimed.

Enforce slot uniqueness on `(installation_epoch, journal_operation_id, attempt_id, transfer_id, generation, role, delivery_index)` independently of request ID/sequence. Changing the request ID does not create a fresh slot; reusing a request ID with different bytes or another slot returns binding_changed. Each successor slot requires the authenticated predecessor settlement and original operation authority.

The full immutable Runtime Binding digest and exact canonical delivery request digest are both bound. The latter excludes the bearer capability bytes, includes the authenticated recipient identity added by Runtime, and covers the exact role, sequence and original objects. A nonsecret reference/digest authenticates neither producer nor holder. Do not persist bearer credentials. Bind `base_journal_digest` to the immutable staged/pre-reservation snapshot retained for this entry; later journal writes cannot invalidate that base or make a hash cycle with the new authority reference. The mutable journal may change only through its legitimate owner and references the reservation after it exists.

## 4. Initialization is an explicit trusted boundary

There are two different operations: enrollment of an installation authority, and reservation of one delivery. Neither is a recovery fallback for a missing file.

**Enrollment:** an installed Agent operation owner creates the private authority directory and initial registry under existing installation locks, fsyncs revision 0 and its parent directories, then durably binds its epoch/location/identity in Agent installation metadata before advertising connected-delivery capability. Only an authenticated fresh-install creation path, or a separately reviewed explicit upgrade enrollment from a positively established pre-transfer state, may enroll. The upgrade enrollment must establish that no previous connected-transfer authority/attempt exists for this installation lineage; absence of marker/ledger/journal or a free lock cannot establish that fact. If that provenance is unavailable, preserve the installation and refuse this capability pending explicit restoration/reconciliation. Legacy capability remains disabled until enrollment commits. This is a feasibility/review gate, not a claim that today's Agent already supplies such proof.

**Opening:** ordinary operation, restart, query and recovery use open-existing only. Missing enrollment binding, missing registry, mismatch, truncated state, unknown revision or inaccessible control storage gives recovery_required/unknown_identity. Do not call mkdir/initialize to repair it. A positively established pre-enrollment installation may continue its existing non-transfer behavior; an enrolled or uncertain active transfer must first reconcile.

**New operation:** the existing Agent operation factory allocates the stable journal ID once under its established locks and publishes staged plus the current authority reference before sending any initialization request. It checks there is no unfinished/release-pending operation and no unresolved registry entry for the installation/realm. Existing App save/POST and same-operation rules remain the admission mechanism; choosing a new random operation/request/generation is not a way to bypass uncertainty. Initialization takes an opaque live `OperationAuthority` from that owner, never a dict asserting “fresh=true.” Reentry recovers the old ID; it does not recreate the factory object for the old attempt.

There is deliberately no durable “unspent, therefore safe to send after restart” state. Only the original live creation/reservation exchange can authorize the first send. This sacrifices an ambiguous never-sent attempt after a crash rather than reconstructing a permit from absence or an old initialized snapshot.

## 5. Minimal private API and first-send ordering

Proposed Agent facade, owned by the existing installer operation; names below are the implementable interface decision:

```python
open_delivery_authority(operation: OperationAuthority, *, enrollment: EnrollmentRef
                        ) -> BoundDeliveryAuthority

BoundDeliveryAuthority.initialize_delivery(*, key: DeliveryKey,
    binding: BindingRef, request: CanonicalDeliveryRequest,
    base_journal: JournalSnapshotRef, broker: AuthenticatedBroker,
    recipient: AuthenticatedRecipient, channel_nonce: ID,
    objects: OriginalObjectIdentities, expected_revision: int
    ) -> CreatedReservation | DeliveryStatus

BoundDeliveryAuthority.query_delivery(*, key: DeliveryKey) -> DeliveryStatus

BoundDeliveryAuthority.settle_delivery(*, key: DeliveryKey,
    expected_revision: int, evidence: VerifiedCloseOrDetachRef,
    journal_snapshot: JournalSnapshotRef) -> DeliveryStatus
```

Proposed Runtime integration:

```python
TransferSession.deliver_once(*, authority: AuthenticatedAuthorityChannel,
    request: CanonicalDeliveryRequest, recipient: AuthenticatedRecipientChannel
    ) -> DeliveryStatus  # initial success may deliver aliases, never grants admission
```

These are private local interfaces. They add no HTTP route, public response field, generated client or change to the existing closed provisional-control/v1 frames. M-RT must define a separately versioned private authority exchange and closed fields for this delta; silently adding fields to v1 or bypassing the proposed wire v3 barriers is forbidden. Until that implemented exchange exists, the opt-in R2 probe is not the product admission route.

`DeliveryStatus` has a closed result code, stage, exact key/reference/revision, `may_have_delivered`, `side_effects` and recovery references. Codes reuse existing `busy_owner`, `unknown_identity`, `binding_changed`, `unsupported_transfer`, `lease_uncertain`, `durability_failed`, `recovery_required`; add a private `created` response only to the initial authority exchange. Query may report `reserved`/settled facts but never `created` or a permission to send. Missing/unknown state is explicit and never translates to delivery success or “not sent.”

Normative sequence:

1. Agent holds its existing installation/operation locks; recover prior operations before downloads/new staging. Persist staged/base snapshot and authority enrollment reference. Runtime authenticates the actual Agent owner and both sides bind the exact broker native incarnation, operation, binding and a fresh process-local channel nonce. Runtime pins/holds original custody using existing ordering. Any missing native/admission facts still refuse the relevant product transition.
2. For one eligible delivery, Runtime authenticates/pins the actual recipient and original directory/owner objects, establishes a fresh authenticated authority exchange, and allocates one local pending nonce. Agent independently checks the live operation, base journal and registry, exact binding/key/recipient/broker/object values and expected revision. A rebound request ID or different bytes under a used attempt slot refuses. A new request ID/role/generation cannot bypass an unresolved predecessor for this realm.
3. Under the **already-held** Agent operation authority, append the new `may_have_delivered` entry by publishing the full next immutable revision. Under the existing cooperating-writer operation lock, validate the complete current chain and expected revision, then create the exact next canonical revision filename with `O_CREAT|O_EXCL|O_NOFOLLOW` (native equivalent on supported platforms), mode 0600, in the pinned private directory. Write completely, file-fsync, verify leaf/parent identity and ownership, directory-fsync, then perform final identity/chain readback. Never replace, unlink or truncate an existing revision. Exclusive creation is the cross-process reservation linearization point; the initial response waits for completed durability. An extant exact revision is occupied, never a fresh successful create. A partial new revision is an explicit poison/blocker, not reusable empty space. Every initial directory creation must also have its parent fsynced. A crash/torn revision/unknown flush result leaves the attempt occupied or unknown; no send is permitted. Refuse storage/platform semantics that cannot meet this durability contract. `core.write_json` alone does not implement these validation and serialization rules.
4. Persist the entry reference and `delivery_pending=true` in the existing Agent transaction context with its normal durable write. **Both step 3 and step 4 must complete before any CreatedReservation response**. There is no claimed cross-file atomicity: a reservation without the journal update blocks; a journal reference without a matching reservation blocks. The registry is the reservation authority; the journal is the lifecycle authority. Disagreement never selects the more permissive state. C2 job mapping must remain exact; if required linkage is unknown, return recovery_required before send.
5. On the uninterrupted initial exchange only, Agent returns CreatedReservation bound to the pending nonce and broker incarnation. An existing entry returns occupied/status even if every byte matches; it never returns created again. Lost response, reconnect, timeout, Agent crash or query cannot recover this response. Runtime accepts it only on that exact authenticated channel with its still-pending local nonce; it creates an opaque process-local `FirstSendPermit`. A serialized receipt, claimed callback result, saved nonce or caller Boolean is insufficient. Permit construction/consumption must be inaccessible to wire callers and query paths.
6. Runtime still performs R2's local durable marker reservation and recovery-required ledger write; revalidate actual original handles, binding, peer, authority reference and live channel. These records add diagnosis and refusal checks; they are not the source of first-send permission. A failure now burns the attempt without sending. The permit is scoped to this single live `deliver_once` invocation: every error/return invalidates it, including a failure before send; it cannot be saved or resumed. Consume the local permit in a mutex-protected transition **before** entering sendmsg; mark it consumed even on an exception. Invoke sendmsg exactly once, with no stream/short-send retry. Do not retry a supposedly zero-byte failure: rights may already be queued.
7. Record available transport result/ACK facts conservatively. `may_have_delivered` remains true for success, lost ACK, close, timeout and error. Receiving aliases establishes neither writer admission nor a successful lifecycle outcome. Ordinary native/source/work/claim-before-DB rules must still be satisfied by their existing verified producers.

Atomicity required is the registry's serialized create-only reservation and the local pending→consumed transition, not a distributed atomic transaction between disk and socket. The ordering `durable reservation → durable journal reference → live permit → local records → one send` makes every crash gap fail closed. Never hold a SQLite write transaction across this exchange. Agent services the authority request under its original operation owner/event loop without reacquiring its own locks; it invokes no ordinary Runtime bootstrap/down or native callback that needs the broker's retained locks. Timeouts are bounded; a dead controller yields no permit. If that nonrecursive topology is unavailable, refuse rather than introduce a lock inversion or second installer owner.

## 6. Crash, loss, unknown and replacement recovery

| State at failure/recovery | Required behavior |
|---|---|
| Before durable reservation | No descriptor send is allowed. A genuine still-live original operation may attempt initialization once; restart/reentry first opens existing authority and reconciles. Do not infer freshness from empty Runtime files. |
| Reservation creation/write/fsync uncertain | No positive initial response, no send; preserve evidence and recovery_required. A visible entry is occupied. Missing or contradictory expected state is unknown, not available. |
| Reservation durable; journal link absent | Discover the occupied entry via enrolled registry before new operation selection; same operation may repair the exact reference after validation, but cannot obtain a first-send permit. |
| Journal link durable; initial reply lost | Query returns may_have_delivered. No repeat initialization response or send, even if the broker claims it never saw the reply. |
| Permit exists; crash before local marker/ledger/send | Reservation stays occupied. New broker/native birth cannot replay the old channel nonce. No recovered permit, no resend. |
| Partial/failed send, lost claim/ACK, peer disconnect | Occupied/lease_uncertain; account for all received/queued aliases and possible inheritors. EOF, process exit, elapsed time and send error do not prove no delivery. |
| Both Runtime marker and ledger lost | Agent reservation is consulted first and returns occupied; no new aliases for the old request. Runtime may record a conservative recovery summary through a separately controlled repair path, but may not recreate an observed/available session or re-send to rebuild state. Product progress requires explicit same-operation reconciliation. |
| Only one Runtime record survives | Keep existing equal/rebound/corrupt-record refusals. Registry and journal cannot weaken them or erase a disagreement. |
| Independent registry/journal unavailable, absent, corrupted or mismatched | Preserve original records/data/selection/replacements; no init fallback, delivery, automatic start, success, clear or synthetic receipt. Return structured recovery_required (binding_changed for positive mismatch; unknown_identity for unavailable identity). Retain available owned custody/gates. |
| Broker/controller replacement or reboot | Existing reservation remains occupied. Authenticate the new owner and reconstruct observation/recovery only. Reacquisition of the actual original lock is required for future work but does not clear history. Changed controller/broker birth cannot inherit a live permit. |
| Root/owner file/authority directory/native manager/recipient replaced | Poison the active attempt; preserve replacements without signal/unlink/overwrite. No pathname reopen/relock as a substitute for the original object. Never overwrite a registry encountered at a changed identity. |
| Agent terminal complete/rolled_back, release pending | Reconcile release and authority settlements before reporting operation finished, admitting a new transfer or guarded ordinary launch. Current terminal short-circuits require protected implementation; do not use them as completion proof. |

A matching registry reservation proves only that the first delivery **may** have occurred. It never proves receipt, adoption, successful migration, readiness, shutdown or native inactivity. Missing records prove none of those either.

`settle_delivery` never clears the reservation or returns created. The registry keeps `active_operation` until the existing Agent terminal decision and native release have both been verified; closure of an intermediate helper clears only its live occupancy, not the enclosing operation. Clear journal `delivery_pending`/release-pending only after the corresponding verified settlement/release. A crash between registry settlement and journal acknowledgment is reconciled idempotently from the exact surviving evidence; it grants no send permit. It may attach `closed` only after Runtime verifies exact DB/work drain and all delivered/queued directory/owner aliases, recipient and possible inheritor closure, using the inherited complete NativeCloseProof and native evidence rules. It may attach `detached` only after exact durable Agent terminal decision plus verified accepted detach and release proof; that preserves the legitimate running writer. Peer EOF, a PID census, current free lock or a driver-created proof digest alone is insufficient. Replacement or incomplete native evidence remains lease_uncertain.

A later sequential helper or recovery candidate uses a **different explicitly armed delivery**, with its predecessor settlement and legitimate same-operation continuation durably bound. It cannot reissue a spent request or disguise it with new random IDs. Before another writer, positively establish the old writer/queues/inheritors closed and recheck actual lock/native/work constraints; after a settled detach, honor the live legitimate owner and existing stop/admission policy. Recovery reconciles actual backup/migration/selection results rather than repeating work based on a missing reply. No automatic repeat is introduced. The exact user continuation and new-generation decision belongs to Agent; Runtime cannot decide it from local file absence.

## 7. Information limit when all durable authority is lost

Consider two histories with identical surviving local state:

- H0: no descriptor was ever sent; all transaction/authority records are absent.
- H1: descriptors were sent, recipients subsequently closed/exited, and all records of that attempt were erased or rolled back.

A recovered process sees the same paths, free locks, native census and request bytes in H0 and H1. Any algorithm that permits a new send from that state in H0 permits a repeat in H1. Random IDs, extra local copies erased by the same event, a hash, fresh initialization or kernel reacquisition cannot reveal the missing history. Thus **no-resend plus useful automatic first-send/retry availability is impossible after loss of all authoritative history without a surviving independent/external monotone authority**. No-resend alone remains achievable by permanently refusing ambiguous recovery.

This API guarantees the two-Runtime-record loss case only while its independently enrolled Agent reservation history survives intact and storage honors acknowledged durability. It does not claim that colocated files survive total disk loss or that arbitrary stale backup restoration is detectable. Detectable loss/replacement refuses. Indistinguishable rollback of every local witness requires an external surviving authority/epoch witness or disabling delivery after restoration until history is restored; this packet supplies neither such external service nor a claim of automatic recoverability. A user approval or a new random operation ID cannot certify that a prior send did not occur.

## 8. Frozen compatibility and safety requirements

Preserve the prior designs' P1–P8: authentic pinned source/selection; exact effective native definition/principal; complete required owner/descendant observations; retained original kernel custody; restart/launch fencing; durable work safety; authorized publication/selection lineage; live authenticated session with supported rollback. A reservation supplies only transport history. It establishes none of those predicates by itself.

Keep close-only disposal and descriptor noninheritance; claim/adopt before DB; no unlock/reacquire gap, new lock, temporary old daemon or synthesized R0 stop receipt. Restore prior stopped R0 to stopped, preserve exact loaded/enabled policy, same-schema post-switch work, verified backup/migration recovery, unrelated sentinels and replacement publications. Unknown/replacement owners receive no unauthorized signal or native mutation. Do not close the final exclusion alias while an uncertain writer/queue can survive.

Non-transfer installations and operations keep their existing command, service and bridge behavior. Merely importing/opening the private API cannot enroll, initialize, write a registry, reserve an attempt, change readiness or touch a native service. Capability negotiation explicitly selects the new private producer/consumer; unsupported/old pairs retain current refusal for transfer. Enrollment/registry defects on a positively non-transfer legacy installation do not create a new general launch gate. An enrolled active or uncertain transfer still requires recovery before a competing operation.

Preserve App failed-save→zero POST, successful-save→exactly one POST, observational GET, explicit same-operation continuation and stable C2 mapping. Recovery before fetch/staging, native release-pending discovery even after a legacy terminal step, and all existing protected data/bridge assertions remain requirements. No public schema, bridge shape, migration, protected test or checker is weakened by this packet. Issuer barriers still authenticate only their defined observations; they do not prove uninterrupted native sampling/custody or retrospective historical containment. No native feasibility gate is discharged here.

## 9. Required implementation evidence and direct review

Required focused tests, to be implemented by the named owner (these descriptions are obligations, not falsely claimed existing test names):

| Owner | Required assertion |
|---|---|
| Agent / M-R1FIX | Enrollment create-only/provenance, open-existing refusal on missing/corrupt/replaced authority, exact source/installation/epoch binding; preserve legacy non-transfer path without creating authority. |
| Agent / M-R1FIX | Two concurrent initializers under real operation locks yield one reservation; changed request ID/bytes/role/generation cannot bypass it; repeated initialize/query never returns created. Keep stable journal/C2 IDs. |
| Agent / M-R1FIX | Failure injection at revision exclusive-create/write/fsync, parent fsync, journal-link write and initial response. No send until both durable writes succeed. Reservation survives a journal-link gap and remains discoverable before new staging. |
| Runtime / M-RT | Existing connected-delivery tests unchanged; add exact both-records-lost case with a surviving Agent reservation, same request after restart and same running holder, assert zero further SCM_RIGHTS sends and preserve original aliases. |
| Joint | First initialization succeeds once; crash before send burns it; lost reply/reconnect/reboot/nonce replay never reconstructs permit; one pending/consumed permit under concurrency; no retry after partial send or zero-byte/error claim. |
| Joint | Authority unavailable/corrupt/deleted, registry-vs-journal disagreement, entire Runtime-support deletion/recreation, authority path/root/owner/recipient/broker replacement, wrong principal and symlinks all refuse without touching replacements or broadening cleanup. |
| Joint | Full alias/queue/inheritor uncertainty blocks successor; complete close permits only explicit new armed role; exact detach/release preserves legitimate writer; terminal release-pending crash is discovered before fetch/new operation. |
| Agent + App owner | Existing same-operation, save/POST counts, observational GET, terminal journal precedence, protected rollback/migration/same-schema data invariants and stopped-R0 exact restoration remain unchanged. No fake success from missing state. |

Source-existing focused regression anchors include the three `tests/test_lifecycle_transfer_delivery.py` tests above, `tests/test_lifecycle_transfer_session.py::test_close_never_unlocks_shared_descriptions`, `::test_replacement_poison_preserves_held_originals`, and `tests/test_lifecycle_transfer_transport.py::test_extra_or_truncated_rights_are_all_closed`. Implementers must collect actual nodes, fail on zero collection, and run the selected tests at the exact resulting heads. This session ran only the three delivery tests and the task-local failing probe.

Native acceptance requires a real non-root installed Linux user-manager stage with exact Runtime/Agent/App pin chain, authentic R0 provenance, original kernel locks, actual SCM_RIGHTS/peer credentials, supported private control-store filesystem, real crash points and unchanged protected journey assertions. Exercise broker death with live receiver, queued/lost rights, controller death, both-file deletion, independent-store unavailability, native replacements and explicit recovery. Include the affected macOS/Windows platform-specific durability/security/identity tests before enabling their positive path; Linux socket tests are not that proof. Record P13 checkpoint observations honestly; do not call polling continuous history. No full journey matrix is requested by this design task.

Direct decisions/review requested on this exact published packet:

1. **M-RT:** accept or give exact counterexample to the reservation→journal→live permit→one-send ordering; own the authenticated private authority exchange, process-local permit construction/consumption, refusal behavior and both-records-lost/native tests. Confirm that connected delivery remains provisional until all inherited admission gates exist. Return exact producer/consumer signatures or required delta.
2. **M-AG:** review Agent ownership, control-store placement relative to every restore root, lock topology, enrollment provenance, retained history, C2 linkage and terminal release-pending recovery. Name the source/API that can establish safe first enrollment for existing installations; if unavailable, identify that feasibility gap explicitly rather than accepting absence as proof.
3. **M-RT + M-AG jointly:** confirm the supported storage/loss model and predecessor settlement needed for an explicit new generation. A demand for automatic recovery after erasure/rollback of all authorities requires a named surviving external authority and a separately reviewed protocol; it cannot be satisfied by Runtime alone.

Design publication is the response to the critical request. Protected review and subsequent implementation/native qualification remain pending; no silence, prior-head review or this source diagnosis counts as approval.
