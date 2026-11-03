# SR-PROJ — project review packet

**Packet state:** skeleton populated; project review not started.
**Review gate:** Q, triggered at I-08 freeze.
**Review owner:** M-INT.  **Review tier:** one Astra project review, at most three rounds.
**Current decision:** PENDING — no approval is implied.  The packet is not an I-08 freeze, RC selection, qualification, publication, or go/no-go record.

## 1. Review scope and rules

SR-PROJ covers the whole release candidate tuple and its protected journey evidence. Under `TASKS.md` §Revision 3 it replaces RV-1, RV-2, and RV-3 when the I-08 freeze exists. Do not run formal RV-1/RV-2/RV-3 ceremonies for this packet. R-32 (RV packet preparation) is cancelled. R-33 remains relevant for its other work, including RV-4 unless the current project-review instructions supersede it; D6 replaces RV-4 human review with a one-page summary. R-34/RV-5 remains the final evidence aggregate gate.

The review is not eligible yet: current records show no I-08 freeze/RC receipt. The review should start only after I-08 selects a coherent, published RC tuple and its manifest, generated outputs, installed identities, and receipts. Review criterion: at most three Astra rounds and no open blockers. Record findings, severity, owner, linked remediation task, and closure evidence per round. No finding is closed by a design approval, publication SHA, preparation packet, worker marker, or broker transport status alone.

## 2. Candidate identity binding

### Current round-1b published tuple (reference only; not RC/frozen)

| Component | Exact published identity | Binding state |
|---|---|---|
| Runtime | `1a275bb339ae897e52a5be5e1da99e846e402037` | PUBLISHED; round-1b reference |
| Agent | `ddd5d278796641901bd11508863a8ea21e6c9697` | PUBLISHED; round-1b reference |
| App | `7f6f99208100e895efebf39c70bc30af6cee0884` | PUBLISHED; round-1b reference |
| Pinokio | `89ed7aacae72bb48eb97ff9ff95c222bdd7e71e4` | PUBLISHED; round-1b reference |
| VibeComfy | `3bebae39655eb460454885040be4d3aefaf2ef31` | Published source reference in older ledger; current CP3 pin is reported as moving to `29670339`; final RC pin PENDING |
| lifecycle artifact | `96360a27a53c4608f780f7b370f747a2aa3c7ca8` | Older ledger reference only; final RC binding PENDING |

### Runtime CP2 dependency (not a complete tuple)

| Component | Exact identity | Binding state |
|---|---|---|
| Runtime CP2 | `47e5949465cb6f3afdfd1cb8a0e90009fd9d4020` | PUBLISHED; M-RT RunPod proof accepted per DECISIONS; not sufficient to claim coherent CP2 tuple |
| Agent CP2 | PENDING | M-AG2 / M-AG assembly, repin and publication receipt required |
| App CP2 | PENDING | M-APP repin and publication receipt required after Agent |
| Pinokio / VibeComfy / lifecycle | PENDING | Bind exact published identities for selected checkpoint |

### Newly published Agent #155 and App #149/#150 checkpoint (2026-11-02; reference only, not RC/frozen)

| Repository / checkpoint | Published `int/r1` commit | Tree | Ordered parents | PR / change | Proof reference and provenance |
|---|---|---|---|---|---|
| Agent #155, `AG-SCHEMA-DACL-ALLOW-R4` | `2de0726d1eaea7f7bd141251e088aa9fdf224525` | `268fae95174047b79664d27fe19d97c6e2e6e837` | `6e46e3bb45ec3feddbd0b0617a50a157e3a4df67`; `e0935655897c513eec85e63d4595f8d30982e050` | PR #155; schema ACL default-owner product change | Exact-head Windows proof run `41441961556`, attributed by M-INT notice at `2026-11-02T12:12:35Z`. Run reference is relayed; no hosted run artifacts/details were mounted for independent inspection in this task. |
| App #149, `I-AP-08-SPLIT` | `2e2b67d47f7162b2f432394d51f04074f1d6f65d` (merge included in App head below) | `f85e207c4931bf7a20b003996192a6b3b60d71e7` | `5c95abb00525e1a77d00c32042b6453d9f14f4e7`; `00665cf9bd6446c143b6f26e664d8d23e4300d8b` | PR #149; tests/script split | No separate proof run is identified in the notice. Do not treat this checkpoint alone as a new I-AP-08 acceptance. |
| App #150, `I-AP-17-R6` | `bf456ac1fec704fbd0c637dd055608a095975c42` | `540f026f837ee9a7e3aa76ff17cfec1cddf1e21e` | `2e2b67d47f7162b2f432394d51f04074f1d6f65d`; `f228beee5841c256bef7db1b00f70553ca12ce19` | PR #150; Direct Journey permission explanation | Hosted protected proof run `41410178199`, attributed by M-INT notice at `2026-11-02T12:14:23Z`. Run reference is relayed; no hosted run artifacts/details were mounted for independent inspection in this task. |

The SHAs, trees, and ordered parents above were inspected with `git show`/`cat-file` in the mounted M-OPS Agent and App repositories. The publication attribution and run IDs come from the cited M-INT notices. No proof result, artifact content, or pass detail beyond the notice wording is asserted here.

**Manifest tuple / re-pin:** At relay time the App manifest still pinned Agent `e425903f7a9fa5b82973281d15054c0716b46e2d` and Runtime `5453366458f7b4ee874c94d15ae7952e3e82d3c3`. M-INT requested M-APP to regenerate/re-pin the manifest to published Agent #155 `2de0726d1eaea7f7bd141251e088aa9fdf224525` (keeping Runtime `5453366458f7b4ee874c94d15ae7952e3e82d3c3`) on top of App `bf456ac1fec704fbd0c637dd055608a095975c42`; the request is **NOT LANDED** in the latest supplied notice. Thus the latest App integration head and latest Agent integration head are individually published, but the manifest pin is stale and does not bind the new Agent checkpoint. No coherent tuple, generated-manifest freshness, or installed-identity conclusion follows.

### I-08 final identity fields — fill from receipts at freeze

| Required field | Value / status |
|---|---|
| RC label and I-08 freeze receipt ID/path | PENDING — M-INT; I-08 prerequisites and coherent I-11 chain |
| Runtime / Agent / App / Pinokio full commit IDs | PENDING — exact selected RC tuple; do not copy reference tuple without freeze receipt |
| VibeComfy source, full commit, and pin/lock location | PENDING — M-GPU / M-INT; CP3 source pin and installed identity evidence |
| lifecycle artifact source, full commit, and installed identity | PENDING — M-INT / relevant artifact owner |
| App public manifest path, SHA-256, embedded release number and pins | Latest Runtime product head reported by M-INT at `ORCHESTRATOR.md` 2026-11-02T13:05:56Z is `30674b3f4203c5d347fe9e900f7960731148a698`. Agent re-pin then App manifest re-pin are required; evidence that either downstream pin landed is not present here. The #152 App head's manifest still records Agent `2de0726d1eaea7f7bd141251e088aa9fdf224525` / Runtime `5453366458f7b4ee874c94d15ae7952e3e82d3c3`; this is an earlier tuple. Exact manifest hash/freshness, installed identities, I-11 receipt and I-08 freeze remain pending. |
| Runtime lock/vendor and Agent generated artifacts / freshness receipt | PENDING — M-AG / M-APP; C-07 and I-11 chain |
| Pinokio package/artifact digest and effective installed version | PENDING — M-PIN; C-07 and selected RC |
| Installed identities observed on each protected journey | PENDING — journey owner; bind to manifest and tuple |
| Artifact SHA-256 inventory (installers, bootstrap, packages, manifests, models as applicable) | PENDING — owners / M-INT; exact bytes and source identities required |
| Repository, branch, merge commit, parent SHAs, and documentation base | PENDING — M-INT freeze receipt; Agent #155 and App #149/#150 identities/parents are recorded above as intermediate published checkpoints only; documentation base supplied for this task is `7873b0a00bb01aa2509efba046d048719fdbe4c4` |

## 3. Protected journey and evidence index

Statuses below are the latest sourced state available for this packet. `PENDING` means no qualifying current-RC evidence is established here. Preserve source receipts and exact run IDs when filling the index.

| Obligation | Owner / gate | Known evidence and disposition | Current packet status / dependency |
|---|---|---|---|
| App #152 activity-label segment (F2 marker presentation in active/completed task surfaces) | M-APP / applicable App proof | Source review of App `1bfbfcc7fb633551dc4f76f16a522ce6d0b99379` found no concrete defect; no exact-head test/journey receipt was identified, and actual Runtime spec family/end-to-end behavior remain unproven. | **AWAITING EVIDENCE** — M-APP owns applicable exact-head focused proof; M-INT/M-QUAL assess evidence and qualification. Not a gate closure. |
| App update guard and Runtime bridge/outbox: failed save causes no POST; successful save exactly one POST; restart replay; observational GET | M-APP / I-AP-08 (F) | DECISIONS entry 12:36Z records acceptance on round-1b Runtime `1a275bb3`, Agent `ddd5d278`, App candidate `0c36714a` (= published App `7f6f9920`); harness/evidence head `ffcdb8bc`. New #149 review finds no exact-head I-AP-08 execution/artifacts; landed runner rejects App `bf456ac1` and does not establish prebuilt distribution provenance. | **ACCEPTED on round-1b tuple only; new #149/#150 checkpoint evidence gap remains OPEN.** Preserve prior receipt/run ID; M-APP supplies exact-head proof, M-INT assesses evidence. Revalidate/invalidate if selected RC differs. Older matrix row is stale relative to later decision. |
| Nine OS × channel direct install/upgrade journeys (R-37 inventory) | M-QUAL / R-37 (F), C-15 (F) | R-37-PREP-R10: 34-row inventory preparation, 9/12/9/4 family counts; all execution receipts missing. R14 confirms preparation only. | **PENDING** — M-QUAL / M-CI; bind final approved inventory to coherent RC and run all required slots. |
| M4-5 scenario group | M-QUAL / R-37 (F), C-15 (F) | Preparation inventory contains 12 rows; no accepted execution receipts. | **PENDING** — M-QUAL / M-CI; execute and authenticate on RC. |
| M4-6 Pinokio slots | M-PIN / R-24/R-25 and R-37 (F), C-15 (F) | Preparation retains all six D9 slots; no six-slot current-RC receipt. The 2026-11-02 focused source review of Pinokio R-21 diagnostic workflow candidate `3922f3f38feaaa62dc21d992c40092b30b480029` records F1/F2 OPEN and F3 addressed at source/fixture level with hosted proof outstanding. The prior correction candidate and its review are historical; run `41444352245` is bound to older head `ee790eed840e7d1e3594be46455de090684097c4` and an older tuple, and is not current-head evidence. | **PENDING** — M-PIN / M-QUAL; six authentic current-RC slots remain required under D9 (or an explicit owner decision narrowing the end state); R-21 findings require correction/review and bound evidence. |
| Protected cross-cutting obligations (4 rows in R-37 inventory) | M-QUAL / R-37 (F), C-15 (F) | Inventory only; executions absent. | **PENDING** — M-QUAL / M-CI; exact obligations and source receipts to be indexed. |
| Populated-data install/migration, backup, interruption, restore and rollback | M-RT / I-RT-04 (F) | Current decision defers v34 custody attempt to post-release; schema stays v33 and tracked-via-Runtime installs remain disabled/fail closed. Existing I-RT-04 protected evidence must be bound to chosen supported install path; deferral condition requires promised Phase 1 install behavior and protected journey proof. | **PENDING / SCOPE LIMITATION** — M-RT / M-INT; do not claim v34 migration proof. Reopen if supported install behavior is not proven. |
| Timeline persistence and render evidence, including T-04 / T-11 | M-TL / T-04, T-11 (Q); C-15 (F) | Current R14 says T-04 R3 is parked with no terminal report and rehearsal-only binding; R-34 aggregate receipt absent. T-11 final installed-pack proof is RC-bound. | **PENDING** — M-TL / M-AG2; terminal T-04 and installed-pack T-11 receipts on coherent RC required. |
| GPU / RunPod target journey, official smoke and exact media/task/no-touch/release evidence | M-GPU / G-08 (Q), G-07 | Current decision says one live GPU RC batch; older evidence ledger records no G-08 receipt. | **PENDING** — M-GPU / M-INT; one authenticated batch on exact RC tuple. |
| Windows CPU attached-ComfyUI journey | M-CI / G-12 (Q) | R14: named Actions run `41288018935` but no local job artifact or terminal evidence. | **PENDING** — M-CI / M-VENUE; authenticated terminal receipt bound to RC. |
| Stop-proof and custody behaviors | M-AG2 / I-AG-03b and related protected obligations | v34 custody attempt deferred; genuine stop-proof fixes remain in scope per 12:10Z decision. | **PENDING** — M-AG2; establish applicable exact-head checks and current receipt before review. |

## 4. Segment review findings

These are completed focused segment reviews of published intermediate checkpoints, not SR-PROJ/Astra project-review rounds. They do not start the freeze-triggered review or close any gate. The disposition below carries the review receipts as written and preserves evidence/provenance limits.

### Agent #155 — AG-SCHEMA-DACL-ALLOW-R4

- **Identity/provenance:** Published Agent merge `2de0726d1eaea7f7bd141251e088aa9fdf224525`, tree `268fae95174047b79664d27fe19d97c6e2e6e837`, ordered parents `6e46e3bb45ec3feddbd0b0617a50a157e3a4df67` and `e0935655897c513eec85e63d4595f8d30982e050`. Review receipt confirms the candidate and merge trees match, which establishes source-tree equality only. M-INT relay names run `41441961556`; the primary API snapshot examined by the review shows the run was still in progress at `2026-11-02T12:17Z`, with no terminal run conclusion.
- **Disposition:** `AWAITING_EVIDENCE`. The Windows job succeeded on the tree-identical candidate, but selected commands omitted `tests/core/rendering/test_local_schema.py` and the new native schema-DACL test. No applicable Windows result for the changed native ACL policy and installed source/destination path is established. Do not infer that omitted tests failed or passed, nor infer installed-byte identity or merge-SHA execution.
- **Open follow-ups / routes:** **M-AG** to clarify/fix the current-user ACE mask predicate (a zero or unrelated mask currently marks `user_access`) or document intended semantics, and strengthen native Windows negative controls. Existing owner monkeypatch coverage is bypassed in the Windows branch, and `chmod` controls do not construct native foreign-write ACEs. **M-AG / M-VENUE** to provide applicable node-level native Windows schema-policy and source/destination results with inventory, inner results and candidate/merge or installed-byte identity; **M-INT** decides any proof carry. **M-AG / M-CI** to classify and resolve or validly adjudicate the observed `make ruff` failure (2,402 findings vs 2,128 baseline, exit 2); parent attribution was not established and mypy/import-cycle steps were skipped. **M-APP / M-INT** retain the manifest re-pin/freshness and applicable protected-evidence route; **M-PIN** retains the requested R-26 Windows follow-up.
- **Limits:** Windows inner selections logged 10, 23 (26 deselected), 36 and 21 passing tests, but none covers the omitted new native ACL test. The Windows job success is not a full run success; the separate fast-check failure is unresolved. Review receipt: `queue/done/O-03-AG-155-REVIEW/{REPORT.md,review-receipt.md,RECEIPT.json}`.

### App #149/#150 — I-AP-08-SPLIT and I-AP-17-R6

- **Identity/provenance:** Published App merge `bf456ac1fec704fbd0c637dd055608a095975c42`, tree `540f026f837ee9a7e3aa76ff17cfec1cddf1e21e`, ordered parents `2e2b67d47f7162b2f432394d51f04074f1d6f65d` and `f228beee5841c256bef7db1b00f70553ca12ce19`. The review verified these identities from retained Git objects. M-INT relay attributes hosted run `41410178199` to #150; reviewed logs bind it to App `80632cfa63a099d6d5e8605589be10f200051e79`, Agent `e425903f7a9fa5b82973281d15054c0716b46e2d`, Runtime `5453366458f7b4ee874c94d15ae7952e3e82d3c3`. That App is not an ancestor of the reviewed merge and its tree differs in workflow, manifest, lock, bridge script and spec.
- **I-AP-08 exact-head evidence gap:** The landed runner hard-codes App `5c95abb0`, exits 65 at `bf456ac1`, and accepts a prebuilt App distribution without proving it came from the claimed App commit; the archived/imported Agent source is harness `6e46e3bb`, while `e425903f` is only the manifest label, without source-equivalence verification. No I-AP-08 execution/artifacts were in the supplied hosted logs. **M-APP** to correct/provide exact-head protected proof binding actual built App and candidate/harness provenance and preserving failed-save/no-POST, successful-save/exactly-one-POST, restart replay and observational GET assertions; **M-INT** to assess evidence for this checkpoint. This is missing evidence/runner binding, not a finding that the guard failed.
- **Observed hosted failures / routes:** Direct Journey run `41410178199` reports 16 passed, 1 failed, 26 not run: a second writer received HTTP 410 instead of expected 200 before draft-conflict assertions. Route to **M-APP** for triage and **M-RT** if a Runtime cause is identified; **M-CI / M-INT** track the red run and obtain applicable proof. Three-products reports 2 failed, 13 not run: Agent-to-open-App expected `F2 Marker, Queued` or `F2 Marker, Ready` but got `Marker, Queued`; native-None transition expected `parent/clip:f2-transition-clip` but got null, including retry. Route to **M-APP / M-TL**, engaging **M-RT or M-AG if implicated**. Causes, base-identical status and attribution to these PRs remain unresolved.
- **Narrow permission evidence:** Permission cases passed on non-ancestor App `80632cfa`; the three changed permission paths are byte-identical to the reviewed head. This supports partial simulated-prompt behavior only. It is neither exact-merge execution nor real Chrome-prompt evidence; “fresh” permission state used seeded tour-seen local storage. The governing known-failure row remains open; route exact-head proof to **M-APP** and ledger disposition to **M-INT**.
- **Disposition and limits:** Protected evidence incomplete; no gate closure. Logs show a red run and omit I-AP-08 proof; their snapshot does not establish a complete hosted-run conclusion. Do not extend the earlier I-AP-08 acceptance beyond its separately recorded round-1b tuple (Runtime `1a275bb3`, Agent `ddd5d278`, App `7f6f9920`). Review receipt: `queue/done/O-03-APP149-150-REVIEW/{REPORT.md,review-receipt.md,RECEIPT.json}`.

### Pinokio R-21 — prior correction candidate source review

The terminal focused static review examined `banodoco/astrid-pinokio` candidate `ee790eed840e7d1e3594be46455de090684097c4`, tree `3df0fb91983f1c2951d5299d4a9cb259c964e49f`, against parent/base `42f13b86512b5b9f980ad18c5fd7882168a43d56` (parent tree `3f0ab40b21762e6510fa174a481a621b0ea8352b`). This is a correction candidate, not the published Pinokio component in the latest tuple (`3a6a98ae251ca126505245286e2d3e760f4348e9`) and not an I-08-selected RC. **Source disposition: F3 addressed; F1 partially addressed with coverage open; F2 open. R-21 protected/hosted closure is not established.**

- **F1 — partially addressed; M-PIN:** Candidate hashes regular-file contents and directory entries, and checks digest equality across first click; changed fixtures cover regular-file mutation and staged versions. Remaining gap: symlink target bytes are not covered, while selected manifest matching follows the path. A selected version/program reached by an unchanged symlink can be modified or removed without changing the recorded installed-byte digest. No evidence establishes whether hosted installed trees contain such links. M-PIN must reject unsupported links or safely bind/hash effective targets, and add positive/negative deletion, missing-selected-state, link-target-mutation and link-target-removal cases (or provide reviewed enforcement/evidence that links cannot occur). Equality must not be presented as independent release authentication.
- **F2 — open; M-PIN:** Local health/schema/CORS/preflight/handshake/scope/realm probes are useful contract checks, but `applicationReady` still accepts arbitrary public HTTP 2xx. The observer does not identify or execute the candidate App/browser client, or observe its connection to this Runtime; a wrong/broken public page plus compatible local Runtime can still yield readiness. M-PIN must bind and observe actual candidate App-to-this-Runtime connection and add a healthy-page/wrong-or-broken-App negative case, or narrow the receipt/verdict to contract probing and leave actual App readiness unproven.
- **F3 — addressed at source level; hosted evidence still required; M-PIN:** Observer/publisher retain request/run-bound native Install details and verifier requires URI, zero unsignalled PTerm exit, stop/disconnect, distinct integer action ID, explicit idle state, dispatch/completion, and explicit false engine-error and launcher-failure flags. Changed fixtures reject missing/wrong/unsuccessful Install and error-with-zero-PTerm cases. This source disposition does not establish that a real Install succeeded.

**Actionable closure route:** M-PIN owns the correction and must supply a correction head that resolves F1/F2, meaningful positive and negative focused cases, inspectable producer/test logs (the `66` reported focused passes are only an M-PIN relay, not independently verified), and source review. Then provide matching request-bound hosted evidence for the actual reviewed correction head: immutable request, runner/workflow SHA, ref, run ID/attempt, candidate tuple and manifest binding, matching sanitized receipt/verdict/checksums, successful setup/journey/watchdog, native Install, protected-state comparisons, first refusal, Stop, fresh retry, readiness claim within its evidenced scope, and cleanup. M-PIN must route any downstream carry/tuple decision to the responsible coordinator; evidence for the candidate does not by itself qualify the current RC.

No producer log/receipt or hosted result for candidate `ee790eed840e7d1e3594be46455de090684097c4` was supplied. The reported 66 passes are not independently verified, and no effective installed bytes or actual candidate App connection was observed. At the 2026-11-02T13:05:56Z M-INT notice, Runtime `30674b3f4203c5d347fe9e900f7960731148a698` is the newer published product head. Agent re-pin and then App manifest re-pin are required; neither downstream pin update is inferred as landed. The prior App/Agent/Runtime/Pinokio tuple remains historical publication context only, not an I-08 freeze or qualification. Six authentic Pinokio M4-6 slots remain PENDING under R-25/R-37/C-15 and D9. See the detailed review in `queue/done/O-03-PIN-R21-FIX-REVIEW/REPORT.md` and `review-receipt.md`.

The SR-PROJ round table remains unstarted until an I-08 freeze selects a coherent RC tuple:

| Round | Reviewer / date | Finding ID and severity | Finding / affected identity | Owner / remediation | Closure receipt | Status |
|---|---|---|---|---|---|---|
| 1 | PENDING — Astra at I-08 | PENDING | PENDING | PENDING | PENDING | NOT STARTED |
| 2 | PENDING if needed | PENDING | PENDING | PENDING | PENDING | NOT STARTED |
| 3 | PENDING if needed | PENDING | PENDING | PENDING | PENDING | NOT STARTED |

## 5. Known ledger dispositions and gate boundary

- The cited `runpod-evidence/GATE-MATRIX.md` snapshot is dated 2026-11-01T07:52:08Z. It labels the then-published `T-PUB` as not coherent/frozen and has no green rows. Its tuple fields are stale for round-1b and it predates the 12:36Z I-AP-08 acceptance.
- The later task-local `M-QUAL/queue/done/GATE-MATRIX-R14/GATE-MATRIX.md` binds T-R1B to Runtime `1a275bb3…`, Agent `ddd5d278…`, App `7f6f9920…`, Pinokio `89ed7aac…`. Its report says 95 unique rows, zero green/accepted/qualified/frozen rows, and explicitly says Runtime CP2 does not prove Agent/App CP2 repins or a coherent CP2 tuple. It remains a task-local fail-closed ledger, not the global source of acceptance.
- DECISIONS at 12:36Z is later than R14 and records I-AP-08 accepted on the published round-1b tuple. Record that narrow acceptance while retaining the R14 snapshot's other dispositions; reconcile with a refreshed canonical matrix when available.
- I-08 remains **MISSING**: selector/checker preparation is not freeze evidence. I-11 remains **MISSING** for a coherent Runtime→Agent→App checkpoint with current pins, generated-artifact freshness, and installed identities. Agent #155 and App #149/#150 are published intermediate checkpoints, but the App manifest still pins the prior Agent at relay time and the requested re-pin has not landed.
- R-36 remains **BOUNDED POSITIVE / NOT ACCEPTED** in R14: synthetic predicate tests do not supply authenticated producer observations or aggregate closure. R-37 is **PREPARATION_ONLY / NOT ACCEPTED**. R-38 is **PREPARATION_ONLY / NOT ACCEPTED**; its report and run sheet are pending review and do not approve release execution.
- R-34/C-15 final evidence aggregation, G-08 RC batch, G-12 Windows RC journey, T-04/T-11 RC evidence, R-38 publication run sheet, and I-12 replacement/invalidation rules remain open where applicable.

