# Independent GenericPackHost consumer-contract audit

**Scope:** Read-only audit of `queue/done/I-AG-HOST-RELEASE-R4/REPORT.md`, the latest relevant M-AG2/M-RT requests in `ORCHESTRATOR.md` (2026-11-03 02:42:25Z, 02:47:55Z, 02:48:17Z), and the existing Agent custody/session release paths. This is a minimum consumer contract, not an implementation claim or wire/API publication. No source or tests were changed or run.

## Finding

The consumer needs one narrow, authenticated authority to request release of the exact ready-verified engine/session instance it was given, plus a reply that can be tied to that authority and request. GenericPackHost must not look up a fresh endpoint, profile, process, Runtime client, or owner during teardown and treat that as the original authority. R4 establishes that current readiness discards its verified client/channel, the existing supervisor release sends `{}`, and the published Runtime tuple has no owner-incarnation transfer operation. The current path therefore cannot satisfy this contract.

M-RT's latest clarification places engine ownership and replacement/cleanup synchronization in the Agent supervisor (`self.engine` / `self.work`). The release and its fencing decision are Agent-owned. Runtime may provide an authenticated adapter/relay only after this authority and its semantics exist; published Runtime `2776ba973b6c8f3555b5629b0840d9691a98d3c2` supplies no such transfer API. Its existing worker credential is not a delegated engine-release capability. Do not infer an endpoint or operation name from this audit.

## Minimum capability identity

Issue an opaque, unforgeable, narrowly scoped release capability only after readiness has authenticated and verified the owner. The capability must bind (directly or through an authenticated immutable record) all of:

- capability/version and unique issuance identity, issuer/owner identity, allowed operation (`release` only), expiry/lifetime, and revocation state;
- the verified engine owner/incarnation: Runtime instance and epoch, engine process PID plus birth identity, engine launch generation, and the owner realm/root and selected endpoint identity needed to distinguish a replacement;
- the selected GenericPackHost executor incarnation (host/supervisor process birth identity), so a capability cannot be claimed by a later host merely because it uses the same installation;
- the complete later `SessionBinding` and its managed-session generation: session ID, Runtime instance/epoch, execution identity, process birth, engine birth, endpoint, launch generation, source/config identity, plus the host's session generation/fence.

The exact representation is an implementation choice. A profile, endpoint string, PID alone, reconstructed client, or caller-provided binding is not authority. GenericPackHost may associate the capability with the later binding only after the execution identity and readiness incarnation have been verified. Capability transfer must authenticate both sides and be scoped to the selected active executor. Claim is single-use or idempotently bound to the same executor/binding; replacement, mismatch, expiry, or revocation fails closed.

## Fencing and serialization

The supervisor must validate capability, request, full binding, executor incarnation, current engine birth/generation, and current managed-session generation while holding the same serialization boundary that excludes engine replacement and cleanup. On the present architecture this means the Agent supervisor's work/replacement lock (and the session manager's generation/binding check), not a Runtime-side observation followed by an unlocked Agent operation.

Within that protected decision, the operation must establish that the authorized owner is still the current live engine, confirm the session is quiescent, perform the unload/release action, collect native release facts, and freeze its result before releasing the fence. A stale/replaced owner, active work, failed identity check, or unresolved process state must not cause mutation of a different engine. Current `ManagedToolSession.release(expected_binding, expected_generation, require_quiescent=True)` already illustrates the binding/generation fence at the session layer, but does not itself transfer readiness authority or fence the supervisor's engine incarnation. Existing supervisor `release` serializes work and stop, but accepts an empty payload and does not compare the requested verified owner/binding.

## Correlation and bounded ACK

Every call carries a fresh unpredictable `request_id`, capability/issuance ID, protocol version, the complete binding (or canonical digest backed by authenticated retained binding), and a caller deadline/budget. The ACK echoes the request ID, issuance ID, binding identity/digest, executor and engine incarnation identities, and the same deadline context. The consumer accepts an ACK only if every echoed value matches its outstanding request. Duplicate delivery of the same request must return the same recorded outcome; reuse of an ID with changed fields is a protocol error.

The supervisor must stop waiting at the supplied deadline and return a definitive state only when it has one. Required states:

- **`busy`** — the exact owner was validated but the session has active work / cannot yet be released; no unload was performed. Custody remains. A later attempt is allowed only after revalidating the same capability and binding; it must never retarget a replacement.
- **`completed`** — the exact bound session was released and unload was positively acknowledged. Include explicit native unload facts and before/after VRAM observations when available. VRAM change is an observation, not proof that every allocation was returned or a success predicate by itself.
- **`failed`** — a definitive failure for the exact operation/owner, with bounded error code and any collected unload/VRAM facts. It does not mean the engine stopped or that custody may be forgotten.
- **`unresolved`** — deadline, channel, process, identity, or ACK uncertainty prevented a definitive result. This is not success and not permission to retry against a newly resolved owner.

