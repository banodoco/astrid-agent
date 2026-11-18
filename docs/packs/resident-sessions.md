# Resident Python actions

A v3 action may opt into a host-owned Python worker that stays alive between
jobs. This is the serial `astrid.pack-session.v1` contract introduced by
Plan 55 LS-8. An action without the declaration keeps its process-per-job route.

```yaml
schema_version: 3
id: example
name: Example
version: 1.0.0
pack_api: "2.0"
actions:
  run:
    description: Run with a retained Python interpreter.
    invocation:
      kind: python
      path: actions/run.py
      function: run
    session:
      kind: resident
      protocol: astrid.pack-session.v1
    inputs:
      - name: value
        type: string
        required: true
    outputs: {}
    metadata:
      adapter_family: cpu
      resource_keys: [cpu]
      project_scope: optional
```

The action keeps the ordinary Python call signature. Its return value and
declared output files go through ordinary validation, harvesting and Runtime
settlement. Use the public SDK to obtain the current job's context:

```python
from astrid.sdk import current_pack_job

def run(value):
    job = current_pack_job()
    return {"value": value, "attempt": job.attempt_id}
```

`PackJobContext` provides `task_id`, `attempt_id`, `fence`, `runtime_epoch`,
`attempt_root`, `output_root`, and a `cancelled()` callback. It exposes no Runtime
client or publication credential. `current_pack_job()` raises `PackSessionError`
outside the executing context or after the job finishes. A copied old context
stays expired when a new job begins. New threads have no job context unless the
pack explicitly propagates it, and propagation cannot extend its lifetime.

## Retained state and authority

Only retain state appropriate to the admitted source, installation, effective
environment and authority scope, such as immutable catalogue facts or prepared
templates. Keep inputs, attempt paths, cancellation, output names and receipt
state per job. The host restores its worker environment and working directory
between jobs; packs must still obey the contract and stop job-owned background
work before returning. Residency is not an OS sandbox for arbitrary Python.

One `ManagedToolSession` owns the worker and its admission tokens. Dispatch is
serial, including when callers arrive concurrently. `WarmHolds` controls idle
release, and native task-scope custody controls process termination. No separate
task queue, Runtime client or persistent-supervisor instance runs in the helper.

The worker runs from an authenticated, private copy of the complete selected
pack map, with the manifest-scoped child environment. The capture stays owned
until native release is proved. Source, dependency, installation, Runtime epoch,
project/realm, authority context or effective environment changes prevent reuse.
An observed source/install change retires the old worker; changed Python is never
hot-reloaded into it. Development trees receive full content checks at dispatch
and during execution. This implementation makes no watcher-only or constant-time
fast-lane claim for mutable trees.

Installation identity includes every selected pack's root and validated `PIN`,
which is deliberately excluded from the pack content digest. Reinstalling the
same bytes with another PIN therefore retires the old interpreter. Captured and
current PINs must agree before launch. The parent also compares the current
manifest-scoped credentials and Runtime authority at dispatch, while executing,
and immediately before consuming the successful admission token. A detected
rotation or revocation fences the old job instead of accepting its result.

Every protocol frame is limited to 1 MiB and names the managed token and Runtime attempt/fence.
The parent checks the returned identity again before settling. A crash, malformed
frame, stale fence, or uncertain result is never automatically replayed. A failed
release leaves the existing custody slot poisoned and occupied. Cancellation
sends the exact current token, then proves termination of the helper's owned
scope before reporting stop; it does not control an attached external server.
Active host shutdown lets the current attempt finish this owned cancellation
and cleanup before closing the session manager. A successful condition skip
retains its explicit skip receipt; an incomplete success frame is refused.

## Initial limits

This version supports admitted Python actions without delegated Runtime
authority, provider brokers, render-host leases or remote placement. Unsupported
routes retain their existing disposable execution path. A declaration grants no
additional permission. Direct Python networking uses the existing network hook,
bound to each live job; idle and expired contexts cannot borrow a later job's
routes. Existing native-network limitations still apply.

An admitted route override that differs from the manifest policy uses the
disposable route until LS-15. The resident worker does not reconstruct that
dynamic authority from the static manifest.

[Follow-up #896](https://github.com/banodoco/astrid-dev/issues/896) tracks the
remaining lifetime work: cancellation during a stalled pipe
write and Runtime status-poll cadence, durable redacted log tails across worker
death, and owner-based recovery of captures/session directories after host
death. Those follow-ups do not change this protocol or the current custody
owner.

The VibeComfy integration and removal of its process-global job state belong to
LS-15. This core contract does not enable concurrent execution or implement
ComfyUI model, schema, transport or queue behavior.
