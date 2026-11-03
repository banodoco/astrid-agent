# AG-ENGINE-RELEASE-DESIGN

2026-11-03 UTC. **Proposed Agent producer contract; design only.**

Agent base: `b54117c9312165e2799554ec27480665d5cad2b1`.
Runtime context: `2776ba973b6c8f3555b5629b0840d9691a98d3c2`.
Lane: `lane/M-AG/AG-ENGINE-RELEASE-DESIGN-design`.

## Decision

Agent owns a release-only capability issued by the engine supervisor after
verified readiness. It is delegated to one selected host process, claimed once,
then associated once with the complete later `SessionBinding` and managed-session
generation. Release compares that retained association and the current engine
under the supervisor's `self.work` lock, which also excludes replacement and
cleanup. A capability never follows an endpoint or installation to a successor.

The proposed consumer object is `BoundEngineRelease`. Its only effectful method
is `release(request_id, expected_binding, expected_generation, deadline)`;
`observe(request_id, deadline)` reads the identical recorded request. Neither
method resolves a new owner or starts an engine. These are design names, **not
APIs present at the pinned tuple**. There is no implementation/producer SHA.

Runtime retains lease, execution-epoch and terminal authority. It may authenticate
host selection or relay the opaque capability, but cannot mint engine authority,
validate `self.engine` remotely, or decide unload success. No Runtime
register/claim endpoint is published at the context SHA. The earlier R4 request
for Runtime to provide the owner operation is superseded: Agent must produce it;
the remaining Runtime question is the adapter in section 4.

## 1. Verified baseline and audit integration

Source anchors below refer to the exact Agent base, not the design commit.

| Existing seam | Verified behavior and missing predicate |
|---|---|
| `engine_readiness.py:613`, `:684`, `:790`, `:829`, `:856` | Verifies installed receipt, live owner token, launch, process/interpreter and source/model facts; repeats file observations. Discards the client/channel and returns a private profile/locator. SDK bootstrap does not call it. A subsequent replacement is possible. |
| `engine_custody.py:194`, `:279`, `:328`, `:803` | Client reconnects by installation path; `release_owned` sends `{}`. Supervisor verifies its current owned HTTP peer and queue, then requests unload and collects VRAM. No readiness incarnation, binding or request ID is supplied. |
| `engine_custody.py:556`, `:767`, `:838`, `:844`, `:906` | `self.work` serializes service/start, release, stop and cleanup. `stop` sets an interruption flag before obtaining it. This is the correct authority boundary, but current serialization does not compare the original requested incarnation. |
| `engine_local_endpoint.py:155`, `:184` | Uses a temporary execution context for status and returns endpoint/output data; HTTP endpoint declares no authentication. It does not return release authority. |
| `host_bootstrap.py:1297`, `:1430`, `:1816`, `:1949`, `:2106`, `:2254` | Parked activation has a private inherited socket; ordinary spawn closes FDs; foreground executes the host CLI; resident attach verifies readiness/registration but transfers no engine capability. Worker credentials have Runtime scopes only. |
| `generic_host.py:11389`, `:11962`, `:16836` | Host has no readiness authority at construction. The engine binding is created later, after claim/profile/execution facts exist. |
| `managed_tool_session.py:78`, `:474`; `generic_host.py:6616` | Exact binding/generation and local quiescence checks exist; host setup/release serialization exists. They do not prove supervisor identity or transfer readiness authority. |
| `managed_tool_session.py:516`, `:527` | Existing release fences/increments G before calling the native adapter. A new capability cannot simply be added inside that callback: its original G has already changed, and a native busy is converted into a poisoned slot. Section 5 defines the checked-release reservation needed for this seam. |

Inputs: R4 `REPORT.md` and `RECEIPT.json`; approved custody intent in I-AG-03a
`DESIGN.md`; ORCHESTRATOR requests at 02:42:25Z, 02:47:55Z and 02:48:17Z;
STATUS peer lines read on this task. Runtime's `astrid_runtime/bootstrap.py:300`
and `:427` were inspected at the context SHA: `BootstrapResult` and worker handoff
contain no engine-release delegation.

