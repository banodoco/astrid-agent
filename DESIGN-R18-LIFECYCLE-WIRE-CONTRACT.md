# R18 lifecycle transfer: private evidence wire contract v2

**PROPOSED INTERFACE REFINEMENT; S / DESIGN ONLY. M-RT and M-AG protected review required before implementation of authority.**

Base: Agent `a98cc6bf0faa8b003f5e1054a9399836348b6b31`, `DESIGN-R18-LIFECYCLE-TRANSFER-DESIGN.md` §§3–6, 9–11 (D). This answers M-RT's `ORCHESTRATOR.md` request at `2026-11-03T04:38:24Z`. D's predicates, ownership, states, ordering and refusal rules remain requirements. All field spellings, encoding, challenge APIs, object-store bounds and verifier contracts below are **new joint proposals**, not already accepted or available native APIs. This document specifies no product change, pin update, public HTTP/bridge/generated contract, native qualification or approval.

**DRAFT AMENDMENT; COMPLETE M-RT REVIEW INPUTS UNAVAILABLE.** Parent wire packet `3dca9e9b7e863b9606d9e5c329b7d25912473d68` addressed the F-1 points relayed in M-RT's 05:18:44Z request: §3.1 closes result/code/fault/FieldCheck combinations; §§5–7 require Runtime to reject every mismatch before issuing or consuming VerifiedEvidence. This amendment addresses M-AG2 findings 1–3: closed FactReply/Seal transport and Runtime ledger binding, replacement-safe mutation requirements, and release-pending on a launch racing terminal gate removal. Adding frame kinds requires proposed v2 under §2; v1 is not wire-compatible and there is no version downgrade or ignore-unknown fallback. The available M-AG review accepts only the earlier `c97517414c2c7be7876f2bac8a6ea623f3f6ae55` interface as fail-closed design. The complete M-RT review/receipt remain missing; exact paths/inspected ref are recorded in the task REPORT/receipt. Full-review assessment and original F-1 closure remain unconfirmed. D's P1–P8, native checks and every unsupported/unknown refusal remain requirements. Protected delta review of this head remains required; no reviewer veto or native feasibility gate is discharged.

## 1. Evidence boundary and exact source findings

Runtime published reference R = `2776ba973b6c8f3555b5629b0840d9691a98d3c2`; Agent observation A = `9482dda551130fa1d03d6c114c6f8199572e1422`. R has no `astrid_runtime/lifecycle_transfer.py`: `RealmStore.__init__` (`runtime_protocol/store.py:788–855`) borrows only directory admission, not owner custody, and `_release_owner` (`:970–978`) explicitly unlocks. `_realm_owner_present` (`astrid_runtime/bootstrap.py:640–660`) releases its probe lock. None can implement D's retained handover. Read-only supplemental Runtime candidate `80fb33cf2ca1d136ff4a3a928de9c70160d8ef12:astrid_runtime/lifecycle_transfer.py:425–497` has `observe_pre_writer`/`transition_pre_writer`; the latter unconditionally refuses because installation authority, preimages and fresh native work/census verification are unavailable. Its ledger encoding and process-local objects are not this wire protocol. No candidate pin is advanced here.

A `astrid/installer/service.py:637–835` provides `_observe_systemd_transfer_definition`, `_snapshot_linux_r0` and `_observe_stopped_systemd_transfer_definition`; these collect read-only observations. Manager peer, effective credentials/action/environment, loaded/disk equivalence, historical containment and descendant completeness remain explicitly unknown. `_prepare_lifecycle_transfer` at `:808–813` unconditionally refuses. None of the seven authority adapters below exists as a usable producer there.

`/home/user/repo/docs/merge-plan/evidence/actions/41575312632/journey-receipt-direct-linux/before-cleanup/install.log:1408` records equal PID 4639 but unequal birth encodings (`proc-start-ticks:16769` versus that value plus boot/PID namespace), `bridge=missing_attribute`, and refusal. Prefix equality must not replace complete incarnation binding. `actions/41576793607/merge-suite-output-41576793607-1/` records `running=1`, `stopped=1` in `case-exits.txt`; running `evidence/probe.log` reports UID 1002, unknown manager/definition/census predicates, and fails `exact selected R0 binding not observed`. Running `result.json` is diagnostic-only, `authority=false`, `stop_exit=null`; stopped `venue.log` records preflight/cleanup with `user_created=0`. These artifacts establish neither stopped-origin historical containment nor suppression, acquisition, drain or transfer. Read-time input hashes and exact commands accompany the task receipt.

**Accepted invariants (D):** P1 authentic source/selection; P2 exact effective definition/principal; P3 complete original-owner and descendant absence with historical containment; P4 retained original kernel lock; P5 restart/launch fencing; P6 unchanged durable work safety; P7 authorized selection/publication lineage; P8 live authenticated session and supported rollback. No new schema, digest, typed placeholder, challenge response, successful manager command or empty current cgroup satisfies those by itself. Unknown is never absent. A stopped R0 rolls back stopped. Windows positive transfer and running-R0 restoration remain unavailable. Preserve the original lock; never unlock/reacquire, substitute a new lock, signal an unknown owner or manufacture an R0 stop receipt.

## 2. Closed types and canonical bytes

The notation below is normative schema notation, not executable Python. `{a:T,b:U}` lists **all** members, all mandatory; no additional members, omissions, defaults, coercion, duplicate JSON keys or implementation-specific extension maps. `|` is a tagged union when object alternatives are given; `T[]` is an ordered array. Every named type in §§2–5 is part of wire v2. Aliases do not add wrapper fields. An unsupported version/type/enum or invalid field is refused before mutation. Adding fields/union cases requires a new negotiated version; there is no ignore-unknown negotiation.

| Primitive | Exact domain |
|---|---|
| `V` | literal string `astrid.lifecycle-transfer/v2` |
| `Text` | Unicode scalar string, no NUL, UTF-8 length 0–4096 bytes; no normalization |
| `Token` | 1–256 ASCII bytes matching `[-A-Za-z0-9_.:/@+]+` |
| `ID` | exactly 32 lowercase hexadecimal characters; new transfer/attempt/generation/request/challenge/channel/writer IDs use independent 128-bit randomness |
| `Digest` | `sha256:` followed by exactly 64 lowercase hex characters; hash of the canonical object or exact raw bytes explicitly identified by its type |
| `GitSHA` | exactly 40 lowercase hex characters |
| `U64` | decimal JSON **string**, either `0` or regex `[1-9][0-9]*`, numeric value ≤18446744073709551615 |
| `Bytes` | unpadded base64url of exact bytes; decode/re-encode must be identical |
| `UUID` | lowercase `8-4-4-4-12` hexadecimal UUID string |
| `Bool` | JSON `true` or `false`, never an integer |
| `Path` | `Text`, absolute platform-canonical path obtained under pinned no-follow traversal; spelling alone grants nothing |
| `FieldPath` | JSON pointer to a declared field/array element of these schemas; cannot name an undeclared extension |
| `Kind` | `ManagerEvidence`, `SuppressionReceipt`, `NativeCloseProof`, `ReadyProof`, `DefinitionProof`, `PriorStateProof`, `ReleaseProof`, `DetachReceipt` (strings) |
| `Role` | `offline-backup`, `migration`, `candidate`, `rollback-r1` |
| `Phase` | `OBSERVED`, `PREPARED`, `HELD_PROVISIONAL`, `RESTART_FENCED`, `HELD_ADMITTED`, `PREPARING_DATA`, `SELECTED`, `WRITER_CLAIMED`, `READY`, `COMMIT_INTENT`, `COMMIT_DECIDED`, `COMMITTED`, `ROLLBACK_INTENT`, `ROLLBACK_DECIDED`, `ROLLED_BACK_STOPPED`, `RECOVERY_REQUIRED`; detach acceptance remains an acknowledgment within COMMIT_DECIDED |
| `Reason` | `unreadable`, `visibility`, `raced`, `missing_provenance`, `unsupported_native`, `unauthenticated`, `incomplete`, `expired`, `mismatch`, `io`, `cancelled`, `not_collected` |

Canonical JSON is ASCII, with keys sorted by their Unicode scalar values, no whitespace/BOM/newline, no JSON numbers, and arrays in declared order. Strings use `\"` and `\\`; every scalar U+0000–001F and U+007F or higher uses lowercase `\uXXXX`, with the standard high/low surrogate pair for scalars above U+FFFF. Other ASCII characters are literal, including `/`; no short `\n`, `\t` or escaped `/`. Lone surrogates, noncanonical encodings and trailing bytes refuse. This explicitly defined encoding does not depend on a library's default formatting. Example bytes for a=é and n=0: `{"a":"\u00e9","n":"0"}`. Digests have no self-digest member. `binding_digest = H(canonical(Binding))`; `request_digest = H(canonical(Request))`; a proof reference hashes the entire evidence object including its common header. Raw file/native-export digests hash raw bytes and never stand for canonical object digests.

