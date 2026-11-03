# I-AG-03a execution-custody decision packet

Date: 2026-11-02 UTC  
Status: proposed design; implementation and protected proof are outside this task  
Review request: `NEEDS: I-AG-03a design review`

## 1. Decision

Resolve the M + Timeline Agent seam as a semantic union with one custody owner at
each boundary:

- Runtime remains the sole authority for the task lease, attempt/fence identity,
  and terminal mutation.
- `GenericPackHost` owns admission, child launch, process-tree custody, source
  descriptors, child bridges, output collection, secret-bearing environments,
  and the single call into Runtime terminal publication.
- A worker owns only execution of the admitted command or capability. It never
  acquires lease or settlement authority.
- The SDK child path may request and observe delegated work only through the
  inherited bridge. It never receives a general Runtime credential or a second
  terminal path.
- Native rendering accepts only host-prepared verified source, uses host-owned
  process-tree and Remotion custody, and cannot accept caller-supplied source
  authority.

The resolution keeps M's child-import receipt, host-owned generation/output,
publication-readback, structured terminal, and filtered-object behavior. It
also keeps Timeline's captured-source channel, verified render source, Remotion
readiness/failure behavior, exact media selection, and verified-media behavior.
No side is selected wholesale.

This decision does **not** change the v33 Runtime schema, generated wire shapes,
bridge operations, task/lease fields, terminal operations, installer/update
contract, or release tuple. Agent base
`b54117c9312165e2799554ec27480665d5cad2b1` already pins Runtime
`2776ba973b6c8f3555b5629b0840d9691a98d3c2`; read-only inspection confirms that
Runtime exports `get_project_media_selection`, `get_project_verified_media`, and
filtered `list_project_objects`.

## 2. Inputs and bounds

### Pinned identities

| Input | Identity | Use in this packet |
|---|---|---|
| Agent integration base | `b54117c9312165e2799554ec27480665d5cad2b1` | Source anchors and existing union context |
| Runtime product | `2776ba973b6c8f3555b5629b0840d9691a98d3c2` | Frozen generated/client operation compatibility |
| M Agent candidate | `3bac7302675cce3dc734639c29d1afaef62e1668` | M custody intent |
| Timeline delivery | `19bd005bbeee41aaa10c2a75ede61a400c5f3d86` | Earliest coherent Timeline delivery |
| Timeline current reference | `a6a94edcf682074829879c2e2cf08d4d2ca8bfe8` | Later Timeline intent |

All three candidate/reference commits resolve locally and are ancestors of the
Agent integration base. That observation establishes local ancestry only; it is
not remote publication, owner sign-off, implementation acceptance, or protected
proof.

### In scope

- Custody and cleanup order across the six named files.
- Lease and terminal publication ownership.
- Secret-bearing environment lifetime.
- Delegated-child and native-render execution boundaries.
- Source-channel closure.
- Focused acceptance requirements for a future implementation task.

### Out of scope

- Product edits, generated client edits, pin changes, workflows, and tests.
- Installer/schema-admission reconciliation (`I-AG-04`).
- Runtime writer cutover (`I-AG-09`).
- v34 durable attempt custody, which is deferred and disabled for Release 1.
- Accepting or landing the separate stop-proof implementation lane. A boolean
  `stop_observed` is not proof; the existing I-AG-03b ruling remains authoritative.
- Claiming that source already present at the integration base is accepted or
  protected-proven.

## 3. Custody state machine

| Phase | Owner | Required transition |
|---|---|---|
| A. Claim/admission | Runtime for lease; host for local admission | Bind task ID, attempt ID, lease ID/token, fence, Runtime epoch, capability digest, and source identity before any child work. Unknown or changed identity fails closed. |
| B. Private preparation | Host | Materialize the attempt, verified inputs, captured-source descriptor, bridge, Remotion lease, and child environment. Keep source authority and secrets outside public action inputs. |
| C. Launch transfer | Host to worker | Pass only admitted inherited descriptors. The parent closes its source-channel handle in the launch `finally`, including launch failure. A partially created process remains host-owned and triggers unresolved-cleanup handling. |
| D. Child execution | Worker under host custody | Read and close the child source descriptor once; observe the receipt entry before captured execution; execute only admitted source; close the inherited bridge in `finally`. |
| E. Local terminal preparation | Host | Prove the command/render process tree gone, or retain and latch uncertainty. Finish/revoke child activity, validate and upload outputs, and settle the managed-session token. A declared warm session may remain only under its existing explicit owner/binding policy. |
| F. Runtime terminal publication | Runtime via one host gate | Freeze exactly one immutable `settle`, `fail`, or evidenced `cancel` request for the exact attempt/fence. A lost acknowledgement enters reconciliation; it must not cause an opposite terminal mutation. |
| G. Post-terminal disposition | Host | Revoke the bridge, clear secret-bearing mappings, release/fence non-retained sessions, and delete or explicitly retain the attempt with a receipt. Cleanup uncertainty is latched and never presented as verified cleanup. |