Both completed independent audits are integrated: `sub/1/REPORT.md` supplies the
source/lifetime and four launch-mode analysis; `sub/2/REPORT.md` supplies consumer
identity, correlation and uncertainty predicates. The lead resolves two points:
Runtime is an adapter dependency, not the engine owner; and an attributable
`busy` requires validated identity, while lock contention before validation is
only a local pending/unresolved observation. A blocked lock cannot certify an
owner. Sub/1's conclusion that a Runtime register/claim API is mandatory is
narrowed: a separately reviewed Agent local transfer can also satisfy the seam.

R4's 42 passing unchanged-base tests are historical evidence only. Its real
non-root attempt failed before tests could run. Neither audit nor that run proves
this proposed protocol.

## 2. Four identities, retained separately

| Symbol | Meaning and source |
|---|---|
| **E: ready engine owner** | Private installation identity (canonical root, engine/platform, recipe/helper digests), supervisor token/incarnation and PID/birth, kernel boot identity, engine PID/birth, launch fingerprint and engine launch generation, endpoint, source/config and readiness/receipt digests. Observed through readiness and rechecked by the live supervisor. |
| **X: selected host** | Executor ID plus executor incarnation when attested, host PID/birth, kernel boot identity, per-boot host nonce, source/boot identity and selected registration. SDK/Runtime selection is verified before delegation; the receiving process proves its own identity. An executor name alone is insufficient. |
| **R: Runtime execution** | Actual Runtime daemon instance, realm/endpoint, authenticated registration and positive claim epoch, plus admitted execution identity. Host obtains these through its scoped existing Runtime client/claim, or a reviewed adapter attestation. |
| **B, G: later session** | Every field of `SessionBinding.identity_key`, with explicit absent-value encoding, and the positive managed-session generation G. Retained immutably at association. |

**Do not conflate the two uses of `runtime_instance_id`.** Readiness currently
sets profile `runtime.runtime_instance_id` to the UUID of the supervisor launch
token (`engine_readiness.py:724`). The VibeComfy binding copies it; its
`process_birth_id` is the session daemon/supervisor birth. SDK startup separately
verifies the actual Runtime daemon instance; claim epoch is from that Runtime.
Keep E's legacy field, R and X as distinct typed sidecar records. Do not assert
that their instance IDs or process births are equal, or reinterpret a frozen
binding field to make them equal.

B comprises `session_id`, `runtime_instance_id`, `process_birth_id`, `endpoint`,
`source_digest`, `config_digest`, `execution_identity`, `runtime_epoch`,
`launch_generation`, `engine_birth_id`. A canonical digest is only shorthand for
retained canonical bytes, never a substitute for verifying their provenance.
Associate session directory, daemon/engine births, endpoint and config to E;
associate execution identity/residency digest and epoch to authenticated R and
host admission. Include the effective source content digest separately: the
current host fills B's source field from a revision, while attached hook
verification expects content evidence.

Two compatibility gaps are explicit implementation gates. Current readiness
does not emit `launch_generation`, which the host requires. Current
`ComfyHostHooks._binding_mismatch` refuses non-null `runtime_epoch` on attached
sessions (`test_comfy_host_hooks.py:90`). The new association must supply verified
engine generation and R evidence without deleting those checks or inserting
fixture defaults. For the existing attached convention, B's launch generation
equals the verified engine birth; an independent supervisor counter, if used,
belongs in E. B/G can change only by a fresh association/capability.

## 3. Capability origin and sequence

Proposed internal namespace: **`astrid.engine-release.v1`**, separate from the
existing v2 engine service envelope and Runtime activation/preparation schemas.
Existing `{}` release is never a fallback for this operation.

Smallest proposed Agent integration: a new
`prepare_ready_engine_release(..., deadline)` returns the existing profile plus
a private `ReleaseOffer`, leaving the profile schema unchanged; bootstrap
delivers that offer to verified X/R; host `claim(...).bind(B, G, R)` returns
`BoundEngineRelease`. Only its `release(...)` and read-only `observe(...)` reach
the consumer adapter. Issue/delegate/claim/bind are private owner protocol
stages, not new Runtime client methods or child-bridge operations.