## 6. Open blockers and owners

1. **No freeze-triggered candidate:** M-INT must complete every F gate and I-11, then publish I-08 with one coherent tuple, manifest hash, and exact RC receipt.
2. **Newer Runtime publication requires downstream pin-chain updates; freeze evidence is still missing:** M-INT reported Runtime product head `30674b3f4203c5d347fe9e900f7960731148a698` at `ORCHESTRATOR.md` `2026-11-02T13:05:56Z` and requires Agent re-pin followed by App manifest re-pin. This packet has no evidence either downstream update landed. Exact manifest hash/freshness, installed identities, coherent I-11 receipt and I-08 freeze remain pending. Runtime CP2 `47e59494…` still lacks Agent/App CP2 repins and remains dependency state only. Neither R-21 review candidate is asserted as the published Pinokio tuple component or as an integrated replacement.
3. **Qualification evidence is incomplete:** M-QUAL/M-CI must replace preparation-only R-36/R-37 with authenticated evidence and a fail-closed C-15 aggregate on the authoritative tuple.
4. **Protected final journeys are outstanding:** M-GPU (G-08), M-CI/M-VENUE (G-12), M-TL (T-04/T-11), M-PIN (six slots as required), and journey producers must provide terminal exact-RC receipts.
5. **Release run sheet is not approved:** M-INT / release owner must resolve R-38 inputs and approve the concrete run sheet before any publication step; no execution authorization is implied by this packet.
6. **Ledger freshness:** M-INT/M-QUAL should publish the current gate ledger with the later I-AP-08 receipt reconciled and every row bound to the selected tuple.
7. **R-01 evidence retention remains conditionally blocked:** M-QUAL owns open F2 (R-01/R-37): inherited seed stdout/stderr pipes can keep unbounded reader joins open, delaying timeout/failure handling and preventing snapshots and named evidence upload. Bound termination/reader completion and provide parent-timeout and parent-exit inherited-pipe regressions with receipt/snapshot proof. Hosted artifact-size and named-evidence measurement, and R-01 qualification, remain PENDING. The lane candidate `6945784f171a7cc6330b9bebcdbe6a86a2c4da90` is not the published checkpoint or a qualified tuple.
8. **Pinokio R-21 source and hosted evidence remain open; M-PIN owns closure.** The current focused review of candidate `3922f3f38feaaa62dc21d992c40092b30b480029` records F1 and F2 as open evidence-integrity findings and F3 as addressed in source/fixtures while hosted proof remains outstanding. The exact-head hosted proof and producer receipt were unavailable to that review. Run `41444352245` belongs to prior head `ee790eed840e7d1e3594be46455de090684097c4` and an older tuple; its failure is non-current evidence. M-PIN owns correction and exact-head request-bound proof. Six authentic M4-6 slots remain PENDING; no slot or gate is closed. Latest Runtime publication context is `30674b3f4203c5d347fe9e900f7960731148a698` per M-INT at `2026-11-02T13:05:56Z`; Agent re-pin followed by App manifest re-pin is required, and neither is inferred as landed.

## 7. Limitations

- This packet is a preparation artifact. It contains no Astra review, no RV ceremony, no qualification decision, no I-08 freeze, no publication, and no go/no-go.
- Round-1b and later relayed tuples are publication context, not an RC. The latest Runtime product head reported by M-INT is `30674b3f4203c5d347fe9e900f7960731148a698` (notice `2026-11-02T13:05:56Z`); Agent re-pin then App manifest re-pin are required, with neither downstream landing inferred. No I-08 freeze, exact manifest hash/freshness, installed identities, or tuple qualification is established. Runtime CP2 alone is not a tuple. Short labels are for navigation; acceptance must use full commit IDs and receipt-level artifact hashes.
- The existing evidence ledger and current DECISIONS differ in age and disposition for I-AP-08. This packet preserves provenance and does not silently rewrite either source.
- Deferred v34 custody is out of the current release attempt by owner decision, with schema v33 and tracked-via-Runtime installs disabled/fail closed. Any review must assess the actually supported install journey and the stated reopening condition.
- Evidence availability in manager workspaces is not proof of current-RC execution. PENDING entries require owner-supplied canonical receipt paths and exact identities.

## 8. Completion checklist

- [ ] I-08 freeze receipt exists; selected Runtime→Agent→App and Pinokio tuple is coherent and published.
- [ ] Full tuple, manifest, lock/vendor/generated artifacts, lifecycle/VibeComfy pins, installed identities, and artifact SHA-256s are recorded.
- [ ] Current ledger is reconciled against owner receipts; all F gates have explicit dispositions.
- [ ] Protected journey index has exact job/run IDs, commands/coverage, effective identities, artifact hashes, verdicts, and limitations.
- [ ] R-37 and C-15 have accepted exact-RC evidence; required G-08, G-12, T-04, T-11, and Pinokio obligations are closed.
- [ ] Every Astra finding has an owner, disposition, and closure receipt; no blocker remains open within the ≤3-round limit.
- [ ] SR-PROJ decision, reviewer identity/date, and round summary are recorded. No formal RV-1/RV-2/RV-3 ceremony was run.
- [ ] R-34/RV-5 aggregate and subsequent R-35 go/no-go remain separately completed before publication.

## Source index

- `TASKS.md` §§I-08, I-11, project review model, R-33/R-34, G-08/G-12, T-04/T-11, C-15.
- `admin/kit/FAST-ITERATION.md` §5: intermediate change-directed coverage; one full G3 pre-freeze; full nine journeys and six Pinokio slots on RC; one live GPU batch; shared executions preserve separate attribution.
- `DECISIONS.md` entries 11:14Z, 12:08Z, 12:10Z, 12:36Z.
- `runpod-evidence/GATE-MATRIX.md` (2026-11-01T07:52:08Z; old T-PUB snapshot; use with freshness limitation above).
- `/home/user/merge/ws/M-QUAL/queue/done/GATE-MATRIX-R14/GATE-MATRIX.md` and `REPORT.md` (task-local fail-closed refresh).
- `/home/user/merge/ws/M-QUAL/queue/done/R-37-PREP-R10/REPORT.md` (inventory preparation only).
- `/home/user/merge/ws/M-QUAL/queue/done/R-36-POSITIVE-R2/REPORT.md` (bounded synthetic semantics; not acceptance).
- `/home/user/merge/ws/M-QUAL/queue/done/R-38-v2/REPORT.md` and `RUN-SHEET.md` (preparation; approval pending).
- I-AP-08 acceptance: `DECISIONS.md` 12:36Z entry; canonical producer receipt path/run ID to be added when available.

## Latest review inputs and provenance

- Agent #155 segment review and canonical receipt: `queue/done/O-03-AG-155-REVIEW/REPORT.md`, `review-receipt.md`, `RECEIPT.json`. The receipt identifies published merge/tree/ordered parents and records run `41441961556` as a tree-identical candidate run; its API snapshot was `in_progress`, Windows job succeeded without selecting the new native ACL test, and fast checks failed at Ruff.
- App #149/#150 segment review and canonical receipt: `queue/done/O-03-APP149-150-REVIEW/REPORT.md`, `review-receipt.md`, `RECEIPT.json`. App merge/tree/parents are retained-Git-object verified; run `41410178199` logs bind a non-ancestor App SHA and record the two failing jobs and exact I-AP-08 evidence gap.
- Earlier Pinokio #10 source review remains preserved at `queue/done/O-03-PIN-R21-REVIEW/{REPORT.md,review-receipt.md,RECEIPT.json}`. It is superseded for current R-21 source disposition by the terminal correction-candidate review at `queue/done/O-03-PIN-R21-FIX-REVIEW/{REPORT.md,review-receipt.md,RECEIPT.json}` (candidate `ee790eed840e7d1e3594be46455de090684097c4`): F3 addressed at source level, F1 partially addressed, F2 open; no producer or candidate-hosted evidence supplied.
- Historical packet context extended through `ORCHESTRATOR.md` 2026-11-02T12:33:53Z. The latest Runtime notice available for this update is M-INT at `2026-11-02T13:05:56Z`: Runtime `30674b3f4203c5d347fe9e900f7960731148a698`; Agent re-pin then App manifest re-pin are required. No downstream pin landing is inferred. This is publication context, not a freeze or proof of installed identity/freshness. The reviewed Pinokio candidate is not asserted as the published tuple component unless separately landed and bound.
- Current Pinokio R-21 source review: `queue/done/O-03-PIN-R21-CURRENT-REVIEW/{REPORT.md,review-receipt.md,RECEIPT.json,input-inventory.md,SHA256SUMS}`; candidate `3922f3f38feaaa62dc21d992c40092b30b480029`; F1/F2 open, F3 source/fixture addressed with hosted proof outstanding. Run `41444352245` is prior-head/older-tuple evidence only.
- App #152 source review: `queue/done/O-03-APP152-REVIEW/{REPORT.md,review-receipt.md,RECEIPT.json,input-hashes.json,output-hashes.sha256}`; head `1bfbfcc7fb633551dc4f76f16a522ce6d0b99379`; no source defect found, exact-head execution gap owned by M-APP.
- Governing sources read: `/home/user/merge-docs/docs/merge-plan/{MERGE-PLAN.md,TASKS.md,DECISIONS.md,KNOWN-FAILURES.md}` and `admin/kit/FAST-ITERATION.md`. The prior packet and its canonical receipt remain at `queue/done/O-06-AG155-APP150-PACKET/`.

## Supplemental segment-review evidence — Agent #154 (2026-11-02)

This supplement records the accepted focused static review in `SEGMENT-REVIEW.md` and the source receipt at `queue/done/O-03-R05-HARNESS/review-receipt.md`. It is segment-level evidence, not an SR-PROJ review round, and does not change any gate, journey-row, project-review, or packet decision state above. In particular, the SR-PROJ project review remains **PENDING / NOT STARTED** until I-08 freezes a coherent RC tuple.

| Field | Reviewed evidence |
|---|---|
| Published Agent head | `6e46e3bb45ec3feddbd0b0617a50a157e3a4df67` (`int/r1`), attributed by `ORCHESTRATOR.md` at `2026-11-02T09:45:41Z` |
| PR #154 merge commit and ancestry | M-QUAL merge #154 commit `a4203d3da8ddedfc600ac7da251bd0376225cad7`; first parent `ed2c4a26d4f2e2460e7f8cc7094594bf6aa6aa68`. The published integration merge head is `6e46e3bb45ec3feddbd0b0617a50a157e3a4df67`, with parents `ed2c4a26d4f2e2460e7f8cc7094594bf6aa6aa68` and `a4203d3da8ddedfc600ac7da251bd0376225cad7`. |
| Focused review disposition | **FAILED**; one conditional **release-blocking** finding F1 remains open. This is a static source review, not a qualification decision. |
| Finding and route | F1: unbounded seed stdout/stderr staging can reach the hosted artifact size rejection boundary, suppressing the entire lifecycle artifact, including receipt and named install/manager evidence. Owner/remediation route: **M-QUAL**, via R-01; affected R-37 direct-journey evidence rows also require retained artifacts. Add protected over-limit/multi-stream coverage at the artifact-boundary decision and provide normal owner proof at the corrected head. |
| Manifest tuple relation | The relay reports the manifest tuple unchanged: App `5c95abb00525e1a77d00c32042b6453d9f14f4e7` → manifest Agent `e425903f7a9fa5b82973281d15054c0716b46e2d` → Runtime `5453366458f7b4ee874c94d15ae7952e3e82d3c3`. This tuple’s Agent identity is distinct from reviewed harness head `6e46e3bb…`; the tuple is not qualified by the segment review. |
| Evidence boundary | No producer tests, qualification suite, hosted job, or journey was run or reviewed as primary execution evidence. No occurrence of the size trigger is asserted. Source and arithmetic establish a conditional retention defect only; no row completion, gate closure, Astra approval, qualification, or product handoff is claimed. |

The segment finding does not replace the SR-PROJ freeze-triggered project review or close any open R-01/R-37 obligation. Keep the existing packet statuses and checklist unchanged pending owner remediation and qualifying receipts.


## Terminal finding-driven review — R-01 artifact-boundary correction (2026-11-02)

This update carries the terminal finding-driven review in `queue/done/O-03-R05-HARNESS-F1-FIX-REVIEW/` into the packet and supersedes the prior supplement's then-current F1 status. It is segment-level evidence, not an SR-PROJ round, R-01 qualification, or gate decision. Existing packet gate, journey, project-review, and checklist states remain unchanged.

| Field | Reviewed evidence and current disposition |
|---|---|
| Original published checkpoint | Agent `int/r1` head `6e46e3bb45ec3feddbd0b0617a50a157e3a4df67`; publication attribution remains the M-INT relay record in the prior receipt. |
| Exact review candidate | M-QUAL lane `lane/M-QUAL/R-01-ARTIFACT-BOUNDARY-F1-R1`, head `6945784f171a7cc6330b9bebcdbe6a86a2c4da90`, tree `327f71ffdd5285cd87f1847346980294652a9c15`, parent `f229ee37beeb739403a2e961c392ac995ab933de`. Candidate and published checkpoint share base `f229ee37beeb739403a2e961c392ac995ab933de`; the candidate does **not** descend from or replace the published checkpoint. |
| F1 — original artifact-size rejection mechanism | **Resolved at source level, conditionally.** Candidate bounds retained seed stdout/stderr and the artifact-boundary logic reduces stream retention while preserving the receipt and named evidence when the required non-stream minimum set fits. Focused final-head producer results recorded in the accepted review are `artifact-boundary` (5 passed) and `seed-diagnostics` (7 passed); these are retained local results, not replayed here or hosted evidence. If the minimum required set cannot fit, the boundary fails closed; this does not establish hosted retention. |
| F2 — new pipe-lifetime evidence-retention blocker | **Conditional release blocker; OPEN.** Trigger: a seed descendant inherits stdout or stderr and outlives its direct parent, including after the parent is killed on timeout. Effect: unbounded pipe-reader joins can hold timeout propagation and prevent failure snapshots and named evidence upload if the job is terminated while hung. Evidence is the static source path documented in the focused review (`scripts/ci_journey.py` pipe readers, joins, timeout/failure capture; `scripts/journey_version_jumps.py` timeout); no hosted occurrence is alleged. |
| Owner, scope, and requested remediation proof | **M-QUAL**, tracked against **R-01 and R-37**. Bound pipe-reader completion and seed termination when descendants retain handles; preserve diagnostics and failure propagation. Provide focused regressions for both parent timeout and parent exit with an inherited open pipe, including receipt/snapshot behavior on supported platforms. M-INT must reconcile/integrate the candidate and prove the applicable integrated head; the lane candidate alone is not the published integration tree. |
| Hosted measurement and qualification | **PENDING.** M-QUAL / M-VENUE must supply authenticated hosted artifact-size measurement and required named before/after evidence plus receipt on the applicable corrected harness and tuple. **R-01 qualification remains PENDING.** No tuple qualification, gate closure, release decision, Astra approval, or product handoff is claimed. |

The earlier Agent #154 section is retained as historical provenance for the original F1 finding and published checkpoint. Its statement that F1 remained open is superseded by this follow-up's source-level disposition only. The prior manifest tuple is context and remains unqualified; the new lane candidate is not asserted as a tuple component or as an integrated replacement. The packet's project review remains **PENDING / NOT STARTED**, I-08 remains **MISSING**, and R-37 remains **PREPARATION_ONLY / NOT ACCEPTED**.


## Terminal focused review — Pinokio #10 R-21 (2026-11-02)

This supplement records the terminal static review in `queue/done/O-03-PIN-R21-REVIEW/`. It is a source-review disposition, not producer or hosted evidence, an SR-PIN or SR-PROJ round, R-21 gate closure, or RC qualification. The review task exited 0 and has a `TERMINAL` marker; its four listed output hashes were verified against `OUTPUT-SHA256SUMS`. The review itself is **FAILED**, with F1–F3 open and owned by M-PIN.

| Field | Reviewed evidence and current disposition |
|---|---|
| Checkpoint identity | `banodoco/astrid-pinokio` `int/r1` merge `3a6a98ae251ca126505245286e2d3e760f4348e9`, tree `181b1f484cf788d99040f416d8017c5a13ba8dac`; ordered parents `2f085c5fc0cde192ac4a022065883c85dcccd0de` and `79109095009c0b32f33da7045e07f319ae80afb0`. |
| Publication and documentation provenance | M-INT relay at `ORCHESTRATOR.md` timestamp `2026-11-02T11:33:11Z` attributes publication to int/r1 and says Pinokio product bytes in the tuple are unchanged. Local refs are stale; publication is relay-attributed, not independently confirmed by a current remote ref. Review documentation base: `7873b0a00bb01aa2509efba046d048719fdbe4c4`. The M-OPS manager workspace is not a usable Git repository; no branch or commit is claimed. |
| Review disposition | **FAILED — static focused review; F1–F3 OPEN, M-PIN.** Findings identify unsupported or incomplete observer/verifier claims, not an asserted hosted failure. |
| F1 — protected installed bytes | Observation covers metadata hashes, not protected selected program/version bytes. Remediation: define and observe protected installed program/version bytes and necessary metadata; reject missing or changed state; add meaningful mutation and missing-state rejection cases. |
| F2 — readiness claim | Public page HTTP success does not establish candidate App revision, browser application execution, or connection to this Runtime. Remediation: bind to actual candidate App plus observable App-to-this-Runtime interaction, or narrow the claim and leave stronger readiness unproven; add wrong/broken app and connection negative cases. |
| F3 — initial Install receipt | Accepted receipt does not require successful initial Install or gate unexpected Install error/launcher failure. Remediation: retain sanitized request-bound Install observation; require positive successful settlement and explicit absence of unexpected errors/launcher failure; add missing, unknown, and error-with-zero-PTerm negatives, distinguishing diagnostics from completion. |
| Required closure evidence | M-PIN to provide correction head, focused test receipt and matching request-bound hosted evidence; relevant coordinator handles any downstream tuple/carry decision. Findings remain open until reviewed correction and appropriately bound proof. |
| Evidence unavailable / not claimed | No producer receipt was located; producer workspace was unavailable. No hosted execution, test run, journey, qualification, or effective installed bytes were inspected or claimed. Verifier request tuple fields are source-declared inputs, not independent installed-byte measurements. The review report's note on stale refs and M-INT relay attribution remains a provenance limitation. |
| Gate and release boundary | No gate closure, tuple qualification, release, Astra approval, or publication is claimed by this packet. I-08 remains MISSING; the six Pinokio M4-6 slots remain PENDING under R-25/R-37/C-15 and D9. R-34/C-15 aggregate obligations remain outstanding. |

The review report, receipt, JSON receipt, and input inventory are preserved as provenance in the referenced task folder and hashed in this task's `INPUT-HASHES.txt`. This supplement does not alter the project-review table, gate dispositions, six-slot obligation, or other completion states.


## Supplemental checkpoint update — Agent #155 and App #149/#150 (2026-11-02)

This update records published checkpoint identities and M-INT proof references from the notices at `2026-11-02T12:12:35Z` and `2026-11-02T12:14:23Z`; repository object metadata (commit, tree, ordered parents) was independently inspected in mounted repositories. The exact-head Windows proof `41441961556` for Agent #155 and hosted protected proof `41410178199` for App #150 are relay-attributed references. Their hosted artifact contents and detailed outcomes were unavailable in mounted evidence and are not inferred. App #149 is recorded as a tests/script split with no separate proof run in the notice.

These additions do not alter any prior gate disposition, I-08/I-11 state, or open finding. In particular, Agent #154 R-01 F2 remains conditionally release-blocking and open under M-QUAL. The earlier Pinokio #10 and correction-candidate reviews are historical; the current R-21 disposition is recorded in the later focused-review supplement below. The App manifest re-pin reported in the 12:31:55Z notice does not establish downstream re-pin after the newer Runtime publication, an I-08 freeze, exact manifest freshness, installed identities, or project-review eligibility.


## Supplemental segment-review update — Agent #156 and App #151 (2026-11-02)

This update records the terminal focused segment reviews of Agent #156 and App #151. These are source/merge segment reviews, not SR-PROJ/Astra rounds, qualification evidence, gate decisions, or a freeze. Prior findings, gate dispositions, journey statuses, owner routes, and evidence limits remain in force except where this supplement updates publication context.

### Current publication context — notice through 2026-11-02T12:38:52Z

M-INT's 12:31:55Z notice reports the Release-1 publication tuple as App `ce1502e94b935fe6ef1e2f363fc6b55aade4a204` → Agent `2de0726d1eaea7f7bd141251e088aa9fdf224525` → Runtime `5453366458f7b4ee874c94d15ae7952e3e82d3c3`, Pinokio `3a6a98ae251ca126505245286e2d3e760f4348e9`, and VibeComfy `e2905d06` (abbreviated in notice). App #151 is the manifest re-pin reviewed below. Agent #156 merge `b302664877041db78e45c54e83f9c2b0b9429dcd` is tests-only and is a later Agent checkpoint; it does not replace Agent `2de0726d…` in the publication tuple. M-INT describes these as candidate-bound publication context; they do not establish I-08 freeze, I-11 acceptance, manifest freshness beyond the static review, installed identities, or tuple qualification.

The latest notices through 12:38:52Z add owner context to the earlier App #149/#150 hosted run: Runtime's 410 on the retired `composition-revisions` writer route is expected; the logged stale writer/harness is owned by M-APP for correction to the requester-bound commit path. This does not close the separate exact-head I-AP-08 proof gap or the other App #149/#150 hosted evidence issues recorded below. M-APP has staged `I-AP-08-EXACT-R1`; that is an owner notice, not evidence or closure.

### Agent #156 — I-AG-04-ADMISSION-FIX-R2

| Field | Reviewed evidence and disposition |
|---|---|
| Published merge identity | Agent `b302664877041db78e45c54e83f9c2b0b9429dcd`, tree `d77b9ad62e85cf9b685261ee71552fc54d8badf3`; ordered parents `2de0726d1eaea7f7bd141251e088aa9fdf224525` and `ba7e91b37042457900a62bcf197b492bda6b1bb1`. The second parent tree equals the merge tree. First-parent change is tests-only: `tests/test_installer_transaction_authority.py` (39 insertions, 2 deletions). These are identities recorded by the review receipt, not a qualification claim. |
| Disposition | **AWAITING_EVIDENCE.** The source review identifies two test-coverage limits; neither demonstrates a Runtime product defect. |
| F1 — preflight race coverage displaced; M-AG | The prior absent-discovery between-preflight-reads race is no longer a separate case. #156 instead injects replacement after the serialized discovery read with both publications already present, and its unmatched-lock test begins with the lock already present. Restore a distinct absent-discovery/new-lock race for migration and app; assert structured pre-destruction refusal and message, stop/manager/mutation behavior, and publication preservation. The prior R6 receipt concerns a different candidate and Runtime and is not #156 execution evidence. |
| F2 — stopped-owner identity not asserted; M-AG | Successful entry/serialized replacement tests assert a stop count, rollback, and restart, while fake `stop_owner` accepts any keyword arguments. They do not assert replacement PID, birth, instance, and realm handoff. Capture/assert those identities for both recovery kinds and PID/birth/instance variants, with a distinct old-owner sentinel where feasible. This limitation predates #156. Runtime source passing the admitted identity is not evidence that the test asserts it; no wrong signal is demonstrated. |
| Evidence and route | M-AG to provide #156 producer receipt, selectors, collection/node results, inner exits, and effective Agent/Runtime identities, or equivalent applicable evidence and a disposition for F1/F2. M-INT/M-OPS reconcile any evidence carry. The review found no #156 producer receipt, run, or result; M-INT's tests-only notice supplies none. Historical R6 evidence is not proof for #156. No tests/jobs/journeys were replayed for this packet. |

### App #151 — I-AP-07-CP7-FINAL-R1 manifest re-pin

| Field | Reviewed evidence and disposition |
|---|---|
| Published merge identity | App `ce1502e94b935fe6ef1e2f363fc6b55aade4a204`, tree `0a3bfec4541285d0dff19943396be932aa61fcb2`; ordered parents `bf456ac1fec704fbd0c637dd055608a095975c42` and `2699f2f7deaa1a9975e43ed76bd00ed008f14095`. First-parent diff changes only `public/install-manifest.json`, Agent pin only; second-parent diff is empty. |
| Static disposition | **No defect found in reviewed manifest re-pin or merge.** Manifest Agent and Runtime values match the 12:31:55Z notice. Static consistency does not establish producer execution, vendor-lock freshness, generated-artifact freshness, installed identities, or qualification. |
| Evidence route | No #151-specific producer receipt, plan/write output, vendor-lock result, or execution evidence was found in searched task locations. **M-APP** owns any required producer/vendor-lock evidence; **M-INT** owns checkpoint/evidence disposition; **M-QUAL** owns candidate-bound qualification. The notice lists Pinokio and VibeComfy separately; their effective installed identities were not checked in this review. |

