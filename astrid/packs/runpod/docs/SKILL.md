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

`--placement runpod` requires an installed layout. The Agent derives its RunPod source root
from the release manifest: it populates `<data-root>/runpod/sources` with clean checkouts at the
pinned commits (Runtime from `runtime.lock.json`, VibeComfy from the recipe pin, and the other
packages from the installed release manifest), then validates them. There is no developer-checkout
route and no environment variable to set; outside an installed layout the command fails with a
`ManagedSourcesError` (see M02-26).

`astrid generate image --placement runpod` uses the retained host adapter, its
single lease owner, and the existing attached ComfyUI transport. The selected
executor environment must contain the manifest-installed Agent, Runtime,
VibeComfy, RunPod SDK and lifecycle packages; a source checkout or ambient
`PYTHONPATH` does not install dependencies into a child interpreter. Use the
official installer's local-source manifest route to select these packages.
The default remains `--placement auto`; saving RunPod preferences does not replace
a configured human ComfyUI target.

### Choosing a GPU

For a single job, pass `--gpu <type>` to both `astrid generate describe --placement runpod`
and the matching `astrid generate ... --placement runpod` command. Repeat `--gpu`
to set an ordered fallback list. A per-job list replaces the saved GPU list for
that job. The disclosed offer digest includes the GPU list, so changing the list
between describe and generation (including omitting an override that differs
from saved preferences) changes the digest. Describe the target again and use
the same ordered list before giving paid consent.

To save the list as the default, use
`astrid generate targets --placement runpod --save --input 'gpu_types=["RTX 4090"]'`
or configure it with `runpod.setup`.

The prepared RunPod path requires a fresh, explicit paid authorization for
each invocation, including reuse. Before claiming, disclose the selected
account, GPU offer and hourly USD rate, pinned image, optional network volume and mount,
maximum USD spend, absolute UTC deadline, and estimated preparation, execution
and idle times. The offer and rate must still match the authorization; a
changed price or fallback offer needs new consent. Unknown rates, insufficient
cap or deadline, and replayed consent block the claim or reuse. The paid bound
includes preparation and idle as well as execution; reuse cannot extend the
existing bound. A denied reuse must not postpone an existing idle deadline.
`astrid machines keep` changes a live machine's paid limit with fresh consent; `--until-release` disables idle release within the required USD cap, and release requires the owning host to stay running.

Validate the entry or workflow and its inputs before claim. The journal owns
only the exact pod it created; it must not adopt or terminate another broker's
pod. A compatible retained worker can be reused after new consent and identity
checks. The generic path defaults to a 1800-second warm lease. Paid preparation,
execution and idle limits bound it. Expiry of the paid deadline fences even an
active hold.

The prepared task adapter admits one ordinary Runtime task, activates that
exact binding, and can return its receipt before completion. Same-host follow
must recover the original receipt, source and settings and observe that same
task and binding, without another claim or submission. Runtime's
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
never create a replacement to work around uncertain custody.

### Replacing preferences

`runpod.setup` is the supported reset of the retained in-process owner. Use a
Runtime-bound client, as the generation CLI does:

```python
import json
from astrid.sdk.client import AstridClient

# preferences is the complete validated policy; it contains credential refs,
# never credential bytes. Saving policy grants no permission to spend.
policy = {"kind": "runpod", "engine": "comfyui", "preferences": preferences}
with AstridClient.open_from_launcher() as client:
    result = client.invoke_result(
        "runpod.setup", kind="action", project=None,
        inputs={"placement_policy": json.dumps(policy, sort_keys=True)},
        wait=True, read_managed_outputs=False,
    )
assert result.ok, result.to_dict()
assert result.raw_result["payload"]["target_operation"]["state"] == "saved"
```

Writing preferences on disk does not replace the host's retained owner.
Status, listing and release reject a different policy and preserve the lease.
Release an existing lease using its original policy, verify cleanup, then call
setup with the replacement. Setup replaces an absent or cleanup-complete
owner only when no operation or invocation remains active; it does not quote,
allocate, or clear unresolved custody. A Runtime restart is unnecessary.

When checking Runtime down/up, pass the same canonical `--data-root` and
`--source-manifest` on both calls. Record discovery and owner PID, birth ID,
instance ID, workspace UUID/root and profile hash before down; verify the old
owner is absent before up. Require a healthy new instance at the same root.
`owner_busy` protects an existing owner; a bare `stopped` result from a
different root does not establish a lifecycle defect.

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