1. **Verify, then issue.** Readiness finishes all existing receipt/source/model,
   live-owner, hardware and private-publication checks. Its retained private
   supervisor connection requests `issue` with exact E and the readiness digest.
   Under `self.work`, the supervisor compares its own token/birth, live owned
   engine/connected peer, immutable launch/source/config facts and current
   receipt association. A mismatch yields no capability. This final compare
   closes replacement between the last readiness observation and issuance.
   Issuance does not create/start an owner. A profile alone cannot invoke it.
2. **Mint at the owner.** The supervisor creates a random 256-bit secret and
   opaque issuance ID; its private in-memory record fixes E, readiness digest,
   `release` permission, expiry and revocation state. The legacy launch-token
   UUID in the profile/B is never accepted as release proof and conveys no new
   general supervisor controls. The producer holds an
   attenuated one-time delegation ticket. Public profiles/stdout/arguments,
   logs, diagnostic receipts and child environments contain no bearer secret.
3. **Select, delegate, claim.** After SDK startup has verified X and R, the
   readiness producer asks the same supervisor to target the ticket to X/R.
   A private handoff delivers it only to that X. The host claims directly at the
   issuer, proving its PID/birth/boot nonce and possession of the ticket; the
   supervisor consumes the ticket and returns a host-bound release handle.
   A target cannot be changed after delegation. Duplicate identical claim can
   read its existing receipt; another process or changed payload is refused.
4. **Bind later.** While host admission is excluded, X verifies the claimed
   execution and constructs B/G. `bind` at the same supervisor checks E is still
   current, X/R match the claim, and the complete B matches the retained E and
   verified R association. The Agent host is trusted to attest its local G and
   authenticated Runtime claim; the supervisor does not invent Runtime proof
   from caller strings. It stores one immutable B/G and returns the
   `BoundEngineRelease` association receipt. An admission-only initial session
   may exist during this step, but native invocation and releasable advertising
   wait for the ACK. Failure fences that slot and retains cleanup custody.
5. **Release or observe.** Consumer retains this handle beside that managed
   session. It supplies exactly B/G and one frozen request; supervisor executes
   section 5. Reconciliation reads only that same owner's request record.

Lifetime proposals for review: unclaimed delegation expires within 60 seconds;
claimed capability expires no later than 24 hours after issuance and cannot be
silently renewed. An expired capability loses mutation permission but does not
erase cleanup obligations. Long-lived sessions require deliberate new readiness
and association while admission is fenced; they never auto-refresh at teardown.
Only one active X/B/G association may control release of E. Concurrent consumers
must share that admission owner or be refused, not mint competing release grants.

Revocation is serialized with mutation: host shutdown/replacement, engine
replacement, session fence/generation change, Runtime instance/epoch change,
expiry or explicit owner revocation prevents new effects. Host generation
changes retire the association locally before admitting a successor and notify
the supervisor; the supervisor also compares all retained fields on every call.
Process/Runtime evidence that is unobservable fails closed. Supervisor restart
loses its secret records, changes issuer incarnation and refuses old handles.
It never imports authority from a disk locator. Old uncertain custody survives
in the host's private cleanup record even when the issuer cannot recover it.

R is an authenticated binding label, not a new Runtime lease authority. The
adapter must order observed epoch/executor invalidation before reassignment of
E to a successor X/B; the Agent association fence blocks that reassignment
until the prior obligation is retired. Instantaneous revocation at a remote
Runtime epoch change cannot be inferred from the local lock or a cached health
response. If consumers require that stronger property, the Runtime adapter must
supply a transition fence before that mode can be supported. The guarantee here
is that an old R/X/B release can never unload a successor session.

## 4. Private transfer and launch-mode boundary