### Preserved earlier review findings and gate states

- App #149/#150 remains as previously recorded: the I-AP-08 exact-head runner/provenance evidence gap is **OPEN** under **M-APP**, with evidence disposition by **M-INT**. The separate prior round-1b I-AP-08 acceptance remains limited to its recorded tuple and is not extended to the newer checkpoints. Earlier hosted failures and any related rows retain their prior findings/routes. The 12:38:52Z notice clarifies the HTTP 410 as an expected retired-writer response to a stale App/harness caller and routes requester-bound writer correction to M-APP; no Runtime change is indicated on the supplied evidence. Neither the notice nor #151 static review closes I-AP-08 or hosted evidence gaps.
- Agent #155's native Windows ACL/installed-path proof and Ruff findings remain independently open as recorded in the prior packet and its review; they are not re-adjudicated by #156. The later M-CI notice classifies run `41441961556` as a confirmed 2,402-vs-2,128 Ruff regression, not shown introduced by #155; M-AG owns the pre-existing backlog. No rerun or baseline edit is claimed.
- Pinokio #10 and the prior correction-candidate reviews remain historical. The current R-21 candidate disposition is superseded by the O-03 current review recorded in the dated supplement below: F1/F2 open; F3 source/fixture addressed, hosted proof outstanding. Producer/hosted closure and all six authentic M4-6 slots remain pending under M-PIN/M-QUAL and D9.
- Existing I-08 **MISSING**, I-11 **MISSING**, SR-PROJ **PENDING / NOT STARTED**, R-37 **PREPARATION_ONLY / NOT ACCEPTED**, C-15/R-34 outstanding, journey statuses, and all other gate dispositions remain unchanged. No qualification, gate closure, Astra approval, release, publication, or handoff is claimed.

Review provenance: `queue/done/O-03-AG156-REVIEW/{REPORT.md,review-receipt.md,RECEIPT.json,OUTPUT-HASHES.txt}` and `queue/done/O-03-APP151-REVIEW/{REPORT.md,review-receipt.md,RECEIPT.json,output-hashes.sha256}`. Their hashes are recorded in this task's `INPUT-HASHES.txt`.


## Supplemental frontier update — Agent #157 and direct journey run 41446010147 (2026-11-02)

This supplement carries the M-INT notice at `2026-11-02T12:51:24Z` and the retained direct journey receipts from run `41446010147`. It records the moved frontier and observed first failures; it does not close any existing gate, qualify a tuple, start SR-PROJ review, or replace prior open findings.

### Publication and identity boundary

M-INT reports Agent #157 `R-05-DIAGNOSTIC-CURRENT` landed on `int/r1` as merge `a02ce55e74ea0b4c8cbfd5372679e6de5b84c385`; producer lane commit `f8649936c74ad730cd1538a98bddd5ec04ea52fd`. The merge object has tree `da75807132bc699fc86ec5f04bf09e37769813f0` and ordered parents `b302664877041db78e45c54e83f9c2b0b9429dcd` and `f8649936c74ad730cd1538a98bddd5ec04ea52fd`. Notice scope: seed/snapshot ordering, Windows R0 log retention, and diagnostic stages.

At the time of run `41446010147`, the relayed manifest tuple was App `ce1502e94b935fe6ef1e2f363fc6b55aade4a204` / Agent `2de0726d1eaea7f7bd141251e088aa9fdf224525` / Runtime `5453366458f7b4ee874c94d15ae7952e3e82d3c3` / Pinokio `3a6a98ae251ca126505245286e2d3e760f4348e9` / VibeComfy `e2905d06` (notice abbreviation). Agent #157 was the later harness checkpoint, not the Agent identity in that manifest tuple. Run receipts bind the candidate checkouts to that earlier tuple and manifest SHA-256 `920477210d01615a6698cda30f248552f13c712734e86ea56f283300cb1a9c87`. The newer Runtime #24 notice at 13:05:56Z requires Agent then App re-pins; no freeze, I-11 acceptance, installed-identity completion, or tuple qualification follows.

### Direct journey frontier

| Platform / job | Receipt result and first failing stage | Evidence-bound disposition |
|---|---|---|
| Linux / `direct-linux` | `journey-result.json`: failed at `update-bootstrap.log`, exit 1. Retained `before-cleanup/install.log` reports `RuntimeError: Runtime discovery owner identity changed; refusing to change it.` The R0 seed step had advanced successfully; the failed operation is the R0→R1 update identity-admission path. | Product failure under Runtime identity admission; route to **M-R1FIX**, with **M-RT**. The exact Runtime identity predicate/remediation and a corrected protected proof remain open. |
| macOS / `direct-macos` | `journey-result.json`: failed at `update-bootstrap.log`, exit 1. Retained `before-cleanup/install.log` reports `astrid_runtime.identity.StartupIdentityError: startup identity must be an object`, followed by `RuntimeError: Live Runtime startup identity cannot be verified; refusing to change it.` R0 seeding completed before update admission failed. | Product failure under Runtime identity admission; route to **M-R1FIX**, with **M-RT**. The identity admission error and corrected protected proof remain open. |
| Windows / `direct-windows` | `journey-result.json`: failed at `release0-bootstrap.log`, exit 1, before R0 seeding/update. Retained `release0-bootstrap.log` line 2: `Get-Acl` was found in `Microsoft.PowerShell.Security`, but the module could not be loaded. | Remains at diagnostic capture/bootstrap stage per M-INT; route **M-QUAL / M-VENUE**. This is not evidence of the Linux/macOS product failure and has not advanced to R0→R1. |

All three receipts identify the same current manifest candidate tuple above. Linux and macOS show service-manager prerequisites passing; their install logs reach the update operation and retain the first Runtime identity refusal. Windows has the early bootstrap diagnostic retained, and its lifecycle stages (`release`, `semantic_data`, `service`, `health`, `task_settled`, and others) are `not-run`. The Linux/macOS receipts likewise leave post-update lifecycle checks `not-run`; their `release_transition` is null. These are failed direct rows, not completed qualification slots.

**Gate effect:** the existing R-37 nine OS × channel inventory remains **PENDING** and **NOT ACCEPTED**; this three-platform run does not complete that inventory. I-08 and I-11 remain **MISSING**, SR-PROJ remains **PENDING / NOT STARTED**, and C-15/R-34 remain outstanding. Preserve all other open findings and owner routes recorded above.

### Evidence and limits

Retained primary evidence: `/home/user/merge-docs/docs/merge-plan/evidence/actions/41446010147/`, including each platform's before/after-cleanup receipts and logs. This task hashed the complete 59-file run evidence directory and the specific platform receipts/logs are indexed in `INPUT-HASHES.txt`. Run receipt metadata binds candidate source checkouts to the published tuple. The M-INT notice supplies the #157 harness checkpoint and owner routing; it does not turn #157 into the manifest Agent pin. M-QUAL's current status file is inventoried as contemporaneous owner context; the retained run artifacts and M-INT notice determine the frontier described here.

No tests, worker checks, hosted jobs, journeys, qualification, publication, or external dispatch were performed by this packet task.

## Supplemental segment reviews — Pinokio R-21 and App #152 (2026-11-02)

These completed focused reviews update source-level evidence only. They do not start SR-PROJ/Astra review, establish hosted execution, close a qualification/release gate, or alter any gate state.

### Pinokio R-21 current candidate

The focused source review examined `banodoco/astrid-pinokio` diagnostic workflow candidate `3922f3f38feaaa62dc21d992c40092b30b480029` (tree `90ed11727804549dd5299c62605528d52ec2165e`, parent `cef085cf195cbbe112eacc138ba892ef6d89bc3c`). This is a workflow candidate, not the published Pinokio component identity `3a6a98ae251ca126505245286e2d3e760f4348e9`, and it is not an I-08-selected RC.

| Finding | Current source disposition | Owner and evidence boundary |
|---|---|---|
| F1 — linked or missing protected state | **OPEN; evidence-integrity finding.** Existing digest behavior does not reliably cover effective bytes through symlink targets and can encode missing state as stable data; relevant target-mutation/missing-root negative cases are absent. | **M-PIN** to define required state and safely reject or bind linked/missing state, add focused coverage, and provide exact-head evidence. |
| F2 — candidate App does not execute the Runtime handshake | **OPEN; evidence-integrity finding.** Observer-side synthetic health/contract requests and a public 2xx do not demonstrate that the candidate App/browser executes the handshake against this Runtime. | **M-PIN** to bind and observe actual candidate App-to-Runtime behavior or narrow readiness claims, with negative controls and exact-head evidence. |
| F3 — Install settlement | **Addressed in source and fixtures; hosted proof outstanding.** Source retains and verifies request-bound Install evidence and fixtures cover missing/unknown/error cases. These inspected sources and fixtures were not executed in this review. | **M-PIN** to provide matching exact-head hosted evidence. |

No exact-head hosted proof or producer receipt was available to the focused review. Its report, receipt, input inventory and checksum manifest are indexed in `INPUT-HASHES.txt`.

Run `41444352245` is explicitly prior-head evidence: workflow head `ee790eed840e7d1e3594be46455de090684097c4`, older tuple, request JSON SHA-256 `a3600bdebd59122b2028c4cfd08f29b534aae0fa7046aa0b1803a71601b2b96b`. The reported observations are verdict `FAILED` / `journey_receipt_missing`, repeated Runtime `ECONNREFUSED`, and initial Install PTerm exit 0 with `installer_success: false`. The reviewed evidence does not establish a cause. This run is not attributed to current candidate `3922f3f38feaaa62dc21d992c40092b30b480029` and does not establish current-head behavior.

### App #152 activity-label review

App #152 exact head is `1bfbfcc7fb633551dc4f76f16a522ce6d0b99379` (tree `d19c0c1e6f0cea75900e8c8c96a59f728d96e5f9`). The source review found **no concrete source defect** in the activity-label change. It did not run the added unit assertion or journey. Exact-head execution remains unproven; **M-APP** owns applicable focused execution proof, with M-INT/M-QUAL evidence and qualification disposition. The actual Runtime-returned spec family and end-to-end behavior remain unproven.

The #152 manifest tuple pins Agent `2de0726d1eaea7f7bd141251e088aa9fdf224525` and Runtime `5453366458f7b4ee874c94d15ae7952e3e82d3c3`. M-INT later reported Runtime #24 at `2026-11-02T13:05:56Z`, product head `30674b3f4203c5d347fe9e900f7960731148a698`. The notice requires Agent re-pin followed by App manifest re-pin. This packet has no evidence that either downstream pin update landed; the #152 tuple is not treated as current latest tuple or qualified.

### Gate and qualification state

The six-slot Pinokio qualification row remains **PENDING**. I-08 and I-11 remain **MISSING**; SR-PROJ remains **PENDING / NOT STARTED**; R-37 remains **PREPARATION_ONLY / NOT ACCEPTED**; C-15/R-34 and all other qualification/release gates remain open. Neither focused source review changes these states. No tests, hosted jobs, journeys, producer commands, or qualification runs were performed by this packet update.

Review provenance: `queue/done/O-03-PIN-R21-CURRENT-REVIEW/{REPORT.md,review-receipt.md,RECEIPT.json,input-inventory.md,SHA256SUMS}` and `queue/done/O-03-APP152-REVIEW/{REPORT.md,review-receipt.md,RECEIPT.json,input-hashes.json,output-hashes.sha256}`. M-INT Runtime publication notice: `ORCHESTRATOR.md` at `2026-11-02T13:05:56Z`.

## Supplemental review and publication index — 2026-11-02 (through 13:19:48Z)

This dated supplement incorporates the completed focused reviews in O-03-RT24-REVIEW, O-03-AG158-REVIEW, O-03-APP153-REVIEW, and O-03-PIN-R21-CORRECTION-REVIEW, plus the M-INT publication notice at `ORCHESTRATOR.md` 2026-11-02T13:19:48Z. These are segment/source reviews and publication relays, not SR-PROJ rounds, qualification receipts, or gate decisions. The prior packet text remains intact except for this index. Review dispositions below describe source coverage and evidence limits; no finding or gate is inferred closed.

### Latest published-head context

| Component | Latest published `int/r1` identity in supplied notices | Scope / binding limit |
|---|---|---|
| Runtime | #24 `30674b3f4203c5d347fe9e900f7960731148a698`; tree `d1ab3dccdd3b455a5f822e9d9be757c8a7ab4696`; ordered parents `8d3e3af4ede6e3579ac673ae77794464f693bf1b`, `ceb741b6a650f4330deb9f118904460016fd02a5` | M-INT attributes publication in the 13:05:56Z notice. The focused review independently inspected Git object metadata and the four-file first-parent delta (157 additions, 7 deletions); it did not verify the relayed Windows run. |
| Agent | #159 `017c5fe68858888cb1201efca57d7a97518b8f2c` | M-INT 13:19:48Z says this is workflow-only and reports an int/r1 push CI run as proof. No run ID, attempt, job results, or primary receipt is included in that notice. Earlier #158 `b5ea0145289b70e4781e1d75f80606c6d87d1785` is tests-only and is now an intermediate head, not the latest published head. |
| App | #153 `b1af52e2376ac75e9ed2d6222b899220a45007c9` | M-INT attributes publication in the 13:11:46Z notice. The manifest was reported unchanged, still pinning Agent `2de0726d1eaea7f7bd141251e088aa9fdf224525` and Runtime `5453366458f7b4ee874c94d15ae7952e3e82d3c3`. |
| Pinokio | `3a6a98ae251ca126505245286e2d3e760f4348e9` | Latest published Pinokio product identity in the accepted base packet's supplied publication context. The R-21 correction candidate below is not this published component identity. |

These are latest supplied publication references, not an I-08-selected RC. Runtime and Agent have advanced beyond the manifest's reported pins; no downstream repin, exact current manifest digest/freshness, coherent tuple, installed identity, I-11 receipt, or I-08 freeze is established here. The base packet's VibeComfy/lifecycle pending fields remain pending. The `13:19:48Z` notice also claims the Agent #159 CI run is proof; this packet has no matching primary run receipt to independently assess that claim.

### Focused review outcomes

| Review | Reviewed checkpoint / scope | Source-review disposition | Evidence boundary and follow-up |
|---|---|---|---|
| Runtime #24 — `O-03-RT24-REVIEW` | Published Runtime `30674b3f4203c5d347fe9e900f7960731148a698`; first-parent delta is four files, +157/−7. | Review complete; no new concrete defect in the changed boundary logic. Finding: the Windows workflow hardcodes Agent `e425903f7a9fa5b82973281d15054c0716b46e2d` rather than honoring `agent_sha` dispatch input. Carried T-11 collection issue remains: two unchanged test modules import `fcntl` unconditionally on Windows. | Notice reports Windows red→green/no new failures, but the focused review could not locate the exact run/attempt, matched baseline, effective identities, selectors, or retained receipt. M-CI/M-INT retain evidence matching; M-CI owns consistent override/per-job identity, M-RT owns the carried imports. Not qualification or a finding that the reported job failed. |
| Agent #158 — `O-03-AG158-REVIEW` | Published merge `b5ea0145289b70e4781e1d75f80606c6d87d1785`, tree `54b7e5e271aa3956584df1c719cc818f57e42df3`; test-only first-parent delta, 62 additions. | **AWAITING_EVIDENCE.** Added fake-boundary tests address #156's absent-discovery replacement race and assert PID/birth/instance/realm passed to `stop_owner`. | M-AG's R1/R2 42/42 carry is relayed but receipts/comparison hashes are not mounted; no exact-published-head execution is established. M-AG to supply receipts, selectors/results, effective identities, inner exits, and carry comparison. The review found no new source defect in this narrow delta. #159 subsequently advanced the published Agent head with a workflow-only change. |
| App #153 — `O-03-APP153-REVIEW` | Published merge `b1af52e2376ac75e9ed2d6222b899220a45007c9`; runner/spec first-parent delta, 6 insertions and 6 deletions. | Assertions remain in source for failed-save/no-POST, successful-save/exactly-one-POST, observational GET, and restart replay. The runner still pins candidate `2699f2f7deaa1a9975e43ed76bd00ed008f14095` and rejects published merge `b1af52e2…` at its HEAD guard. Prebuilt distribution provenance is unbound to source/build. | No exact-head I-AP-08 execution or producer receipt was found in searched mounted paths. M-APP owns merge-head runner binding and served-build provenance; M-INT assesses applicable receipt. This is an evidence/runner blocker, not a demonstrated bridge behavior failure. Prior acceptance remains limited to the separately recorded round-1b tuple. |
| Pinokio R-21 correction — `O-03-PIN-R21-CORRECTION-REVIEW` | Candidate `c0804f235cfd9053ab11f2c2499ce51034048194`, tree `0f6f4cdcaa02fd78b1901a4d4a5b761779793a5d`, parent `3922f3f38feaaa62dc21d992c40092b30b480029`; not a published component or RC. | **F1 OPEN. F2 improved at source-design level, but OPEN. F3 remains addressed at source/fixture level only.** F1: protected data digest permits missing data root and does not bind linked data targets; installed-link traversal lacks containment/bounds. F2: tests fabricate validator facts and do not run the collector against the pinned App, including positive and wrong/broken-app or failed-connection cases. | Relay claims 73 focused passes and an exact-head hosted request, but matching canonical receipt, request/verdict/logs, run/attempt and hashes were not mounted; the 13:00/13:03 request-time descriptions remain unreconciled. No hosted outcome, protected acceptance, or six-slot evidence is established. M-PIN owns corrections and bound proof; M-OPS matching and M-QUAL/M-INT qualification remain separate. This supersedes the older candidate-specific R-21 source disposition in the earlier supplement only where it described the prior candidate; it does not close R-21 or replace the published Pinokio identity. |

### Gate and preparation state

The accepted base packet's states remain in force: SR-PROJ is **PENDING / NOT STARTED** until I-08 freezes a coherent RC; I-08 and final tuple binding remain **PENDING/MISSING**; R-37 remains preparation only; R-21 protected acceptance remains open; all six authentic M4-6 Pinokio slots remain required under D9 unless an explicit owner decision narrows them. The I-AP-08 acceptance stays bounded to its recorded round-1b tuple and does not extend to App #153. Runtime #24, Agent #158/#159, App #153 and the R-21 correction review do not establish qualification or gate closure.

R-32 RV packet preparation is cancelled, and R-33 RV-1/RV-2/RV-3 are replaced by the freeze-triggered SR-PROJ process under current TASKS.md. This supplement is packet preparation only: it starts no RV ceremony, SR-PROJ round, or Astra review and records no Astra approval.


## Supplemental focused review — Agent #159 exact-head CI proof (2026-11-02)

This supplement incorporates the accepted focused static review `queue/done/O-03-AG159-REVIEW/` and clarifies the run referenced in the M-INT publication notice at `ORCHESTRATOR.md` section `2026-11-02T13:19:48Z`. It is segment-level source-review evidence and an exact-head CI proof status update, not an SR-PROJ/Astra round, qualification, I-08 freeze, or gate decision. The existing findings, gate states, journey statuses, and project-review decision above remain unchanged.

| Field | Accepted evidence and disposition |
|---|---|
| Published Agent identity and scope | `int/r1` commit `017c5fe68858888cb1201efca57d7a97518b8f2c`; the publication notice describes PR #159 `AG-CI-WORKFLOW-HANDOFF-R1` as workflow-only with the manifest unchanged. The focused review inspected the first-parent workflow delta and found no concrete source defect in that change. |
| Static review outcome | **No concrete source defect established; release-blocking exact-head proof gap remains open.** Review completion is not release approval. The accepted report routes completed run/job/step evidence and classification of any red jobs to M-CI, with M-INT to reconcile it against the landing claim. |
| Exact-head CI run | GitHub Actions CI run `41451996939`, attempt 1; event `push`, branch `int/r1`, workflow `.github/workflows/ci.yml`, exact head `017c5fe68858888cb1201efca57d7a97518b8f2c`. In the review's read-only API snapshot around `2026-11-02T13:22Z`, the run was `queued`, conclusion `null`; the eight `unit-shard` jobs and `darwin native` were queued with no step results. Other listed jobs were also queued; aggregate `unit` had not appeared in that snapshot. A queued run is not a failed run, and the snapshot may become stale. |
| Required proof and owner route | **M-CI:** retain the completed run/attempt's job and step results/logs, resolved and checked-out VibeComfy SHA, install and collection/test outcomes for all eight unit shards and Darwin, and classify remaining red jobs; provide applicable successor proof if this run cannot establish the repair. **M-INT:** reconcile that evidence with the landing claim. This is a recorded route only; no message or shared ledger was sent or edited. |
| Evidence limits | The review is static source inspection plus a queued-state API snapshot. It does not establish a successful checkout/install, test collection, test result, green CI, or execution at the reviewed commit. API response bodies were not retained by the review; its input inventory records hashes and endpoints for the observed snapshots. The CI result is pending, not failed. |

This addition does not change the packet's pending SR-PROJ state or any unrelated finding or gate. I-08 remains missing; no coherent RC tuple or qualification is established. R-32 RV packet preparation remains cancelled. No RV ceremony was started, and no Astra approval, gate closure, or release decision is claimed.


## Supplemental publication and focused-review update — 2026-11-02 (through 14:35:50Z)

This dated supplement adds the accepted Runtime #25 and Agent #161 focused reviews, the accepted integrated Pinokio R-21 review, and the latest supplied M-INT publication and diagnostic notices. It preserves the packet's prior findings and gate states. These are focused source-review dispositions and relayed publication/evidence context; they are not SR-PROJ/Astra rounds, qualification results, I-08 freeze evidence, or gate decisions.

### Latest supplied publication context

| Component | Latest supplied published identity | Binding and evidence limit |
|---|---|---|
| Runtime | #25 `2776ba973b6c8f3555b5629b0840d9691a98d3c2`, tree `f8647c2ab71d923df670eea1ec7824a06344a51b`; ordered parents `30674b3f4203c5d347fe9e900f7960731148a698`, `06a94073a9a6fffd8b701ad034501944e5ced730`. | M-INT notice at `ORCHESTRATOR.md` `2026-11-02T13:39:11Z` attributes publication. The notice says the manifest is unchanged pending the re-pin chain. Runtime #25 is an intermediate published source checkpoint, not a frozen RC. |
| Agent | #161 `999d3046321456d0ede1ceb86d564bf549ccfdda`, tree `e13e8414ef1780481bac4fe745363f17c4bed0d9`; ordered parents `959cdff8d5e93f027e612e5e1dd42419938a3a40`, `26899700775588c680b9c5a2747cdef8ee7e7912`. | M-INT notice at `2026-11-02T13:45:14Z` attributes publication. The notice reports run `41451268790` reached R0 install and seed on Linux/macOS, then stopped at update-bootstrap; review could not inspect the underlying run receipts/logs. |
| App | #153 `b1af52e2376ac75e9ed2d6222b899220a45007c9` remains the latest App publication named in the supplied notices. | The supplied notice/context says its manifest remains pinned to Agent `2de0726d1eaea7f7bd141251e088aa9fdf224525` and Runtime `5453366458f7b4ee874c94d15ae7952e3e82d3c3`. No evidence here establishes the later Agent/Runtime re-pins or a fresh manifest. |
| Pinokio product | `3a6a98ae251ca126505245286e2d3e760f4348e9`. | The R-21 workflow candidate reviewed below is diagnostic/venue workflow source, not a new Pinokio product identity or selected RC identity. |

The reported App manifest therefore does not bind the latest supplied Runtime #25 and Agent #161 publications. No coherent current tuple, exact fresh manifest digest, installed identity set, I-11 completion, I-08 freeze, or selected RC is established by these notices.

### Accepted focused review dispositions

#### Runtime #25 — `O-03-RT25-REVIEW`

The accepted review examined Runtime #25 merge `2776ba973b6c8f3555b5629b0840d9691a98d3c2`. Its first-parent delta is two files and 35 insertions; the second-parent diff is empty. The focused source review found no new defect in the reviewed delta. The code permits the neutral R0 identity only when the owner health mapping matches a nonempty requested runtime instance and the `identity` key is absent. Explicit null, malformed present identities, and mismatches remain refused. The added test uses a synthetic health mapping; it is not an authentic R0 journey or installed update proof.

