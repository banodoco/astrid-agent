# Astrid Python SDK

The `astrid` package exposes a public Python SDK for capability discovery,
schema inspection, generation, and runtime-backed invocation. Import the
top-level package — the supported SDK surface is available from `import
astrid`.

Stage1 runtime boundary: ordinary SDK clients require an explicit endpoint,
credential, realm id, actor id, client identity, and `workspace.v1` protocol
version. They never infer a checkout, mutate `sys.path`, or launch a process.
Astrid's installer owns the local Runtime service lifecycle. Set
`ASTRID_RUNTIME_SOURCE_MANIFEST` and, for an explicitly configured client,
`ASTRID_RUNTIME_CREDENTIAL` directly.
The runtime is the sole authority for projects, media, timelines, tasks, runs,
receipts, and events. `AstridClient` is a runtime client: it does not open a
checkout-local database or content store and does not execute a pack
in-process as the live authority.

The Astrid checkout contains no schema registry or migration runner. The
neutral workspace runtime is the sole owner of product tables, constraints,
and migration application; pack manifests remain executable capability
metadata.

## Shot text bindings

Shot-owned prompts, voiceover scripts, and transcripts are persisted by the
neutral runtime as project-scoped immutable text bindings. A binding identity
is deterministic UUIDv5 over `(project_id, shot_id, kind, slot)`; prompt slots
are lowercase slugs and `slot=null` is the unslotted identity. Text is frozen
as strict UTF-8 of at most 1 MiB and stored in the runtime CAS. Set/rebind
commands require `expected_head`, use an idempotency key, and return the
runtime's committed receipt. The remote shots adapter exposes
`list_text_bindings`, `show_text_binding`, `set_text_binding`, and
`rebind_text_binding`; it never opens local persistence. Pass `include_text=True`
to list/show to fetch and verify the exact immutable UTF-8 text alongside its
binding head. The CLI exposes this as `timelines shots text list/show/set`.
Voiceover scripts and transcripts are unslotted; only prompts accept a slot.

The published Astrid wheel includes the generated
`astrid_runtime_client` transport package. It is a pinned vendor copy of
the neutral runtime contract (source commit is recorded in
`astrid_runtime_client.contract_metadata`); it is never loaded from a
sibling checkout or an ambient `PYTHONPATH`.

> **Compatibility policy**: This document is a user-facing walkthrough. The
> normative v1 compatibility contract lives in
> [docs/platform-contract.md](../contracts/platform-contract.md). That file defines the
> supported export list, SemVer rules, deprecation window, DTO stability tiers,
> manifest schema contract, and disclosure-only trust block. When this doc and
> `platform-contract.md` differ, the platform contract wins.

```python
import astrid

# Discovery
inventory = astrid.discover()
for cap in inventory.capabilities:
    print(cap.id, cap.capability_type)

# Lookup
cap = astrid.get_capability("editorial.arrange", kind="executor")
print(cap.schema, cap.inputs, cap.outputs)

# Invocation (dry-run)
result = astrid.invoke(
    "iteration.experiment_review",
    kind="executor",
    project="demo",
    inputs={"review": "experiments/prompt-brevity/review.json"},
    dry_run=True,
)
print(result.ok, result.raw_result)

# Generation
gen_result = astrid.generate(
    "editorial.arrange",
    prompt="Generate a short video from these clips.",
    inputs={"clips": ["clip1.mp4", "clip2.mp4"]},
)
print(gen_result)
```

> **Tutorial**: For a step-by-step walkthrough building your first Astrid
> agentic UX, see [docs/build-your-first-agentic-ux.md](../guides/build-your-first-agentic-ux.md).

## Quick Examples

### Discovery

```python
import astrid

inventory = astrid.discover()

# Typed grouped access
for executor in inventory.executors:
    print(executor.id)

for element in inventory.elements:
    print(element.id, element.native_kind)

# Flat all-capabilities view
print(len(inventory.capabilities))

# JSON-safe serialization
import json
json.dumps(inventory.to_dict())
```