The proposed local Linux transfer is a **new Agent-owned `ReleaseHandoff`
side-channel**, independent of the frozen activation/child bridge. The minimum
adapter operations are deliver-once to X, claim receipt, and revoke. Transport
must be bounded, owner-only, symlink-safe, and mutually verify real peer
UID/PID/birth and boot identity. Use kernel peer credentials and a pinned live
process handle (pidfd where available), with real birth checks at effect
boundaries; numeric PID comparisons alone do not survive reuse. The delegation
secret plus issuer-side target
record provides operation authority; same UID or a plausible socket path alone
does not. Pin the issuer incarnation on connection and validate its challenge
against the retained ticket. Every reconnect must prove that same issuer and
capability; it cannot use `start=True` or rediscover an engine.

| SDK mode | Proposed transfer and guarantee gate |
|---|---|
| Ordinary detached spawn | New dedicated socketpair/FD explicitly passed despite `close_fds=True`; child identifies itself, SDK verifies registration X/R, then sends the targeted ticket. Parent closes its copy on every launch outcome. Never pass a secret in argv/environment. |
| Runtime-supervised foreground | Foreground Agent bootstrap retains the same private offer object/channel into the CLI. Runtime restart yields a new X and cannot inherit an old grant from discovery. Adapter must supply verified active-host selection. |
| Already resident host attachment | New Agent host rendezvous in its private support directory, with exact process/nonce/ready identity verification and a fresh challenge. Deliver the targeted ticket to that live X and await issuer claim receipt before declaring release support. An older resident host without this receiver is explicitly unsupported; matching installation/healthy ready file is insufficient. Do not kill/restart it just to fake successful transfer. |
| Parked Runtime activation | Use a separate dedicated Agent descriptor, preserving existing activation grant shape and one-shot ACK. Activation ACK proves activation only. A Runtime launch adapter may need to preserve/pass this descriptor and attest selected X/R. Current `host-control-fd` also is not engine authority. |

These are **intended supported Linux local modes after implementation and
proof**, not capabilities on the pinned tuple. Currently none supports this
end-to-end release contract. Remote/RunPod/endpoint-only attachment, unregistered
external Comfy owners, macOS and Windows engine custody are outside v1 (the
existing engine custody client is Linux-only). Unsupported transfer refuses
release support and retains the original custodian; ordinary host readiness must
not be reported as proof of release readiness.

**Runtime decision requested:** confirm whether active X/R can be attested and
these side-channel transports supported through existing adapter callbacks,
or identify the smallest separately scoped adapter addition. No endpoint name
or Runtime implementation is assumed. If new Runtime/bootstrap wire fields or
activation semantics are necessary, they require a Runtime-owned proposal and
protected review. Work cannot claim resident/parked support until that decision
and real transfer proof exist. A versioned opaque relay is an alternative to
the direct Agent channel, with identical correlation, target and lifetime rules.

## 5. Atomic release and lock order

Host order is `_session_task_lock` -> `ManagedToolSession._lock` -> outbound
supervisor call. Use deadline-aware acquisition; no callback from the supervisor
may acquire either host lock. The supervisor has its own `self.work` lock;
no reverse RPC is allowed while it is held. A response/result-table lock, if
needed for bounded observation, never waits for `self.work` while held by code
that `self.work` calls. This prevents a timeout/reconciliation deadlock.

With both host gates held, validate B/G, stop idle timers and exclude new
admissions. Non-quiescent local work may return a local pending indication
without sending unload; it must not invent an authenticated supervisor `busy`.
The host remains a trusted admission authority for this one engine/session.
If either host lock cannot be acquired by D, return a local deferred/unresolved
receipt for the requested snapshot, with `effect_sent=false`. No release
reservation exists yet: do not fence or cancel the running admission, and do
not later apply this request to whatever slot becomes available. A malformed
or stale request similarly must not poison an unrelated current slot.