The review classifies the `41410178199` second-writer HTTP 410 as a stale App caller based on the mounted older journey log and source route; that run used Runtime `54533664`, not #25. **M-APP** owns moving the second-writer setup to the requester-bound commit route and restoring conflict/draft assertions. **M-QUAL with M-RT and M-R1FIX** own separate exact-tuple authentic R0 owner/update, migration, interruption/restore, and real-R0 rollback evidence; **M-INT** coordinates the coherent re-pin. Carried RT24 Windows workflow/collection findings and directly relevant CP4 proof gaps remain with their prior owners. The local integration refs in the receipt were stale, and publication attribution comes from the M-INT notice. No tests, migration replay, hosted job, or installed journey was run by the source review.

#### Agent #161 — `O-03-AG161-REVIEW`

The accepted review examined Agent merge `999d3046321456d0ede1ceb86d564bf549ccfdda`. Its first-parent delta is journey driver and focused tests only (+217/−2); the three changed blobs match producer candidate bytes, but complete trees differ, so producer execution is not exact-merge-tree execution. Disposition: **AWAITING_EVIDENCE for hosted journey proof; scoped source review complete.** Source and retained producer output support #157 F2 for the representative persistent realm mutations tested, not an authentic hosted seed or lifecycle pass.

The capture double-scan detects differences between sampled trees; it is not an atomic snapshot and does not justify a claim that every mutation during capture is detected. No concurrent mutation failure was observed or asserted. **M-QUAL** owns accurate claim wording and any stronger capture protocol/evidence required. M-INT reports run `41451268790` reached R0 install/seed on Linux and macOS and stopped at update-bootstrap; raw run receipts/logs were not mounted, so harness SHA, candidate/installed tuple, seed/checkpoint outcomes, and stop cannot be independently confirmed from that relay. **M-QUAL with M-VENUE** owns exact harness and tuple/manifest identities, platform outcomes, receipts/logs, and update failure evidence; **M-INT** reconciles with the published tuple. The prior M-R1FIX/M-RT route remains relevant subject to evidence. #157 Windows artifact-cap F1 remains outside this delta; #158/#159 proof gaps were not adjudicated. No rerun was performed or requested.

#### Integrated Pinokio R-21 — `O-03-PIN-R21-INTEGRATED-REVIEW`

The accepted review examined workflow candidate `a70615355d350f5eb95f896d333f451ad8e51c27`, tree `b885447617253545d4ddd5e9ff7ecc3de1a8ea03`, parent `175c6bc247fa24368f70429456d0784e81457062`. This is not the Pinokio product pin or a selected RC. Disposition: further source correction and exact-head evidence required; protected qualification is not established.

- **M-PIN — portable regression test:** the new admission test depends on the checkout being on the producer branch and fails in detached or other-branch checkouts independent of admission behavior. Remove that environment dependency while retaining ref/SHA negative controls and test the detached/other-branch cases.
- **M-PIN — F1 bounds/coverage:** source now requires a valid data root and binds in-root linked file bytes, addressing prior missing/linked-data false-safe paths at source/fixture level. Recursive full-file snapshots remain without byte, node, or depth limits, and selected-file/link negative coverage is incomplete. Define enforceable bounds or an approved bounded snapshot strategy and close the selected coverage gaps with focused evidence.
- **M-PIN — F2/F3 proof:** F2 specifies building and locally serving the pinned App, but actual pinned-App browser success and broken-App/broken-connection full-path negatives are unproven. F3 remains source/fixture-only. Provide exact-head focused and request-bound hosted evidence.
- **M-OPS — evidence reconciliation; M-QUAL/M-INT — qualification:** the named producer receipt was not mounted and the bounded search found no candidate-bound hosted artifact. The prior parent run failure and relayed request are not a candidate verdict. Reconcile any later request/workflow/run attempt, tuple, hashes, and all inner exits. Authentic D9 Pinokio slots remain a separate protected qualification obligation.

The latest supplied `ORCHESTRATOR.md` notice at `2026-11-02T14:35:50Z` says diagnostic receipt `R-21-INSTALL-MARKER-DIAG-R1` saw the same opaque failure signature in runs `41447528750` and `41451306649` but could not establish root cause; exact-head run `41459848667` remains pending. It says a diagnostic-only observation candidate was staged without changing protected behavior and that review of `a7061535` remains requested. These relays do not supply the missing candidate verdict or establish root cause, current-candidate behavior, or protected acceptance.

### Gate states and preparation boundary

All states recorded in the accepted base packet remain unchanged: SR-PROJ is **PENDING / NOT STARTED** until I-08 freezes a coherent RC; I-08 and I-11/final tuple binding remain missing or pending; R-37 remains preparation only and not accepted; the six authentic Pinokio M4-6/D9 slots remain required; C-15/R-34 and other qualification/release gates remain open. Existing findings and owner routes remain open unless their existing evidence records explicitly say otherwise. Static source review, producer-focused tests, publication relays, diagnostic notices, and relayed requests do not qualify journeys or close gates.

R-32 RV packet preparation remains cancelled. This is preparation only: no RV-1/RV-2/RV-3 ceremony, SR-PROJ round, or Astra review was started, and no Astra approval or qualification is claimed.


## Supplemental publication and evidence update — 2026-11-02T16:37:10Z

This dated supplement is appended to the accepted R2 bytes. It records the newest supplied publication and mounted evidence context only. It does not start an SR-PROJ/Astra round, close a protected journey or segment review, qualify a tuple, establish an I-08 freeze, authorize publication, or replace any accepted receipt. The accepted base's fail-closed states remain controlling.

### Agent #164 publication — open review input

| Field | Exact supplied identity / disposition |
|---|---|
| Repository | `/home/user/merge/ws/M-OPS/repos/astrid-agent` |
| Published merge | `ab43bad818ef65ca1e929b973a2f61cd9fc4bf61` |
| Tree | `87a5888860c8b2fb2611e76ed6b316503780c4e5` |
| Ordered parents | `fb222b3a54e9ffe2fa4eb2c382bc25033e773949`; `0fed29efaf2dd009d68551d051e602c2e6bdca89` |
| Notice | M-INT `ORCHESTRATOR.md` `2026-11-02T16:00:07Z`: Agent #164 `AG-O03-AG155-ACL-R4`; fix-forward of #155; exact-head Windows positive and negative controls reported `19/0`; Runtime lock unchanged at `2776ba973b6c8f3555b5629b0840d9691a98d3c2`; M-APP is said to pin this head. |
| Packet disposition | **OPEN REVIEW INPUT / AWAITING REVIEW.** The commit, tree, and ordered parents were verified from the mounted Git repository. The notice is a publication relay, not an accepted focused-review receipt; no manifest hash, generated-artifact freshness, installed identity, coherent current tuple, I-11 receipt, or I-08 freeze is established. |

The `19/0` result is retained as the notice's claim only. It is not promoted to accepted protected proof or to closure of the Agent ACL segment. Any review must bind the exact merge, candidate/installed bytes, selected controls, inner results, and request-bound receipt before changing a gate or journey row.

### Agent #161 platform evidence — mounted, awaiting review

The newly mounted evidence root is `/home/user/repo/docs/merge-plan/evidence/actions/41451268790/` (65 retained files; inventory-manifest SHA-256 `d31a6b4feaa48e03d7b5b0f525b7258b0fc86eb52debc4c1fe136a724f54a399`). The run and candidate identity visible in the supplied M-QUAL notices are:

| Field | Evidence-bound value |
|---|---|
| Actions run | `41451268790` |
| Candidate App | `b1af52e2376ac75e9ed2d6222b899220a45007c9` |
| Candidate Agent | `2de0726d1eaea7f7bd141251e088aa9fdf224525` |
| Candidate Runtime | `5453366458f7b4ee874c94d15ae7952e3e82d3c3` |
| Candidate Pinokio | `3a6a98ae251ca126505245286e2d3e760f4348e9` |
| Mounted outcome | Before-cleanup `journey-result.json` files for Linux, macOS, and Windows report `status: fail`, `installed_tuple: null`, and `update-bootstrap.log: exit 1`; the run's platform receipts and logs are retained under the root. |
| M-QUAL boundary notice | `2026-11-02T16:34:19Z` and `2026-11-02T16:37:10Z` say two sequential scans cannot prove every mutation during capture; universal wording must be qualified. |
| Packet disposition | **AWAITING REVIEW.** The mounted run evidence is not an accepted task-local review receipt for this packet. It does not establish current-RC qualification, an accepted platform journey, or any gate closure. |

This supplements the accepted R2 source-review entry for Agent #161; it does not silently convert that scoped review into acceptance of this later mounted run. The named `queue/running/O-03-AG161-RESULT-REVIEW` output was not used as accepted evidence. Review remains required for exact harness/checkout identity, platform outcomes, receipts, failure evidence, and the double-scan limitation.

### Pinokio R-21 exact-head failure evidence — mounted, awaiting review

The mounted evidence root is `/home/user/repo/docs/merge-plan/evidence/actions/41462939920/r21-published-tuple-diagnostics-a70615355d350f5eb95f896d333f451ad8e51c27-41462939920-1/` (15 retained files; inventory-manifest SHA-256 `7ec24643c6417b10badae9cca9047b6020f69c258c8aa8a30bd1e7bf83552269`). It is bound as follows:

| Field | Evidence-bound value |
|---|---|
| Request | `R-21-PUBLISHED-TUPLE-R1` |
| Run / attempt | `41462939920` / `1` |
| Candidate / runner / workflow SHA | `a70615355d350f5eb95f896d333f451ad8e51c27` for `runner_ref` and workflow SHA |
| Workflow ref | `refs/heads/lane/M-PIN/R-21-VENUE-REF-BINDING-R1` |
| Request tuple | Runtime `5453366458f7b4ee874c94d15ae7952e3e82d3c3`; Agent `2de0726d1eaea7f7bd141251e088aa9fdf224525`; App `ce1502e94b935fe6ef1e2f363fc6b55aade4a204`; Pinokio `3a6a98ae251ca126505245286e2d3e760f4348e9`; old launcher `bbba620e3d89eeff8021417f365e6ea584203105`; pinokiod `add4a674ad1ba95bb62ec427fce519fe5bf0e9bf`; pterm `5be43718701c448b43daf0ce51d8935f170065bb`. |
| Manifest / harness | Manifest SHA-256 `920477210d01615a6698cda30f248552f13c712734e86ea56f283300cb1a9c87`; Agent harness ref `6e46e3bb45ec3feddbd0b0617a50a157e3a4df67`, marked `executed: false`. |
| Recorded result | `verdict.json`: `FAILED`, reason `journey_receipt_missing`, setup exit `0`, journey exit `1`; install inner exit `0`; first-click/stop/retry/cleanup are null. |
| Failure boundary | `installed_tuple_present: false`; `install_marker_observed: false`; start `engine_error: true`; Runtime unavailable; no listener on port `17340`; observer failure records transport `ECONNREFUSED`. |
| Packet disposition | **AWAITING REVIEW.** The evidence is exact-request/run/candidate-bound and records a terminal failure, but no accepted `O-03-PIN-R21-EXACT-RESULT-REVIEW-R1` receipt exists at generation time. It is not a protected acceptance, R-21 closure, Pinokio slot qualification, or RC decision. |

The failure is retained as evidence to review, not as a basis for inferring root cause beyond the recorded fields. In particular, an install inner exit of `0` does not establish an installed tuple or successful journey when the mounted verdict says the journey receipt is missing and the installed tuple/marker are absent.

### Accepted versus awaiting evidence at packet generation

| Evidence class | Disposition in R3 |
|---|---|
| Accepted R2 packet and its accepted deliverables | **ACCEPTED BASE; PRESERVED BYTE-FOR-BYTE.** |
| Existing accepted focused reviews already incorporated in R2 | **RETAINED AS IN R2**, with their original scope and limits. |
| Agent #164 publication and exact Git identity | **OPEN REVIEW INPUT / AWAITING REVIEW**; relay and Git identity only. |
| Agent #161 mounted platform evidence, run `41451268790` | **AWAITING REVIEW**; no new acceptance or qualification. |
| Pinokio R-21 exact-head evidence, run `41462939920` attempt `1` | **AWAITING REVIEW**; terminal failure evidence, no accepted result-review receipt. |
| I-08 freeze, coherent RC tuple, protected journey/segment review, qualification, C-15/R-34, publication, go/no-go | **PENDING / OPEN / NOT CLAIMED**, as applicable in the accepted base. |

No output from the named unaccepted running tasks `O-03-AG161-RESULT-REVIEW`, `O-03-AG164-REVIEW`, or `O-03-PIN-R21-EXACT-RESULT-REVIEW-R1` was used to manufacture acceptance. No task-local terminal/accepted receipt for Agent #164, the newly mounted Agent #161 run, or the exact-head R-21 result was present before this supplement was generated.

### Fail-closed gate statement

The packet remains **PENDING / NOT STARTED** for SR-PROJ and remains not an I-08 freeze, RC selection, qualification, publication, or go/no-go record. The Agent #164 publication does not close the Agent review or manifest/tuple binding. The Agent #161 platform evidence does not close a platform journey, R-37/C-15 row, or qualification gate. The Pinokio R-21 failure evidence does not close R-21, any M4-6/D9 slot, or a protected journey; it instead preserves a concrete awaiting-review failure boundary. No relay notice, exact-head publication, unaccepted review task, source review, or hosted diagnostic is treated as protected acceptance.

R-32 remains cancelled; no RV-1/RV-2/RV-3 ceremony, Astra round, test run, hosted job, RunPod action, generator, or installed journey was run by this packet refresh. Closure requires accepted, request-bound evidence on the selected coherent RC and the existing owner/reviewer decisions.

## R4 supplement — accepted exact-head Pinokio result review (2026-11-02T16:49:07Z)

This is an append-only refresh of the accepted R3 packet. The preceding R3 bytes are preserved byte-for-byte; this supplement changes only the disposition of the accepted O-03-PIN-R21 exact-result review from awaiting evidence to an **accepted bounded failed execution**. It does not turn the failed execution into protected evidence or close any protected gate.

### Accepted review and exact binding

The accepted review receipt is `/home/user/merge/ws/M-OPS/queue/done/O-03-PIN-R21-EXACT-RESULT-REVIEW-R1/result-review-receipt.md`, with review report `REPORT.md`, input inventory `INPUT-HASHES.txt`, and output inventory `OUTPUT-HASHES.txt`. Their verified SHA-256 values are respectively `b89dcbf39ad815be1e258b2fcf067920a90ce869952edef025f70708a09501b6`, `b2d834a3a62c8052009f0d407223335c2d4410e65a9935992dc8282faef553c6`, `83c6c302109fb05e6000a9e300c0bdc133a0dbc77e38ad7df04ebc022e184175`, and `e426c846ec3951564fa104d73bc42f40c54d711a2755946229c910da65b46261`.

The review is provenance-bound to request `R-21-PUBLISHED-TUPLE-R1`, candidate/workflow `a70615355d350f5eb95f896d333f451ad8e51c27`, lane `lane/M-PIN/R-21-VENUE-REF-BINDING-R1`, Actions run `41462939920`, and attempt `1`. The supplied request, verdict, diagnostics, and review receipt agree on that identity. The result is **FAILED** with `journey_receipt_missing`: setup exit `0`, journey exit `1`, watchdog exit `1`, and `timed_out: false`.

### Bounded failed execution disposition

The installer child exited `0`, but the result projections record `installer_success: false`, no install marker, no installed tuple, no port-17340 listener, and an initial Start engine error with Runtime unavailable. The observer recorded five Runtime `ECONNREFUSED` observations. The run did not reach a valid journey receipt, first click, Stop, retry, cleanup, candidate-App browser observation, or protected-state comparison. The review therefore establishes only this exact request-bound failed execution and its observed boundary. It does not establish a bootstrap root cause, a product-wide failure, F1/F2/F3 closure, an installed identity, protected qualification, or a successful Pinokio result. The declared tuple remains request input, not an installed tuple proven by this run.

This accepted review supersedes the earlier R3 statement that this exact Pinokio result was awaiting review, but it does not supersede the broader open R-21 obligations or the prior source-review findings. M-PIN must diagnose the producer-side failure and return a newly bound exact-head result with success/installed-tuple evidence and the complete later action receipt before any R-21 or protected qualification claim can be considered.

### Agent review evidence status at generation

- **Agent #161 — AWAITING_EVIDENCE.** At the pre-copy inspection, the current folder `/home/user/merge/ws/M-OPS/queue/running/O-03-AG161-RESULT-REVIEW/` had only task metadata and an `ALIVE` marker; by final validation it had produced review outputs but still had no `TERMINAL` marker or accepted disposition. Its receipt reports the three retained direct rows as failed and not exact-head execution of Agent #161. The orchestration notice and retained run `41451268790` are not an accepted Agent #161 review receipt, so the failed platform evidence and its two-scan limitation remain awaiting review.
- **Agent #164 — AWAITING_EVIDENCE.** The folder was inspected while running and moved by the host to `queue/done/O-03-AG164-REVIEW/` during this refresh. Its `TERMINAL` marker does not change the review disposition: `REPORT.md` and `review-receipt.md` explicitly retain `AWAITING_EVIDENCE` because the designated producer receipt is absent, the exact-candidate Windows job omitted the focused schema-ACL selectors, and the claimed `19/0` is not candidate-bound. Terminal status alone is not accepted evidence, so no #164 promotion is made.

### Gate and release boundary remains fail-closed

The accepted R3 gate states are otherwise unchanged: SR-PROJ remains **PENDING / NOT STARTED**; I-08 remains **MISSING**; I-11, R-37, R-34/C-15, required RC journeys, and the Pinokio obligations remain open or not accepted as previously recorded. No protected qualification, freeze, publication, release, or go/no-go is claimed. No product SHA, branch, integration handoff, or release artifact is produced. This supplement records review provenance and a bounded failure only.

## R5 supplement — accepted App #156 manifest/lock re-pin review (2026-11-02T16:46:03Z)

This is an append-only refresh of the accepted R4 packet. Every preceding byte is the immutable R4 prefix. This supplement incorporates only the accepted `O-03-APP156-REVIEW` static segment review. It does not start an SR-PROJ/Astra round, establish a freeze or coherent RC, qualify direct journeys, authorize release publication, or make a go/no-go decision.

### Accepted App #156 identity and static disposition

The accepted review is `queue/done/O-03-APP156-REVIEW/{REPORT.md,review-receipt.md,INPUT-HASHES.txt,OUTPUT-HASHES.txt}` for App #156 `I-AP-07-PUBLISHED-R1-R4`:

| Field | Accepted value / disposition |
|---|---|
| Published App head | `933c53a6fb1a11e5f8a01335d6252016d703beec` |
| Tree | `a601d9c6c2452a8218a516325da49926250fc93e` |
| Ordered parents | First `8948f0dad225cb8588ede98ec0ca240ac7a547c9`; second `7f3fb69dfb7e7082dcf3bc4596524aec62cb6d91` |
| Merge/delta | First parent is an ancestor of second; second-parent tree equals head; first-parent delta is exactly `public/install-manifest.json` and `runtime.lock.json`, 3 insertions and 3 deletions |
| Manifest pins | Agent `ab43bad818ef65ca1e929b973a2f61cd9fc4bf61`; Runtime `2776ba973b6c8f3555b5629b0840d9691a98d3c2` |
| Lock assertion | `runtime.lock.json.source_commit` equals manifest `.runtime.sha` exactly: `2776ba973b6c8f3555b5629b0840d9691a98d3c2` |
| Manifest digest | Raw SHA-256 `b2584f0bb5a949890364004572a018f2f2b568adcdba061e73b739e1d3d2d623f`, matching the supplied M-INT landing notice |
| Static disposition | **CLEAR — no source discrepancy found** in the manifest/lock re-pin review |

The accepted review also records manifest `schema_version: 1`, `release: 1`, Runtime schema digest `sha256:9628bb5fd6e1dbbec392dd39f2e01031622dcf52d7ccff7cd1f0ad10743dcb1c`, lock producer `astrid-runtime`, producer version `sha256:37a460109f3644e71010dd7626ea15cfbffe8e6e5705470d5ea56baa2b2a3576`, and 14 lock file entries. The raw lock SHA-256 is `1f2f4c6123209f25383af1936447e26eca6b2201f2240eee3804489da26f0c86`. Whitespace checks against both ordered parents were clean.

### Explicit direct-row and protected-evidence boundary

The M-INT notice at `2026-11-02T16:46:03Z` records a `direct_only` R0→R1 dispatch on candidate App `933c53a6`, Agent `ab43bad8`, Runtime `2776ba97`, and Pinokio `3a6a98ae`. The accepted review’s bounded mounted-context search identifies Actions run `41484584766`, but no request/result-bound direct-journey receipt, per-row outcome, inner exit, installed tuple, artifact digest, or terminal verdict is mounted. The dispatch is therefore not an execution result.

Disposition: **AWAITING_EVIDENCE / OPEN** for the direct R0→R1 rows and protected qualification. The review establishes no journey pass or failure, no R-37 row completion, no protected qualification, and no change to the existing fail-closed gate states. M-QUAL owns collection/assessment of request-bound row results; M-INT owns tuple reconciliation and qualification disposition.

### App #157 boundary and preserved gate state

The M-INT notice at `2026-11-02T16:51:11Z` is recorded only as orchestration context for the newly staged App #157 review. App #157 remains **PENDING / NOT ACCEPTED** here: neither its open task folder nor its report/landing notice is used as accepted review evidence. The #157 notice does not supersede, revise, or qualify the accepted #156 static review.

The R4 fail-closed states are preserved: SR-PROJ remains **PENDING / NOT STARTED**; direct journeys and protected qualification remain open; I-08 and final RC binding remain missing/pending; R-37 remains preparation-only/not accepted; I-11, C-15/R-34, publication/release, and go/no-go remain open and unclaimed. No freeze, qualification, release publication, or go/no-go is recorded by R5.

Review provenance and file hashes are recorded in the R5 `INPUT-HASHES.txt`, `OUTPUT-HASHES.txt`, `REPORT.md`, and `VALIDATION.txt`. No tests, generators, hosted jobs, RunPod work, installed journeys, product edits, branch changes, or external dispatches were performed by this packet refresh.

## R6 supplement — accepted Agent #165 and #166 focused segment reviews (2026-11-02)

This is an append-only refresh of the accepted R5 packet. Every preceding R5 byte is the immutable prefix. This supplement incorporates only the accepted `O-03-AG165-REVIEW` and `O-03-AG166-REVIEW` reports, receipts, and hash inventories, reconciled with the two specified M-INT landing notices. Both reviews are accepted **static source/merge reviews with disposition `ACCEPTED — AWAITING_EVIDENCE`**. They do not establish installed-wheel evidence, direct or Pinokio journey qualification, protected qualification, I-08, RC selection, freeze, publication, release, or go/no-go.

### Orchestration context and binding boundary

The supplied M-INT notices are recorded as relay context only:

| Notice | Relay claim retained | Boundary |
|---|---|---|
| `ORCHESTRATOR.md` `2026-11-02T17:07:41Z` | Agent #165 `AG-CI-H3-WHEEL-R9` landed at `ef8df6eb25c5de1691c357c2a6d0e3b87f5074c2`; the notice describes workflow/artifact-checksum changes, says installed Agent product code is unchanged against `ab43bad818ef65ca1e929b973a2f61cd9fc4bf61`, and says the manifest remains `b2584f0bb5a949890364004572a018f2f2b568adcdba061e73b739e1d3d2d623f`. | No hosted run ID, retained receipt, installed wheel, effective installed identity, selector result, or qualification result is supplied by the notice. The sidecar/C-AG-UNIT-ARTIFACT-R3 and CYCLE-COMBINE-R2 follow-up is an owner request, not a result. |
| `ORCHESTRATOR.md` `2026-11-02T17:10:22Z` | Agent #166 `INST-SCHEMA-PIN-FIX` landed at `b54117c9312165e2799554ec27480665d5cad2b1`; the notice describes a test-only producer pin to Runtime `2776ba973b6c8f3555b5629b0840d9691a98d3c2`, says installed Agent product code is unchanged against `ab43bad818ef65ca1e929b973a2f61cd9fc4bf61`, and says the manifest remains `b2584f0bb5a949890364004572a018f2f2b568adcdba061e73b739e1d3d2d623f`. | No test run, wheel build/acquisition, installed behavior, journey result, or qualification result is supplied by the notice. The next `UPD-LEGACY` route is an owner route, not closure. |

The accepted task receipts, not the relays, control the review dispositions and evidence limits below. The review labels are `lane/M-OPS/O-03-AG165-REVIEW` and `lane/M-OPS/O-03-AG166-REVIEW`; neither created a product branch or handoff.

### Agent #165 — `O-03-AG165-REVIEW`