Discovery is scoped to the checkout's manifest-ledger pack inventory plus any
explicitly configured read-only external roots. Stage1 runtime capability
registration is explicit and runtime-owned; dynamic installed-pack discovery
and install mutation are not product extension points. Use local pack
directories only while authoring or testing a pack.

`kind="executor"` (or `"orchestrator"` / `"element"`) filters the returned
inventory to that capability type — `capabilities` then carries only those
entries; an invalid kind raises `CapabilityValidationError`.

`discover()` loads the executor, orchestrator, and element registries in
dependency order (executor first, then orchestrator, then elements). Element
loading is theme-aware: pass `active_theme` to include theme-specific element
overlays, and `include_missing_roots=True` to include element roots that have
not been locally installed yet.

The readiness ledger is a separate, host-facing projection of this inventory.
In the current checkout it reconciles 76 pack labels (81 historical non-Fal
labels) and 74 historical executor contract rows; the local generic host
discovers 59 executable in-tree executor manifests. Historical external rows
(including Hivemind, Discord-local, and Seedance-local) are reported as
`unavailable_external` when their source pack is not installed, never as ready
local capabilities. The host re-runs preflight before each runtime claim, so
the capability IDs offered for admission are exactly the capabilities that
are currently ready. The projection also retains 19 retired pre-cutover App capability
IDs. Hivemind's v2 executor and contributor-operation coverage is maintained
in the Astrid-side [external-pack contract](hivemind-pack-contract.md). The
delivery configuration carries a full immutable source pin; setup does not
count the pack as installed unless that exact object is locally available.

### Schema Inspection

```python
import astrid

cap = astrid.get_capability(
    "editorial.arrange",
    kind="executor",
)

# Stable DTO fields
print(cap.id)                # "editorial.arrange"
print(cap.capability_type)   # "executor"
print(cap.native_kind)       # "built_in" or "external"

# Inspect inputs and outputs
for port in cap.inputs:
    print(port.name, port.type, port.required)

for output in cap.outputs:
    print(output.name, output.type)

# Serialized schema mapping (keyed by field name)
print(cap.schema["runtime_kind"])
print(cap.schema["isolation"]["mode"])

# Full definition payload (same as schema for executors/orchestrators;
# separate from schema for elements)
print(cap.definition["inputs"])

# Identity handle with aliases and provenance
print(cap.handle.canonical_id)
for alias in cap.handle.aliases:
    print(alias.alias, "->", alias.canonical_id, "deprecated:", alias.deprecated)
```

### Dry-Run Invocation

Dry-run invocations validate request construction and registry routing without
executing side effects. The returned `InvocationResult` carries the normalized
command, environment, and dry-run metadata.

```python
import astrid
from pathlib import Path

result = astrid.invoke(
    "iteration.experiment_review",
    kind="executor",
    out=Path("./out/review-test"),
    project="demo",
    inputs={"review": "experiments/prompt-brevity/review.json"},
    dry_run=True,
)
print(result.ok)                     # True if no validation errors
print(result.raw_result["dry_run"])  # True
print(result.raw_result["command"])
```

`kind` is required (`"executor"` or `"orchestrator"`). Pass the connected
`client=` for live module-level invocations, or use `client.invoke_result()`.
For project work, `project=<slug-or-id>` overrides the runtime's persisted
current selection; omitting it reuses that selection. Dry runs do not read
selection or admit work.

Public Hivemind reads (`hivemind.search`, `hivemind.get_item`, and
`hivemind.refresh_media`) work without a selected project. Omit `project` for
workspace-scoped research, or supply it explicitly to associate the run with a
project. These calls still pass through runtime admission and artifact storage;
there is no standalone executor fallback. Completed invocations expose
artifact descriptors in `result.outputs["artifacts"]`; retrieve a descriptor's
`digest` with `client.media.read_bytes(digest)`. This Python-only method returns
bytes and raises typed SDK errors for runtime rejection or malformed download
responses. It also supports projectless artifacts; authorization remains with
the runtime.