Each local IPC message is exactly one `uint32_be(length) || canonical(Frame)`; length 1–16384, no trailing frame in that message or ancillary data except an explicitly armed FD delivery. `Frame={version:V,kind:"request"|"challenge"|"fact-reply"|"seal"|"evidence"|"verification"|"query"|"status",body:Ref}`. `Ref={type:Token,digest:Digest,byte_length:U64}` names one immutable, complete private object. Its `type` must be the exact declared type for this frame/action, not an arbitrary token. Bodies/attachments are stored/read through pinned owner-restricted handles and authenticated local IPC: no caller-selected URL or unrestricted path fetch. Maximum object/attachment is 1048576 bytes, aggregate 16777216 bytes per request, including all subcollector replies/seals; reject overflow before mutation, never truncate a census. Object materialization/attachment delivery needs an implemented bounded private store; it is not provided by R or A. No streaming/pagination completeness shortcut is defined in v2.

The frame/body mapping is closed. A wrong body type, direction or unarmed message is invalid; framing does not authorize an action.

| Frame.kind | Exact Ref.type / direction |
|---|---|
| `request` | `Request`; authorized caller to Runtime issuer |
| `challenge` | `Challenge`; Runtime issuer to logical collector or ledger-assigned subcollector |
| `fact-reply` | `FactReply`; assigned independent subcollector directly to Runtime over its authenticated C/W connection |
| `seal` | `Seal`; Runtime issuer to logical collector, only after accepting the bound FactReply |
| `evidence` | Exact Challenge.evidence_kind, one of Kind; logical collector to Runtime |
| `verification` | `Verification`; Runtime to authorized caller, observational record only |
| `query` | `Query`; authorized caller to Runtime |
| `status` | `QueryStatus`; Runtime to authorized caller, observational record only |

FactReply and Seal are separate earlier messages within the same logical request, not extra frames appended to an evidence message. Runtime first pins the complete canonical FactReply bytes in its private store, then issues/stores Seal. The collector copies that exact Seal into the applicable Obs<Seal> in the composite evidence; its reply_digest resolves to the already pinned FactReply. No payload field, raw attachment or caller-selected reference can substitute for receipt of the fact-reply frame on the assigned live channel. Native subcollectors are installed custodians/verifiers speaking C and observing N; no native manager is assumed to implement these frames.

Typed Ref bodies use their schema name exactly. Raw attachments use only `raw:file`, `raw:native-export`, `raw:environment`, `raw:module-set`, `raw:containment-history`, `raw:data-verification`, `raw:journal`, or `raw:transcript`; these hash exact bytes. Each digest-valued field fixes its expected attachment kind by meaning, and the pinned reviewed native/source parser profile must fix that kind's native byte representation and complete validation. Unknown profile/encoding refuses, rather than comparing ambiguous text. Private preimage selection/catalog/environment refs use raw:file; definition refs use Definition; data_baseline/backup use raw:data-verification. Attachments cannot declare a new evidence schema or bypass any native check. All reference graphs are acyclic, referring only to already materialized inputs/earlier records; no signature/hash authenticates an observation. PriorImage intentionally binds operation/generation and root IDs, not Binding's digest: Binding hashes PriorImage, avoiding a hash cycle.

```text
Obs<T> = {state:"known",value:T,evidence:Digest}
       | {state:"unknown",reason:Reason,evidence:Digest|null}
Presence<T> = {state:"present",value:T} | {state:"absent"}
Fault = {field:FieldPath,reason:Reason,detail:Text}
Code = "none"|"busy_owner"|"unknown_identity"|"binding_changed"|
       "incomplete_census"|"restart_unfenced"|"busy_work"|"unsupported_transfer"|
       "lease_uncertain"|"durability_failed"|"recovery_required"
FileID = {platform:"posix",device:U64,inode:U64}
       | {platform:"windows",volume:Bytes,file_id:Bytes}
FileObject = {path:Path,id:FileID,kind:"file"|"directory",mode:U64,
              uid:U64|null,gid:U64|null,acl_digest:Digest,content_digest:Digest|null}
Scope = {kind:"linux-cgroup",native_id:Bytes,manager_epoch:Bytes,launch_id:Bytes,
         object:Obs<FileObject>}
      | {kind:"darwin-retained-scope",native_id:Bytes,manager_epoch:Bytes,
         launch_id:Bytes,retention_digest:Digest}
      | {kind:"windows-job",native_id:Bytes,manager_epoch:Bytes,launch_id:Bytes,
         job_creation_digest:Digest,retained_handle_slot:ID}
Principal = {platform:"posix",uid:U64,gid:U64,groups:U64[]}
          | {platform:"windows",sid:Text,token_digest:Digest}
Process = {platform:"linux",pid:U64,start_ticks:U64,boot_id:UUID,
           pidns_inode:U64,userns_inode:U64,principal:Principal,image:FileObject,
           argv_digest:Digest,environment_digest:Digest,scope:Presence<Scope>}
        | {platform:"darwin",pid:U64,birth_sec:U64,birth_nsec:U64,
           audit_token:Bytes,principal:Principal,image:FileObject,
           argv_digest:Digest,environment_digest:Digest,scope:Presence<Scope>}
        | {platform:"windows",pid:U64,creation_filetime:U64,principal:Principal,
           image:FileObject,command_line_digest:Digest,scope:Presence<Scope>}
Target = {kind:"systemd-user"|"launchd-gui"|"scheduled-task",namespace:Text,target:Text}
Manager = {target:Target,peer:Process,native_session:Bytes,epoch:Bytes}
NativeState = {run:"stopped"|"running"|"transitioning",loaded:Bool,
               enabled:"enabled"|"disabled"|"masked"|"static"|"not-applicable",
               native_enablement:Text,instance_ids:Bytes[]}
Publication = {name:"instance"|"discovery"|"selection"|"catalog"|"journal",
               object:Obs<Presence<FileObject>>,bytes_digest:Obs<Presence<Digest>>}
Source = {repository:"banodoco/astrid-agent"|"banodoco/astrid-runtime",
          commit:GitSHA,version_document:Digest,profile:Digest,manifest:Digest,
          interpreter:FileObject,module_set:Digest}
SourcePair = {agent:Source,runtime:Source}
Lineage = {from_root:FileID,from_owner:FileID,to_root:FileID,to_owner:FileID,
           intent_digest:Digest,role:Role}
Binding = {version:V,operation_id:Token,journal_operation_id:Token,attempt_id:ID,
           transfer_id:ID,generation:ID,controller:Process,installation:FileObject,
           program:FileObject,data:FileObject,support:FileObject,realm_uuid:UUID,
           realm_root:FileObject,owner_lock:FileObject,old:SourcePair,candidate:SourcePair,
           prior_selection_digest:Digest,prior_journal_digest:Digest,
           manager:Obs<Manager>,effective_definition_digest:Obs<Digest>,
           original_publications:Publication[],original_owner:Obs<Presence<Process>>,
           origin:"stopped-r0"|"live-r1",prior_state:Obs<NativeState>,
           prior_state_digest:Digest,permitted_lineage:Lineage[],permitted_roles:Role[],
           required_services:Token[],source_authentication:Obs<Digest>}
```

`FileObject.content_digest` is null exactly for directories; uid/gid null exactly for Windows, where ACL digest and SID apply. Paths, IDs, ownership/mode/ACL, digest, image origin and parent chain all require native verification. PID >0, nsec <1000000000; platform/principal/scope kinds must agree. No synthetic cross-platform Process. A missing birth component produces `Obs<Process>.unknown`; do not partially construct a known Process. `Publication[]` has exactly the five names once. `original_owner` known-absent requires positive native evidence, not missing discovery. Unknown binding fields permit only OBSERVED/read-only diagnostics, not PREPARED/admission. Once established, Binding bytes are immutable; expected changes use the authorized chain/lineage, not editing Binding. Controller takeover/new generation follows D §9, preserving operation identity and reconciling any writer first.