For a capability-backed slot, use an internal **checked-release reservation**:
`begin_release(B, G)` freezes a `ReleaseIntent` with association ID, original
B/G, request ID and D and closes admission without changing G or invoking the
current fence callback. This is a proposed narrow ManagedToolSession seam,
not its existing `release` behavior. While locks remain held, send the frozen
request. `busy` cancels the reservation and preserves G. `completed` retires
the reserved slot and advances the local fence exactly once, consuming cached
ACK evidence with no second native release. `failed`/`unresolved` advances the
local fence to Gf and retains the original association/ReleaseIntent in the
poisoned slot. New admissions remain blocked. Generation-changing external
fences still revoke mutation authority; they never create a new release intent.

Do not call today's `manager.release()` and then build the capability request
from its now-incremented generation. Late readback can retire only the retained
association whose original B/G and poisoned local Gf match that ReleaseIntent;
it cannot clear another slot or reset the generation counter. Local retirement
failure after a completed producer result retains a local cleanup latch and
the cached result; it never repeats unload.

The supervisor acquires `self.work` within the remaining operation budget, then
performs **all** of the following in that critical section:

1. Authenticate caller X and capability; compare E, R, B/G, lifetime/revocation,
   engine object/PID/birth/generation and owned listener/connected HTTP peer.
   Validate request digest/idempotence before any native effect.
2. Mark the association releasing; reject competing mutation/admission owners.
   Check stop/revocation intent, host quiescence and the exact engine's native
   running/pending queue. An attributable busy is valid only after identity
   validation and with no unload request sent.
3. Dispatch `/api/free` at most once through the already verified original
   engine connection, with the fixed remaining deadline. Recheck identity at
   effect boundaries and while collecting observations. Do not fall back to a
   bare endpoint if peer proof fails.
4. Freeze the immutable result and native facts in the request record before
   releasing `self.work`. The record is available even if response delivery
   fails. For uncertain native I/O, latch supervisor uncertainty before unlock;
   deny further effects/replacement until the old effect is resolved or the
   original process scope is positively contained by explicit owner recovery.

Start, replacement, legacy release, signal cleanup and stop must use the same
fence and revoke the old association before changing `self.engine`. Stop may
set interruption intent outside `self.work` as today, but only work under that
lock changes owner state. If stop wins before unload, no unload is sent; if
unload was sent, preserve its actual/uncertain outcome, never relabel it a
definite no-effect failure. A stop cannot swap in a successor during the call.
Legacy `{}` release must not bypass an enrolled association's fence.

A held `self.work` alone does not freeze arbitrary HTTP clients. The guarantee
requires all admitted work for E to pass the one host admission gate, with that
gate held through the unload. Queue-empty observation alone cannot exclude a
direct unauthenticated `/prompt` arriving afterward. Shared/untracked HTTP
writers are unsupported for v1: if exclusive admission cannot be established,
refuse association/release support. Supporting them later requires an engine
ingress fence or producer-enforced admission protocol and a separate review;
it cannot be claimed from a queue test.

## 6. Bounded request, ACK and outcomes

Release budget is finite, positive, at most 10 seconds (default 5 seconds).
Use one caller deadline D in monotonic nanoseconds plus kernel boot identity,
valid only for this local-machine v1. Lock wait, adapter wait, queue/stats,
unload and reply all spend that same budget. No `_CLEANUP_SECONDS` response
extension applies to this new operation. A relay cannot restart the budget.
No native effect may begin after D or capability expiry. Cleanup can outlive D
only with its custody/fence retained; it does not extend caller waiting.

The frozen canonical request contains version, random 128-bit `request_id`,
issuance/association IDs, E/X/R identities or retained-record digests, complete
B digest and G, operation=`release`, fixed D/boot ID and capability proof.
Store the full canonical request privately, excluding bearer bytes from public
receipts. ACK carries the same version/IDs, E/X/R and B/G digests, D/boot ID,
request digest, status, bounded error code, `effect_sent`, and typed native
facts. The authenticated channel/issuer and every echoed value must match.
An unknown/malformed/foreign ACK is not an ACK for this request.