### Regular Invocation

A real executor run addressed with `project=` is admitted by the workspace
runtime. Pack code may write attempt-local files and return a result manifest;
the runtime owns durable object publication and task/run state. Do **not** pass
`out=` together with `project=` (the two are mutually exclusive):

```python
import astrid

result = astrid.invoke(
    "iteration.experiment_review",
    kind="executor",
    project="demo",
    inputs={"review": "experiments/prompt-brevity/review.json"},
)
print(result.ok)        # True on success
print(result.run_id)    # durable kernel run identity
    print(result.run_root)  # runtime-owned result reference
    print(result.outputs)   # runtime-published artifact references
print(result.error)     # error mapping if not ok
```

Orchestrator invocations take the same arguments minus `out` (their outputs
also land in the project run tree); `brief` and `orchestrator_args` are
orchestrator-specific options.

For maker-facing loops that need one JSON-safe branch for both pre-admission
and runtime failures, use `invoke_result`. It preserves the typed exception
API of `invoke` while serializing a validation/precondition failure into the
same `InvocationResult` error mapping, without admitting a run:

```python
import astrid.sdk as sdk

result = sdk.invoke_result(
    "generation.generate_image",
    kind="executor",
    project="demo",
    inputs={
        "model": "not-a-real-model",
        "mode": "text-to-image",
        "execution": "cloud",
        "prompt": "a blue square",
    },
)
print(result.ok)                      # False
print(result.error["sdk_category"])  # validation
print(result.to_dict())               # JSON-safe structured result
```

`astrid.sdk.invoke` and the typed `astrid.generate.*` facades intentionally keep
raising `AstridSDKError` subclasses for code that wants precise recovery
branches. `invoke_result` is the canonical agent-loop boundary when catching
and serializing those typed failures is preferable.

When a caller already has a task/run locator from a trusted journal, use
`observe_task_invocation` to read Runtime state through the same normalized
result path. It is read-only: it does not admit, retry, or settle work, and a
caller-supplied locator is not proof that an admission occurred. The SDK checks
the returned task/run identity and can optionally read settled managed-output
rows:

```python
from astrid.sdk import observe_task_invocation

result = observe_task_invocation(
    client,
    capability_id="vibecomfy.run",
    capability_type="executor",
    native_kind="executor",
    task_id="task-from-journal",
    run_id="run-from-journal",
    wait=False,
    read_managed_outputs=True,
)
```

Use `wait=True` to follow the known task to a terminal state. An unavailable or
mismatched Runtime identity remains an unsuccessful result; observation never
substitutes for exact admission recovery. With `wait=False`, `ok` means the
Runtime task read succeeded, so inspect the returned task state before treating
the work as complete.

### Exact admission recovery

Use `recovery_path` when one invocation must survive a lost response or process
restart. It is opt-in; calls without it keep their normal behavior. Keep the
same receipt path and request for a resume:

```python
from pathlib import Path

receipt = Path("operation/04-run.recovery.json")
result = client.invoke_result(
    "vibecomfy.run",
    kind="executor",
    project="demo",
    inputs=run_inputs,
    execution_request=execution_request,
    recovery_path=receipt,
    wait=True,
)

# After a restart, reconnect with current Runtime credentials and repeat the
# same invocation arguments with the same receipt path.
result = client.invoke_result(
    "vibecomfy.run",
    kind="executor",
    project="demo",
    inputs=run_inputs,
    execution_request=execution_request,
    recovery_path=receipt,
    resume=True,
    wait=True,
)
```