The distinction between phases E and G is intentional. Active child writers and
render descendants must be gone before terminal **success**. Deleting an inert
attempt spool may follow terminal publication. If deletion cannot be proved, the
spool is retained and the uncertainty is reported; it does not authorize a
second terminal call.

## 4. Cross-cutting invariants

### C1. One terminal authority and one frozen intent

1. Runtime is authoritative for lease validity and terminal state.
2. Every terminal call carries the same task/attempt/lease/fence identity that
   was admitted. No worker or SDK child may construct alternate authority.
3. The first terminal attempt atomically freezes its operation and arguments.
   Later activity may return the recorded result or idempotently reconcile the
   same request; it may not issue a different terminal operation.
4. The lease pump remains active through collection, upload, managed-session
   settlement, and the Runtime terminal call.
5. If lease validity is lost or lifecycle termination wins, a late success
   response cannot revive the attempt.
6. A potentially committed success with a lost acknowledgement retains its
   exact request and spool for reconciliation. It is never converted to failure
   merely because readback was unavailable.

### C2. Cleanup is evidence, not an assertion

- Source-channel closure, bridge closure/revocation, process-tree cleanup,
  managed-session release/fencing, and attempt disposition are separate facts.
- `stop_observed=True` is permitted only after positive, incarnation-bound
  containment evidence for the relevant child/process scope. A cancel request,
  leader exit, bridge close, or cleanup attempt is insufficient by itself.
- A render leader exiting does not prove its native descendants are gone.
  Rendering retains the owned-group/tree census until absence is established.
- Cleanup functions are idempotent locally, but idempotence must not mask an
  unresolved first cleanup. Uncertainty retains evidence and latches the host
  diagnostic.
- Cleanup failure preserves the original execution error and adds bounded,
  redacted custody detail. It does not rewrite an execution failure as success.

### C3. Source channel has a single-use lifetime

- The parent creates a canonical, bounded descriptor only from the Runtime-
  selected source and admitted definition.
- The descriptor FD/handle is the only source transfer. Captured external code
  is not placed on ambient `PYTHONPATH`.
- The parent closes its handle in a `finally` around the launch attempt. It must
  close on success, pre-spawn failure, and partial-spawn failure.
- The worker reads the bounded descriptor once through an owning file object,
  validates canonical bytes and file digests, and closes its handle as part of
  the read.
- A successful-launch receipt is recorded only after the process exists and the
  parent's source handle is closed. Failed launch finalizes a failure/missing
  receipt but never records `launched`.

### C4. Secret lifetime is bounded to one launch

- Runtime credential, provider credentials, broker token, review nonce, and
  other declared secrets exist only in host-local secret/environment mappings
  and the admitted child environment.
- Render readiness validation runs on the final projected environment. Any
  validation failure clears both secret values and the environment before
  returning.
- One outermost finalizer covers preparation and launch as well as running-child
  cleanup. Thus a failure before `Popen`/spawn must clear mappings too.
- Output capture redacts known secret values before persistence. Exception text,
  terminal receipts, cleanup diagnostics, and doctor output must contain no raw
  secret.
- Delegated children do not receive the general Runtime endpoint/credential;
  they use only the inherited three-operation bridge and any narrowly admitted
  broker material.

### C5. Delegated children remain subordinate

- The parent host creates and retains `HostChildBridge`; the SDK only submits,
  polls, and reads outputs through the inherited client.
- Every status/readback is bound to the returned task ID, run ID, and attempt ID.
  Foreign/malformed status, output pages, or publication readback close the
  channel and fail the parent.
- Parent success waits for child terminal success and verified outputs/readback.
  Timeout, parent lease loss, or parent cancellation revokes the channel and
  contains descendants.
- Host-owned H3 output roots and host-derived publication controls remain
  unforgeable action inputs.