| Status | Producer evidence and consumer disposition |
|---|---|
| `busy` | Exact association validated; native queue or validated admissions active; no unload sent. Retain capability/custody; reopen admission only if still current. A later distinct request requires fresh validation after quiescence. |
| `completed` | Positively acknowledged `models_unloaded=true` from the exact live peer, identity unchanged through result freeze. This releases the bound model/session slot, **not the engine process**. Retire capability mutation rights; host clears slot only after a matching on-time ACK or explicit same-request reconciliation. |
| `failed` | Definitive refusal/no effect (stale, revoked, expired, unauthorized, invalid association), or an explicit attributable native negative reply. Include `effect_sent` and any observations. Preserve custody; a native negative result poisons/fences the slot. This status never proves engine death or memory return. Unauthenticated invalid input gets a bounded protocol refusal, not a claim about some other capability. |
| `unresolved` | Owner/process/epoch visibility, deadline, effect completion or result delivery uncertain. Keep request, capability reference, binding and cleanup record; a reserved release retains its poisoned slot/admission fence. A pre-reservation deferred call leaves existing active work untouched. Do not target a replacement. |

Native facts distinguish `unload_ack` = true/false/unknown, queue observation,
engine-alive observation, and VRAM total/free before/after in bytes, with sample
time/device and availability/error. Validate finite integer ranges and
consistent devices/totals. Absent/malformed VRAM is explicitly unavailable;
if unload is positively acknowledged it does not turn into failure just because
stats were unavailable. Unknown/malformed unload ACK is unresolved. Increased
free VRAM is an observation, not a success predicate or proof of all allocations
returned. `memory_observed_returned` cannot be inferred from unload alone.

Consumer stops waiting at D regardless of producer progress. A matching result
must arrive and validate by D to count as the initial call's definitive ACK.
There is no promise that an ACK can always be delivered over a broken channel.
If D expires, record consumer `unresolved` even if producer stored completed.
A late result may be retained as evidence but cannot silently clear that latch.
A caller waiting on a stalled lock/worker returns unresolved by D; it does not
release a native-operation fence still held by that worker.

Idempotence is record-first: register/freeze the request before unload. Identical
duplicate delivery returns the same stored outcome (or unresolved while it is
in progress), never performs unload again. Changed bytes under the same request
ID are rejected. A recorded busy remains busy for that ID. Retain results until
capability expiry plus a bounded 24-hour observation window; never evict a
record in a way that makes an old ID eligible for execution. After that window,
unknown IDs are refused, not re-executed. Issuer loss means unresolved, not an
empty successful result. Expiry/revocation removes mutation rights; authenticated
read-only observation of already recorded requests remains allowed during the
retention window.

## 7. Uncertainty and custody state machine

| From | Event | To / obligation |
|---|---|---|
| verified | issuance and target claim ACKs | claimed; keep E/X/R and original issuer |
| claimed | immutable B/G association ACK | bound; allow matching managed admission |
| bound | validated release, quiescent | releasing; close admission |
| releasing | authenticated busy, no effect | bound; retain custody and revalidate before admission/retry |
| releasing | completed ACK | released; retain receipt; supervisor still owns live engine |
| any live state | definitive stale/revoked/failed | fenced; retain old cleanup evidence, no authority over successor |
| any live state | timeout, lost claim/bind/release ACK, channel/owner/epoch uncertainty | unresolved; retain original handles/records and block new effects |
| unresolved | explicit `observe` of identical request at identical issuer, with complete matching final record | reconciled original result; check the retained ReleaseIntent, original B/G and current poisoned Gf before retiring only the old slot |
| unresolved | issuer lost/replaced or record unavailable | remain unresolved; explicit owner-level containment/recovery required |

Claim/bind uncertainty is not permission to issue a second grant to a different
host. Release uncertainty permits read-only `observe` with a new wait deadline,
but the frozen release D/identity never changes. It does not permit a new
effectful request, endpoint lookup, profile reload, fresh client or replacement
capability during teardown. If the original process disappears, retain positive
incarnation-bound containment evidence separately; disappearance or a later
healthy engine does not prove the unload completed. Recovery must identify and
retire the prior obligation before any new association and must preserve exact
prior state when a proposed recovery cannot be completed.

