---
name: runpod
description: >
  RunPod pack — provision GPU pods, execute scripts remotely, pull
  artifacts, and tear down, including a guaranteed-cleanup composite
  session. Requires a RunPod credential through Astrid's resolver.
---

# RunPod

The runpod pack manages ephemeral cloud GPU compute on RunPod through
`runpod-lifecycle>=0.3`: provision pods, ship and run scripts, pull artifacts,
and tear down with a composite session that guarantees cleanup.

## Ordinary generation on a RunPod target

The Phase 1 prepared-worker components support ComfyUI only. The current
checkout's `astrid generate` parser accepts `--target runpod`, but its public
preflight still rejects that target: automatic claim and the public follow and
release wiring are not yet available here. Do not present the selector as a
working paid-generation command until the A10 integration is installed and
verified. The default remains `--target auto`; saving RunPod preferences does
not replace a configured human ComfyUI target. The older `runpod.session`
action and H3's specialized placement are separate paths, not proof that the
ordinary generation selector is connected.

The prepared RunPod path requires a fresh, explicit paid authorization for
each invocation, including reuse. Before claiming, disclose the selected
account, GPU offer and hourly USD rate, pinned image, network volume and mount,
maximum USD spend, absolute UTC deadline, and estimated preparation, execution
and idle times. The offer and rate must still match the authorization; a
changed price or fallback offer needs new consent. Unknown rates, insufficient
cap or deadline, and replayed consent block the claim or reuse. The paid bound
includes preparation and idle as well as execution; reuse cannot extend the
existing bound. A denied reuse must not postpone an existing idle deadline.

Validate the entry or workflow and its inputs before claim. The journal owns
only the exact pod it created; it must not adopt or terminate another broker's
pod. A compatible retained worker can be reused after new consent and identity
checks. The idle policy is request override, then caller
`ASTRID_WARM_IDLE_SECONDS`, then service environment, then 600 seconds; zero is
valid. Expiry of the paid deadline fences even an active hold.

The prepared task adapter admits one ordinary Runtime task, activates that
exact binding, and can return its receipt before completion. Same-host follow
must recover the original receipt, source and settings and observe that same
task and binding, without another claim or submission. The installed public
follow bridge remains an integration dependency in this checkout. Runtime's
existing output settlement owns artifacts: verify the published output hashes
and durable readback; a successful remote process alone is not delivery. Keep
the existing spool when publication is unavailable, and resume the same
receipt instead of replaying an uncertain submission.

Idle expiry and explicit release must go through the Runtime release task and
drain or fence the exact task, stop the owned worker and revoke its activation
before terminating the journal-owned pod. Release is complete only when an
independent exact pod GET returns 404 **and** a successful account list shows
the pod absent; preserve the network volume. Ambiguous provider observations,
unresolved cancellation, or missing stop evidence remain `cleanup_pending` and
block reuse. Recover with the same host, journal, task and release receipt;
never create a replacement to work around uncertain custody. The shared
release-task dispatcher is also an A10/F07 integration dependency here.

## Actions

- **`runpod.provision`** — Launch a GPU pod and emit `pod_handle.json`.
  It does not terminate; pair it with `runpod.teardown`. The handle is
  durable and secret-safe: it stores `api_key_ref: RUNPOD_API_KEY`, never the
  API key value.
- **`runpod.exec`** — Reattach to a provisioned pod, ship code, execute a
  script, and collect the fixed artifact directory emitted by
  `runpod-lifecycle` v0.3. It leaves the pod alive and does not expose
  caller-selected artifact paths or pass unsupported detached-run kwargs such
  as `artifact_paths`, `guard_factory`, `poll_command_template`, or
  `poll_exit_marker`.
- **`runpod.pull`** — Reattach using an existing `pod_handle.json` and pull
  remote artifacts into local storage through SSH/SCP. It is the compatibility
  remote-copy action for callers such as `training.training_run`, distinct
  from detached execution artifact collection in `exec` and `session`.
- **`runpod.teardown`** — Terminate a pod by handle. It is idempotent.
- **`runpod.session`** — Composite provision → exec → teardown with
  `try/finally` guaranteed cleanup. It writes the same `pod_handle.json` shape
  as `provision` immediately after launch as a sweeper breadcrumb, then removes
  the transient handle only after successful or idempotent teardown.

## Storage contract

`storage_name` is optional for `provision` and `session`; storage-free modes
run without it. When a caller passes `--storage-name`, or marks the path as
storage-required with `--require-storage` / `RUNPOD_REQUIRE_STORAGE`, Astrid
checks that the named RunPod network volume already exists before provisioning.
The action never creates a volume implicitly. If storage is required but not
configured or the named volume is missing, create the named volume first with
the storage helper (`ensure_storage` in
`astrid/core/integrations/runpod/storage.py`), then retry with
`--storage-name <storage-name>`.

## Credentials