- Bridge revocation is unconditional in the host finalizer and does not itself
  claim process stop or Runtime terminality.

### C6. Native rendering retains verified-source custody

- `rendering.render` and `rendering.timeline_visualize` reject reserved source
  keys on the copied request before top-level or child dispatch.
- Only host-prepared verified source is handed to the renderer. Caller JSON,
  ambient environment, equal bytes at a different path, or installed-source
  fallback cannot replace it.
- Remotion readiness is validated against the worker's actual launch context.
  Failed readiness does not launch a renderer and does not leak secrets.
- Render commands use owned process-group/tree custody and continue observing
  descendants until cleanup is proven.
- Timeline's actionable Remotion failure formatting is applied after preserving
  the original error, coded fields, recovery suffix, and cleanup state.

## 5. File-by-file custody resolution

### `astrid/core/execution/generic_host.py`

Keep and order both sides as follows:

1. Build the child environment from declared inputs and secrets. Keep M's scoped
   VibeComfy execution request and Timeline's render-environment validation.
   Validate the final environment; clear secret/environment mappings on every
   failure, including failures before spawn.
2. Prepare child bridge, captured-source channel, Remotion lease, and M child-
   import receipt before launch. None is terminal or stop authority.
3. Launch inside a `try/except/finally` whose `finally` always closes the parent
   source channel. If the launcher exposes a partially created process, track
   it, latch cleanup uncertainty, and return an unresolved-cleanup error; do
   not pretend the launch never occurred.
4. Mark the import receipt launched only after successful spawn and parent
   source closure. Finish the receipt on success and all failures. Receipt
   persistence failure is diagnostic and must not replace the child outcome.
5. Use one idempotent local cleanup owner for the process scope. For render
   paths it must prove the native descendant tree absent. Keep the original
   execution error when cleanup also fails.
6. Preserve `_terminal["error"]`, structured error fields, Timeline's Remotion
   message, and recovery guidance together. `fail_admission` and later failure
   paths both use the single terminal-publication gate.
7. `completion_call` (or its replacement with the same semantics) is the only
   path to Runtime `settle`, `fail`, or evidenced `cancel`. It freezes one exact
   request, rejects a late settle after forced termination, and supports same-
   request reconciliation without double settlement.
8. Finish the child bridge before collection and revoke it unconditionally in
   the outer finalizer. Clear worker and command environment/secret mappings
   whether spawn succeeded or failed.
9. Runtime success follows verified outputs and managed-session settlement.
   Non-retained sessions are released/fenced afterward; cleanup uncertainty is
   retained and latched. No release action may interrupt a human-owned ComfyUI
   server.

This file is the primary protected surface. A textual union of the conflict
hunks is insufficient because launch, receipt, source close, cleanup, and
terminal publication occur in different nested finalizers.

### `astrid/core/execution/generic_host_worker.py`

- Keep Timeline's captured-source reader and captured-command path.
- In receipt-bearing child/renderer command execution, install the inherited
  bridge first, call `observe_action_entry("worker_entry")`, then branch to the
  captured-command path. Ordinary and captured execution therefore observe the
  same worker entry before pack code.
- Close the inherited bridge in `finally`, including captured-command failure.
- The direct captured-source descriptor reader continues to own and close the
  FD while validating bounds, canonical encoding, selected definition, and
  hashes.
- Do not add lease, Runtime credential, output publication, or terminal
  authority to the worker.

### `astrid/core/execution/orchestrator/runner.py`

- Keep both `logging` (M receipt-persistence diagnostics) and `json` (Timeline
  captured-source command encoding).
- A captured Python orchestrator must match the internal invocation, selected
  orchestrator identity, and definition digest. It may not fall back to
  installed source.
- Close the parent captured-source channel and remove request/result files in
  the subprocess `finally`, on success and failure.
- For M's local H3 orchestrator path, prepare/launch/finish the import receipt,
  terminate and verify the owned tree on failure, and attach the finished
  receipt without replacing the underlying outcome.
- Runner returns an execution result only. Host/Runtime retain lease and
  terminal settlement ownership.

### `astrid/core/gateway/dispatch.py`

- Initialize M's supervision `warning` to `None` before diagnostics.
- Import Timeline's `RuntimeCLI` and `RuntimeCLIError` outside the diagnostic
  `try` so Runtime unavailability remains a stable catchable boundary even when
  diagnostic setup fails early.