GenericPackHost keeps the private record across cleanup attempts; if it exits,
its cleanup receipt must retain enough non-secret identity/request evidence for
operator recovery. Recreating a host cannot resume old authority from that
receipt. Idle timers, explicit release, task teardown and host shutdown all
route through the same stored handle and outcome rules. Runtime task terminal
publication follows I-AG-03a's existing sole-owner/frozen-request rules; an
unload result creates no second lease, terminal mutation or stop proof.

## 8. Focused test contract (specified, not run)

New node names below are **proposed**, not existing coverage. Parameterize fault
sets so owner, host, Runtime, B/G, quiescence, transport and deadline predicates
are evaluated together, including replacement after readiness and after bind.

| Proposed exact node | Required assertion |
|---|---|
| `tests/core/generation/test_engine_release_authority.py::test_issue_requires_final_ready_owner_compare` | Full readiness succeeds; replacement before issue yields no grant; profile/endpoint/token string alone cannot issue. |
| `tests/core/generation/test_engine_release_authority.py::test_claim_is_targeted_single_use_and_expiring` | Wrong host birth/nonce, epoch, replay with changed fields, expired/revoked grant cannot claim or unload; identical claim readback is safe. |
| `tests/core/generation/test_engine_release_authority.py::test_bind_preserves_engine_and_runtime_namespaces` | Different legitimate engine-owner/Runtime UUIDs work; conflation, absent generation/content proof, wrong execution identity or unverified epoch fail. |
| `tests/core/generation/test_engine_release_authority.py::test_release_compares_all_identity_fields_before_effect` | Individually mutate E/X/R and every B field/G; same root, PID reuse, endpoint reuse and changed source each cause zero native effects. |
| `tests/core/generation/test_engine_release_authority.py::test_replacement_and_cleanup_share_release_fence` | Barrier before effect and during unload; release races start/stop/signal/final cleanup; one order, zero successor unloads, revoked handles remain unusable. |
| `tests/core/generation/test_engine_release_authority.py::test_busy_requires_valid_owner_and_quiescent_gate` | Native running/pending and host in-flight cases; lock contention cannot forge busy; no admission between quiescence and unload; untracked writers refuse support. |
| `tests/core/generation/test_engine_release_authority.py::test_duplicate_request_never_repeats_native_effect` | Lost response + identical duplicate/observe cause one unload; changed request/deadline rejected; busy replay stays busy; aged/missing record never re-executes. |
| `tests/core/generation/test_engine_release_authority.py::test_ack_is_attributable_and_uses_one_deadline` | Foreign/late/malformed ACKs, slow locks/stats/unload and relays; no grace budget reset, finite bounded return, unresolved custody retained. |
| `tests/core/generation/test_engine_release_authority.py::test_native_ack_and_vram_facts_are_independent` | True/false/unknown unload ACK; missing/malformed/unchanged VRAM; completed requires positive unload ACK, no invented memory-return claim. |
| `tests/sdk/test_engine_release_handoff.py::test_transfer_targets_selected_host_in_all_local_modes` | Parameterize detached, foreground, resident, parked; private descriptor/receiver, selected active X/R, parent/child closure on success and partial launch failure. |
| `tests/sdk/test_engine_release_handoff.py::test_resident_and_parked_channels_do_not_widen_authority` | Old resident, wrong peer UID/birth, reused ready file, vanished host, replacement before claim, old activation ACK and worker credential all fail closed. |
| `tests/core/execution/test_comfy_session_f07.py::test_bound_release_uncertainty_retains_exact_slot` | Idle/explicit/teardown share handle; timeout/failure fences slot and preserves cap/B/G/request; late ACK cannot clear a newer generation. |
| `tests/core/execution/test_managed_tool_session.py::test_checked_release_reserves_prefence_generation_once` | Original G reaches the supervisor; busy preserves it, completed retires once without second unload, unresolved retains original G plus poisoned Gf, and stale readback cannot retire a successor. |
| `tests/core/execution/test_comfy_session_f07.py::test_same_owner_observation_reconciles_without_retargeting` | Completed readback resolves only original request; unknown/replaced issuer retains latch; zero new engine/control lookups and zero second terminal mutations. |
| `tests/core/generation/test_engine_release_native_linux.py::test_nonroot_ready_replace_release_and_reconcile` | Real ordinary UID, real kernel `/proc`/peer credentials/birth, private Unix channels, real supervisor/engine processes; replacement between readiness and release and ACK loss cannot touch successor. |