| Field | Accepted value |
|---|---|
| Repository | `/home/user/merge/ws/M-OPS/repos/astrid-agent` |
| Head / tree | `ef8df6eb25c5de1691c357c2a6d0e3b87f5074c2` / `c003eb723c399c59e53caca57864cf82183a6f77` |
| Ordered parents | First `ab43bad818ef65ca1e929b973a2f61cd9fc4bf61`; second `053640803bceed3ec01e12143379955af1996ea3` |
| Disposition | **ACCEPTED — AWAITING_EVIDENCE** |
| First-parent delta | `M .github/workflows/lanes.yml`; `A remotion/artifacts/banodoco-timeline-composition-0.0.17.tgz.sha256`; 328 insertions and 5 deletions |
| Second-parent relation | The second-parent tree equals the head; the accepted receipt records an empty second-parent diff |
| Sidecar | Blob `4d41098899c7bd328d44e85fe1a07ebbd8b7192f`; text `cdd89c78d18b4b437a03efd1fff081f1ce00ee448d644ef8360fc1937df60f0f`, matching the committed Composition `.tgz` SHA-256 |
| Product surface | No Agent product-code path changed against the first parent |

The changed workflow is evidence plumbing for installed-wheel receipt collection, generated-file/artifact hashing, inner-exit capture, JUnit receipt collection, and the H3 selector. The accepted review identifies three evidence-boundary gaps:

1. Requested/effective tuple fields are recorded but Git success and equality of effective refs to requested refs are not asserted; `unavailable` can be recorded, while status is based on inner exits.
2. The Composition artifact and sidecar are hashed, but the workflow does not compare the sidecar text to the artifact digest. The committed source files matching is source evidence only.
3. JUnit counts are recorded without explicit positive non-empty, zero-failure/error, and no-skip assertions; selector exit remains primary.

These are evidence/plumbing findings, not a product or protected-contract finding. **M-AG2/M-CI** own tuple and JUnit/sidecar enforcement decisions; **M-CI** owns workflow follow-up. The accepted coverage inventory includes merge identity, ordered parents and trees, first/second-parent deltas, diff checks, product-code identity, `lanes.yml` installed-job behavior, and Composition package/lock/sidecar/artifact consistency. No hosted receipt, uploaded installed-wheel receipt, wheel hash, runner log, or installed-render result was inspected.

### Agent #166 — `O-03-AG166-REVIEW`

| Field | Accepted value |
|---|---|
| Repository | `/home/user/merge/ws/M-OPS/repos/astrid-agent` |
| Head / tree | `b54117c9312165e2799554ec27480665d5cad2b1` / `3eaa0f3326ec85635f10368aefca76716499b437` |
| Ordered parents | First `ef8df6eb25c5de1691c357c2a6d0e3b87f5074c2`; second `890ca2347bdc8c9377e178fd176a9eca3152e00f` |
| Disposition | **ACCEPTED — AWAITING_EVIDENCE** |
| First-parent delta | `M tests/test_installer_installed_schema.py`; 1 insertion and 1 deletion |
| Changed value | `PRODUCER` changes from `5453366458f7b4ee874c94d15ae7952e3e82d3c3` to `2776ba973b6c8f3555b5629b0840d9691a98d3c2` |
| Unchanged lock assertion | `runtime.lock.json.source_commit` names `2776ba973b6c8f3555b5629b0840d9691a98d3c2` |
| Product surface | No Runtime source, schema, installer implementation, or other shared product path changed |

The accepted source review found no concrete static product/shared-file defect. The changed test fixture is consistent with the unchanged lock and asserts the copied lock/source-commit relationship. The inherited installed-wheel workflow selects a different H3 test and was not treated as coverage for this changed test. The review does not prove wheel build, installed Runtime acquisition, test execution or success, installer behavior, journey behavior, or qualification. Route any execution gap to **M-R1FIX/M-CI** as appropriate.

### Combined coverage, limitations, and preserved gate state

The accepted review coverage is limited to Git identity/tree/parent inspection, first-parent changed surfaces, source/fixture assertions, workflow/source review, artifact-sidecar consistency, and the exact review-file inventories. The reviews do not supply execution evidence. No tests, generators, hosted jobs, RunPod work, installed journeys, or product files were run or changed by this packet refresh.

The R5 gate state is unchanged and remains fail-closed: SR-PROJ is **PENDING / NOT STARTED**; I-08 is **MISSING**; coherent RC binding, I-11, direct journeys, protected qualification, R-37, R-34/C-15, and required installed identities remain open or not accepted. The Agent #165 workflow landing does not become installed-wheel evidence, and the Agent #166 test-source pin does not become installed-schema or journey evidence. The notices' unchanged-manifest statement does not establish a fresh coherent tuple or freeze.

No I-08 freeze, RC selection, protected or journey qualification, release publication, release approval, or go/no-go is claimed. Existing owner routes and prior findings remain in force; this supplement adds the review-specific routes above only.

## R7 supplement — direct-Windows evidence and M-INT disposition (2026-11-02T17:31:27Z)

This is an append-only refresh of the accepted R6 packet. Every preceding byte is the immutable R6 prefix: 105,383 bytes, 616 lines, SHA-256 `0f0189ae8c7a3c17c0bf725a7154728d0625f4b2fbfd3e2949b72c30ca3c1e69`. This supplement records only the mounted direct-Windows evidence for Actions run `41488425066` and the specified M-INT Windows disposition. It does not start SR-PROJ/Astra review, qualify a journey, establish a coherent RC, close R1 bootstrap coverage, create an I-08 freeze, authorize publication, or make a release/go/no-go decision.

### M-INT disposition and harness boundary

The specified M-INT `2026-11-02T17:31:27Z` disposition is retained as follows. The confirmed first stop was a harness/environment defect, not a product defect: `pwsh` 7 launched Python `ci_journey.py`, which launched Windows PowerShell 5.1 with inherited PS7 `PSModulePath` entries. The mounted `windows-bootstrap-environment.log` shows Documents\PowerShell\Modules, Program Files\PowerShell\Modules, and PowerShell\7\Modules entries in that inherited environment. M-INT attributes the resulting incompatible `Microsoft.PowerShell.Security` load and frozen R0 `install.ps1` `Get-Acl` failure to that contamination; a normal user launch is not represented by this harness failure.

V-03 `lane/M-VENUE/V-03-PSMODULEPATH-R1@0f4c7ac5ea08882c4964367517ec9d70342c9f2e` is therefore recorded as advancing R0: in run `41488425066`, the R0 bootstrap exited `0` and the frozen Release-0 fixture hashes were unchanged. This is a bounded R0 bootstrap result, not a landed harness or qualification result. M-INT requires the same PSModulePath reset for the Release-1/update bootstrap before harness landing, followed by one exact-head direct-only dispatch with the full candidate and the required harness/M-CI boundary. No R1 bootstrap reset, harness landing, or co-sign is claimed here.

The new stop is after the R0 bootstrap: the owner assertion expected exactly the selected Runtime owner PID `3032` and reported two owner PIDs, `[2972, 3032]`. Do not relax the exactly-one-owner check. M-QUAL owns the owner-census diagnosis, with M-RT for Runtime ownership; the packet records the pair from `journey-result.json` and does not infer executable, parent, creation-time, or product ownership details that are not present in the mounted result. The retained `runtime-owner-processes.json` is a full process census receipt; its unrelated `RuntimeBroker.exe` entries are not substituted for the selected-owner pair.

The synchronized orchestration files render this same decision under adjacent `17:31:21Z`/`17:31:23Z` entries rather than a literal `17:31:27Z` heading. The packet uses the task brief's specified `17:31:27Z` notice label and reconciles only the matching four-point disposition above; no later orchestration notice is used.

### Exact run binding and mounted result

| Field | Evidence-bound value / disposition |
|---|---|
| Actions run / job | `41488425066`; the named Windows receipt set is mounted, but no separate Windows job identifier is present in the supplied files |
| Row | `direct-windows`, `direct`, `release0-to-release1`; receipt kind `release1-installed-lifecycle` |
| Runner | `windows-2022`; `Windows-10-10.0.20348-SP0`; `RUNNER_OS=Windows`, `ImageOS=win22`, `ImageVersion=20261026.347.1` |
| Candidate App | `933c53a6fb1a11e5f8a01335d6252016d703beec` |
| Candidate Agent | `ab43bad818ef65ca1e929b973a2f61cd9fc4bf61` |
| Candidate Runtime | `2776ba973b6c8f3555b5629b0840d9691a98d3c2`; schema digest `sha256:9628bb5fd6e1dbbec392dd39f2e01031622dcf52d7ccff7cd1f0ad10743dcb1c` |
| Candidate Pinokio | `3a6a98ae251ca126505245286e2d3e760f4348e9` |
| Candidate manifest | SHA-256 `b2584f0bb5a949890364004572a0182f2b568adcdba061e73b739e1d3d2d623f` |
| Candidate bootstrap artifacts | `public/install` `100bab1deb15d620abd910c8fb0c9a6d77f4945428a3229dac6b506705e3fd0d`; `public/install.ps1` `bba3f2edc41c047cb51475ceeac4489a04610af73a3af44bdc05287964d28258`; `public/install.sh` `591c4c51b148fccef2a7d1c45782d9ff949d5fd03d46cd82b5591e3a23d1b285` |
| Release-0 fixture | Agent `3204bb00135bec8ee2d2d3fb21eed5abfceca649`; Runtime `6d53a19070faf63fc15ae1f81417722edc2c8a5f`; fixture sums `58221a50aa7940e3121d6239ddeb92d6409df3711096aac1161c7ec130b755c0`; fixture manifest `fe12781e4da60d441595c08f84cb8d0390d840ab7c71227b8c4eff791553795e`; provenance `d87272d1d8638e8f7293fa9c728cfd8f6bfc63eeab0b7893324b6224c90787e5` |
| Result | `status: fail`; `installed_tuple: null`; `release_transition: null`; `semantic_data: null` |
| First failure | `RuntimeError: Expected exactly the selected Runtime owner 3032, found [2972, 3032]` |
| Lifecycle state | `health`, `one_runtime`, `release`, `semantic_data`, `service`, `task_settled`, and `unrelated_sentinel` are all `not-run` |
| Service-manager prerequisite | Task Scheduler, PowerShell `5.1.20348.5756`, required, `outcome: pass`; this prerequisite pass is not a lifecycle or qualification pass |

The before- and after-cleanup `journey-result.json` files agree on the candidate, manifest, R0 fixture, `status: fail`, null installed tuple, null release transition, not-run lifecycle checks, and the exact owner stop. The before/after R0 bootstrap logs are byte-identical and the result records bootstrap exit `0` with the same bootstrap-log SHA-256 `5cf571e46a9d28fad4d56bae90525243ad9c6c4313c68b9e54041848a9ce839d`. The logs are the generic installer output; the exit and unchanged-frozen-hash conclusion come from the request-bound receipt and M-INT disposition.

### Cleanup evidence and limits

The after-cleanup evidence records the `Astrid Runtime` scheduled task as absent with query exit `0`; `manager-state-after-cleanup.json` reports `not-found`, `preflight-scheduled-task.txt` classifies the task as absent, and `unit-file-after-cleanup.txt` says the Task Scheduler definition is unavailable. `final-uninstall.log` says Astrid was unregistered while the program `versions` directory and workspace remained. These are cleanup observations only. They do not prove the failed row, R1 bootstrap, lifecycle, installed identity, or owner-census cause was corrected. The mounted process census does not provide the missing owner metadata for PIDs `2972` and `3032`.

### Gate and coverage disposition

| Obligation / gate | R7 disposition |
|---|---|
| R0 bootstrap / PSModulePath harness stop | **ADVANCED / BOUNDED PASS** — exit `0` on `41488425066`, frozen R0 bytes/hashes unchanged; no harness landing claimed |
| R1/update bootstrap | **OPEN** — the exact run stopped before it; no R1 bootstrap log or exit is present; reset and exact-head proof remain required |
| Exactly-one Runtime owner | **FAILED / OPEN** — two selected owners `[2972, 3032]`; assertion remains fail-closed and must not be weakened |
| Direct Windows journey | **FAILED; NOT QUALIFICATION EVIDENCE** — installed tuple is null and all lifecycle checks are not-run |
| R-37/C-15, protected/direct qualification, I-08/I-11, RC binding | **PENDING / NOT ACCEPTED / UNCLAIMED** |
| I-08 freeze, publication, release, freeze, and go/no-go | **UNCLAIMED** — unchanged from R6; this supplement is not an authorization or approval |

The R0 harness stop is resolved only in the narrow sense that V-03 advanced this exact row past the R0 bootstrap. It does not resolve R1 bootstrap coverage or the exact owner-process stop. No direct/protected qualification, I-08, RC, freeze, publication, release approval, I-08 review, I-08 gate, or go/no-go is claimed.

### Owner routes and limitations

- **M-VENUE / M-CI:** apply and co-sign the PSModulePath reset to the Release-1/update PowerShell child; preserve frozen R0 bytes, shipped installers, and checks; dispatch exactly one full-candidate exact-head direct-only Windows proof after landing conditions are satisfied.
- **M-QUAL, with M-RT:** diagnose whether the two selected Runtime owners are a scheduled-task/second-launch harness condition or two real Runtime owners after R0 install. Retain the exactly-one-owner assertion and return request-bound process metadata and installed/runtime identity before any qualification decision.
- **M-INT:** reconcile the next exact-head run and any harness landing against the candidate, manifest, artifact hashes, inner exits, R0/R1 bootstrap receipts, and owner census. The current notice is relay/disposition context, not a qualification receipt.
- **M-OPS:** preserve this packet as documentation-only under `lane/M-OPS/O-06-WINDOWS-R7`; no product branch or integration handoff exists.

No tests, product commands, hosted jobs, RunPod work, installed journeys, generators, branch changes, product edits, publication, or release actions were run by this packet refresh. The evidence is one direct Windows run plus its before/after-cleanup receipts. It does not prove behavior on other OS/channel rows, the R-37 inventory, protected journeys, Pinokio slots, installed-wheel evidence, I-08, freeze, publication, or go/no-go.

## R8 supplement — Hannah Option B Pinokio custody decision and bounded collector review (2026-11-02T20:00:34Z / 20:01:05Z)

This is an append-only refresh of the accepted R7 packet. Every preceding byte is the immutable R7 prefix: 114,423 bytes, 677 lines, SHA-256 `25e7b38ec6b068ca5692044f1fd85332b7e4d7db2e6a43a5c94d41f965aada35`. This supplement reconciles only Hannah's Option B decision recorded by M-INT at `2026-11-02T20:00:30Z`, the governing decision at `DECISIONS.md` `2026-11-02T20:00:34Z`, and the `2026-11-02T20:01:05Z` Rule 35 notice. It also carries the accepted bounded collector review `queue/done/O-03-PINOKIO-COLLECTOR-REVIEW/`. It does not start an SR-PROJ/Astra round, create a qualification receipt, establish a coherent RC, close a protected path, create an I-08 freeze, authorize publication, or make a release/go/no-go decision.

### Governing decision and fail-closed scope

Hannah selected **Option B** for Pinokio continuous custody. For Release 1, all three OS Pinokio rows may qualify only from harness-owned process and service snapshots at every action boundary, bounded in-action polling, independent initial and final censuses, and data invariants. The R1 snapshot/census/data-invariant scope is: install; start; stop; retry-after-stop; update; uninstall end-state; exactly-one-owner; and data kept or removed. The collector schema also retains the six ordered action boundaries `start`, `ready`, `stop`, `update`, `rollback`, and `uninstall`, with `before`, immediate `during`, bounded interval `during`, and final `boundary` observations where that action is exercised.

The following are **not** Release-1 continuity evidence and are explicitly deferred, not deleted: gap-free continuous custody and removal-authorization continuity. The exact 24 continuous-custody IDs are eight per OS:

| OS | Exact deferred-R1.x IDs |
|---|---|
| Linux | `interruption-recovery:pinokio-backend-linux`; `rollback:pinokio-backend-linux`; `keep_data_uninstall:pinokio-backend-linux`; `stop_safety:pinokio-backend-linux`; `interruption-recovery:pinokio-fully-local-linux`; `rollback:pinokio-fully-local-linux`; `keep_data_uninstall:pinokio-fully-local-linux`; `stop_safety:pinokio-fully-local-linux` |
| macOS | `interruption-recovery:pinokio-backend-macos`; `rollback:pinokio-backend-macos`; `keep_data_uninstall:pinokio-backend-macos`; `stop_safety:pinokio-backend-macos`; `interruption-recovery:pinokio-fully-local-macos`; `rollback:pinokio-fully-local-macos`; `keep_data_uninstall:pinokio-fully-local-macos`; `stop_safety:pinokio-fully-local-macos` |
| Windows | `interruption-recovery:pinokio-backend-windows`; `rollback:pinokio-backend-windows`; `keep_data_uninstall:pinokio-backend-windows`; `stop_safety:pinokio-backend-windows`; `interruption-recovery:pinokio-fully-local-windows`; `rollback:pinokio-fully-local-windows`; `keep_data_uninstall:pinokio-fully-local-windows`; `stop_safety:pinokio-fully-local-windows` |

These IDs are marked `deferred-R1.x (Hannah decision, 2026-11-02 option B)` with the controlling pointer `docs/merge-plan/DECISIONS.md:316` / decision timestamp `2026-11-02T20:00:34Z`. R1 excludes exactly this deferral set explicitly; no other continuity, owner, data, qualification, I-08, RC, freeze, publication, or go/no-go check is relaxed. Snapshots, polling, equal initial/final state, or a `collector-complete` receipt must never be represented as continuous custody or removal authorization. Gap-free observers named by the decision (Windows ETW, Linux eBPF/audit equivalent, and `eslogger` on hannah-mac) remain R1.x work.

The `2026-11-02T20:01:05Z` Rule 35 notice changes decision routing only: M-INT decides Release-1 product, design, scope, and proof-policy questions by default under rule 21, records `DECIDED-BY-DEFAULT` where applicable, and escalates only under rule 34. It does not change this packet's gate states or authorize external, public, credentialed, or destructive actions.

### Accepted bounded collector review and receipt boundary

The accepted review disposition is **ACCEPTED — bounded source-level non-continuity design review only; AWAITING_AUTHENTIC_EVIDENCE**. It reviewed exact source heads Agent `a6b68d5b5478326a4db1971966d33a2cfe61d091` (tree `f2380335f2f176b723e06ff205af2e6c5267fd0f`, parent `142d7b0ba9de0971cb2fd642f9de8660ae887086`) and Pinokio `95aafb5a029c3f53cb2268d8596909eab0deb3c4` (tree `b93f65758c0f9450b156971accd0fa1d79a22651`, parent `5f5cd159e90f9054b769924007f3c0ad1616a5bc`). The review confirmed source-level collector shape only; it did not accept a producer receipt, hosted execution, M4-6/R-30/R-36 evidence, qualification, freeze, publication, or protected path.

For the four reviewed slots—`pinokio-backend-macos`, `pinokio-fully-local-macos`, `pinokio-backend-windows`, and `pinokio-fully-local-windows`—the supported snapshot-compatible obligation IDs are:

| OS | Exact IDs |
|---|---|
| macOS | `fresh-install:direct-macos`; `fresh-install:pinokio-backend-macos`; `fresh-install:pinokio-fully-local-macos`; `populated-r0-update:direct-macos`; `release2-button-direct-macos`; `release2-install-line-direct-macos`; `release2-pinokio-update-pinokio-backend-macos`; `release2-pinokio-update-pinokio-fully-local-macos` |
| Windows | `fresh-install:direct-windows`; `fresh-install:pinokio-backend-windows`; `fresh-install:pinokio-fully-local-windows`; `populated-r0-update:direct-windows`; `release2-button-direct-windows`; `release2-install-line-direct-windows`; `release2-pinokio-update-pinokio-backend-windows`; `release2-pinokio-update-pinokio-fully-local-windows` |

Here `{macos,windows}` denotes the two separately named OS IDs, not an unverified wildcard. The accepted review also records analogous Linux inventory IDs, but its R-25 component exposes only the four macOS/Windows slots; this packet infers no Linux collector implementation, producer evidence, or qualification from that review. The Hannah decision separately requires the corresponding harness-owned R1 boundary evidence on all three OSes.

The accepted source design requires an immutable `request.json`, complete `observations/`, and `snapshot-receipt.json`, with raw-byte SHA-256, byte size, action, phase, sample, capture time bounds, collector identity, profile/limits, and binding digest. The request and receipt must bind the exact four-repository tuple, manifest/artifact hashes, repository/workflow IDs and SHAs, run/attempt/head, producer job, artifact, slot, reviewed source hashes, and ordered action IDs/hashes. A producer-declared artifact ID, archive hash, or source hash is non-authoritative until independently authenticated and finalized.

The reviewed finalizer is fail-closed. It rejects omitted, skipped, out-of-order, duplicate, extra, mutated, or substituted action/snapshot/filesystem records; failed polling or capture; forged structural/raw-byte/source values; wrong tuple, candidate/head, source closure, slot, workflow, run, attempt, job, artifact, or action binding; replay or duplicate evidence; producer-only artifact assertions; missing external artifact-origin joins; missing independently authenticated finalization/post-check admission; and absent hash-bound action/filesystem records. These rejection predicates remain in force for the snapshot-compatible R1 scope.

The review's exact continuity limitation is 24 IDs, eight per OS as listed above. Snapshots cannot prove that no process existed, was replaced, or acted between samples and cannot authorize or finalize removal. The collector's `collector-complete` marker is not M4-6 acceptance. An authentic producer/origin/finalization join and the adopted gap-free observer remain required for the deferred set.

### Coverage, owner routes, and preserved gates

| Coverage item | R8 disposition |
|---|---|
| R7 immutable prefix | **PASS** — exact 114,423-byte, SHA-256-matched prefix; all R7 bytes and prior gate states preserved before this supplement |
| Release-1 Pinokio snapshot/census/data-invariant scope | **ELIGIBLE IN PRINCIPLE, NOT PROVEN** — all three OS rows may qualify only with harness-owned boundary snapshots, bounded polling, independent initial/final censuses, data invariants, exact tuple/run/source binding, and authenticated finalization |
| Collector source review | **ACCEPTED, BOUNDED** — reviewed Agent/Pinokio heads and source design only; no producer receipt or hosted result |
| 24 gap-free custody/removal-continuity IDs | **DEFERRED-R1.x** — exact IDs above, explicitly excluded from R1 with `DECISIONS.md:316`; snapshots never satisfy them |
| Other M4-6, R-37/C-15, I-08/I-11, RC, freeze, publication, release, and go/no-go gates | **OPEN / PENDING / UNCLAIMED** — unchanged from the R7 prefix unless separately accepted by their own owner receipts |

Owner routes: **M-PIN** supplies authentic all-OS action-session artifacts and producer finalization; **M-QUAL/M-CI** maintain the inventory/checker, apply the explicit deferred-R1.x labels, independently join artifact to producer run/attempt/slot, and retain all negative rejection cases; **M-QUAL/M-PIN/M-INT** preserve the exact 24-ID map and route R1.x observer/scope work; **M-INT** reconciles accepted receipts and lands any protected checker change; **M-OPS** maintains this documentation-only packet. No route authorizes weakening another manager's continuity check.

No product tests, hosted jobs, RunPod work, installed journeys, product/harness commands, generators, branch changes, product edits, publication, release, or external dispatch was run by this refresh. The packet remains fail-closed: the R1 snapshot-compatible design boundary is recorded, the gap-free continuity/removal-authorization boundary is explicitly deferred, and authentic producer/origin/finalization evidence plus all other qualification and release gates remain open.

## Supplemental rehearsal evidence — O-01-R1 VibeComfy identity reconciliation (2026-11-02)

This append-only supplement records the accepted O-01-R1 I-08 rehearsal receipt. It
does not rewrite the packet's earlier tables or dispositions. The rehearsal is a
dry-run identity-boundary result, not an I-08 freeze, RC selection, qualification,
publication, release approval, or go/no-go decision.

### Result and exact identity boundary

O-01-R1 is **DRY-RUN PASS** for the protected VibeComfy identity comparison at
App commit `87d9afc761bc4eb037170b1f6254273347a6127f` on
`lane/M-OPS/O-01-R1`, with required retry base
`c2588c2b3a8d2911a9b8dc8b202d18939f7ff47b` and base parent
`7f6f99208100e895efebf39c70bc30af6cee0884`. Its only committed product-lane
change is the rehearsal stand-in/tooling path recorded by the accepted receipt.

The protected identity boundary is exact:

| Field | Rehearsal value / boundary |
|---|---|
| Runtime | `1a275bb339ae897e52a5be5e1da99e846e402037` |
| Agent | `ddd5d278796641901bd11508863a8ea21e6c9697` |
| App | `7f6f99208100e895efebf39c70bc30af6cee0884` |
| Pinokio | `89ed7aacae72bb48eb97ff9ff95c222bdd7e71e4` — context only |
| VibeComfy source | `https://github.com/peteromallet/VibeComfy.git` @ `ac0ee1b80991f2414dae27d264af4b5e2890a7a6` |
| Agent recipe pin-file SHA-256 | `7aca02677d65905d2c9a4e3adf88c95cabc6542ba59e0a9ae74308ac71de3602` |
| Expected effective installed identity | `{"sha":"ac0ee1b80991f2414dae27d264af4b5e2890a7a6"}` |
| Tuple boundary | Runtime / Agent / App; the VibeComfy field is derived protected installed identity and does not grow the public tuple |

The governing rule is `DECISIONS.md` 2026-11-02T20:04:00Z: the pinned VibeComfy
source is part of protected installed identity, its expected value is derived
from the candidate Agent pin and compared exactly, and the candidate tuple does
not grow. The accepted rehearsal follows that rule. The public candidate
manifest has no top-level `vibecomfy` component; the R2 stand-in adds only the
derived effective value and is not a public manifest, publication, or installed
product.

### Passing dry-run artifacts and focused coverage

The accepted receipt retained these exact artifacts:

| Artifact | SHA-256 | Meaning |
|---|---|---|
| `queue/done/O-01-R1/public-manifest-dry-run.json` | `d0a110efd5e6bfbf75ee093746a186cdc3cb177060d6003f59192ab390a3e829` | Release-1 public candidate; Runtime/Agent/schema exact; no top-level VibeComfy field |
| `queue/done/O-01-R1/stand-in-r2-dry-run.json` | `0b19a66dc3ae7b8648d2117ebf20b616f174d1b114530f728d0cabc562dccaa8` | Non-public Release-2 stand-in with the Agent-derived effective identity |
| `queue/done/O-01-R1/identity-comparison.txt` | `b9dcf24fb1856464c36467019e858d723aa0dd3452892230ca8dafc7559df721` | Independent exact comparison and canonical-byte result |

The focused reconstruction and comparison passed. The Agent
`_manifest_preservation_preflight` passed and returned the stand-in hash. The
required Node installer suite passed **24/24, 0 skipped** on one retry; its first
23/24 attempt had one external raw-GitHub HTTP 503 and no local assertion failure.
Python syntax, Runtime vendor-lock, App conformance **9/9**, and diff checks also
passed. No product install line, browser, Pinokio, hosted, GPU, or other
installed journey ran.

### Disposition, limitations, and owner routes

- **I-08:** **MISSING / PENDING.** This rehearsal supplies no freeze receipt and does not select or freeze a coherent current RC tuple.
- **Freeze / qualification:** **OPEN / NOT ESTABLISHED.** No installed identity was observed, and no R-37/C-15 or other qualification row is closed by this result.
- **Publication / deployment:** **NO PUBLICATION OR DEPLOYMENT.** The dry-run candidate and stand-in are retained artifacts only; no public manifest or release state changed.
- **SR-PROJ / go-no-go:** **REVIEW NOT STARTED; GO/NO-GO OPEN.** The rehearsal is not an Astra/project-review round and is not a gate pass.
- The pin-file digest binds the Agent source pin; it is not a VibeComfy source-content/publication digest. The rehearsal does not prove public availability, source-content publication, effective installed bytes, or protected-journey behavior.
- At a real freeze, M-INT must resolve a current trusted published-main baseline and bind the selected Runtime/Agent/App/Pinokio tuple, manifest freshness, receipts, and artifact hashes. M-AG/M-INT must attest the selected VibeComfy source/publication binding; M-QUAL must bind the effective installed identity in an authorized qualifying journey.

All earlier packet limitations and open owner routes remain in force. This
supplement is evidence of a successful rehearsal boundary only; it does not
promote O-01-R1 to an I-08 freeze, qualification, publication, or gate pass.

## O-06-R10 supplement — hosted qualification evidence refresh (2026-11-02T21:12:28Z)

This is an append-only refresh of the accepted O-06-R9 packet. Every preceding
byte is the immutable O-06-R9 prefix: 129,062 bytes, 798 lines, SHA-256
`593f9d18794fa6a55fa62e67772c1916c649acdc3d69af9cd77203079b887919`.
The documentation-only lane is `lane/M-OPS/O-06-R10`; its base is exactly
`queue/done/O-06-R9/SR-PROJ.md`. This supplement records observed evidence from
Actions runs `41524280243` and `41521795291`. It does not establish
qualification, an I-08 freeze, RC approval, publication/deployment, or a
go/no-go decision.

### Disposition at a glance

| Evidence item | Observed result | Packet disposition |
|---|---|---|
| Hosted App direct journey, run `41524280243` | 16 passed, 1 failed, 26 skipped/not run; inner test exit 1 and fail-closed checker exit 5 | **FAILED / PARTIAL; not qualification evidence** |
| Hosted three-products, run `41524280243` | 2 failed specs, 13 skipped/not run; 3 failed result records across the two failing specs; inner test exit 1 and checker exit 5 | **FAILED / PARTIAL; not qualification evidence** |
| Direct Release-0→Release-1 rows, run `41521795291` | Linux, macOS, and Windows all failed before protected lifecycle assertions; each workflow exit 1 | **FAILED / PARTIAL; no row passes** |
| Qualification / I-08 / RC / publication / go/no-go | Not proven by these runs | **OPEN / UNCLAIMED** |

The skipped counts are retained as observed coverage loss, not as passes. The
two hosted jobs were not re-run by this packet refresh.

### Run 41524280243 — hosted App direct and three-products evidence

The exact recorded App ref/head is
`lane/M-APP/I-AP-18-R10-SEED-COMMIT` /
`1582b038ae44d215842e5fd8414abab4d54d4dc6` (subject `test: migrate
extensions seed to timeline commit`). The companion source tuple recorded in
`three-products-summary.json` and the hosted log is:

| Component | Exact head / binding |
|---|---|
| App | `1582b038ae44d215842e5fd8414abab4d54d4dc6` |
| Agent (`astridHead`) | `f4f4fc99b42bcf4421f2aa27ad07ddad85fc18f9` |
| Runtime (`runtimeHead`) | `255ecda21ba904520eb867936ef173cc5f7d3ae4` |
| App vendor lock source | `2776ba973b6c8f3555b5629b0840d9691a98d3c2` |
| Agent vendor lock source | `255ecda21ba904520eb867936ef173cc5f7d3ae4` |
| Producer-version lock | `sha256:37a460109f3644e71010dd7626ea15cfbffe8e6e5705470d5ea56baa2b2a3576` for both locks |
| Schema digest | `sha256:9628bb5fd6e1dbbec392dd39f2e01031622dcf52d7ccff7cd1f0ad10743dcb1c` for App, Agent, and Runtime |

No separate Actions ref beyond the App branch ref above is emitted in the
supplied JSON/log evidence; this packet does not infer one.

#### Direct journey

Evidence paths:

`/home/user/repo/docs/merge-plan/evidence/actions/41524280243/direct-journey/direct-journey.json`

`/home/user/repo/docs/merge-plan/evidence/actions/41524280243/logs/direct-journey-125547916854.log`

The JSON reports 43 specs: `expected=16`, `skipped=26`, `unexpected=1`,
`flaky=0`. The first real failure is
`direct-journey.spec.ts:433:27`, in
`App: a second writer saves first, the App shows the conflict and keeps the
draft`: expected HTTP `200`, received `410`, while posting the second
composition revision. The decisive log records `1 failed`, `26 did not run`,
`16 passed`, then the test process exits `1`; the subsequent fail-closed jq
coverage checker exits `5` because it requires zero unexpected and zero
skipped tests. The 26 not-run tests were serially skipped after the earlier
failure, not declared successful skips.

#### Three-products

Evidence paths:

`/home/user/repo/docs/merge-plan/evidence/actions/41524280243/three-products/three-products-summary.json`

`/home/user/repo/docs/merge-plan/evidence/actions/41524280243/three-products/three-products.json`

`/home/user/repo/docs/merge-plan/evidence/actions/41524280243/logs/three-products-125547917601.log`

The summary says `reported, never judged`, with one case named `M2 native None
single: ordinary default crossfade clears raw mirrors, reloads, retries exactly
after restart and undoes through Runtime`, status `failed`. Its source tuple
matches the table above. The Playwright JSON reports 15 specs:
`expected=0`, `skipped=13`, `unexpected=2`, `flaky=0`; the two failing specs
produce three failed result records because the M2 case has two recorded
failures. The first log failure is
`three-products.spec.ts:458:138`: the App could not show `Complete` for
`F2 Marker` in the task peek surface. The second failing spec is the M2
transition clear assertion at `three-products.spec.ts:2852:68`, where expected
`parent/clip:f2-transition-clip` was received as `null`. The log records `2
failed`, `13 did not run`, test-process exit `1`, and checker exit `5`.

These are App/hosted three-products and direct-journey observations. They route
the product assertion failures to **M-APP** for product/harness triage, with
**M-QUAL** retaining the failed coverage and checker disposition. They do not
close an App gate, and they do not establish that the Runtime or Agent is the
root cause.

### Run 41521795291 — direct-row evidence

The exact workflow dispatch ref/head recorded by the tuple-evidence job is
`cabeb4032909d21f8c8305137e8ceeb60ef5300a` for the driver Agent checkout.
The exact candidate tuple recorded by the workflow and all three row receipts
is:

| Component | Exact candidate head |
|---|---|
| App | `79f7e5397c044c4c97dc7e97b20ad96f787e5247` |
| Agent | `ab43bad818ef65ca1e929b973a2f61cd9fc4bf61` |
| Runtime | `2776ba973b6c8f3555b5629b0840d9691a98d3c2` |
| Pinokio | `3a6a98ae251ca126505245286e2d3e760f4348e9` |
| Release-1 manifest SHA-256 | `b2584f0bb5a949890364004572a0182f2b568adcdba061e73b739e1d3d2d623f` |

The exact tuple evidence log records `CONFIG_RESULT=success`,
`JOURNEY_RESULT=failure`, `M4_4B_RESULT=skipped`, `M4_6_RESULT=skipped`, and
`RELEASE2_RESULT=skipped`; its own process exits `1`. The candidate identity
was therefore checked, but the journey result failed and the optional/dependent
results were skipped. This is not a successful exact-tuple qualification
receipt.

The requested evidence roots are the three complete receipt trees:

`/home/user/repo/docs/merge-plan/evidence/actions/41521795291/journey-receipt-direct-linux/`

`/home/user/repo/docs/merge-plan/evidence/actions/41521795291/journey-receipt-direct-macos/`

`/home/user/repo/docs/merge-plan/evidence/actions/41521795291/journey-receipt-direct-windows/`

The workflow logs are:

`/home/user/repo/docs/merge-plan/evidence/actions/41521795291/logs/journey_direct-linux_direct_release0-to-release1_ubuntu-24.04_sh_linux_-125539570181.log`

`/home/user/repo/docs/merge-plan/evidence/actions/41521795291/logs/journey_direct-macos_direct_release0-to-release1_macos-15_sh_macos_-125539570562.log`

`/home/user/repo/docs/merge-plan/evidence/actions/41521795291/logs/journey_direct-windows_direct_release0-to-release1_windows-2022_powershell.exe_w-125539570039.log`

`/home/user/repo/docs/merge-plan/evidence/actions/41521795291/logs/Journey_evidence_exact_tuple_-125551121507.log`

| Row | Receipt result and first real failure | Coverage / inner exit |
|---|---|---|
| Linux / `ubuntu-24.04` | `status=fail`; first journey error `RuntimeError: update-bootstrap.log: exit 1`. The staged install evidence captures `RuntimeError: Runtime discovery owner identity changed; refusing to change it.` | `health`, `one_runtime`, `release`, `semantic_data`, `service`, `task_settled`, and `unrelated_sentinel` are all `not-run`; workflow exit `1` |
| macOS / `macos-15` | `status=fail`; first journey error `RuntimeError: update-bootstrap.log: exit 1`. The staged install evidence captures `ValueError: The Astrid service .../haus.astrid.runtime.plist has an unknown or changed action/owner. Re-run the install line to repair it.` | The same seven protected checks are `not-run`; after-cleanup service observation also records manager command exit `113`; workflow exit `1` |
| Windows / `windows-2022` | `status=fail`; Release-0 bootstrap evidence has exit code `0`, then the first journey failure is `RuntimeError: Timed out after 900s: pack-host is running with its current ready file.` | The same seven protected checks are `not-run`; workflow exit `1` |

All three rows report `final_cleanup=pass`; cleanup success does not convert the
failed journey into a pass. The direct-row evidence supports **M-QUAL** triage
of the driver/preflight/receipt boundary for all three rows. The Linux and
macOS installer/update ownership failures support routing the update-path
investigation to **M-R1FIX**, consistent with the existing known-failure rows
for Release-0 update identity/ordering; they do not prove those rows are fixed
or identify a new root cause. The Windows `pack-host` readiness timeout
supports M-QUAL harness/service-settlement diagnostics; it is insufficient by
itself to assign a Runtime product defect to **M-RT**. No direct-row evidence
supports an **M-APP** route because no App assertion ran before the row failed.
The existing known-failures owner boundary remains unchanged: this supplement
does not reassign installer/toolchain, Pinokio, GPU, or other rows, and does not
close or weaken any known failure.

### Coverage, routes, and open gates

| Item | Disposition |
|---|---|
| O-06-R9 prefix | **PASS** — exact 129,062-byte prefix preserved; verified separately with `cmp -n` |
| Run `41524280243` exact tuple and hosted test results | **OBSERVED / FAILED-PARTIAL** — exact App ref/head and source tuple recorded; direct and three-products gates fail with skipped coverage |
| Run `41521795291` exact tuple and direct rows | **OBSERVED / FAILED** — exact candidate tuple recorded; Linux/macOS/Windows rows fail before protected lifecycle checks |
| Direct-row owner routing | **ROUTED FOR TRIAGE ONLY** — M-QUAL all rows; M-R1FIX Linux/macOS update-path evidence; no unsupported M-RT/M-APP root-cause assignment |
| Qualification / R-36/R-37 / M4-6 or other release gates | **OPEN / NOT PROVEN** — failed or partial runs cannot close them |
| I-08 freeze / coherent RC | **OPEN / MISSING** — no freeze receipt or RC approval is created by this supplement |
| Publication / deployment / release approval | **NOT RUN / NOT AUTHORIZED** |
| Go/no-go | **OPEN / UNCLAIMED** |

This packet is documentation-only. No product code, scenario inventory,
checker, frozen contract, release reference, publication/deployment state,
`STATUS.md`, `ORCHESTRATOR.md`, other task folder, hosted journey, or product
test was modified or run by this refresh. The evidence is retained for owner
triage and future exact-head reproof; it is not a qualification receipt or a
gate pass.

## O-06-R11 supplement — direct M4-6 Option-B proof-rule refresh (2026-11-02T22:04:52Z)

This is an append-only supplement to the accepted O-06-R10 packet. Every byte
before this heading is the immutable O-06-R10 prefix: 139,748 bytes, 970 lines,
SHA-256 `b19ece4c2f48da315dfdd327791346054bb834ecf9a58460aa23f32450f37489`.
The documentation-only label is `lane/M-OPS/O-06-R11`; the base is exactly
`queue/done/O-06-R10/SR-PROJ.md`. This supplement records the newest M-INT rule
36 P13 decision only. It claims no new hosted run, replay, proof, qualification,
freeze, publication, deployment, release approval, or go/no-go result.

### P13 ruling and evidence-class boundary

The governing `M-INT 2026-11-02T22:04:52Z` ruling states that Option B extends
to the direct M4-6 slots, consistently with the Hannah Pinokio Option-B
decision. For Release 1, the direct M4-6 slots use the checkpoint evidence
class: harness-owned step-boundary snapshots, bounded in-step polling,
independent initial and final censuses, and data invariants for what is kept or
removed. This is a proof-rule and scope record, not evidence that any direct
slot has supplied those artifacts.

Containment obligations and continuous-history obligations remain
`deferred-R1.x` in the inventory, exactly as the Option-B boundary requires,
and are never counted as passed. In particular, checkpoint snapshots are
state-at-boundary evidence only: they must never be represented as continuous
history, continuous custody, containment, or removal-authorization continuity.
The corresponding continuous observers and containment work remain R1.x work.

### Direct-slot coverage inventory

The governing task inventory describes nine M4-6 slots across the three OSes
and three channels. This P13 extension applies to the three direct-channel
slots only:

| Direct M4-6 slot | R1 evidence class | P13 disposition |
|---|---|---|
| Linux | Step-boundary snapshots; bounded in-step polling; independent initial/final censuses; data kept/removed invariants | Checkpoint obligations required for R1; containment and continuous history `deferred-R1.x`, never passed |
| macOS | Step-boundary snapshots; bounded in-step polling; independent initial/final censuses; data kept/removed invariants | Checkpoint obligations required for R1; containment and continuous history `deferred-R1.x`, never passed |
| Windows | Step-boundary snapshots; bounded in-step polling; independent initial/final censuses; data kept/removed invariants | Checkpoint obligations required for R1; containment and continuous history `deferred-R1.x`, never passed |

This table is an evidence-class map, not a receipt or row result. It does not
replace the direct-slot inventory, checker, authenticated tuple binding,
producer/origin join, finalization, or any owner acceptance. The P13 ruling
requests the corresponding inventory/checker reconciliation and M-CI co-sign;
this documentation-only task makes no protected inventory or checker change.

### Preserved evidence, gates, and limitations

All O-06-R10 evidence, failure dispositions, owner routes, PENDING/OPEN states,
and non-claims remain exactly as written in the immutable prefix. The direct
rows and M4-6 qualification gate remain **OPEN / NOT PROVEN**; no checkpoint
artifact is supplied or newly judged here. I-08 remains **MISSING / PENDING**,
coherent RC approval remains absent, qualification remains **OPEN / NOT
ESTABLISHED**, publication/deployment and release approval remain **NOT RUN /
NOT AUTHORIZED**, and go/no-go remains **OPEN / UNCLAIMED**. The packet remains
documentation-only and does not claim freeze, qualification, publication,
release, or approval.

No product code, scenario inventory/checker, frozen contract, release
reference, publication or deployment state, `STATUS.md`, `ORCHESTRATOR.md`,
prior task folder, other manager file, hosted job, RunPod action, journey, or
product test was modified or run. The new text is driven solely by the
published P13 decision and does not assert that the direct M4-6 checkpoint,
containment, or continuous-history obligations have been satisfied.

## O-06-R12 supplement — terminal M-PIN diagnostic refresh (2026-11-02T23:35:37Z)

This is an append-only supplement to the accepted O-06-R11 packet. Every byte
before this heading is the immutable O-06-R11 prefix: 143,857 bytes, 1,034
lines, SHA-256
`81185301f1e700d2f0080fdf7d36104915df5bed2ec7a4c466938df364c6734e`.
The documentation-only label is `lane/M-OPS/O-06-R12`; the base is exactly
`queue/done/O-06-R11/SR-PROJ.md`. This supplement records three terminal M-PIN
diagnostic receipts and their evidence boundaries. It claims no qualification,
I-08 freeze, coherent RC, publication, deployment, release approval, or
go/no-go result.

### Evidence class and shared tuple boundary

The current/published tuple recorded by the Windows and Mac receipts is:

| Component | Exact ref |
|---|---|
| App | `79f7e5397c044c4c97dc7e97b20ad96f787e5247` |
| Agent | `ab43bad818ef65ca1e929b973a2f61cd9fc4bf61` |
| Runtime | `2776ba973b6c8f3555b5629b0840d9691a98d3c2` |
| Pinokio | `3a6a98ae251ca126505245286e2d3e760f4348e9` |
| Release-1 manifest SHA-256 | `b2584f0bb5a949890364004572a0182f2b568adcdba061e73b739e1d3d2d623f` |

This table is provenance only. It is not an accepted tuple qualification or
an authenticated R-24/R-25 slot receipt. The O-06-R11 Option-B boundary is
unchanged: checkpoint evidence is the R1 evidence class for direct M4-6, while
containment and continuous history remain `deferred-R1.x` and are never counted
as passed. None of the receipts below supplies direct M4-6 checkpoint evidence,
continuous history, containment, or an acceptance decision.

### R-PIN-WINDOWS-RESULT — Actions 41541744248

The terminal receipt is
`/home/user/merge/ws/M-PIN/queue/done/R-PIN-WINDOWS-RESULT/REPORT.md`, with
canonical receipt at the same directory. Their supplied SHA-256 values are
`6c6a847a55584a669457c7618488756e357e77c8d9c7056f078e8b791af4c522` and
`30df08151aa62dac98eba4c070b3796ae8038164c1defce0194cb99724a4ba39`.

Exact provenance: repository `banodoco/astrid-pinokio`, branch
`lane/M-PIN/R-PIN-WINDOWS-BOUNDED`, run `41541744248`, job
`windows-six-action` / `125607363670`, runner `windows-2022`, workflow
`.github/workflows/r25-snapshot-journey.yml`, run head
`2aee4f026a17b59868766b1bfb173b6797d65f61`, tree
`d0b57bb8dd21e47e60db35c731e505dbfd0c098d`, base
`50be43b3fd7bf68cacda5ef121e300da13457054`. Native refs were collector
`a6b68d5b5478326a4db1971966d33a2cfe61d091`, pinokiod
`add4a674ad1ba95bb62ec427fce519fe5bf0e9bf`, and PTerm
`5be43718701c448b43daf0ce51d8935f170065bb`.

The exact candidate tuple was the shared tuple above. Setup and source
predicates passed, including focused tests with inner exit `0` (`8 passed,
0 failed`), but the real-engine boundary did not complete:

- setup indices 0 and 1 each recorded `start/result`;
- setup index 2 recorded `shell.run` start and one `stream`, with no result,
  error, terminal packet, shell-helper event, or nested child exit;