The receipt freezes the prepared admission and its idempotency key before the
first Runtime request. If the response or saved task locator is lost, resume
reads the exact known task or replays those frozen admission arguments under
that same key. It does not prepare a different task, make a new key, or retry
executor work. A timeout or temporary task/output read failure is returned to
the caller; retry observation explicitly with `resume=True` after the Runtime
is reachable. Runtime checks current authorization for replay and readback, so
a receipt does not preserve revoked access. A new intended execution uses a
new receipt path and a fresh invocation; an existing receipt path is not a
fresh-operation slot.

Recovery owns Runtime task admission and observation only. It does not claim a
machine or prepare its software. Complete any machine/target preparation first
and freeze that execution request into the invocation; recovery reuses the
recorded request and does not silently select or replace a target.

For generation tasks, authorized task reads include the immutable admitted
`expected_effect` alongside `generation_intent`. Downstream code can compare
that declaration with Runtime's applied publication and managed-output rows;
reading it grants no mutation authority.

### Typed Error Handling

All SDK errors inherit from `AstridSDKError`. Catch the base class for general
handling or specific subclasses for precise recovery.

```python
import astrid

# Not-found
try:
    astrid.get_capability("missing.capability", kind="executor")
except astrid.CapabilityNotFoundError as e:
    print(f"Not found: {e}")

# Ambiguous bare lookup
try:
    astrid.get_capability("fade", kind="element")
except astrid.CapabilityAmbiguousError as e:
    print(f"Multiple matches: {e}")
    # e carries candidate list in the message

# Element invocation is rejected
try:
    astrid.invoke("effects/text-card", kind="element")
except astrid.UnsupportedCapabilityError as e:
    print(f"Cannot invoke element: {e}")

# Executor runs require a project (validation happens before any side effect)
try:
    astrid.invoke("iteration.experiment_review", kind="executor")
except astrid.CapabilityValidationError as e:
    print(f"Project required: {e}")

# Runner exceptions are preserved as __cause__
try:
    astrid.invoke("some.executor", kind="executor", out="/tmp/out", project="demo")
except astrid.CapabilityInvocationError as e:
    original = e.__cause__  # The exception raised by the runner
```

### Event Observation

Events are read through the canonical runtime operations on an explicitly
connected client: `client.runs.events(run_id)` or
`client.tasks.events(task_id)`. The SDK does not expose local-file readers,
checkout fallbacks, or subscription aliases. The returned runtime resources
remain unchanged; callers choose their own polling policy.

Each event resource contains:

| Field | Type | Description |
|---|---|---|
| `source` | `str` | Runtime event stream source (for example `task`) |
| `line` | `int` | One-indexed line number in the event log |
| `timestamp` | `str \| None` | ISO-8601 timestamp from the event |
| `kind` | `str \| None` | Event kind (`"run_started"`, `"step_dispatched"`, `"run_completed"`, etc.) |
| `hash` | `str \| None` | SHA-256 hash for chain verification |
| `payload` | `dict[str, Any]` | Runtime event payload and integrity metadata |

Integrity, ordering, and retention are runtime responsibilities.

## DTO Reference

See [platform-contract.md](../contracts/platform-contract.md).

### `manifest_path` Fallback Rules

`manifest_path` is an additive optional pointer to a universal
`manifest.json` emitted by the invoked capability. When present it is an
absolute path or a runtime object reference. Callers must not infer a logical
pack root from an artifact path. The SDK derives it from the normalized runtime
payload when the payload exposes a universal manifest pointer; attempt-local
manifests are valid only for that invocation.

The discovery follows a two-step fallback:

1. **Payload preference**: If the executor's stdout `payload` includes a
   `manifest_path` or `manifest` key pointing to a file named `manifest.json`,
   that path is used directly.
2. **Attempt output fallback**: Otherwise, the SDK may return an
   attempt-workspace `manifest.json` when the invocation explicitly exposes
   that local artifact.

When neither source yields a `manifest.json` file, `manifest_path` is `None`.