An ACK must be returned within the caller's deadline; neither the adapter nor the consumer should extend the authority's lifetime while waiting. If the deadline expires before an attributable ACK reaches GenericPackHost, the consumer records unresolved regardless of whether the producer may have completed internally.

## Uncertainty and custody rule

A lost, malformed, late, mismatched, or timed-out ACK leaves outcome uncertain. GenericPackHost retains the exact capability reference, binding, request ID, and cleanup/custody record; marks release unresolved; and does not clear ownership, claim release, or send a new operation to a re-resolved owner. Resolution may query/reconcile only the same retained supervisor and exact request identity through an authenticated mechanism. If that identity is unavailable or has been replaced, keep uncertainty latched and require explicit owner-level recovery; never infer success from process disappearance, endpoint failure, or a later healthy engine. Definitive `busy`/`failed` likewise retain custody and do not authorize a different incarnation.

Revocation, host shutdown, or replacement must invalidate outstanding capabilities at the Agent authority. A relay failure must preserve the same uncertainty rule. Runtime adapter duties are transport authentication, private delivery to the selected executor (including resident-host attachment), bounded delivery/response, and faithful correlation; Runtime must not mint or widen the Agent capability, select a replacement owner, or decide release success.

## Launch and attachment boundary

The same Agent contract applies to each SDK startup path: ordinary spawned host, Runtime-supervised foreground host, and attachment to an already resident host. Each must establish a private authenticated transfer/claim to the selected executor incarnation before later associating the session binding. A resident attach must prove that the attached process is the selected active host; it cannot merely connect to a matching installation or endpoint. In the absence of such a transfer/claim adapter, none of these modes may claim this release guarantee. The existing Runtime worker credential and current readiness profile are insufficient.

## Focused consumer/producer test predicates

These are proposed predicates for the implementation lane; they are not evidence that the current base passes them.

1. **Readiness issuance:** valid verified readiness yields one release-only capability bound to observed owner/engine incarnation; invalid health, owner, token, birth, or identity mismatch yields no capability.
2. **Transfer and claim:** ordinary spawn, supervised foreground, and resident attachment deliver authority only to the selected active executor; wrong host, wrong Runtime epoch, replayed claim, expired/revoked token, or changed binding is rejected without mutation.
3. **Full identity fence:** same installation/endpoint with a replacement PID birth, engine birth, launch generation, Runtime epoch, execution identity, session binding, or managed-session generation is rejected; the replacement remains untouched.
4. **Atomic replacement race:** race release against replacement and stop/cleanup. Exactly one serialized owner outcome occurs; a stale release never unloads the successor; no gap exists between identity check and unload decision.
5. **Quiescence:** active/in-flight session returns `busy` before unload; after quiescence, only the same still-current binding can be released. A competing new admission cannot enter between the check and release.
6. **Request correlation/idempotence:** ACKs echo all correlation identities; delayed/foreign/malformed ACKs are rejected; duplicate identical requests return the stored result; same request ID with altered capability/binding/deadline is rejected.
7. **Outcome facts:** successful unload returns `completed` plus unload result and typed VRAM before/after facts; queue busy returns `busy` with no unload; explicit unload failure returns `failed`; malformed stats or uncertain unload cannot be reported completed.
8. **Deadline and uncertainty:** deadline expires before/during unload or ACK delivery loss yields consumer `unresolved`; capability/binding/request custody is retained; no retry targets a newly observed owner; same-owner reconciliation is limited to the identical request.
9. **Revocation and teardown:** revoke/replacement/shutdown prevents later claim or mutation. No profile, endpoint, or newly constructed client can recreate authority during teardown.
10. **Non-root and platform behavior:** run the focused supervisor/capability tests as a real non-root Linux user where applicable, and cover descriptor/channel ownership and process birth checks on supported platforms. Root-only socket/process tests are not sufficient evidence for the privilege boundary.

Useful existing test anchors are the readiness owner/identity tests, `engine_custody` supervisor release/stop serialization tests, `ManagedToolSession` stale binding/generation and quiescence tests, and GenericPackHost release routing/pending tests cited by R4. New contract coverage should live beside those suites and specifically add transfer, owner replacement between readiness and release, ACK correlation/deadline, and uncertainty custody predicates; no existing node currently proves those properties.

## Boundary and disposition

**Agent-owned:** capability issuance/validation/revocation; exact incarnation and session association; supervisor lock/fencing; idempotent request record; ACK state and unload/VRAM evidence; custody retention on uncertainty.

**Runtime/adapter dependency:** authenticated private delivery/claim to the selected executor across supported launch and resident-attachment modes, if the existing launch channel cannot carry the capability. There is no published Runtime API for this on the inspected tuple, and this report proposes no endpoint. Agent protocol work can specify the authority independently, but end-to-end consumer support remains unavailable until the transfer/claim mechanism is identified and reviewed.

**Current conclusion:** contract is sufficiently bounded for a consumer design, but not implemented or published on the cited tuple. R4's unchanged-base lane is not a producer SHA. This audit does not provide a producer SHA, implementation status, or HANDOFF.