Set-like arrays (principals' groups, roles, services, instance IDs) sort by canonical element bytes, without duplicates. File/route/process/delta inventories sort by canonical identity keys (`FileID`, route target+name, complete Process, object+postimage); mutation chains, argv, ancestry and event arrays preserve causal order. Every inventory carries the completeness observations below; an empty array alone says nothing. `Obs.evidence` points to a retained collection transcript/attachment, not a signature. Producer provenance must be established through §6; a hash or valid structure never authenticates it.

## 3. Shared requests, challenges and results

```text
Action = "snapshot"|"install_suppression"|"verify_suppression"|"register_candidate"|
         "restore_while_suppressed"|"publish_ordinary_candidate"|"remove_owned_suppression"
SessionRef = {transfer_id:ID,generation:ID,holder:Process,endpoint_id:ID,
              descriptor_generation:ID,root:FileID,owner:FileID}
StartReference = {endpoint_id:ID,ticket_slot:ID,writer_id:ID,role:Role,
                  source_digest:Digest,expected_definition_digest:Digest}
DecisionRef = {outcome:"complete"|"rolled_back",journal_digest:Digest,
               ready_or_prior_digest:Digest,release_pending:Bool}
AdapterArguments = {action:"snapshot",binding:Ref}
          | {action:"install_suppression",expected:Ref,provisional:SessionRef}
          | {action:"verify_suppression",receipt:Ref}
          | {action:"register_candidate",expected:Ref,armed_reference:StartReference}
          | {action:"restore_while_suppressed",expected:Ref,preimage:Ref}
          | {action:"publish_ordinary_candidate",expected:Ref}
          | {action:"remove_owned_suppression",expected:Ref,terminal_decision:DecisionRef}
RuntimeArguments = {action:"collect_close",writer_id:ID,role:Role}
                 | {action:"collect_ready",writer_id:ID,role:Role}
                 | {action:"collect_detach",writer_id:ID,decision:DecisionRef,
                    ready_digest:Digest,ordinary_definition_digest:Digest}
Arguments = AdapterArguments | RuntimeArguments
Request = {version:V,operation_id:Token,transfer_id:ID,generation:ID,request_id:ID,
           expected_phase:Phase,binding_digest:Digest,sequence:U64,arguments:Arguments}
Challenge = {version:V,challenge_id:ID,request_digest:Digest,operation_id:Token,
             transfer_id:ID,generation:ID,binding_digest:Digest,sequence:U64,
             expected_phase:Phase,evidence_kind:Kind,issuer:Process,
             collector:Process,issuer_boot_id:Bytes,clock_id:"issuer-monotonic",
             issued_ns:U64,deadline_ns:U64,nonce:Bytes}
Header = {version:V,operation_id:Token,transfer_id:ID,generation:ID,
          binding_digest:Digest,sequence:U64,observed_start_ns:U64,observed_end_ns:U64,
          collector_identity:Process,result:"observed"|"unknown"|"refused"|
          "unsupported"|"recovery_required",request_digest:Digest,challenge_id:ID,
          challenge_digest:Digest,stage:Phase,code:Code,
          side_effects:"none"|"owned"|"uncertain",recovery_refs:Ref[],faults:Fault[]}
Seal = {producer:Process,channel_id:ID,challenge_id:ID,reply_digest:Digest}
FactReply = {version:V,request_digest:Digest,challenge_id:ID,producer:Process,
             observed_start_ns:U64,observed_end_ns:U64,facts:Fact[]}
Fact = {field:FieldPath,value_digest:Digest}
Verification = {version:V,request_digest:Digest,challenge_id:ID,evidence_digest:Digest,
                verifier:Process,stage:Phase,sequence:U64,
                result:"verified"|"refused"|"unknown"|"unsupported"|"recovery_required",
                code:Code,checks:FieldCheck[],side_effects:"none"|"owned"|"uncertain",
                recovery_refs:Ref[],faults:Fault[]}
FieldCheck = {field:FieldPath,producer_digest:Digest,native_readback_digest:Obs<Digest>,
              result:"match"|"mismatch"|"unknown"|"unsupported"}
Query = {version:V,operation_id:Token,transfer_id:ID,generation:ID,binding_digest:Digest}
QueryStatus = {version:V,operation_id:Token,transfer_id:ID,generation:ID,
               binding_digest:Digest,recorded_phase:Phase,recorded_sequence:U64,
               recorded_results:Ref[],recorded_release_pending:Bool,
               recorded_writer_occupancy:"none"|"known"|"uncertain",code:Code,faults:Fault[]}
```

Each evidence type below is `Header + {kind:literal,...}`: flatten both closed records, never wrap a `header` member. `expected` is a ManagerEvidence reference for the exact prior boundary, `receipt` a SuppressionReceipt, `binding` a Binding, and `preimage` the immutable PriorImage defined below. Ref.type and the digest's decoded schema must agree. The terminal decision must be independently read from Agent's durable journal, `release_pending=true`; no caller-created DecisionRef grants authority. SessionRef/StartReference are nonsecret references. Neither is serializable lock ownership or a bearer capability.

Adapter actions map in order to ManagerEvidence, SuppressionReceipt, ManagerEvidence, ManagerEvidence, PriorStateProof, DefinitionProof, ReleaseProof; collect_close/collect_ready/collect_detach map to NativeCloseProof/ReadyProof/DetachReceipt. No other action/kind pairing is valid. Runtime collection actions only gather challenged evidence; they confer no writer/start/stop/transition authority. Original D session methods still own those actions and use the existing operation/request ledger. Composite proofs use the same challenge with separately authenticated native and writer subcollectors, bound by §6's SubcollectorEntry in that ledger to the one logical collector. Subcollectors do not replace Challenge.collector or submit a composite as that collector.

Seal.reply_digest hashes the entire earlier canonical FactReply accepted directly by Runtime over the live channel named by Seal.channel_id, never the enclosing composite evidence object. Seal is Runtime-issued ledger evidence of that receipt, not a producer signature or bearer capability. Seal.producer equals FactReply.producer and the ledger-authenticated peer; both challenge IDs and FactReply.request_digest must match the ledger's exact Challenge/Request. Fact.value_digest hashes the canonical actual value at its declared fact path in the expected evidence kind (for Obs, the value or unknown reason, excluding evidence links); no fact path may address Header/Seal/FactReply or another evidence-link field. Facts sort by field path, unique and complete for the Runtime-assigned fields. Runtime compares them against the composite payload and its own native readback. This prevents a self-referential hash; it supplies no historical trust merely by being well formed.

Only Runtime's live holder issues Challenge. Nonce is 32 random bytes, single-use and privately bound to the authenticated collector, request bytes, evidence kind, phase, sequence, generation and current descriptor generation. A nonce echo is not channel authentication. Times use the issuer's monotonic domain: the collector receives start/end stamps from live issuer barriers around collection; its own local clock is diagnostic only. Runtime checks `issued ≤ start ≤ end ≤ now ≤ deadline`; boot/domain mismatch, inverted/foreign timestamps or a restarted issuer invalidate the challenge. IPC budget is 10 seconds; native start/drain has D's fixed 90-second stage budget. A poll uses a new ≤10-second challenge inside the original stage deadline; polling never extends it. No TTL grants authority to a later phase. Repeat all required readbacks at each mutation/authority boundary under the retained locks/gates; snapshots do not establish continuity between them.

Identical Request retry returns the durable recorded operation result with its original challenge/evidence references; it does **not** reauthorize the next transition or repeat native side effects. Rebound request ID/different canonical bytes returns `binding_changed`. A fresh observation requires a fresh request/challenge/sequence. Stale phase/sequence/generation yields `binding_changed` (stale state detail in faults), never an implicit replay. Consume a challenge atomically with verification and transition intent in the existing ledger; a lost reply uses observational `query`, with fresh native verification before further action. Cancellation/expiry before mutation has `side_effects=none`; after any owned change, persist owned/uncertain effects and recovery references, retain available custody/gates and reconcile. Do not turn after-action uncertainty into an initial safe refusal.

### 3.1 Closed outcome and check invariants (F-1)

The following are normative cross-field constraints on the closed schemas above, enforced by the producer and independently by Runtime. They apply to every flattened Header in all seven §5 evidence records and DetachReceipt, and to Verification from every adapter/Runtime collection action. A syntactically valid record with an inconsistent combination is invalid evidence. Runtime must reject it; it must never repair its result, clear a fault, drop a check or infer a successful outcome from code alone.

| Record / result | Required code | Required faults and meaning |
|---|---|---|
| Header / `observed` | `none` | `faults=[]`; collection completed. Truthful phase-inapplicable unknowns and negative observations remain payload facts, never positive authority |
| Verification / `verified` | `none` | `faults=[]`; Header is `observed`/`none`/empty faults, every required check exists exactly once and matches, every emitted check matches, and every consuming predicate is positively satisfied |
| Either / `unknown` | Any Code except `none` | Nonempty faults, including a field and a Reason identifying the unavailable/uncertain required fact; no successful verification |
| Either / `refused` | Any Code except `none` | Nonempty faults identifying the failed predicate, inconsistency or mismatch; no successful verification |
| Either / `unsupported` | `unsupported_transfer` | Nonempty faults including `reason=unsupported_native` at the unsupported field; no successful verification |
| Either / `recovery_required` | Any Code except `none` | Nonempty faults and nonempty recovery_refs identifying retained intent/evidence/recovery state; no successful verification |

These rows are exhaustive and subject to mandatory escalation after effects: the unknown/refused/unsupported rows apply only before effects at this boundary; a failure after owned effects, or any uncertainty about effects, requires recovery_required as specified below. `none` is legal **if and only if** Header.result is `observed` or Verification.result is `verified`, respectively. Every other combination, including non-success with empty faults and success with faults/non-none code, must be rejected. A refusal fault may use `reason=mismatch` for a known negative predicate even when producer and readback agree on that negative value; fault detail must state the required value and observed failure. Header.observed does not imply Verification.verified. Header non-success can never produce VerifiedEvidence, even if its payload contains useful known facts.

`faults` use declared paths in the applicable record; a Verification fault about its input identifies `/evidence_digest` and the exact input field in detail, or the relevant `/checks/<index>` when available. Malformed input whose field cannot be addressed uses the declared input reference field, never an invented path. Faults for nested Census/Containment errors must also identify the blocking error through the enclosing record's path/detail. Each collected blocking failure must be retained; one primary code does not erase other faults. `QueryStatus` is observational: `code=none` requires empty faults and non-none code requires nonempty faults, but neither combination grants verification or freshness.

For every FieldCheck, producer_digest must be recomputed from the actual input field and native_readback_digest must come from Runtime's own authenticated native readback or the structural/derived issuer-ledger check permitted in §6. Both digests must refer to the same declared field and canonical value projection; a digest of different bytes/type/field is an invalid check. Checks are sorted by field path, unique, and cover the complete required set derived from the trusted action, evidence kind, phase, Binding and selected proof alternative **before** inspecting the result. A collector cannot declare a failed field optional. Missing, duplicate, undeclared, misaddressed or omitted required checks are failures, never an empty successful conjunction.

| FieldCheck.result | Exact required native_readback_digest state and digest relation |
|---|---|
| `match` | `known`; readback value equals the recomputed producer_digest |
| `mismatch` | `known`; readback value differs from the recomputed producer_digest |
| `unknown` | `unknown` with Reason other than `unsupported_native` |
| `unsupported` | `unknown` with `reason=unsupported_native` |

These four cases are mutually exclusive and exhaustive. A claimed match with unknown/unequal readback, mismatch with equal/unknown readback, or unknown/unsupported with known readback is invalid evidence. Unknown readback may retain a transcript digest in Obs.evidence; that does not make its value known. Matching digests establish agreement only: `known false`, nonempty live-owner lists, or known-absent required suppression still fail a consuming positive predicate. A required unknown Obs cannot be made positive by hashing its reason. For a phase-inapplicable unknown, Runtime may verify only its truthful encoding/provenance and trusted inapplicability using structural/ledger checks; that match supplies no native fact. If such checking itself remains unknown/unsupported, successful Verification is unavailable. In particular, the complete native-death close alternative in §5 may leave cooperative DB/drain acknowledgments unknown; it must still independently prove exact death, durable work safety and every alias/queue/inheritor closure. No new proof alternative is introduced here.

**Mandatory Runtime rejection:** any emitted `mismatch`, `unknown` or `unsupported` check, missing required check, failed semantic predicate, non-success Header, or inconsistent result/code/fault/check combination prevents both creation and consumption of VerifiedEvidence and prevents the requested transition. A mismatch anywhere, including an additional checked field, cannot be ignored because another required field matched. Before effects, a native disagreement yields `refused` with a non-none failure code selected under §6 (normally `binding_changed`) and a mismatch fault; missing native information yields unknown/unsupported with the matching fault. Invalid combinations are refused as invalid evidence with a mismatch fault at the offending declared field; no malformed producer outcome is trusted. If this boundary has already changed native state or side effects are uncertain, return `recovery_required`, persist non-none code, faults and recovery_refs, retain all available custody/gates and reconcile; never relabel that outcome as an initial safe refusal. Verification must faithfully record all checks actually performed, including failures; no success by filtering the check array.

`side_effects` and recovery_refs describe persisted reality, independently checked against intent/native ledgers. `none` is legal only when no effects occurred at this boundary; `owned` requires known operation-owned effects and recovery_refs, and `uncertain` always requires recovery_required plus recovery_refs. A successful owned mutation may report `observed`/`verified` with `owned` and retained references. Failed readback after any effect requires recovery_required regardless of which predicate failed. Read-only verification never erases the effects of its associated adapter action. No failed result releases custody or clears pending recovery.

## 4. Native observation components

```text
Command = {image:FileObject,argv:Text[]}
Route = {target:Target,name:Token,kind:"service"|"timer"|"socket"|"dependency"|
         "login"|"task-trigger"|"helper",definition_digest:Digest,
         state:"denied"|"ticket-only"|"ordinary",pending:Bool}
DefinitionView = {start:Command[],start_pre:Command[],stop:Command[],working_directory:Path,
                  principal:Principal,environment_digest:Digest,environment_files:FileObject[],
                  restart_policy:Token,kill_policy:Token,stop_timeout_ns:U64,
                  conditions_digest:Digest,activation_routes:Route[],
                  security_properties_digest:Digest,unmodeled_semantics:Bool}
Definition = {manager:Manager,files:FileObject[],native_export_digest:Digest,
              native_parser_profile:Digest,view:DefinitionView,effective_digest:Digest}
MutationGuard = {kind:"exclusive-cooperating-scope"|"native-no-replace",
                 ownership_scope_digest:Digest,independent_writers_excluded:Bool,
                 verification_digest:Digest}
DeltaObject = {kind:"file",object:FileObject}
            | {kind:"native-setting",target:Target,setting:Token,native_epoch:Bytes}
OwnedDelta = {object:DeltaObject,preimage:Presence<Digest>,postimage:Presence<Digest>,
              owner_operation:Token,owner_generation:ID,guard:Obs<MutationGuard>,
              readback:Obs<Presence<Digest>>}
Containment = {original_launch:Obs<Bytes>,scope:Obs<Scope>,
               source_digest:Obs<Digest>,launcher:Obs<Process>,
               retained_from_launch:Obs<Bool>,history_digest:Obs<Digest>,
               no_escape:Obs<Bool>,visibility:Obs<"complete">,
               members:Obs<Process[]>,dead_members:Obs<Process[]>,
               errors:Fault[],native_seal:Obs<Seal>}
Census = {containment:Containment,selected_owners:Obs<Process[]>,
          descendants:Obs<Process[]>,known_escapes:Obs<Process[]>,
          complete:Obs<Bool>,visibility:Obs<"complete">,errors:Fault[]}
Alias = {alias_id:ID,kind:"owner"|"directory",root:FileID,object:FileID,
         writer_id:ID|null,recipient:Obs<Presence<Process>>,channel_id:ID,
         sent:Obs<"not-sent"|"queued"|"delivered">,
         received:Obs<Bool>,closed:Obs<Bool>,
         queue:Obs<"empty"|"destroyed"|"pending">,inheritors:Obs<Process[]>,
         resolution:Obs<"never-sent"|"live-recipient-close"|
                        "recipient-and-inheritors-dead"|"undelivered-channel-destroyed"|
                        "retained-holder"|"retained-writer">}
Comparison = {name:"data"|"selection"|"catalog"|"definition"|"environment"|"publications",
              expected_digest:Digest,actual_digest:Obs<Digest>,
              policy:"exact"|"same-schema-preserve"|"migration-restore"|"recovery-retained"}
PriorImage = {version:V,operation_id:Token,transfer_id:ID,generation:ID,
              realm_root:FileID,owner_lock:FileID,native_state:Obs<NativeState>,
              selection:Ref,catalog:Ref,definition:Ref,environment:Ref,publications:Publication[],
              data_policy:"same-schema-preserve"|"migration-restore",data_baseline:Ref,
              backup:Obs<Presence<Ref>>,permitted_deltas:OwnedDelta[]}
Endpoint = {kind:"unix"|"http-local",address:Text,instance_id:Token,peer:Process,
            authentication_digest:Digest}
ServiceCheck = {name:Token,instance_id:Token,endpoint_digest:Digest,
                semantic_result:Obs<Bool>,observation_digest:Digest}
```

Definition native exports contain the complete effective native property inventory, not only listed convenient properties; files include fragment/plist/XML and every override/environment source. `native_parser_profile` pins a jointly reviewed decoder/semantic coverage profile; `view` is its lossless-for-authority projection, `effective_digest=H(canonical(view))`. The native export is retained as exact platform bytes, independently collected and decoded; a matching digest alone proves no semantics. Unknown property semantics, unsupported argv/credential encoding, omitted inherited configuration, `unmodeled_semantics=true`, uncertain loaded-versus-disk interpretation or unavailable native observation returns unknown/unsupported. This packet does not claim such a complete decoder currently exists.

`native_epoch`/Manager.epoch bind the actual native manager incarnation. A `compare_generation` below is only a local digest of expected native exports/files/routes/state plus authorized deltas, not a native atomic CAS facility. Guard must establish exclusion of independent writers for the chosen object and trust boundary; a pre/post equality sample cannot set `independent_writers_excluded=true`. If no ownership/no-replacement mechanism exists, stop before the mutation (`unsupported_transfer`), or preserve an observed replacement and remain recovery-required after effects. Existing independently editable native definitions are an explicit feasibility gate.

**Replacement race requirement:** every install, register, publish, restore and removal (including the final guard) requires a jointly reviewed native mechanism for that exact file/parent or manager setting. Its profile must name all mutators, the exclusion/no-replace primitive and its limits, bind owner_operation + owner_generation + exact object/native epoch + expected pre/postimage, and make that ownership comparison and mutation indivisible with respect to independent replacement. A file creation no-replace primitive alone does not establish conditional overwrite/unlink or manager mutation. The guard must cover replacement, reload/restart and the entire check/mutate/readback interval; loss of exclusion or a manager epoch change invalidates it. Pinned handles, advisory installation locks and OwnedDelta fields alone do not exclude independent editors.

No such complete Linux systemd/file mechanism is established here. Before effects, an observed foreign/replaced object yields binding_changed without touching it; inability to exclude a check-to-write replacement yields unsupported_transfer before mutation. After any effects or uncertainty, preserve the replacement, persist recovery_required and recovery_refs, and retain every available gate/custody/control authority. Do not undo effects by overwriting the replacement, remove its gate, or reinstall a guard over it. If safe retention/restoration itself is unavailable, leave the operation unfinished for explicit recovery. This rule applies to each delta, not only the first precondition or final sample.

Containment history must originate in the authentic original launch, bind source/launcher/scope before descendants can escape, and retain authenticated membership/inheritance provenance through the current complete census. `history_digest` names that retained witness. An empty/missing/deleted current cgroup, UID scan, process-name scan, successful `/proc` reads, log PID or later newly created scope cannot supply it. Native seal validation requires the actual trusted producer/retention chain; Seal fields themselves authenticate nothing. A and the cited runs provide no such witness. With no witness, `retained_from_launch`, `history_digest`, `no_escape`, and completeness remain unknown; **incomplete_census is mandatory even if the original lock can be acquired**.

## 5. The seven evidence schemas

All fields below are required, including explicit unknown observations. Arrays are complete sets under §2, not best-effort lists. Every schema inherits §3.1's outcome constraints. `Header.result=observed` only describes completed collection with code none and empty faults. Authority requires §6 verified required facts with positive values; false, mismatched, unknown, unsupported or incomplete required facts never pass. Before suppression exists a snapshot can truthfully report known-absent suppression; the admission predicate cannot accept that state. Runtime rejects any FieldCheck mismatch for every schema, including DetachReceipt, regardless of the producer's Header.result.

```text
ManagerEvidence = Header + {
  kind:"ManagerEvidence",adapter_version:Token,manager:Obs<Manager>,
  disk_definition:Obs<Definition>,loaded_definition:Obs<Definition>,
  loaded_disk_equivalent:Obs<Bool>,ownership:Obs<Bool>,
  mutation_chain:OwnedDelta[],compare_generation:Obs<Digest>,
  invocations:Obs<Process[]>,native_state:Obs<NativeState>,
  pending_jobs:Obs<Bytes[]>,routes:Obs<Route[]>,routes_complete:Obs<Bool>,
  census:Census,suppression:Obs<Presence<Ref>>}

SuppressionReceipt = Header + {
  kind:"SuppressionReceipt",manager:Obs<Manager>,before:Ref,
  compare_generation_before:Obs<Digest>,compare_generation_after:Obs<Digest>,
  independent_guards:Obs<FileObject[]>,owned_deltas:OwnedDelta[],
  denial:Obs<"all-managed-denied"|"candidate-ticket-only">,
  restart_denied:Obs<Bool>,triggers_denied:Obs<Bool>,routes:Obs<Route[]>,
  routes_complete:Obs<Bool>,pending_jobs:Obs<Bytes[]>,
  native_readback:Obs<Digest>,release_pending:Obs<Bool>,preimage_digest:Digest}

NativeCloseProof = Header + {
  kind:"NativeCloseProof",writer_id:ID,role:Role,writer:Obs<Process>,
  db_closed:Obs<Bool>,workers_drained:Obs<Bool>,durable_work_safe:Obs<Bool>,
  runtime_seal:Obs<Seal>,native_seal:Obs<Seal>,census:Census,
  aliases:Alias[],alias_inventory_complete:Obs<Bool>,
  channels_complete:Obs<Bool>,zero_writers:Obs<Bool>}

ReadyProof = Header + {
  kind:"ReadyProof",writer_id:ID,role:Role,writer:Obs<Process>,
  invocation:Obs<Bytes>,source:Obs<SourcePair>,endpoint:Obs<Endpoint>,
  services:ServiceCheck[],services_complete:Obs<Bool>,
  adopted_root:Obs<FileID>,adopted_owner:Obs<FileID>,
  descriptor_generation:ID,owner_alias_id:ID,directory_alias_id:ID,
  directory_alias_closed:Obs<Bool>,adoption_before_db:Obs<Bool>,
  definition_digest:Obs<Digest>,definition_status:Obs<"transfer-bound"|"ordinary-no-ticket">,
  semantic_result:Obs<Bool>,one_owner:Obs<Bool>,no_old_tree:Census,
  runtime_seal:Obs<Seal>,native_seal:Obs<Seal>}

DefinitionProof = Header + {
  kind:"DefinitionProof",writer_id:ID,manager:Obs<Manager>,ordinary:Obs<Definition>,
  disk:Obs<Definition>,loaded_disk_equivalent:Obs<Bool>,
  no_transfer_reference:Obs<Bool>,release_pending_gate:Obs<Bool>,
  ready_digest:Digest,terminal_journal_digest:Digest,
  owned_deltas:OwnedDelta[],compare_generation:Obs<Digest>,native_seal:Obs<Seal>}

PriorStateProof = Header + {
  kind:"PriorStateProof",preimage_digest:Digest,comparisons:Comparison[],
  prior_state:Obs<NativeState>,restored_state:Obs<NativeState>,
  zero_writers:Obs<Bool>,close_proofs:Ref[],suppression:Ref,
  independent_suppression_effective:Obs<Bool>,pending_jobs:Obs<Bytes[]>,
  owned_remaining_deltas:OwnedDelta[],authorized_lineage:Lineage[],
  publications:Publication[],native_seal:Obs<Seal>}

ReleaseProof = Header + {
  kind:"ReleaseProof",decision:DecisionRef,decision_digest:Digest,
  detach_receipt:Obs<Presence<Ref>>,prior_state_proof:Obs<Presence<Ref>>,
  aliases:Alias[],alias_inventory_complete:Obs<Bool>,channels_complete:Obs<Bool>,
  owned_removals:OwnedDelta[],suppression_removed:Obs<Bool>,
  native_state:Obs<NativeState>,definition_digest:Obs<Digest>,
  replacement:Obs<Presence<Definition>>,subsequent_launch:Obs<Presence<Process>>,
  disposition:Obs<"committed-ready"|"restored-stopped"|"subsequent-old-launch">,
  native_seal:Obs<Seal>}

DetachReceipt = Header + {
  kind:"DetachReceipt",writer_id:ID,writer:Obs<Process>,
  terminal_journal_digest:Digest,ready_digest:Digest,ordinary_definition_digest:Digest,
  accepted_sequence:U64,writer_recorded:Obs<Bool>,writer_ack:Obs<Seal>}
```

Cross-field requirements:

* Suppression `before` is the verified exact ManagerEvidence preimage; `preimage_digest` is Binding.prior_state_digest, referencing PriorImage. Independent guards must survive terminal journal state and unguarded R0 restoration. Every route is denied for stopped admission/rollback restoration. `candidate-ticket-only` is a later, separately verified phase after the candidate action and one-use launch are armed; it is not evidence of all-managed denial. Pending jobs must be known empty at denial barriers. Each delta binds this operation/generation; never remove a foreign/preexisting guard or overwrite its replacement.
* CloseProof's alias inventory is Runtime's full ledger of originals, every send/queue/receipt/recipient, duplicate and possible inheritor, including directory aliases. Live recipients require an authenticated close acknowledgment after DB/worker drain. Alternatively the exact recipient and every possible inheritor are positively dead under complete retained containment, with durable work reconciled. On this death route, unavailable process DB/drain acknowledgments stay unknown; complete native death plus work reconciliation must independently establish closure, never infer cooperative drain. Queued copies require receipt-and-close or destruction of the undelivered channel; an empty current member list, EOF, PID disappearance or send success is insufficient. Unresolved queue/delivery/inheritance/close means `lease_uncertain`. Do not drop the broker alias to probe another alias's existence.
* Ready requires the exact armed writer/source/role and native invocation to equal the authenticated child and endpoint, expected services exactly equal Binding.required_services, all semantic checks positive, owner/root IDs in authorized lineage, directory alias acknowledged closed, and adoption before DB/credentials/discovery. Helpers never produce service Ready or publish final doors. `one_owner` combines Runtime's one logical claimed writer with independent complete native checks; no numeric process count substitutes for custody. A directory alias remains uncertain until close/death-and-queue proof resolves it.
* DefinitionProof is for the final ordinary restartable candidate, without consumed ticket/reference/secret, while its adopted writer remains fenced and release-pending gate still applies. It does not itself detach. DetachReceipt requires that exact writer's durable acceptance and authenticated acknowledgment bound to terminal/ready/definition digests. No terminal journal bit synthesizes this acknowledgment.
* PriorStateProof has each Comparison.name exactly once, policies equal the accepted preimage/journal policy (data may preserve valid same-schema later writes). Non-data policies are exact except for explicitly authorized publication reconciliation; unsafe resurrection of dead-owner discovery stays recovery-required. It proves restoration **while independently suppressed**, zero writers and prior stopped/load/enable dimensions, not future release. No R0 restart or blanket backup overwrite.
* Release commit requires known-present DetachReceipt for the same writer/decision and known-absent PriorStateProof; broker/directory aliases closed/resolved and final writer's owner alias still open. Exact stopped rollback requires known-present PriorStateProof, known-absent DetachReceipt, zero writers and all owner/directory aliases closed/resolved. Native observed owned removal and exact final state are required for release observed. A replacement or unknown cleanup remains pending/recovery-required. An authenticated subsequent old launch after rollback gate removal is preserved and recorded separately (`subsequent-old-launch`); this disposition is diagnostic and cannot itself clear release_pending or prove exact-stopped restoration. No new update is authorized by that disposition alone.

**Terminal gate-removal race:** any launch racing removal, final native readback or durable release observation keeps release_pending=true. Runtime must refuse VerifiedEvidence consumption/finalize_release for that boundary until the exact source/definition/native incarnation is independently authenticated, preserved without signaling, and reconciled under D §9. Unknown launch identity, interval coverage, replacement or ordering requires recovery_required after effects, never restored-stopped/committed-ready inferred from a clean sample. A before/after sample cannot establish continuous absence or exclude a launch that exited between samples. Before removing the last gate, a reviewed native mechanism must cover the removal/readback/release-observation interval with complete launch/epoch ordering and reconciliation; if unavailable, retain suppression and refuse removal. No such native mechanism is established here. If lost after removal, retain remaining control/custody where available and unfinished recovery state; do not reclaim a released alias or stop a possible new owner. A reconciled subsequent launch is a separate D §9 outcome, never relabeled exact-stopped restoration or consumed as the old ReleaseProof; any later release authorization needs a fresh challenged proof and durable reconciliation.

Alias resolutions retained-holder/retained-writer identify positively tracked live aliases, never closure proofs. Commit permits exactly the selected final writer's owner alias to remain retained-writer; helper succession/stopped rollback permits no such alias. QueryStatus contains recorded ledger facts only, including occupancy/release-pending; it grants no current liveness, free-lock, absence, freshness or phase-transition authority. ReadyProof/collect_ready role is candidate or rollback-r1; other Role values are a cross-field error. Native scope IDs must bind actual retained native objects/provenance, not display names or new random labels; Darwin/Windows Scope alternatives are proposed representations with no currently established positive producer here.

## 6. Producers, authenticated channels and independent verification

**Proposed interfaces, absent until implemented/protected-reviewed:** Runtime `lifecycle_transfer.challenge(request, evidence_kind) -> Challenge`; installed Agent `ManagerAdapter` below; Runtime-owned native `NativeVerifier.observe(challenge, evidence_ref) -> NativeReadback` and `TransferSession.verify_evidence(challenge, evidence_ref, readback) -> VerifiedEvidence`. `VerifiedEvidence` is an opaque, process-local, single-use reference bound to the live holder, native readbacks, descriptor generation and exact next transition. It has no JSON encoding and cannot be reconstructed from Verification or a file. This is an internal refinement of D's facade; methods such as `admit_stopped_r0` retain their signatures and validate/consume these references internally. They must reject unverified serializable substitutes.

Proposed Agent exposure is `astrid.installer.service.Service.lifecycle_adapter(*, binding:Binding, session_reference:SessionRef) -> ManagerAdapter`; Runtime challenge/verifier symbols live under `astrid_runtime.lifecycle_transfer`. The Agent update facade resolves the exact installed Service/selection and supplies this adapter explicitly, never discovers an authority method by duck typing the observation helpers. Accessor construction is read-only and grants no manager/realm authority. R/A have no such accessor or implemented native verification API; absence returns unsupported_transfer. Implementers must not alias A's `_snapshot_linux_r0` or an accepting stub to this interface.

`NativeReadback` is also a local object, not a caller-supplied wire struct. Runtime obtains it through its own installed read-only verifier, with fresh native calls and live peer authentication. It emits the auditable closed `Verification` record subject to §3.1. Every required field has a FieldCheck, including provenance and completeness checks; structural/derived fields use the exact input digest plus the relevant native transcript or issuer-ledger digest as readback. Missing required check is failure. Runtime validates every check's digest/result relationship, complete trusted applicability set and semantic predicate; it must reject any mismatch or non-match check and must not issue or consume VerifiedEvidence for an inconsistent or failed result. An external Verification record is never authority. A second parse of Agent bytes or two samples from the same unauthenticated command is not independent verification. Query of the same native manager is permissible only through Runtime's own authenticated connection and decoder. No verifier can retrospectively reconstruct lost launch history.

Channel C: D's private owner-only local AF_UNIX transport, both peers checked against frozen process/image/principal/source/incarnation. Linux requires native peer credentials plus boot/birth/namespaces/image/cgroup; Darwin requires actual audit-token identity and selected launchd invocation. Capabilities are compared in constant time through private IPC/storage, not argv/logs/public receipts. A peer UID, nonce, digest or socket path alone is insufficient. Channel N: independently authenticated connection to the exact native manager plus OS process/file/scope observations under pinned handles; implementation must identify the actual supported native API and its limits. Channel W: authenticated installed Runtime writer/helper over C, distinct from selected manager service authentication. No unauthenticated helper output or driver hash can be promoted to N/W. No authenticated positive Windows transport/custody producer is claimed here.

The existing Runtime operation/request ledger has the following **proposed closed local entry**, keyed by `(H(canonical(request)), challenge.challenge_id, channel_id)`. It is not a wire type, new journal authority, or an assertion of an implemented ledger API. LocalChannel is an opaque retained OS connection handle plus its native authentication context; it has no serializable encoding.

```text
SubcollectorEntry (Runtime-local only) = {
  channel_id:ID,live_channel:LocalChannel,authenticated_peer:Process,
  producer:Process,producer_role:"native"|"writer",collector:Process,
  issuer:Process,descriptor_generation:ID,request:Request,challenge:Challenge,
  assigned_fields:FieldPath[],seal_fields:FieldPath[],
  received_reply:Ref|null,issued_seal:Ref|null,
  state:"armed"|"received"|"sealed"|"consumed"|"invalid"}
```

Runtime alone allocates channel_id after authenticating that actual connection; IDs are never rebound to another connection/incarnation. It arms the entry before collection, freezing complete Request/Challenge bytes, expected producer equal to authenticated_peer, native/writer role, logical collector equal to Challenge.collector, issuer equal to the live Challenge.issuer, current descriptor generation, trusted assigned fact fields and exact allowed Obs<Seal> slots in the expected evidence kind. Request/Challenge agreement includes operation, transfer, generation, binding, action/evidence kind, phase, sequence, nonce and issuer clock/deadline. The logical collector cannot nominate a peer, channel ID, producer role or required-field/Seal-slot set by returning evidence. Each subcollector receives the exact challenge over its assigned connection and issuer barriers bracket its observation interval.

On fact-reply receipt, Runtime authenticates the same live peer again, validates all closed bytes/ref bounds and FactReply producer/request/challenge/time/field bindings, then pins the bytes and records received_reply (type FactReply) before state received. It creates issued_seal (type Seal) from that entry only, records state sealed, and sends the seal frame to the logical collector. A producer-sent seal frame is invalid. Copying a valid Seal to another request/channel/collector/role/slot cannot create a new entry. Exact retries return recorded objects without rearming or reissuing authority; different bytes for the same entry invalidate it. Every present composite Seal must occupy a ledger-allowed seal_fields slot, equal its issued_seal and resolve through its received_reply to the pinned bytes, live channel authentication and complete assigned facts; an unknown required Seal remains non-authoritative. Same bytes read from an unbound file/forwarded channel are insufficient.

Before issuing/consuming VerifiedEvidence, Runtime rechecks these entries together with §3.1 and its own independent native readbacks. Peer/channel disconnect, incarnation/issuer restart, descriptor-generation change, expiry or binding disagreement invalidates unconsumed live provenance; retained bytes are diagnostic, not reconstructed authority. A native-death alternative still needs its own fresh trusted live native producer and complete death/work/alias/queue proof. All entries used by one transition become consumed with the challenge/transition in the existing ledger; only that exact transition may use them. Missing authentication/binding is unknown_identity/binding_changed before effects, unsupported native authentication is unsupported_transfer, and any failure after effects is recovery_required. No seal replaces P1–P8, native historical provenance or actual alias custody.

The following ownership rules apply **recursively to every field** in each listed component; grouping never makes an inner field optional. Structural fields (names, enums, IDs, counts, references, timestamps) are checked against the closed schema and live issuer ledger as well as the native checks indicated. Every Obs is checked for both provenance and its value. Runtime refuses if its required native provider is unavailable.

| Fields / component | Producer and native observation | Runtime independent check / failure |
|---|---|---|
| Header, Request, Challenge, Frame, Ref, FactReply, Seal, FieldCheck, Verification; local SubcollectorEntry | Runtime issuer owns request/phase/sequence/clock and channel/peer assignments; each producer sends FactReply directly; Runtime issues Seal; collector returns composite evidence through C | Recompute canonical hashes; match all IDs/nonces/phase/clock/deadline, live channel/producer and pinned reply/seal bytes under SubcollectorEntry; compare effects to persisted intent/native ledger. Malformed/identity→`unknown_identity`, rebound→`binding_changed`, after effects→recovery pending |
| Binding IDs/controller/paths/realm/original publications/prior state/source fields; Source/SourcePair/PriorImage/Lineage/Publication | Agent derives selection, manifest/module/preimage objects from installed pinned sources under installation operation lock; Runtime supplies actual realm/file/descriptor lineage | C plus Runtime own file/source/publication reads, image hashes, object ancestry/ownership/ACL, existing journal/outer-ID mapping and actual held descriptors. Unknown source/ancestry/prior state refuses; changed binding→`binding_changed`; failed durability→`durability_failed` |
| Manager/Target/NativeState; adapter_version/manager/compare_generation/pending_jobs/routes/routes_complete/invocations in ManagerEvidence and SuppressionReceipt | Agent native adapter uses N: exact systemd user target/bus manager, launchd GUI job/session, or selected Scheduled Task, actual invocations and all activation sources | Runtime own N connection/process checks against Binding, known parser profile and authorized chain. UID/text command output is insufficient. Unknown manager/principal→`unknown_identity`; any live old invocation→`busy_owner`; unknown route/queue/fence→`restart_unfenced` |
| Definition/DefinitionView/Command/Principal/FileObject/FileID; disk_definition/loaded_definition/loaded_disk_equivalent/ownership/mutation_chain and all definition/no-ticket/gate fields | Agent observes complete native exports and exact file/drop-in/environment/security inventory, decoded effective argv/principal, loaded state and ownership; publishes only owned authorized deltas | Runtime rereads native effective configuration and pinned files, checks complete decoder coverage, source/argv/principal, loaded equivalence, intent and guarded chain. Unsupported/uncertain effective semantics refuse. Drift/replacement→`binding_changed`; no safe mutation guard→`unsupported_transfer` |
| Containment/Census/Process/Scope and all census/no_old_tree/member/escape/history/seal fields | Native custodian's authentic original-launch witness plus current native scope/member/death/visibility observations; Agent collects via N/C | Runtime validates original producer and retained provenance, independently observes current scope and all possible descendants/inheritors with no hidden/unreadable/racing member. Missing history/visibility/escape coverage→`incomplete_census`, live owner/child→`busy_owner`. Current emptiness cannot repair missing history |
| Suppression before/independent_guards/owned_deltas/denial/restart/triggers/native_readback/release_pending/preimage; OwnedDelta/DeltaObject/MutationGuard/Route | Agent mutation adapter: durable exact preimage/intent, owned native denial/links/guards, effective native readback and replacement-safe ownership | Runtime rechecks pinned independent guard, effective denial/all routes, empty pending jobs, exact preserved preimages and exclusion mechanism through N/C. Repeated snapshots do not prove atomicity. Unknown→`restart_unfenced`; replacement→`binding_changed`; retain gates/recovery after effects |
| NativeClose writer/role/db_closed/workers_drained/durable_work_safe/runtime_seal; Ready source/endpoint/services/adoption/descriptor/semantic fields; Endpoint/ServiceCheck | Installed writer/helper produces DB/drain/adoption/semantic/endpoint facts over W; Runtime issues the bound Seal and owns descriptor ledger and one-writer state; Agent owns native invocation/census part | Runtime checks its own channel/FD ledger, authentic writer and independent actual endpoint/source/native identity; work safety uses unchanged Runtime durable interruption checks. Missing drain/alias closure→`lease_uncertain`; busy durable work→`busy_work`; false readiness refuses and keeps custody |
| Alias, alias inventory/channels/zero_writers/one_owner, Close/Ready native_seal | Runtime owns actual syscall/send/receive/close/queue lineage; native custodian owns complete recipient/inheritor death/scope facts | Runtime uses actual owned live channel/handle state, independently checked W acknowledgments or complete N death proof. Validate every alias, not just writer PID. Unknown delivery/inheritor/queue→`lease_uncertain`; never serialize a claim of lock ownership as proof |
| DefinitionProof ready/terminal digests and DetachReceipt all fields | Agent durable decision + ordinary native publication; exact writer records accepted detach and ACKs over W | Runtime rereads Agent journal/native definition and verifies same writer's accepted sequence/ACK, still held owner and closed directory. Lost ACK→query/reconciliation, no second start or release |
| PriorStateProof all comparisons/prior/restored/close/suppression/delta/lineage/publication fields; Comparison | Agent restores authorized preimages/data policy while native independent suppression remains effective; Runtime contributes zero-writer/close proofs | Runtime independently rereads data-policy verification, selection/catalog/publications/native state under locks, checks all six comparisons, unchanged suppression across restoration and exact authorized lineage. False/unknown restore→`recovery_required`; no automatic prior R0 activation |
| ReleaseProof decision/detach/prior/alias/removal/native-state/definition/replacement/subsequent-launch/disposition fields; DecisionRef | Agent durable terminal decision and owned cleanup N readback; Runtime actual broker alias close; selected Runtime retains live owner on commit | Runtime matches durable decision+detach/prior reference, actual alias accounting, all owned cleanup/native final state and any replacement/subsequent incarnation. Unknown cleanup or nonexact outcome remains release-pending; no success based only on terminal state or Agent result |

§3.1 is the common outcome rule for every producer and verifier: observed/verified requires code none and empty faults, non-success requires non-none code and nonempty faults, unsupported requires unsupported_transfer and an unsupported_native fault, and recovery_required retains recovery_refs. Phase-inapplicable unknowns never become positive native facts. Failure priority: preserve any observed replacement/identity uncertainty and effects; before effects, report the first missing required predicate in P1–P8 order, with all collected faults. Any mismatch forces Runtime rejection even if another predicate has higher reporting priority. Never infer absence by retrying. Runtime's unchanged native lock/work checks remain authoritative even when all external fields verify.

## 7. Seven adapter entry points and call order

These are the seven D methods, with the common keyword-only envelope/challenge added to their private implementation. Existing positional meaning is unchanged. They run in the installed Agent adapter under the existing operation authority. No call recursively enters ordinary Runtime bootstrap/down while the broker holds its mutex. Agent facade owns adapter invocation; broker issues challenges and consumes verification, avoiding mutex-held callbacks that would reacquire bootstrap/DB locks.

```text
snapshot(binding, *, request:Request, challenge:Challenge) -> ManagerEvidence
install_suppression(expected, provisional, *, request:Request, challenge:Challenge) -> SuppressionReceipt
verify_suppression(receipt, *, request:Request, challenge:Challenge) -> ManagerEvidence
register_candidate(expected, armed_reference, *, request:Request, challenge:Challenge) -> ManagerEvidence
restore_while_suppressed(expected, preimage, *, request:Request, challenge:Challenge) -> PriorStateProof
publish_ordinary_candidate(expected, *, request:Request, challenge:Challenge) -> DefinitionProof
remove_owned_suppression(expected, terminal_decision, *, request:Request, challenge:Challenge) -> ReleaseProof
```

The common sequence for **every** adapter call is: persist exact Agent intent if mutating → Runtime checks live holder/state, creates challenge and arms required SubcollectorEntry records → Agent performs challenged native observation/owned action under §4's replacement guard → each required independent producer sends fact-reply directly to Runtime → Runtime validates/pins it and returns seal to the logical collector → collector sends complete composite evidence via C → Runtime independently collects N/W readback → enforce §§3.1/5/6 and verify/consume local VerifiedEvidence at that one boundary → persist observed outcome. All seven entry points use the identical outcome/check rejection rule, with no adapter-specific success shortcut: snapshot, install_suppression, verify_suppression, register_candidate, restore_while_suppressed, publish_ordinary_candidate and remove_owned_suppression. collect_close/collect_ready/collect_detach and every later consumer enforce it too. Any mismatch, malformed outcome, missing/non-match check or failed required predicate blocks the transition; after effects it retains recovery-required state. The challenge alone never allows mutation; existing operation authority, verified expected state and valid retained session are also required. `verify_suppression` and `snapshot` are read-only. A missing verifier/producer returns structured unknown/unsupported before mutation.

| Step / phase | Calls and required verification |
|---|---|
| 1 / OBSERVED → PREPARED | Reconcile existing unfinished/release-pending operation before staging/fetch/new intent. Assemble immutable source/native/preimage Binding from initial read-only observations; D `begin_transfer` allocates the control-only live issuer, without realm permission. `snapshot(binding)` with fresh challenge, then independent verification of source, effective definition, old launch history/census, prior state and preimages. Unknown binding facts allow diagnostic collection only and refuse progress; a later complete binding requires a new reconciled attempt/generation, never editing an active binding. Live/unknown R0 with no supported stop path refuses here without manager stop |
| 2 / HELD_PROVISIONAL | D `pin_and_acquire_original`: broker pins directory then actual original owner.lock, NB acquire and post-open IDs/publications. It retains original descriptors. SessionRef can be checked only against this holder. Provisional custody is not stopped admission |
| 3 / RESTART_FENCED | `install_suppression(expected, provisional)` while originals remain held; independently verify all-owned denial/restore capability, then `verify_suppression(receipt)` for fresh exact native manager/definition/census/pending-job state. D `admit_stopped_r0` consumes P1–P8 verified facts plus actual live custody/work check. No marker/discovery deletion or fabricated stop proof |
| 4 / PREPARING_DATA | Sequential D `arm_writer`/`claim_transfer` for supported offline backup/migration; independently authenticate helper scope/source before FD delivery and DB access. Consume NativeCloseProof after each helper before arming another role. No temporary old Runtime or concurrent uncertain helper |
| 5 / SELECTED → READY | Persist selection/registration intent; `arm_writer(candidate)` before `register_candidate(expected, armed_reference)` (launchd may autolaunch). Verify native candidate definition and separately ticket-only suppression transition. Authenticate claim via W+N, deliver actual aliases, adopt before DB, close directory alias. D `record_ready` verifies composite ReadyProof, required services and one owner |
| 6 / COMMIT_DECIDED | Prepare intended persistent native policy under independent release-pending gate; durable `complete` decision with release_pending. `publish_ordinary_candidate(expected)` publishes/readbacks no-ticket ordinary definition. D `detach_committed` sends bound terminal/ready/definition decision, exact writer durably accepts/ACKs; verify DetachReceipt |
| 7 / release | After accepted detach, broker closes only its required owner/directory aliases, never LOCK_UN, and resolves all other directory/queued copies; final writer keeps owner alias. Retain bootstrap/control/installation authority. `remove_owned_suppression(expected, decision)` requires §4's indivisible ownership comparison/removal for every delta and §5's covered terminal race boundary; read exact final native state. Verify ReleaseProof; D `finalize_release` may mark release observed only after those checks and exact-incarnation reconciliation of any racing launch, then release remaining control authority |
| rollback / ROLLBACK_INTENT → ROLLBACK_DECIDED → release | Keep custody, drain only exact admitted candidate/helpers, consume complete NativeCloseProof. Reinstall/read back all-managed independent suppression **before** restoring any unguarded old action. `restore_while_suppressed(expected, preimage)` verifies authorized data/selection/definition and exact prior stopped/load/enabled state under §4's guard. Durable rolled_back+release_pending and D `rollback_decision`; close final owner/directory aliases after zero writers. Remove only owned suppression under §§4–5; a racing launch keeps release pending until independently observed/reconciled and never proves exact-stopped restoration. Gate removal after the durable decision is the authorization point for subsequent ordinary R0 launch, not operation completion; no R0 restart by rollback |

Freshness/replay barriers also apply before selection publication, suppression relaxation, helper succession, detach, final alias close and each guard removal. Query is observational, may return the old recorded evidence and never changes phase or mints a fresh verification. Missing detach/directory-close reply, broker/collector crash or reboot invalidates live verification: reconcile exact operation/generation/journal/native state; retain uncertain writer occupancy. A new holder requires actual original NB acquisition and the complete conjunction again. The journal and Runtime ledger may record decisions while `release_pending=true`; normal activation/new operation remains blocked until native cleanup is positively observed. Never claim independent suppression existed before its installation readback.

## 8. Native feasibility and protected review questions

| Observation / mechanism | Current disposition / required owner decision |
|---|---|
| Linux authenticated user-manager peer and complete effective argv/principal/environment/loaded-definition export | Not supplied by A; systemctl text/NeedDaemonReload and file digests do not supply it. M-AG/M-RT must name a real native transport/decoder/credential API and Runtime's independently usable readback path, including manager restart/namespace races. Until then unknown_identity/unsupported_transfer |
| Authentic original-launch containment, retained membership/inheritance history and complete escaped/reparented census | No witness established in named source/artifacts. M-AG/M-RT must identify a producer that existed for the authentic old launch, its retained trust chain and complete current visibility. It may require a newly witnessed authentic reproduction. Existing unwitnessed installations stay incomplete_census; a new scope cannot cure old history |
| Linux effective independent denial, exhaustive trigger set, loaded/disk equivalence and replacement-safe mutation/restore | D's drop-in/condition plan is a proposed mechanism, not native proof. No demonstrated indivisible owner+generation comparison/mutation for every file/manager delta or independent-editor exclusion across replacement/reload/restart. §4 refuses before effects or retains recovery after effects; preserve replacements. M-AG/M-RT must name the concrete native mechanism and limits |
| Terminal gate-removal/readback/release-observation race | No complete native launch/epoch ordering and reconciliation mechanism established. §5 requires retaining suppression before removal if unavailable; any racing/unknown launch after effects remains release-pending/recovery-required until its exact incarnation is independently reconciled. Clean samples do not prove interval absence |
| Runtime independent verifier, private artifact store, cross-process broker/SCM_RIGHTS adoption, queue accounting, close-only exceptional disposal, authenticated detach | No positive path in published R; supplemental refusal-only candidate does not supply these. M-RT must expose real implemented sources and focused native proof. Missing pieces retain unsupported_transfer/lease_uncertain; no object type alone advances HELD_ADMITTED |
| Unix offline predecessor backup/migration and root-changing lineage | Separate RT3 feasibility; verified predecessor-compatible snapshot and overlapping old/new object custody are required. This schema is not backup verification or permission to modify source/realm |
| macOS 15 effective disable/restore, complete historical descendant scope, audit-token peer/native job binding, Darwin descriptor custody | Unproven in this task; no invented equivalent to Linux cgroup history. M-AG/M-RT must name supported native observations and prove restart suppression/close-only adoption; missing observations refuse |
| Windows original exact-launch Job/drain receipt and LockFileEx custody | Positive transfer remains unsupported. No retrospective receipt from current census, no SCM substitution for Scheduled Task, no DuplicateHandle/POSIX-equivalence claim. M-RT/M-AG must separately resolve original producer reachability and same-process lock placement before proposing positive authority |
| Prior running R0 restoration, arbitrary same-user adversary, unqualified filesystem | Outside the narrow accepted positive route; refuse unavailable variants. Trust boundary remains trusted installed code/installation OS user, not protection from arbitrary same-user ptrace or code deliberately bypassing advisory locks |

Protected **M-RT** review: accept/reject exact bytes/envelopes/unknowns and local VerifiedEvidence consumption; identify real native verifier and independently authenticated manager/census sources; confirm alias inventory/death-or-close/queue evidence and detach/release ordering; state which predicates remain unimplementable at the named Runtime head. Do not approve placeholder evidence as admission.

Protected **M-AG** review: accept/reject seven facade signatures and per-field producer ownership; identify native effective-definition/credential/route completeness and manager authentication APIs; identify an authentic historical containment producer or explicitly retain incomplete_census; show the ownership mechanism that preserves independent replacements and stopped rollback across terminal fsync/gate removal. A pre/post file comparison is not that mechanism.

Joint decisions requested: (1) approve or revise this proposed v2 and its closed fact-reply/seal directions and SubcollectorEntry binding, with no v1 fallback, (2) name each real collector/verifier API with exact supported native versions/limits, including every replacement-safe mutation and terminal launch-race boundary, (3) explicitly list unsupported observations and refusal code, (4) require source/negative focused checks and an authentic non-root native result in subsequent implementation lanes. This design task runs no native operations or product tests. Protected review of D or the earlier wire head does not approve this packet or prove any native predicate. The report/receipt carries the published design-only head for direct review; implementation authority waits for both owners' protected interface disposition.