- When available, preserve the supervision warning in successful text and JSON
  doctor output. If Runtime is unavailable, preserve the same warning in the
  error payload and text output.
- Doctor remains read-only. It reports cleanup/renderer/service facts but gains
  no cleanup, launch, lease, or settlement authority.

### `astrid/sdk/invocation.py`

- Import and use both `_child_status_error` and
  `_validate_publication_readback`.
- Preserve M's host-owned H3 output rejection, OpenAI normalization, and
  child-publication restrictions.
- Preserve Timeline's `reject_source_keys` for both native render capabilities,
  before render-mode validation or delegated dispatch.
- Delegated status must validate task/run identity on every poll. Output pages
  additionally bind the settled attempt. Invalid status, output, or publication
  readback closes the bridge and fails closed.
- Successful staged child publication is projected only after readback matches
  task, run, attempt, and managed outputs. The SDK does not settle the parent or
  retry a terminal mutation.

### `astrid/sdk/workspace_client.py`

- Keep Timeline's exact `get_project_media_selection` and
  `get_project_verified_media` wrappers.
- Keep M's `kind`, `model`, `target`, and `tag` filters on
  `list_project_objects`, forwarding only non-`None` filters while preserving
  cursor and limit.
- Keep all three operations in the generated-operation allowlist. Missing
  generated methods fail explicitly as Runtime/client incompatibility; do not
  silently fall back to an unfiltered or local implementation.
- Do not hand-edit the generated Runtime client as part of this seam. Verify
  vendored parity against Runtime `2776ba…`; regeneration/pin ownership remains
  with the tuple integrator.

## 6. Focused acceptance for a future implementation lane

These are required future tests, not results from this design task.

| ID | Scenario | Required observation |
|---|---|---|
| AG03-01 | Command and capability spawn succeed with captured source | Parent source handle closes exactly once before the launch receipt is marked; child reads/closes its handle; execution proceeds from captured bytes only. |
| AG03-02 | Failure before spawn and launcher failure after partial spawn | Source channel and all secret/env mappings clear. No false `launched` receipt or success settlement. A retained partial process is tracked, cleanup is attempted, uncertainty is latched, and terminal failure occurs at most once. |
| AG03-03 | Captured receipt-bearing child command | `worker_entry` precedes captured pack import/action entry; inherited bridge closes on return and exception; receipt cannot contain child-supplied host/task identity. |
| AG03-04 | Normal failure plus cleanup failure | Original execution/coded error remains primary; bounded cleanup detail is redacted; attempt is retained; no success or second terminal mutation occurs. |
| AG03-05 | Success, deterministic admission failure, execution failure, cancellation, and deadline | Exactly one frozen terminal operation per attempt/fence. Heartbeat remains live through publication. Result receipt and Runtime terminal agree. |
| AG03-06 | Lost terminal acknowledgement and concurrent finalizer/observer | Only the identical frozen request may reconcile. Potential success is not converted to failure; spool remains until readback. Opposite/double terminal calls are zero. |
| AG03-07 | Lease loss during execution, collection, and settlement | Child/bridge are revoked and descendants contained; late settle is refused. `stop_observed` is absent/false until positive cleanup evidence exists. |
| AG03-08 | Delegated child success/failure/timeout/foreign status | Parent waits for verified terminal/output/readback; malformed or foreign identity closes the channel; parent lease loss contains child work; no Runtime credential crosses the bridge. |
| AG03-09 | Native `rendering.render` and `timeline_visualize` | Caller source keys are rejected before bridge/launch; final Remotion environment is validated; successful render settles once; detached renderer descendants are cleaned. |
| AG03-10 | Render readiness/launch/render failure with planted secret | No renderer success is recorded; Timeline failure guidance and original error survive; source/bridge close and secret clearing occur; secret is absent from stderr, result, requests, and doctor data. |
| AG03-11 | Captured orchestrator success and failure | Both `json` path and `logging` receipt path work; source channel closes and temp request/result files are removed; installed-source fallback is refused. |
| AG03-12 | Runtime unavailable with supervision warning | `astrid doctor` text and `--json` retain the warning and stable Runtime-unavailable classification; `warning` is never unbound. |
| AG03-13 | Workspace media and object calls against Runtime `2776ba…` | Exact media selection and verified-media shapes pass unchanged; each object filter and combined filters reach the generated client; no-filter pagination stays compatible. |
| AG03-14 | Final source/client freshness | Agent lock remains `2776ba…`; vendored client operation catalog and bytes match that Runtime; no generated artifact was hand-merged. |

