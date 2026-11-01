# F07 Comfy host hook contract

Status: **interface only**. The host calls this contract; the shared Comfy
implementation and the child-side consumer are not yet present. Until they
land, `CheckoutServerAdapter.open_host_session` is absent and managed checkout
execution fails closed. No provider or real engine is exercised by these tests.
This is an in-process owner interface, not a Runtime wire change.

The pre-existing `comfy_url` local-server path uses a separate journal cleanup
adapter, not these hooks. Its cancellation helper does not currently prove
that an interrupted prompt has stopped, so its acknowledgement cannot establish
the quiescence required below. It needs owner-lane reconciliation before this
contract can be claimed for real attached servers.

Types below are from `astrid.core.execution.managed_tool_session`; `Mapping`
means `collections.abc.Mapping`. Methods are synchronous. False or missing
success evidence must never be interpreted as success. The owner must not
perform side effects in a failed open. All control methods may raise instead of
returning failure.

```python
@classmethod
def CheckoutServerAdapter.open_host_session(
    cls, *, binding: SessionBinding, hc03_profile: Mapping[str, Any],
    endpoint_transport: Any = None,
) -> ComfyHostHooks: ...

class ComfyHostHooks(Protocol):
    def invoke(self, *, token: AdmissionToken, model_id: str,
               template_id: str) -> Mapping[str, Any]: ...
    def observe(self, *, binding: SessionBinding) -> Mapping[str, Any]: ...
    def fence(self, *, reason: str) -> Mapping[str, Any]: ...
    def cancel(self, *, reason: str) -> Mapping[str, Any]: ...
    def release(self, *, reason: str) -> Mapping[str, Any]: ...
```

`open_host_session` binds an **existing selected owner**, it does not start or
replace an engine. Verify the ready profile, Runtime instance and claim epoch,
supervisor launch generation, supervisor process birth and engine process
birth, endpoint, source revision and content, configuration and effective
resident model/workflow requirements. `SessionBinding` carries session ID,
Runtime instance/epoch, process birth, endpoint, source revision, config digest,
residency (`execution_identity`), launch generation and engine birth. The
profile provides the independent source-content and remaining owner evidence;
the residency digest also includes source-content and effective requirements.
Missing or mismatched evidence must be refused. A listener URL by itself is not
ownership evidence: an attached/human server must never be upgraded to owned.
Use the installed engine's selected receipt/custody owner, not discovery in the
agent interpreter, and preserve its verified PID on compatible reuse. Transport
is optional and, when supplied, belongs to the retained owner until successful
release. No implicit launch, restart, adoption or replacement is authorized.

The host opens the manager slot and observes it, calls `admit`, then consumes
`adapter_for(token, begin=True)` immediately before dispatch. `invoke` binds
prompt acceptance, journals and all later native work to that exact token;
refresh the model, template and invocation context on each warm call without
changing residency. A fresh prompt-attempt client is required for every call.
Return `{"ok": True, "child_context": context}`, where `context` is JSON
serializable and contains `token_id`, `invocation_id`, `generation`, and
`binding_identity` (a JSON list exactly equal to the token's tuple). Additional
opaque owner-specific fields may identify a securely scoped journal. The host
validates these four fields and passes the context only to the admitted child in
`ASTRID_COMFY_INVOCATION`. The future child-side hook must consume it and bind
prompt acceptance and completion to that token; it must reject missing, stale,
or mismatched context. Until that consumer lands, the fake is the only proof of
prompt custody. The manager will not issue a second dispatch for a token.

`observe(binding=...)` independently revalidates the **entire** current owner
binding against live Runtime, supervisor, process/engine birth, endpoint,
source/config and residency evidence, including profile facts not contained in
`SessionBinding`. Return `{"ok": True, ...}` only when all match; return
`{"ok": False, ...}` or raise for changed or missing evidence. The host observes
before admission and again before settlement/publication; a failed observation
fences the slot and prevents successful settlement.

`fence(reason=...)` prevents output from the exact currently bound work from
being accepted, and returns `{"ok": True, "fenced": True, ...}` only when
fencing is established. `cancel(reason=...)` targets only the exact prompt(s)
recorded for that invocation, returning `{"ok": True, "cancelled": True, ...}`
only on confirmed quiescence. A human/attached server permits only
prompt-specific cancellation; never global interrupt, queue clear, stop,
restart, or `/api/free`. Missing prompt identity or an uncertain response must
remain fenced and must not be reported as cancelled. The host checks the
admission token before invoking cancellation; an undispatched admission never
cancels the preceding warm call.

`release(reason=...)` quiesces the exact work before relinquishing bookkeeping.
It returns `{"ok": True, "released": True, ...}` only after owner-specific
release is verified. If work or cancellation remains uncertain, fail and retain
the fenced slot for reconciliation. For an owned engine, revalidate its full
binding before `/api/free` with `unload_models` and `free_memory` true; keep the
supervisor's verified process alive. For an attached engine do bookkeeping
only, return `external_residency_unchanged: True`, and never issue `/api/free`.
Report `release_requested` separately from `memory_observed_returned`: an
acknowledgement is not evidence that VRAM returned.

Explicit owner release must route to the retained `GenericPackHost` via the
existing Runtime/worker control, using
`release_managed_session(*, expected_binding: SessionBinding,
expected_generation: int, reason: str = "requested") -> Mapping[str, Any]`.
It returns `release_pending` while setup or admission is busy, refuses stale
binding/generation, and retains a poisoned slot when fencing or release fails.
It preserves the native evidence. No independent CLI-owned adapter may be
constructed for release. The actual public control route is owned by the shared
hooks lane and remains unimplemented here.

## Binding field mappings (recorded per M-GPU, 2026-11-01T15:17Z)
- engine_birth_id = comfy_process_birth_id.
- launch_generation = the same process birth value on both the managed and the attached paths, matching the wan2gp and generic_host builders. It is NEVER launch_token, which is a credential.
- execution_identity = the residency digest. The attached path refuses when the hc03 profile has none. The managed path takes it from the attested engine environment, or refuses.

## Updates from the shared hooks lane (recorded per M-GPU, 2026-11-01T15:26Z)
- Managed release signature: open_host_session(..., *, host=None). With a host, managed release routes to host.release_managed_session(require_quiescent=True). Without a host, it fails closed.
- source_digest = source_content_digest.
- Reference implementation of the shared hooks: lane/M-GPU/p1-f07-comfy-hooks at bd1561f4 (stacked on de43cadf). Radagast owns it.