Local Astrid commands resolve the configured credential reference from the
shared per-user `~/.astrid/astrid.env` file, overridden by an explicit
operation value and then process environment. Compute profiles store only the
reference. CI, containers, and deployed services should inject
`RUNPOD_API_KEY` through their own secret manager. See
[RunPod credentials](../../../../docs/reference/runpod-credentials.md) for the
local setup and replacement path.

## Artifacts and cost

`runpod-lifecycle` v0.3 detached execution has fixed artifact behavior:
`exec` and `session` mirror the returned artifact root into
`produces/artifact_dir` when one is present and otherwise create an empty
artifact directory. They do not accept caller-specified artifact paths.

Successful `provision`, `exec`, `teardown`, and `session` paths write
`cost.json` with `amount`, `currency`, and `source`; Astrid also includes a
local diagnostic `basis` string for auditability.

## Remote-artifact smoke

The task adapter stays provider-neutral. A RunPod smoke step should use the
generic `remote-artifact` subprocess-plus-manifest contract and put the RunPod
action behind the call:

```python
import astrid.sdk as sdk
result = sdk.invoke(
    "runpod.session",
        kind="executor", project="demo",
    inputs={
        "gpu_type": "NVIDIA_L40S",
        "local_root": ".",
        "remote_root": "/workspace",
        "remote_script": "smoke.sh",
    },
)
```

Expected manifest/fetch shape:

```json
{
  "result.txt": {
    "path": "result.txt",
    "source": "/local/pulled/result.txt",
    "sha256": "<sha256>",
    "provider": "runpod"
  }
}
```

`path` is relative to the canonical task produces directory, `source` is a
local fetched file, and `sha256` is verified before the task can complete.
The fetch state records `fetched`, `missing`, `mismatched`, and computed
`checksums`; retries are idempotent. If a smoke keeps a pod alive and uses
`runpod.pull`, repeated `remote_path` values must be supplied as separate
`remote_path` entries in the `inputs` dict so the downstream command emits
ordered repeated `--remote-path` flags.

Live RunPod validation is opt-in only. Run it only when `RUNPOD_API_KEY` and
the required RunPod environment are present and spend is approved, for example:

```bash
ASTRID_LIVE_RUNPOD_SMOKE=1 RUNPOD_API_KEY=... \
python3 -m pytest tests/packs/runpod/test_manifest_contract.py
```

Without those variables, CI runs mocked checks only: command rendering,
manifest checksum/fetch behavior, repeated pull paths, and cleanup contract
documentation are verified without contacting RunPod.

## When to use

- Use `runpod.session` for one-shot GPU jobs that should always clean up.
- Use individual actions when you need manual control over the pod lifecycle.

## When NOT to use

- Do not use for orchestrating LoRA training workflows end to end — use the
  `training` pack, which drives RunPod under the hood.

## Quick-start

For a prepared existing pod, the standalone `runpod-lifecycle run POD_ID
--script /path/to/job/run.sh --keep-pod` is the practical upload/execute/fetch
path. It does not create Astrid task history. See the
[operator guide](../../../../guides/spinning-up-and-executing-tasks-on-runpod.md)
for verified behavior, storage checks, timeout limits, and current native
integration prerequisites.

For native invocation, use a connected client and the manifest's exact input
names. `runpod.exec` accepts `pod_handle` and `remote_script`, not `pod_id` and
`script`. The claim waiter's smaller handle is not a provision handle.

```python
from astrid.sdk import AstridClient

with AstridClient.open_from_launcher() as client:
    result = client.invoke_result(
        "runpod.exec",
        kind="executor",
        project="<selected project>",
        inputs={
            "pod_handle": "/path/to/astrid-provision-pod_handle.json",
            "local_root": "/path/to/job",
            "remote_script": "/path/to/job/run.sh",
            "remote_root": "/workspace/unique-job-id",
            "timeout": 900,
            "upload_mode": "sftp_walk",
        },
        wait=True,
    )
    if not result.ok:
        raise RuntimeError(result.error)
```

Verify managed output settlement; successful remote exit alone does not prove
artifact delivery. `runpod.exec` leaves the pod alive; `runpod.session`
terminates it. Keep-running timeout currently stops local polling, not remote
inference, so bound the owned workload separately. Do not run concurrent
detached lifecycle jobs on one pod until their shared temporary paths are fixed.

## Requirements

`runpod-lifecycle>=0.3.1.dev0,<0.4` is an optional dependency gated behind the
project's `[runpod]` extra. This extra is for developers and operators
assembling a custom RunPod-capable worker environment from source or release
artifacts; do not add it to Astrid's managed installed environment. The
optional dependency is available through the configured private index or
local wheelhouse described in `pyproject.toml`. The shared dependency resource
pins this range because Astrid targets the v0.3 `ship_and_run_detached`
signature and artifact contract. Pack discovery and offline validation remain
usable without resolving the optional dependency; action execution requires
it. For normal Astrid setup, follow the OS-specific installer instructions in
the [packaged Astrid skill](../../_core/docs/SKILL.md), then use
[astrid.haus](https://astrid.haus). The installer does not provision this
private dependency, RunPod credentials, or provider-side resources.