The invocation result exposes the normalized manifest fields documented in the
executor and run-ledger contracts.

```python
import astrid

result = astrid.invoke(
    "iteration.experiment_review",
    kind="executor",
    project="demo",
    inputs={"review": "experiments/prompt-brevity/review.json"},
)
if result.manifest_path:
    import json
    with open(result.manifest_path) as fh:
        manifest = json.load(fh)
    print(f"kind={manifest['kind']}, outputs={len(manifest['outputs'])}")
```

## Rendering SDK

The public rendering tooling (`astrid.support`, `astrid.renderer_main`, and
`astrid.RenderContext`) wraps the frozen protocol-v1 backend boundary described in
[render-backend-v1.md](../contracts/render-backend-v1.md). Every JSON payload
it writes is the `to_dict()` of a frozen core DTO — there are no SDK-only wire
fields and no semantics drift from the raw command/JSON backend path.

Product rendering is admitted only through the workspace runtime and its
generic host. Use the stable capability surface; it resolves the canonical
timeline, materializes runtime-owned objects into the attempt, executes the
renderer in a child process, and settles the run:

```python
import astrid.sdk as sdk

result = sdk.invoke(
    "rendering.render",
    kind="executor",
    project="demo",
    inputs={"timeline_ref": "main", "expected_version": 4},
)
print(result.run_id)
```

`timeline_ref` is a canonical runtime timeline slug, UUID, or ULID. Use
`expected_version` when the observed stream head must remain unchanged. The
runtime owns output publication and provenance; callers must not invoke
the rendering service, a pack `run.py`, or a direct-render convenience symbol.
No direct product-render symbol is exposed by the SDK.

### `support(backend, ...) -> SupportReport`

Resolves the qualified backend and returns its request-sensitive
`SupportReport` — exactly what the public backend path produces. Pass a frozen
`RenderRequest`/wire mapping or friendly path/audio/profile arguments:

```python
import astrid

report = astrid.support("acme_wave.wave", timeline_path="out/hype.timeline.json")
print(report.supported, report.features, report.alternatives)
```

### `renderer_main([...]) -> int`

The protocol-v1 command entrypoint, mirroring the raw-command backend's file
protocol exactly — this is what a manifest `command` can point at instead of a
hand-written `render.py`:

```
python -m astrid.sdk.rendering render|support --request <abs.json> --result <abs.json>
```

It reads the request JSON, dispatches through the rendering registries and the
command transport, validates the result with the core artifact validator, and
writes the frozen result shape to `--result`. Failures are written as the
frozen `RendererError` JSON shape (exit 0); `KeyboardInterrupt` and
`SystemExit` are re-raised. The selected backend is resolved from
`ASTRID_RENDER_BACKEND` (set by the transport when this runs as a manifest
command), then from the request's `backend_config` namespace — exactly one,
never from timeline shape.

### `replay` — re-run a captured failure bundle