Existing regression anchors to run in the implementation lane:

```text
tests/core/generation/test_engine_readiness.py::test_installed_engine_produces_private_profile_consumed_by_existing_validators
tests/core/generation/test_engine_readiness.py::test_refusals_fail_closed_without_profile_or_engine_change
tests/core/generation/test_engine_custody.py::test_release_runs_in_live_supervisor_against_owned_child
tests/core/generation/test_engine_custody.py::test_stale_registration_foreign_listener_receives_no_release
tests/core/generation/test_engine_custody.py::test_stop_racing_release_cancels_before_replacement
tests/core/generation/test_engine_custody.py::test_original_native_owner_loss_refuses_replacement
tests/core/generation/test_engine_custody.py::test_connected_peer_must_be_owned_even_if_listener_check_passes
tests/core/generation/test_engine_custody.py::test_live_supervisor_busy_and_failed_free_are_not_released
tests/sdk/test_host_bootstrap_source_identity.py::test_bootstrap_passes_inventory_identity_and_restarts_on_change
tests/sdk/test_host_bootstrap_source_identity.py::test_bootstrap_refuses_worker_credential_outside_the_pack_host_contract
tests/core/execution/test_managed_tool_session.py
tests/core/execution/test_comfy_session_f07.py::test_owner_release_refuses_stale_authorization_without_native_control
tests/core/execution/test_comfy_session_f07.py::test_release_waits_for_exact_admission_then_preserves_native_evidence
tests/core/execution/test_comfy_session_f07.py::test_host_serializes_comfy_setup_and_release_cannot_race_it
tests/core/execution/test_comfy_session_f07.py::test_failed_release_retains_poisoned_comfy_slot
tests/core/execution/test_comfy_session_f07.py::test_cancelled_session_wait_never_fences_the_running_comfy_task
tests/core/generation/backends/test_comfy_host_hooks.py
```

P19 requires real non-root Linux coverage, with UID/GID, kernel/platform, boot
and namespace identity, exact Agent/Runtime heads, process births and immutable
raw event/ACK captures in the future receipt. No FakePath or mocked `/proc` for
that proof. An unprivileged harness may use a tiny actual HTTP engine to prove
fencing and unload call count; real supported Comfy/GPU evidence is additionally
required to claim native unload/VRAM behavior. Root or fixture runs alone do not
close either claim. R4's failed `runuser` attempt is a venue limitation, not a
waiver; use a venue with a real product UID. No tests are run by this task.

## 9. Decisions still requiring owners

1. **M-AG2 consumer:** confirm acceptance of the immutable one-session handle,
   four outcomes, poisoned-slot retention and explicit same-request readback,
   including the truthful VRAM rule and launch-mode refusal behavior. Identify
   the consumer adapter seam; the design SHA cannot be consumed as product code.
2. **M-RT adapter:** decide how active selected X/R is attested and the separate
   private transfer is carried for foreground/parked/resident paths; if existing
   callbacks cannot do it, name a scoped Runtime adapter proposal and future
   immutable SHA. No Runtime owner-incarnation API is assumed.
3. **Protected review:** the new Agent supervisor wire operation, host custody
   ordering and readiness/binding compatibility seam need independent M-AG2
   review for an Agent implementation. Any Runtime activation/bootstrap/schema,
   frozen bridge/generated contract or workflow change requires its owning
   protected review (M-RT and, for workflow/harness changes, M-CI; checker profile
   M-QUAL). This packet authorizes none of those changes. Preserve I-AG-03a's
   fail-closed custody and sole Runtime terminal authority; do not import the
   deferred stop-proof lane or treat models unloaded as stop evidence.

NEEDS: AG-ENGINE-RELEASE-DESIGN design review