- the observer's first real error was exactly `R-25 setup-install PTerm
  timeout`, owned by `astrid-pinokio/.github/venue/r21-observer-diagnostics.cjs:44`;
- the observer timer fired at `2026-11-02T23:11:16.657Z`, PTerm exited with
  `code: null`, `signal: SIGTERM` from observer timeout cleanup, and the
  runner/diagnostic step and workflow each exited `1`;
- the nested PowerShell/bootstrap/installer child exit is unavailable. No
  native installer error was retained, and the six measured actions and
  provider action were not run.

The retained evidence includes the run/job/artifact metadata, logs ZIP,
artifact ZIP, `r25-install-diagnostics.json`, `r25-failure.json`, and exact-head
source files under
`/home/user/merge/ws/M-PIN/queue/done/R-PIN-WINDOWS-RESULT/evidence/`. The
canonical receipt records hashes for each. Missing stdout/stderr, shell-helper
and bootstrap stages, nested child exit/close, daemon settlement, process-tree
snapshot, and setup result are unknown or unavailable, never success. The
normal snapshot output was empty and no R-25 slot receipt was produced.

Key retained artifact hashes from the canonical receipt are:

```text
run.json                         3574c517249c6c0889d303eb4e10866a2679a579ff195860f18a3aef0cf3cdf9
jobs.json                        4443bc8c4e95c11107fd47394e6c9d521eb86b43fe3f28bbb0ccda110fa41c77
artifacts.json                   efd7487668b796b5162dbd7af3501fa049326db9aa334d70922064ce8b4de749
logs.zip                         eab8ac1fe217bdf3351b5b067bd3bf31699043ebe0de7016efa462ea49e00cec
artifact-13188145071.zip         f7bc8c1a11c5a86968df32453ac091bd30940118571238fc10e5964507315361
r25-install-diagnostics.json     609fc7a063fc6005f1a9107a99cd13fd8b1d9d90bc01bd3897ced3f1eef03865
r25-failure.json                 a7170cba132dbc2d10a2c2f4ba3ea31805c929e57c6f40afff8b761a3ace0e12
failed-step log                  5ee11fe2e2a923698ffa0d60a329dede228afc2a4d59e8db994c34404cb8a2a9
full job log                     0aa2f812b0a058f263ce28b41c4654ea3afb525299302a031001990f1f226ca4
observer source                  ebfe9420a4c26449bdfa5e6ceb2c3e7144a9a266b8c14b938f7ccc3f5ccc03be
runner source                    22f52538518076aea9fc3f43c5ab94642ef9b764d40762b8aa74db8c5685fb17
shell source                     610fea5167c0d3119c409c77f879f5474d952ddbee2f628c6217e22d6da45967
install source                   60f4473e91b769af0480098b96a36500f3c1628ac33a7617f68df4de1fe436e2
lib source                       59bb0a556b425dafb4d986aaab54f0dbd23a988814a0d64a6cb4437c52d3d998
```

Route this observer-timeout diagnosis to **M-PIN / astrid-pinokio venue
diagnostics** for one changed, bounded setup-only capture that retains PTerm
and pinokiod output and a pre-timeout process snapshot. Do not assign a
product, Runtime, App, or installer root cause from this receipt. Route to
**M-R1FIX** only if a future capture retains an actual installer error with its
nested exit; that condition is not met here.

### R-PIN-FFMPEG-TRACE — Actions 41542501277

The terminal receipt is
`/home/user/merge/ws/M-PIN/queue/done/R-PIN-FFMPEG-TRACE/REPORT.md`, with
canonical receipt at the same directory. Their supplied SHA-256 values are
`85ef0cee37912fecb75b1a15638d37e48cd8abbfe7834f8c0ffdcb006067d125` and
`3245a50426f12e6e61340a988ad8e72eaa20a0c16f377c3aca4ead835fa53b48`.

Exact provenance: repository `banodoco/astrid-agent`, workflow
`.github/workflows/r24-linux-live.yml`, venue ref
`refs/heads/venue/M-PIN-R-PIN-IDENTITY-RUN`, run `41542501277`, job
`r24-live` / `125609905045`, runner/workflow head
`b5aadb98e0d9579cd9ba9704a660a24cb27cc69e`, mode `fully-local`,
`install_only=true`, producer and mode-producer ref
`13cb31104c8aae655d0ee04efdb9537aa111e340`. The published tuple is the
shared tuple above. The run candidate tuple used provisional Pinokio ref
`dbc837992be9cf2b489bd96b495add366261a575`; it is not a published tuple
qualification.

The first real installer error is exactly:

```text
Download checksum mismatch: https://johnvansickle.com/ffmpeg/releases/ffmpeg-7.0.2-amd64-static.tar.xz
```

It occurred in App `public/install` through the pinned Agent installer. The
later outer stop was `installed data root mismatch` at materializer
`install-candidate`, with materializer status `blocked`. The inner/outer exits
are:

| Boundary | Exit/result | Meaning |
|---|---:|---|
| Candidate archive materialization | `0` | materialized |
| Candidate extraction | `0` | extracted |
| Pinned pinokiod cleanup/start boundary | `-9` | cleanup/termination outcome, not first installer error |
| PTerm `pinokio/install-local.js` transport | `0` | transport completion only |
| Nested App `public/install` | unavailable | PTerm did not expose nested shell exit |
| Materializer inner exit | `1` | checksum failure propagated to materializer |
| Outer workflow propagation | `1` | fail-closed propagation |

The provider and mode action were not invoked. The hosted received digest,
received byte count, and response metadata were not retained. Later bounded
GETs from the diagnostic host returned the expected FFmpeg digest
`abda8d77ce8309141f83ab8edf0596834087c52467f6badf376a6a2a4c87cf67` from
both proxy and no-proxy paths, with identical bodies; that later observation
does not classify the bytes received by the hosted installer. The candidate
manifest was 4,705 bytes with SHA-256
`b2584f0bb5a949890364004572a0182f2b568adcdba061e73b739e1d3d2d623f`, and
the selected data root was not a directory. No R-24 receipt or qualification
result was produced.

Key retained artifact hashes from the canonical receipt are:

```text
actions artifact ZIP             765085ed96b365dfdbc85cc183a7736f08daa0caec9c03918f591c47f999a18a
Actions logs ZIP                 f154f260ec0c401b6a6fa1909a14c1344262facbc12f6801f11db9bc960ed28c
r24-materializer.json            e6facc9548d044f0ab2ac3f0971aec35fa8b3c4627d7fd3b4d82c459a6a202b1
installed-identity.json          09e7daa78027d33de763714069a003fc9c733c7a2ccf2fc614c42bf298e26e9d
install.log                      3a338c8d4f261381ab427b7273975d90e1e69b6a88627cb4b8a63c8ec9b30192
pinokiod.log                     fa66ae8b3cbbffa3ad996edbfdcd3fd8ee15d9879dbc4e666f502acc4e31b731
route request                    298dfa8e25e31d8d194d0ac847afea0dce99b5bb2d481ab00abbb52e2e2c54ec
```

The receipt supports preserving the existing manifest/identity review route
to **M-APP / M-R1FIX**: the exact manifest identity and tuple are recorded, but
the missing hosted response digest prevents a manifest, App, proxy, or
installer root-cause claim. The actionable checksum-stop follow-up routes to
**M-R1FIX**: add diagnostic-only allowlisted received digest, byte count, HTTP
status, final URL, and response metadata to the existing installer log while
keeping the strict SHA-256 assertion and no retry/alternate URL. The pushed
Agent materializer capture head is
`67a8d2ec36c921319dc09a0e77ed29ccf939cece`; it is diagnostic preparation,
not a hosted result.

### R-PIN-MAC-OBSERVABILITY — diagnostic plumbing only

The terminal receipt is
`/home/user/merge/ws/M-PIN/queue/done/R-PIN-MAC-OBSERVABILITY/REPORT.md`, with
canonical receipt at the same directory. Their supplied SHA-256 values are
`4b88d586e264d47cd3f34156a034db57d3fdbe671dad170ca9a4db43c064d987` and
`481825958512ead7b4708b9770b0f1a7e7237cc5af74ddc2131d8bd1f61d5dd4`.

Exact source provenance: repository `banodoco/astrid-pinokio`, branch
`lane/M-PIN/R-PIN-MAC-OBSERVABILITY`, head
`1b96b6d1f3f0d6a34d2f9677c32a13cbdd8fe8c1`, base and parent
`808ebb757d43dbc91a8219185d00a645f9f271a2`, tree
`cc68a9f792f71b37a6d359d1f27fab0849e844d0`. The shared current tuple and
manifest hash are recorded above. The related active Mac context in the
receipt is Actions run `41538027902` on old head
`856980b6388ab4d57578b87c659364e3ef82a118`; the receipt records that run as
in progress at its latest status and without a terminal artifact or inner
exit. It is not converted here into a hosted R-25 result.

The diagnostic branch passed source checks and 53 focused tests (`0`), and
adds bounded redacted daemon/PTerm/terminal/installer/stage/lifecycle and
pre-timeout process evidence. No workflow, timeout, action plan, installer
transaction, proof assertion, or active run was changed. No hosted Mac run was
dispatched by this receipt, no Mac six-action result exists, and no installer
error or slot acceptance is established. Missing hosted-run inner exits and
slot artifacts are not-run/unavailable, not successful.

The changed-file hashes are retained in the canonical receipt: observer
diagnostics `b3eb6d0943efaf8aaae268f6ffd94ab8826a7053aff4df142238cc74b73fc94`,
snapshot runner `2ecd102caaf72a58e64b96b32b7d1d852914e822134161f75ae967bffcf6e308`,
bounded observer tests `469dea28fc5e1dfd95debf912e832cd6dc2b1007aa834dd6e9d4336d71c9e58d`,
diagnostics tests `256f32295571707c05958a209aaabac5757e6b391576c400fd469581b9a6b583`,
and Mac observability tests
`295d3740859d6a5a9677867f396cda64d85421a1dac9200051f4babeda515030`.

Route this plumbing and any future changed-input Mac setup observation to
**M-PIN**. If a future retained artifact contains an actual Agent installer
error, route that exact frame to **M-R1FIX**. The current receipt does not
support an M-R1FIX, M-APP, Runtime, or qualification finding.

### Coverage inventory, routes, and preserved gates

| Evidence item | Recorded disposition | Not established / missing |
|---|---|---|
| Windows `41541744248` | **FAILED DIAGNOSTIC** at observer timeout after setup index 2 `shell.run` stream | Native installer cause, nested child exit/close, six actions, R-25 slot acceptance |
| FFmpeg `41542501277` | **FAILED DIAGNOSTIC** at exact checksum mismatch; outer materializer exit `1` | Hosted received digest/metadata, nested `public/install` exit, provider/mode action, authentic R-24 receipt |
| Mac observability branch `1b96b6d1…` | **DIAGNOSTIC PREPARATION**; source/focused tests only | Hosted R-25 result, terminal Mac slot receipt, installer error, acceptance |
| Direct M4-6 Option-B class | **PRESERVED from O-06-R11** | No new checkpoint, census, invariant, containment, or continuous-history evidence |
| Failed/skipped coverage in O-06-R11 prefix | **PRESERVED byte-for-byte** | No result is reclassified by these diagnostics |

The Windows timeout is routed to M-PIN. The FFmpeg checksum metadata follow-up
is routed to M-R1FIX. The manifest/identity route remains with M-APP/M-R1FIX
only as the already-recorded review route supported by the exact manifest and
tuple identity; these receipts do not expand it or assign a root cause.

All prior gate dispositions remain unchanged and open: M4-6/R-36/R-37 and
other qualification gates **OPEN / NOT PROVEN**; I-08 freeze **MISSING /
PENDING**; coherent RC/qualification **OPEN / NOT ESTABLISHED**;
publication/deployment/release approval **NOT RUN / NOT AUTHORIZED**; and
go/no-go **OPEN / UNCLAIMED**. The three receipts add diagnostic evidence only
and do not close, weaken, or qualify any gate.

No product code, workflow, scenario inventory/checker, frozen contract,
release ref, publication/deployment state, `STATUS.md`, `ORCHESTRATOR.md`,
prior task folder, other manager file, hosted job, journey, RunPod action, or
product test was modified or run by this refresh. No rerun or dispatch was
made. The packet remains documentation-only.

---

## O-06-R13 — terminal Windows PTY-integrated diagnostic supplement

**Supplement date:** 2026-11-03 UTC.  **Disposition:** diagnostic-only;
append-only evidence refresh.  This section is appended after the immutable
O-06-R12 packet and does not revise any earlier disposition, finding, route,
coverage gap, or gate state.

### Exact provenance

The producer receipt is
`/home/user/merge/ws/M-PIN/queue/done/R-PIN-WINDOWS-PTY-INTEGRATE/REPORT.md`
(SHA-256
`33fde569e7fbc182d915aae7211dab2895f32a4974bf51b38a2b40bb1e63b833`). Its
canonical receipt is
`/home/user/merge/ws/M-PIN/queue/done/R-PIN-WINDOWS-PTY-INTEGRATE/canonical-receipt.json`
(SHA-256
`5fc425c3ce158f48fbd06b6da6d117c68c8177602af03870828fd9c306591b05`). The
producer receipt says the branch was pushed as a diagnostic-only setup request;
it makes no acceptance, qualification, or handoff claim.

| Field | Exact value |
|---|---|
| Repository | `banodoco/astrid-pinokio` |
| Event / ref | `push` / `lane/M-PIN/R-PIN-WINDOWS-PTY-INTEGRATE` |
| Final head | `ab639d18b1cda9895a38a51322d997067b1229a9` |
| Merge commit / parents | `3511abba4c1180ab855c10dfea08f8a93d3473c3`; `bb8f6005b431be817d97799550b6c1c4c72e8a14`, `e8ae514ac126a424cb746602aeb89271e1b3d1cb` |
| Final tree | `fb77585e5e2dc123aa1824ae0c2b7fb014ee6600` |
| Workflow | `R-25 authenticated six-action snapshot journey` |
| Run / job | `41551034466` / `windows-six-action` (`125638377354`) |
| Slot | `pinokio-fully-local-windows` |
| Setup mode | `SETUP_ONLY=true`; six-action execution was not requested |
| Setup timeout | `600000` ms |
| Agent collector ref | `a6b68d5b5478326a4db1971966d33a2cfe61d091` |
| pinokiod ref | `add4a674ad1ba95bb62ec427fce519fe5bf0e9bf` |
| PTerm ref | `5be43718701c448b43daf0ce51d8935f170065bb` |

The exact tuple passed to the hosted job was:

```text
app      79f7e5397c044c4c97dc7e97b20ad96f787e5247
agent    ab43bad818ef65ca1e929b973a2f61cd9fc4bf61
runtime  2776ba973b6c8f3555b5629b0840d9691a98d3c2
pinokio  3a6a98ae251ca126505245286e2d3e760f4348e9
manifest b2584f0bb5a949890364004572a0182f2b568adcdba061e73b739e1d3d2d623f
```

The tuple source is `/home/user/repo/docs/merge-plan/CURRENT-TUPLE.json`,
SHA-256 `71a738081dba7f84bea0b882a975cd540b62684e8f6f1fa0d99f572e62908e50`.
The tuple also bound `public/install` to
`100bab1deb15d620abd910c8fb0c9a6d77f4945428a3229dac6b506705e3fd0d`,
`public/install.sh` to
`591c4c51b148fccef2a7d1c45782d9ff949d5fd03d46cd82b5591e3a23d1b285`, and
`public/install.ps1` to
`bba3f2edc41c047cb51475ceeac4489a04610af73a3af44bdc05287964d28258`.

### First bounded failure and evidence boundary

The job log records the first bounded workflow failure as:

```text
Error: R-25 setup-install PTerm timeout
at .github/venue/r21-observer-diagnostics.cjs:50:14
Process completed with exit code 1.
```

The timeout deadline was `2026-11-03T00:23:30.727Z`; the timer fired at
`2026-11-03T00:23:30.734Z`. The diagnostic JSON records setup-install action
index `2` of `4` as the last started action. It has a `shell.run` start at
`2026-11-03T00:14:29.738Z` and one stream at `2026-11-03T00:14:30.609Z`:

```text
████
██ Starting Shell 4ee8667f-2a2c-43e1-91be-7dd89f8abe38
```

No shell event, shell result, terminal error, or installer output followed
that stream. The first explicit low-level error text retained in the bounded
daemon stderr is `Error: AttachConsole failed` from
`@homebridge/node-pty-prebuilt-multiarch` `conpty_console_list_agent.js:17`.
The packet records that text as observed diagnostic evidence only; the
available files do not establish that it is the causal root of the timeout or
provide an ordered native failure classification before the observer stop.

The setup/observer/PTY boundary is therefore: daemon spawned and became
ready; observer opened and subscribed; PTerm spawned; `shell.run` started;
one PTY stream marker was captured; the observer timer fired; bounded
pre-timeout process and stdio evidence was captured; cleanup sent SIGTERM to
the PTerm child. There is no retained shell close/result or installer
settlement.

### Inner and outer exits

| Boundary | Recorded result | Evidence limit |
|---|---|---|
| Focused hosted setup checks | `25 passed, 0 failed`, exit `0` | Preflight/check coverage only; not a journey result |
| Setup action indexes `0`, `1` | Result events recorded | Their individual semantic result is not a six-action acceptance predicate |
| Setup action index `2` / `shell.run` | Started; one stream; no result | First bounded failure is the observer PTerm timeout |
| Setup action index `3` | Not reached | Setup stopped at index `2` |
| PTerm child | `code: null`, `signal: SIGTERM`; close `code: null`, `signal: SIGTERM` | Terminated by timeout cleanup; no native exit code |
| Daemon | Exit code/signal unavailable | Process tree was captured before timeout; no daemon settlement record |
| Snapshot runner / setup branch | exit `1` | Propagates `R-25 setup-install PTerm timeout` |
| GitHub job / workflow step | exit `1`, job conclusion `failure` | Hosted diagnostic failure only |
| Snapshot output | No files found; no snapshot artifact uploaded | No R-25 slot result or six-action artifact |

The process snapshot captured root PIDs `2728` and `3236`, with rows for PIDs
`900`, `2728`, `3236`, and `6928`; it does not supply child exit codes or
installer settlement. The diagnostic lifecycle records PTerm exit/close and
cleanup start after the snapshot, but not a native PTerm exit code. The
provider/mode action was not invoked. No real-engine install, six-action
journey, or M4-6 acceptance predicate was reached.

### Retained relay artifacts and hashes

The authenticated relay root is
`/home/user/repo/docs/merge-plan/evidence/actions/41551034466/`. The eight
listed run files were inspected, including their byte counts and SHA-256:

| Relative file | Bytes | SHA-256 |
|---|---:|---|
| `logs/windows-six-action-125638377354.log` | 34571 | `8b2caf18257286a17cfe952a5a5609d0429cd2774f012acd23848095b8efe06f` |
| `r25-snapshot-diagnostics-pinokio-fully-local-windows-41551034466-1/daemon.stderr.before-timeout.log` | 864 | `c20d91ce54a36ed1cdc38ef01e84a5465944be57c7ac9d5a32d63de4c79293f4` |
| `r25-snapshot-diagnostics-pinokio-fully-local-windows-41551034466-1/daemon.stdout.before-timeout.log` | 4430 | `a8eef0ce76e00d163fdb62f85d21120100dac107594ab6f27b06f9db02e1f66b` |
| `r25-snapshot-diagnostics-pinokio-fully-local-windows-41551034466-1/process-tree-before-timeout.json` | 441 | `a0086b9aea15150b970ca62cae8542d550e08e017640ed621fb9e0838a51e8a8` |
| `r25-snapshot-diagnostics-pinokio-fully-local-windows-41551034466-1/pterm.stderr.before-timeout.log` | 0 | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| `r25-snapshot-diagnostics-pinokio-fully-local-windows-41551034466-1/pterm.stdout.before-timeout.log` | 76 | `f05bbc0396bbcb14caa18f991237a29eb6e352b3b6d9299912d9082c2e15f992` |
| `r25-snapshot-diagnostics-pinokio-fully-local-windows-41551034466-1/r25-failure.json` | 161 | `ad5044d997826e229a32174174a97c2db31d935bede59d49334a7b6d8ec058dd` |
| `r25-snapshot-diagnostics-pinokio-fully-local-windows-41551034466-1/r25-install-diagnostics.json` | 5879 | `e017729a4f47041606b429ae4cd77f3c828bfc97d9899236fe48db92ca8c1f62` |

The relay log records the diagnostic ZIP upload as SHA-256
`9ead7e5b13475450590137b361fa4e6f62201273d2b54fc96a01d5388884eaab`, artifact
ID `13191778380`, final size `6834` bytes. The separate snapshot-output path
had no files and was not uploaded. The eight file hashes above are the
evidence hashes used by this supplement; duplicate post-cleanup log names are
not substituted for the before-timeout files.

### Coverage inventory, gaps, and owner route

| Predicate / evidence item | Disposition in this supplement | Missing or not reached | Route |
|---|---|---|---|
| Exact run/ref/head/tuple and native refs | **RECORDED; diagnostic provenance** | No RC/freeze binding follows | M-PIN supplies/maintains source provenance; M-INT assesses any carry |
| Daemon startup, observer subscription, PTY spawn, bounded stdio | **CAPTURED** through pre-timeout evidence | Daemon/native settlement and ordered causal attribution | **M-PIN**: Windows PTY/setup boundary |
| Setup action index `2` `shell.run` | **FAILED DIAGNOSTIC** at timeout after one stream | Shell result, close event, installer output, nested child status | **M-PIN**: investigate/fix or further instrument PTY/setup boundary |
| Low-level `AttachConsole failed` text | **OBSERVED; no root-cause claim** | Causal native classification and reproducible child exit | **M-PIN**; do not route as product/qualification finding from this packet |
| Six-action R-25 journey / provider / mode action | **NOT REACHED / NOT REQUESTED** (`SETUP_ONLY=true`) | All six action results and slot predicates | M-PIN / M-QUAL when a separately authorized exact request exists |
| Snapshot output and installed identity | **NOT AVAILABLE** | Files, installed bytes, identity, cleanup settlement | M-PIN; no installed-identity or R-24/R-25 acceptance claim |
| Current-RC qualification, R-36/R-37, M4-6, I-08 | **OPEN / NOT PROVEN; prior disposition preserved** | Authentic current-RC evidence and required inventory | M-QUAL / M-INT under existing gates |

The first candidate push `41550911016` was skipped because the job predicate
initially omitted the integration branch; it is not a result for this run.
The corrected push produced the single executable setup-only request
`41551034466`. No manual dispatch or rerun was performed by this task.

All prior failed/skipped/not-reached coverage in the O-06-R12 prefix remains
unchanged. This new run adds a Windows setup/PTY diagnostic boundary only; it
does not add a protected journey result, an M4-6 checkpoint, a continuity
invariant, a provider result, or a current-RC acceptance.

### Preserved gate state and non-claims

Qualification remains **OPEN / NOT PROVEN**; I-08 freeze remains **MISSING /
PENDING**; coherent RC binding remains **OPEN / NOT ESTABLISHED**; release
approval, publication, and deployment remain **NOT RUN / NOT AUTHORIZED**;
and go/no-go remains **OPEN / UNCLAIMED**. This supplement makes no
qualification, freeze, RC approval, publication, deployment, or go/no-go
claim. It is not a HANDOFF.

No product code, workflow, scenario inventory/checker, frozen contract,
release ref, publication/deployment state, `STATUS.md`, `ORCHESTRATOR.md`,
prior task folder, other manager file, hosted job, journey, RunPod action, or
product test was modified or run by O-06-R13. The packet remains
documentation-only and routes the bounded Windows setup/PTY boundary to
**M-PIN**.

## O-06-R14 supplement — protected Mac cleanup source review (2026-11-03)

This is an append-only supplement to the accepted O-06-R13 packet. The
immutable O-06-R13 prefix is 168716 bytes with SHA-256
`1ac69b13dabcbcb46966a357a896f99cef668ad48d429069b453c4817f783f10`.
O-06-R14 copied that packet before editing and verified the initial copy
byte-for-byte. No earlier evidence, coverage disposition, owner route, or gate
state is rewritten by this supplement.

### Exact source and protected review

The protected review `review/R-PIN-MAC-CLEANUP-VERIFY/REVIEW.md` has SHA-256
`993e39dfb421b404d4b87eefd0d875b0ee738b982c2e4940442915319e876ea3`. It
reviewed `banodoco/astrid-pinokio`, branch
`lane/M-PIN/R-PIN-MAC-CLEANUP-VERIFY`, exact head
`0c61c3d51c8288bfa0e13a0da24fee5d007d9ce9`, based on
`d8cae20e5b3e130dac3269f9787cb7221cac419c`. Review time was
`2026-11-03T00:59:34Z`. Scope was source-only review of descendant ownership
validation and signal ordering in `.github/venue/r25-snapshot-runner.cjs`; no
code execution was part of the review. The M-PIN canonical receipt at
`/home/user/merge/ws/M-PIN/queue/done/R-PIN-MAC-CLEANUP-VERIFY/canonical-receipt.json`
has SHA-256
`ddb062b0cf397e38bc206c6ee11d002346c48f4a1b6b962369291db1e2d8cfbe`.
It identifies the same branch, head, and base and reports no macOS or
non-root Mac execution; its local test UID was 0.

### Finding and safety disposition

**NOT APPROVED for the Mac cleanup candidate yet.** The reviewed descendant
signal path is fail-closed, but its identity predicate has a Darwin
compatibility bug that prevents the intended cleanup path. This is a
functional false negative, not an unsafe-signal finding: on failed identity
validation, the candidate returns unknown/failed cleanup without signalling
the descendant.

In `readProcessIdentity()` the Darwin identity contains `command_line`, but
has no `argv` or `script`. The descendant census stores that identity.
`validatedSignal()` reconstructs `script` as `expected.argv?.[0] || null` and
passes it to `identityMismatches()`. The Darwin comparison calls
`observed.command_line.includes(expected.script)`. For census-derived
children, `expected.script` is null, so JavaScript searches for the literal
string `"null"`; an ordinary command line does not contain it. Validation
therefore fails, SIGSTOP containment/identity validation fails closed, and the
cleanup result remains unknown/failed without signalling that descendant.
The daemon has the explicitly captured script and is not affected by this
particular mismatch.

The review found the bounded census and deepest-first direct SIGKILL sequence
otherwise scoped to same-UID processes with cwd inside the private run root,
with each signal preceded by a fresh identity read. Unknown ownership or an
unavailable census remains failure. No SIGTERM, process-group signal,
foreign-service probe, or outside-account action appears in the reviewed
cleanup path. These source observations do not establish runtime safety or
Mac cleanup success.

### Required correction and coverage

Before requesting another protected review, bind Darwin descendant signals to
the captured `command_line` with an explicit, non-null expected value and a
comparison that proves the exact intended executable/script identity.
Retain fresh PID, PPID, UID, executable, cwd, and start-identity checks. Add
focused Darwin identity-comparison coverage that exercises the census-derived
identity shape (`command_line` present, `argv`/`script` absent), proves the
correct intended identity passes, and proves a mismatched or unavailable
identity fails closed without a signal. Re-review the new exact head before
approval. The M-PIN receipt's 25 passing focused tests are reported Linux
coverage and do not prove this Darwin branch; this task did not replay them.

### Runner-account evidence boundary and open work

M-OPS has no authenticated `astrid-runner` session. It cannot inspect
runner-private files or signal runner processes, and coordinator-side process
inspection is not runner-account evidence. Use M-PIN's authorized `MAC-TASK`
relay for a self-contained task on hannah-mac running as non-root
`astrid-runner`. Before cleanup, capture a bounded census and hash/copy the
named run's private receipts and logs into the returned task evidence; identify
only PIDs recorded as belonging to that task. For already-cancelled run
`41538027902`, preserve its receipt and log evidence first, then revalidate
PID/start identity, UID, executable, cwd, and command against its recorded run
root before any runner-account-owned cleanup. If ownership or identity cannot
be established, leave the process untouched and report unknown. Do not inspect
another account's service or touch port 17340.

Still open are: (1) correction and focused Darwin comparison coverage followed
by protected review of the new exact head; (2) native macOS proof as non-root
`astrid-runner`; (3) authenticated runner-account ownership and evidence for
run `41538027902`, including preservation before any cleanup; and (4) an
independent final census proving the recorded owned processes are absent.
No authenticated ownership, final census, cleanup completion, or Mac success
is established here.

### Coverage inventory, routes, and preserved gates

| Predicate / evidence | Disposition | Boundary / next evidence |
|---|---|---|
| Exact candidate branch, head, and base | **RECORDED** from protected review and matching M-PIN receipt | Source provenance only; no runtime proof |
| Protected source review | **NOT APPROVED** for this candidate | Correct Darwin identity binding and focused coverage; then protected review on new exact head |
| Darwin census-derived descendant identity | **FUNCTIONAL FALSE NEGATIVE; FAIL-CLOSED** | `command_line.includes(null)` searches for literal `"null"`; validation fails and no descendant signal is sent |
| Unsafe signal finding | **NOT FOUND in reviewed path** | Bounded same-UID/private-root census and fresh identity checks observed; source review does not prove runtime behavior |
| M-PIN focused tests | Receipt reports 25 passed on Linux | Reviewer did not replay; Linux coverage does not exercise/prove Darwin behavior |
| Native non-root Mac execution | **OPEN / NOT EVIDENCED** | Exact-head execution as `astrid-runner` required |
| Runner-account proof for run `41538027902` | **OPEN / NOT AUTHENTICATED** | Preserve private receipts/logs before cleanup; ownership and each PID identity must be established by runner account |
| Independent final census | **OPEN / NOT EVIDENCED** | Must be available and empty for owned run-root processes after any authorized cleanup |
| Qualification / M4-6 / R-36 / R-37 | **OPEN / NOT PROVEN** | Existing qualification inventory and owner gates remain in force |
| I-08 freeze / coherent RC | **MISSING / PENDING; OPEN / NOT ESTABLISHED** | No freeze receipt or RC approval is supplied |
| Publication / deployment / release approval | **NOT RUN / NOT AUTHORIZED** | No state changed or approval supplied |
| Go/no-go | **OPEN / UNCLAIMED** | No decision made |

All O-06-R13 and earlier failed, skipped, not-reached, pending, and open
dispositions remain unchanged. Qualification remains **OPEN / NOT PROVEN**;
I-08 freeze remains **MISSING / PENDING**; coherent RC binding remains
**OPEN / NOT ESTABLISHED**; publication, deployment, and release approval
remain **NOT RUN / NOT AUTHORIZED**; go/no-go remains **OPEN / UNCLAIMED**.
This source-review supplement makes no qualification, cleanup-success,
freeze, publication, deployment, release, handoff, or go/no-go claim.

The review routes the Darwin source correction and exact-head re-review to
**M-PIN**. Authenticated runner-account evidence must follow the authorized
MAC-TASK route; qualification and RC assessment remain with their existing
owners and gates.

O-06-R14 was documentation-only. No product code, workflow, scenario
inventory/checker, frozen contract, release ref, publication/deployment state,
`STATUS.md`, `ORCHESTRATOR.md`, prior task folder, or other manager file was
modified. No product test, hosted job, cleanup code, or cleanup action was run.


# O-06-R15 — bounded Mac recovery review supplement

**Review basis:** M-PIN `R-PIN-MAC-RECOVERY-PLAN`, reviewed by M-OPS at 2026-11-03T01:11Z. This supplement is append-only. The accepted O-06-R14 packet above is preserved byte-for-byte as the prefix.

## Decision and old source behavior

Preserve Actions run `41538027902` and its processes untouched. M-OPS accepts the bounded decision to preserve the run and acquire read-only runner-account evidence. No recovery operation is approved: no signal, cancellation, API request, port-based selection, or cleanup.

The run used `banodoco/astrid-pinokio@327d0fc29f0b63d58e6c6c3449c81af5912ac85c`. In that exact source, `.github/venue/r25-snapshot-runner.cjs` is blob `77fd2b509b0e2c7ec21e297e965e90132b2d6a4c` (SHA-256 `58bfafdd894492bc1bb0b2194823808d4da12dbbb1b6d1feaa7758e627379708`). The runner starts the Pinokio daemon as a child (reviewed lines 303–311). Its process-exit handler calls `daemon.kill()` without a signal, and its `finally` path calls `worker.kill()` and `daemon.kill()` the same way (reviewed lines 387–389); Node's default is SIGTERM. The old daemon's TERM handler recursively kills registered shell trees and a detected Caddy tree without run-root checks. These are broad lifecycle behaviors; they do not establish a run-scoped recovery primitive or preserve the run's receipt/upload path. Later cleanup logic cannot be applied retroactively to this already-running process tree.

The M-PIN evidence identifies only historical coordinator-side observations: PIDs 8661 and 8688, UID 503, an inferred run root, and a listener summary for port 42000. Executable, cwd, start identity, a complete descendant graph, raw listener ownership, runner-private evidence hashes, and terminal/cleanup receipts are absent. Therefore the full current descendant map and live identities are missing; the historical observations cannot select a process target.

## Missing predicates before any recovery decision

The current evidence does not establish all of the following required facts:

- Fresh identity from a task executed as `astrid-runner` (`id -un`, `id -u`) and current UID ownership of the observed processes.
- Exact run-to-root binding read from the run's own record; a physical non-symlink run root, its owner, and verified cwd containment for every selected process.
- Current snapshot-runner and daemon PID, PPID, UID, executable, command/argv, cwd, and process-start identity, including the runner-to-daemon child relation, revalidated immediately before any later action.
- A complete same-UID descendant map, with those identity fields for every node and stable repeated samples; no unknown, changed, or out-of-root process.
- Raw current listener evidence tied to the exact validated daemon PID, plus an authenticated API endpoint bound to that daemon and documented bounded stop semantics.
- Allowlisted runner-owned copies of relevant receipts/logs, preserved before any recovery, with independently verifiable hashes; and the missing inner route, sequence, daemon, cleanup, and artifact terminal receipts.

No authenticated safe stop API is established. The later candidate's bearer controller belongs to an Astrid Runtime session controller; evidence does not bind it or its callback to old daemon PID 8688. The PTerm listener summary on port 42000 is not a scoped safe-stop contract. Parent termination, TERM, process-group/tree signals, cancellation, direct KILL, and port-based target selection remain unsupported by the missing identity and evidence predicates and the old source behavior.

## Credential-free access boundary

The only acceptable access route is an authorized M-PIN `MAC-TASK` that executes a self-contained, read-only capture **directly as non-root `astrid-runner`**, without `sudo` or added credentials, and returns the exact task/run identity, account identity, current complete census, run-root binding, and capture/evidence hashes. Preserve and hash an allowlisted evidence copy before any separately reviewed recovery. Do not expose environment variables, tokens, or arbitrary private files. If the relay cannot execute directly as that account, the route is unavailable: preserve run 41538027902 and report the missing prerequisite. M-OPS has no authenticated runner-account session or alternate credential-free route. A successful capture alone would not authorize a signal or API request.

## Separate exact-head cleanup review and open gates

The exact-head source review of `banodoco/astrid-pinokio@0c61c3d51c8288bfa0e13a0da24fee5d007d9ce9` in `review/R-PIN-MAC-CLEANUP-VERIFY/REVIEW.md` remains **NOT APPROVED**. Its Darwin census identity has `command_line`, while validation expects `argv`/`script` and passes a null expected script to `includes(null)`, so descendant validation fails closed. Require a non-null captured command identity, focused Darwin comparison coverage, and native non-root Mac proof, then review a corrected exact head. This is a separate cleanup-candidate review and does not authorize applying it to run 41538027902.

All prior packet dispositions remain unchanged. Qualification remains **OPEN / NOT PROVEN**; I-08 freeze remains **MISSING / PENDING**; coherent RC binding remains **OPEN / NOT ESTABLISHED**; publication, deployment, and release approval remain **NOT RUN / NOT AUTHORIZED**; go/no-go remains **OPEN / UNCLAIMED**. This supplement makes no cleanup-success, qualification, freeze, publication, deployment, release, handoff, or go/no-go claim. No Mac operation, cleanup code, product test, hosted job, or external state change was performed.


# O-06-R16 — exact-head Darwin identity source-review update

**Documentation-only label:** `lane/M-OPS/O-06-R16`  
**Base:** the complete accepted O-06-R15 `SR-PROJ.md` byte sequence, preserved unchanged as the prefix. This is not a product-repository branch or an integration handoff.

## Exact-head source-review verdict

M-OPS completed the protected source review recorded in `review/R-PIN-MAC-DARWIN-IDENTITY/REVIEW.md` at `2026-11-03T01:21:38Z`. The review is a **PASS for the exact-head Darwin command-identity source fix and its scoped identity predicates** in `banodoco/astrid-pinokio`, branch `lane/M-PIN/R-PIN-MAC-DARWIN-IDENTITY`, head `b2fc56ae445703795bd8ffc6c7038feb6073dccb`, based on `0c61c3d51c8288bfa0e13a0da24fee5d007d9ce9`. The reviewed runner fix is commit `6c159e87ebb4f8965f93f6de7a9cfa25778b01d4`.

The correction carries the observed non-empty Darwin `command_line` into fresh signal validation and requires exact equality with the fresh command line. Missing or changed command identity fails closed. The review found the existing fresh PID, PPID, UID, executable, cwd, start-identity, script, and private-run-root checks retained; Linux continues to use exact argv-array equality. The scoped change and focused tests were reviewed with the immutable runner binding. No weakened check or broader cleanup primitive was found in the reviewed diff.

The M-PIN report and canonical receipt report the focused command as passing 26 tests (0 failed, 0 skipped), including synthetic Darwin predicate coverage. M-OPS did not replay those tests. The Darwin predicate comparisons emit no signal; the existing cleanup tests use disposable test-owned processes. This is source-review evidence only, not native Mac runtime proof.

## Operational boundary and remaining gates

This PASS does **not** approve Darwin cleanup execution, stranded-run recovery, or any process signal, API request, or cleanup operation. Actions run `41538027902` remains preserved and untouched; its runner-account ownership, private evidence, current process identities, and recovery disposition remain unresolved. No cleanup success or independent final census is established. Native macOS behavior under non-root `astrid-runner` has not been proved.

R-25 acceptance and qualification remain **OPEN / NOT APPROVED**. Remaining R-25 evidence includes exact-head native non-root Mac proof, authentic receipts for all six slots, and independent observations. The protected source review recorded here does not supply those runtime or qualification predicates.

All release gates remain explicitly open or unclaimed: stranded-run recovery is not approved; qualification remains **OPEN / NOT PROVEN**; I-08 freeze remains **MISSING / PENDING**; coherent RC binding remains **OPEN / NOT ESTABLISHED**; publication, deployment, and release approval remain **NOT RUN / NOT AUTHORIZED**; go/no-go remains **OPEN / UNCLAIMED**. Handoff remains unapproved. This supplement claims no cleanup success, recovery, qualification, freeze, publication, deployment, release, handoff, or go/no-go decision. All earlier packet dispositions remain unchanged.

No product test, Mac operation, cleanup, hosted job, or qualification action was performed for this documentation update. Only the five O-06-R16 deliverables are written in this task folder.

# O-06-R17 — hosted R-24 diagnostic first-stop evidence

**Documentation-only label:** `lane/M-OPS/O-06-R17`  
**Base:** the complete accepted O-06-R16 `SR-PROJ.md` byte sequence, preserved unchanged as this packet's prefix. This label is documentation-only; it is not a product-repository branch or integration handoff.

## Authenticated run and dispatch binding

This supplement records the supplied GitHub Actions evidence for `banodoco/astrid-agent`, run `41559633705`, job `r24-live`, attempt `1`. The recorded workflow is `lane/M-PIN/R-PIN-FFMPEG-MANIFEST-BIND-R1`; workflow head and `GITHUB_SHA` are `948253ab51e897751dd3bd8fedb8c970fdb4dc29`. The materializer record names the workflow ref `banodoco/astrid-agent/.github/workflows/r24-linux-live.yml@refs/heads/lane/M-PIN/R-PIN-FFMPEG-MANIFEST-BIND-R1`, and records run ID, attempt, job, workflow SHA, and workflow SHA equality. The dispatch request binds the event to `refs/heads/lane/M-PIN/R-PIN-FFMPEG-MANIFEST-BIND-R1`, runner ref `948253ab51e897751dd3bd8fedb8c970fdb4dc29`, workflow SHA of the same value, and producer ref `93c21a16dc94269c0c575f1a721e9b01ef1f386b`. The owner evidence records run ID `96cc09b1668346f793015fc1719c077e`, the run-local data root `/home/runner/work/_temp/r24-live-run/installer-home/.local/share/astrid-runtime`, and runtime root `/home/runner/work/_temp/r24-live-run/pinokio-home/api/astrid/app/astrid`.

## Candidate-bound first stop

The `astrid.r24.materializer.v1` record has status `blocked`, mode `fully-local`, and first stop `authenticate-and-start-worker` (stage `authenticate-and-start-worker`). Its corresponding recorded materializer command label is `authenticate-health-start-local-worker`; that inner `ensure-worker` command exited **1** after 0.164 seconds. The bounded worker log contains exactly:

```text
TaskBoundaryError: Runtime owner discovery mismatch
```

The captured materializer error is `MaterializerError: authenticate-health-start-local-worker exited 1`. These are the observed first-stop records only; they do not establish a broader product root cause.

The failed command is bound in `r24-materializer.json` to candidate tuple:

- App: `bfb15570d72c39fbdc32f6209ecec7c929b71d5c`
- Agent: `69dda026212ca16c37cc8239d06ccd159603aa24`
- Runtime: `2776ba973b6c8f3555b5629b0840d9691a98d3c2`
- Pinokio: `7955800419fc92b11e553d6f7acb7613090617e6`
- Manifest SHA-256: `09707100ad238cf206085aa5b267ca4108e482ac50e06068a3475932180b65bd`

The materializer record also binds its `pinokio-fully-local-linux` install-mode receipt to this same tuple. The evidence file paths and SHA-256 digests are inventoried in the O-06-R17 `INPUT-HASHES.txt`.

The hosted job log `logs/r24-live-125666850680.log` is transport and workflow-step evidence for this run. Its presence does not replace the materializer JSON's inner command exit record or the worker log's bounded error.

## Disposition and open gates

This is a diagnostic first stop only. It does not establish R-24 or R-25 qualification, product acceptance, a product root cause beyond the captured error text, or release gate closure. R-24/R-25 qualification and product acceptance remain **OPEN / NOT ESTABLISHED**. Freeze, coherent release-candidate binding, publication, deployment, release approval, handoff, and go/no-go remain **OPEN / NOT ESTABLISHED / NOT AUTHORIZED / UNCLAIMED**, as applicable. No test or hosted job was rerun for this documentation update; no product or workflow files were modified.

# O-06-R18 — hosted R-24 owner-discovery diagnostic update

**Documentation-only label:** `lane/M-OPS/O-06-R18`  
**Base:** the complete accepted O-06-R17 `SR-PROJ.md` byte sequence, preserved unchanged as this packet's prefix. This label is documentation-only; it is not a product-repository branch or an integration handoff.

## Authenticated run and workflow/head binding

This supplement records the supplied GitHub Actions evidence for `banodoco/astrid-agent`, run `41560168557`, job `r24-live`, attempt `1`. The materializer record binds the run to workflow `lane/M-PIN/R-PIN-OWNER-PREDICATE-CAPTURE`, workflow ref `banodoco/astrid-agent/.github/workflows/r24-linux-live.yml@refs/heads/lane/M-PIN/R-PIN-OWNER-PREDICATE-CAPTURE`, and workflow/head SHA `0135545f29747cf5249913e7b10f02776123112c` (`GITHUB_SHA` and `GITHUB_WORKFLOW_SHA`). The exact workflow-attempt evidence directory is `r24-first-stop-0135545f29747cf5249913e7b10f02776123112c-41560168557-1/` under the Actions evidence root.

## Backend candidate and first-stop record

The `astrid.r24.materializer.v1` record is `blocked`, mode `backend`, with first stop and stage `authenticate-and-start-worker`. Its recorded inner command label is `authenticate-health-start-local-worker`; that command exited **1**. The recorded error is `MaterializerError: authenticate-health-start-local-worker exited 1`. The worker log contains `TaskBoundaryError: Runtime owner discovery mismatch`. These values describe this captured run and do not establish a broader product root cause.

The materializer binds the command to candidate tuple:

- Agent: `ab43bad818ef65ca1e929b973a2f61cd9fc4bf61`
- App: `a10b36b3ecf1365c7e122643b2e3cae5cb5c1c8a`
- Runtime: `2776ba973b6c8f3555b5629b0840d9691a98d3c2`
- Pinokio: `3a6a98ae251ca126505245286e2d3e760f4348e9`
- Manifest SHA-256: `751bb2fcb489c1194c7a158f5d79d3532a594344ae93cc2da4902947382df3d7`

## Captured owner-discovery predicates

The same-invocation `owner-discovery-diagnostic.json` records canonical endpoint equality `true`, endpoint parse success `true`, expected owner token regular/non-symlink `true`, and discovery credential path equality `false`. This section preserves those captured diagnostic values only. It does not infer why the path equality was false or claim a product root cause.

The materializer JSON is the source for the run/workflow/head, mode, candidate tuple, manifest digest, first-stop stage, inner command exit, and materializer error. The owner diagnostic is the source for the owner-discovery predicates. `worker-ready.log` records the bounded worker error. The hosted job log is workflow/transport-step context only and does not substitute for the materializer's inner exit record. Exact evidence paths, SHA-256 digests, and byte counts are in the O-06-R18 `INPUT-HASHES.txt`.

## Disposition and open gates

This is a diagnostic-only update. R-24/R-25 qualification and product acceptance remain **OPEN / NOT ESTABLISHED**. Freeze, coherent release-candidate binding, publication, deployment, release approval, handoff, and go/no-go remain **OPEN / NOT ESTABLISHED / NOT AUTHORIZED / UNCLAIMED**, as applicable. No gate is closed. No qualification, acceptance, product root cause beyond the recorded values, or release decision is claimed. No tests or hosted jobs were rerun, and no product or workflow files were modified.

# O-06-R19 — hosted R-25 fully-local macOS diagnostic first-stop evidence

**Documentation-only label:** `lane/M-OPS/O-06-R19`  
**Base:** the complete accepted O-06-R18 `SR-PROJ.md` byte sequence, preserved unchanged as this packet's prefix. This label is documentation-only; it is not a product-repository branch or integration handoff.

## Authenticated run and workflow/head binding

This supplement records the supplied GitHub Actions evidence for `banodoco/astrid-pinokio`, run `41562449901`, attempt `1`. The workflow lane/ref is `lane/M-PIN/R-PIN-MAC-HOSTED-EXECUTION`; workflow head and checkout SHA are `70275524f4b1c9501ed1ceda98d2c6f42064a89b`. The failed job log records the checkout fetch at that exact SHA and the run's `pinokio-fully-local-macos` slot. The selected mode is **macOS fully-local**, as bound by the selected slot and runner invocation evidence.

## Candidate-bound first failure

The Actions diagnostic `r25-failure.json` records run ID `41562449901`, attempt `1`, and first error **`producer job id was not independently resolved`**. The failed job log records that same error at the snapshot runner step and `Process completed with exit code 1`; the inner exit is **1**. This is the first observed failure in the supplied evidence. It records the failure only and does not establish why producer job identity resolution failed or any broader product root cause.

The job log's candidate tuple and manifest digest are:

- App: `79f7e5397c044c4c97dc7e97b20ad96f787e5247`
- Agent: `ab43bad818ef65ca1e929b973a2f61cd9fc4bf61`
- Runtime: `2776ba973b6c8f3555b5629b0840d9691a98d3c2`
- Pinokio: `3a6a98ae251ca126505245286e2d3e760f4348e9`
- Manifest SHA-256: `b2584f0bb5a949890364004572a0182f2b568adcdba061e73b739e1d3d2d623f`

The log also records Agent ref `a6b68d5b5478326a4db1971966d33a2cfe61d091`, Pinokio daemon ref `add4a674ad1ba95bb62ec427fce519fe5bf0e9bf`, and Pterm ref `5be43718701c448b43daf0ce51d8935f170065bb`. The tuple and manifest values are copied from the run log; they do not independently establish release qualification.

## Evidence and disposition

The diagnostic JSON is the source for run ID, attempt, and exact `first_error`. The failed job log provides workflow/head checkout, selected slot/mode, candidate tuple and manifest digest, runner error text, and inner exit code. Both supplied regular files are inventoried with exact paths, SHA-256 hashes, and byte counts in this task's `INPUT-HASHES.txt`.

This is **diagnostic evidence only**. It does not establish R-24 or R-25 qualification, product acceptance, or root cause. R-24/R-25 qualification and acceptance remain **OPEN / NOT ESTABLISHED**. Freeze, coherent release-candidate binding, publication, deployment, release approval, handoff, and go/no-go remain **OPEN / NOT ESTABLISHED / NOT AUTHORIZED / UNCLAIMED**, as applicable. No gate is closed. No tests or Actions jobs were rerun for this documentation update, and no product or workflow files were modified.

# O-06-R20 — hosted R-24 fully-local diagnostic first-stop update

**Documentation-only label:** `lane/M-OPS/O-06-R20`  
**Base:** the complete accepted O-06-R19 `SR-PROJ.md` byte sequence, preserved unchanged as this packet's prefix. This label is documentation-only; it is not a product-repository branch or integration handoff.

## Run, workflow/head, and selected-mode binding

This supplement records supplied GitHub Actions evidence for `banodoco/astrid-agent`, run `41559633705`, attempt `1`, job `r24-live`. The materializer record binds the run to workflow `lane/M-PIN/R-PIN-FFMPEG-MANIFEST-BIND-R1`, workflow ref `banodoco/astrid-agent/.github/workflows/r24-linux-live.yml@refs/heads/lane/M-PIN/R-PIN-FFMPEG-MANIFEST-BIND-R1`, and workflow/head SHA `948253ab51e897751dd3bd8fedb8c970fdb4dc29`. The selected mode is `fully-local`. The hosted log records the requested runner ref and mode; the dispatch request records the matching workflow ref, runner ref, workflow SHA, and producer ref.

## Candidate-bound first stop and captured inner result

The `astrid.r24.materializer.v1` record binds the `pinokio-fully-local-linux` install-mode slot to the same candidate and records first stop `authenticate-and-start-worker` (stage `authenticate-and-start-worker`). The corresponding inner command is `ensure-worker`, recorded under label `authenticate-health-start-local-worker`, and its exit is **1**. The bounded `worker-ready.log` contains the captured error:

```text
TaskBoundaryError: Runtime owner discovery mismatch
```

The materializer's candidate tuple is:

- App: `bfb15570d72c39fbdc32f6209ecec7c929b71d5c`
- Agent: `69dda026212ca16c37cc8239d06ccd159603aa24`
- Runtime: `2776ba973b6c8f3555b5629b0840d9691a98d3c2`
- Pinokio: `7955800419fc92b11e553d6f7acb7613090617e6`
- App manifest SHA-256: `09707100ad238cf206085aa5b267ca4108e482ac50e06068a3475932180b65bd`

The materializer JSON is the source for run/workflow/head, selected mode, candidate tuple and manifest digest, first-stop stage, command label, and inner exit. The worker log is the source for the exact captured diagnostic. The hosted job log records `INNER_EXIT: 1`; it does not contain the candidate tuple or the worker diagnostic. Exact paths, SHA-256 digests, and byte counts for every regular file in the supplied Actions evidence root are recorded in `INPUT-HASHES.txt`.

## Disposition

This is a bounded diagnostic-only record of the captured first stop and inner exit. It does not infer root cause, qualification, or product acceptance. R-24/R-25 qualification and release gates remain open. No test or Actions run was replayed, and no product or workflow files were modified.