The internal rendering CLI verb
`python3 -m astrid.core.rendering.cli replay <bundle-dir>` re-runs a captured
replay bundle's pinned command with its localized `request.json` and `inputs/`
copies in a fresh temporary workspace. It verifies the pinned
`request_digest` against the on-disk localized request, refuses manifest or
localized-input drift unless `--acknowledge-drift` is passed, refuses an
unresolvable pinned renderer, and persists the reproduced output plus its
provenance sidecar under `<bundle-dir>.replay-output/`. Bundles are produced
by `RenderService` on any failed `render`/`finalize`/`plan`/`support`
invocation (and on success when capture is explicitly enabled); they carry
the localized request, hashed inputs, redacted logs and partial result,
`support_report`, `backend_config`, source-pack/trust identity, and the
pinned digests. See the worked example in
[render-backend-v1.md](../contracts/render-backend-v1.md#the-replay-verb).

The rendering DTO and `RenderContext` below remain the pack-authoring and
attempt-local artifact contract. They are not a second live execution
authority; product renders are admitted and recorded by the workspace runtime.

### `RenderContext`

`RenderContext` is the per-invocation facade a third-party `render.py` author
gets for the duration of one render: workspace-validated path allocation
(`output_path`, `workspace_path`, `temp_dir`), asset descriptor resolution
(`asset_path`, `asset_url`, `resolved_registry`), a sanitized subprocess
runner (`run` with scrubbed env, bounded redacted output, hard timeout,
process-group teardown), redacted logs (`log`, `progress`), a cooperative
interruption flag (`interrupt_requested`, `raise_if_interrupted`), media
probing and hashing (`probe_media`, `sha256`), audio completion
(`complete_audio`), and named attachments (`add_attachment`, `attachments`).
It is not an OS sandbox — it enforces workspace conventions, not process
isolation — and it cleans up its temp artifacts on exit.

```python
from astrid import RenderContext
from astrid.core.rendering.contracts import RenderRequest, RenderResult

def render(workspace, request: RenderRequest) -> RenderResult:
    with RenderContext(workspace, backend="acme_wave.wave") as ctx:
        ctx.log("rendering %s" % request.output_name)
        out = ctx.output_path(request.output_name)          # workspace/outputs/<name>
        ctx.run(["vendor-tool", "--input", ctx.asset_path("hero"), "--out", out])
        media = out.read_bytes()
        ctx.add_attachment("project", media, kind="project")
        return RenderResult(
            schema_version=1,
            video={"path": "outputs/%s" % request.output_name,
                   "profile": _probe(ctx, out),
                   "sha256": ctx.sha256(out),
                   "duration_frames": 240,
                   "audio": "rendered",
                   "attachments": {a.name: a for a in ctx.attachments.values()}},
            backend_fragments={"acme_wave.wave": {"vendor_version": "7.2"}},
            audio_ownership="rendered",
            normalization=[],
            logs=list(ctx.logs),
            metadata={},
        )
```

## Stability Tiers

See [platform-contract.md](../contracts/platform-contract.md).

## Exception Hierarchy

```
RuntimeError
 └── AstridSDKError
      ├── CapabilityNotFoundError
      ├── CapabilityAmbiguousError
      ├── UnsupportedCapabilityError
      ├── CapabilityInvocationError
      ├── CapabilityValidationError
      │    └── CapabilityMissingInputError
      ├── CapabilityPreconditionError
      ├── CapabilityRuntimeError
      ├── CapabilityLeaseError
      └── CapabilityEventLogError
```

All 11 SDK exceptions are public and importable from `astrid`.
`CapabilityMissingInputError` is a subclass of `CapabilityValidationError` and
carries the names of missing required inputs. `CapabilityInvocationError`
preserves the original runner exception as `__cause__`.
`CapabilityEventLogError` is raised by a runtime event read when the hash chain is
broken; `CapabilityPreconditionError` is raised when a project slug or
prerequisite check fails. `CapabilityLeaseError` is raised when the writer
lease cannot be acquired. `CapabilityRuntimeError` signals an unexpected
failure during SDK operations.

## Lazy Loading

`import astrid` does not eagerly load internal registry or runner modules.
The SDK module (`astrid.sdk`) is loaded on first attribute access. Registry
and runner imports happen inside the `discover()`, `get_capability()`, and
`invoke()` call sites. This keeps the import boundary lightweight and allows
tooling to inspect the public surface without pulling in execution machinery.

## Element Source Behavior

Element discovery reads canonical pack manifests. A runtime theme is visual
data and cannot shadow or replace pack element code; edit the owning source
pack when changing an element. Returned capability DTOs therefore carry the
manifest-backed identity, source, and digest of the discovered element.

Pass `include_missing_roots=True` to include element roots that are declared
but not yet present in the checkout. This is useful for pack authoring and
local validation workflows
where the operator needs to see what elements are available before they are
locally fetched.