Minimum focused selection should include the affected cases in:

- `tests/core/execution/test_child_import_receipt.py`
- `tests/core/execution/test_generic_host_child_bridge_d18.py`
- `tests/core/execution/test_render_scope_launch.py`
- `tests/core/execution/test_render_scope_cleanup.py`
- `tests/core/execution/test_runtime_orchestrator_custody_d40.py`
- `tests/core/gateway/test_doctor_bundle.py`
- `tests/sdk/test_child_bridge_d18.py`
- `tests/packs/rendering/test_verified_source.py`
- `tests/test_generic_host.py`
- `tests/test_generic_host_result_json.py`
- `tests/stage1/test_vendored_workspace_client_parity_luna.py`
- Runtime `tests/test_generated_client.py`,
  `tests/test_project_object_filters.py`, and exact/verified media client tests
  at `2776ba…`

New or strengthened cases are still required for AG03-02, AG03-05/06, AG03-07,
and the combined secret/source/receipt ordering in AG03-10. Existing individual
tests must not be treated as proof of the composed ordering unless they assert
the relevant call counts and happens-before relations.

No bridge, install/update, data-preservation, or protected execution-custody
failure may be waived or converted into a count-only known-failure allowance.

## 7. Rationale and rejected alternatives

### Chosen: explicit union with a host-owned ordering

The M and Timeline changes protect different facts. M proves which child ran and
how terminal/output authority is retained. Timeline proves which source bytes and
render context the child consumed. They are complementary only if source close,
receipt launch, process cleanup, and terminal publication have a defined order.

### Rejected: select either conflict side

Selecting M loses captured-source/render custody. Selecting Timeline loses
receipt, publication-readback, host-owned output/filter, and terminal tracking.
Either choice violates the stated combined-release intent.

### Rejected: close the source channel only after child completion

The child already owns its inherited descriptor. Keeping the parent descriptor
open for the task lifetime extends authority, leaks handles, and makes launch
failure cleanup ambiguous. Parent closure belongs immediately around spawn.

### Rejected: let the worker or SDK settle

The worker lacks the authoritative lease pump and cleanup census; the SDK child
is deliberately constrained to the inherited bridge. A second settlement owner
would create double-settle and lost-ack races and would widen the frozen bridge.

### Rejected: treat leader exit or cancel request as stop proof

Native renderers and delegated work can outlive a leader. Stop proof must be
incarnation-bound containment evidence. This follows the existing genuine-stop-
proof ruling; it is not a new boolean contract.

### Rejected: regenerate or widen Runtime for this seam

Runtime `2776ba…` already contains the needed exact-media and object-filter
operations, and the Agent base pins its generated client. Changing the contract
would add tuple and frozen-interface risk without resolving execution custody.

## 8. Review classification and exact question

| Category | Disposition |
|---|---|
| Frozen bridge/Runtime contract | **No change proposed.** Existing task/lease/terminal and child-bridge shapes are preserved. Any implementation that requires a new operation or field must stop and request a separate frozen-contract decision. |
| Migration/data safety | **No schema, migration, backup, rollback, or writer-policy decision.** Execution custody is nevertheless a protected surface; its focused cleanup/settlement tests remain non-waivable. |
| Cross-owner seam | **Review required.** This packet orders M receipt/terminal intent with Timeline source/render intent. Under the newest review override, orchestrator approval replaces separate GPU + Timeline signatures. |
| Genuine stop proof | **Existing ruling applies.** Boolean stop authority is insufficient, and the separate I-AG-03b stop-proof/liveness lane is not accepted or pulled in by this packet. |
| Generated client/pin | **Compatibility verified read-only; no decision needed.** Runtime `2776ba…` exposes all required methods and Agent base pins it. Freshness remains an implementation gate. |

Exact review question:

> Approve the host-owned ordering in sections 3–5: Runtime remains the sole
> lease/terminal authority; captured source and secret-bearing launch material
> are closed/cleared on every path; active child/render scopes are positively
> contained before terminal success; and M receipt/publication behavior is
> composed with Timeline verified-source/render behavior without changing the
> bridge or Runtime contract.

Approval releases only a future implementation task. It does not accept current
source, protected proof, or the deferred stop-proof lane.

`NEEDS: I-AG-03a design review`
