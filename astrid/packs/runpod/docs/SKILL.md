---
name: runpod
description: >
  Set up, claim, inspect, keep and release RunPod generation machines with
  paid consent and bounded retention. Also provision GPU pods, execute
  scripts remotely, pull artifacts and tear down through RunPod actions.
---

# RunPod

The runpod pack manages ephemeral cloud GPU compute on RunPod through
`runpod-lifecycle>=0.3`: provision pods, ship and run scripts, pull artifacts,
and tear down with a composite session that guarantees cleanup.

## RunPod generation machines

Use `astrid machines` for the retained RunPod machine that ordinary generation
uses. A claim and a later compatible generation share the same pod, custody
journal and paid limit. The older `runpod.provision` handle belongs to the
individual actions below; it is not a retained generation machine.

Generation path: approve paid limits with `astrid machines claim`, then run
`astrid generate <image|video|audio> --entry ... --placement runpod` on that
claimed machine. `runpod.provision`, `runpod.session`,
`scripts/pod_session.py` and `scripts/claim_runpod_5090_backup.py` are operator
tools and are not for generation.

### Set up and inspect preferences

Run these from the installed Astrid launcher when the user asks to configure
RunPod. The credential command prompts for the key; never put key bytes in a
command, settings, output or error message.

```sh
astrid credential set runpod
astrid machines setup
astrid machines setup --show
```

Setup lists existing account network volumes. Choose a volume, or press Enter
for ephemeral disk. A volume fixes the datacenter. Without one, choose a
datacenter or leave it at `any`. Choose a nonempty ordered GPU list and a
maximum budget per paid run. Setup saves preferences; it neither creates a
volume nor claims a pod, and saving a budget grants no permission to spend.
Missing preferences refuse with `astrid machines setup`; setup never starts
automatically.

For unattended setup, supply the choices and `--json`:

```sh
astrid machines setup --volume my-volume --gpu "RTX 4090" --gpu "RTX 5090" --budget-usd 5 --json
```

On a fresh setup, omitting `--volume` uses ephemeral disk. When updating with
`--json`, omitted volume, GPU and budget choices keep their saved values.
Use interactive setup to choose ephemeral disk after a saved volume. An
optional `--datacenter ID` selects a datacenter; it must match a chosen volume.

Setup derives account identity from the saved credential reference. The key
is resolved in memory only to authenticate read-only setup lookups; it is
never stored in preferences, shown, logged or used as identity. A missing
image uses the validated default
`runpod/base@sha256:820c605eaaa276de20a0d77993fb0846d54e180efff5de67d46341ed399b5a7f`.
A saved image is kept; `--image name@sha256:DIGEST` explicitly replaces it.
`setup --show` prints sanitized preferences and their Runtime data root and
realm. The authoritative settings are `machines.runpod` in the Runtime's
per-user `generation` app-state document, not a project config file.
The default shape is `"models": {"trust_unchanged": true}`; the controller carries this choice into each pod job.
Opt in to full per-job re-verification with `"models": {"trust_unchanged": false}` in that same document (`python3 -m astrid generate settings checkout --file "$WORK/generation.json"`, edit, then `python3 -m astrid generate settings checkin "$WORK/generation.json"`).
The default skips re-checking model files that haven't changed since they were verified: faster warm jobs; trusts file metadata, not a fresh checksum.
With it on, a file counts as unchanged when its size, timestamps and inode match the verified record; another process running as you could make a record match, and a network volume remounted on another pod may match by metadata alone. Set it to false to re-check every file on the next preparation.

To replace preferences while a machine is held, release that machine using
its original policy and confirm cleanup first. Setup refuses unresolved
custody. It uses the existing `runpod.setup` action; do not edit a journal or
restart Runtime to force a policy change.

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

### Choose a GPU and datacenter

The saved GPU list is tried in the user's order. Within each GPU type, the
cheapest allowed datacenter is tried first. After a definite capacity
rejection, Astrid tries the next allowed GPU/datacenter pair, then observes
stock again. With no volume or selected datacenter, every datacenter returned
by RunPod is eligible. There is no default geographic filter.

Repeated `--gpu TYPE` replaces the saved GPU list for one claim or quote.
`--datacenter ID` overrides its datacenter for that claim or quote. Neither changes
saved preferences, and a volume always stays in its own datacenter. A held
machine with a different GPU or storage policy must be released before a new
choice can be claimed; Astrid does not create a second pod around that refusal.

Review the target with the same ordered GPU and datacenter choices you will
use for the claim:

```sh
astrid generate describe --placement runpod --gpu "RTX 4090" --gpu "RTX 5090" --datacenter US-TX-3 --max-hourly-usd 1 --json
```

The hourly figure is an example ceiling, not a current price quote. Ask the
user to approve the actual disclosed GPU choices, storage, pinned image,
hourly ceiling, spend cap and idle retention before claiming a machine.

To change just the saved GPU list after setup, the existing command is:

```sh
astrid generate targets --placement runpod --save --input 'gpu_types=["RTX 4090"]'
```

### Consent and capacity waiting

Every machine claim needs fresh paid consent. Validate the entry or workflow
and its inputs before claim. The requested cap must fit the saved budget
ceiling. `--max-hourly-usd` approves the chosen GPU list and saved storage
below that ceiling, with `--max-usd` bounding the paid lifetime. Description
can disclose this policy even when no matching GPU is in stock. An optional
`--deadline` on claim adds a hard absolute UTC limit and must cover the
disclosed idle retention.

For example, after approval of these illustrative limits:

```sh
astrid machines claim --gpu "RTX 4090" --gpu "RTX 5090" --datacenter US-TX-3 --max-hourly-usd 1 --max-usd 3 --deadline FUTURE_UTC_DEADLINE --for 30m --consent paid
astrid generate image "A studio portrait" --placement runpod
```

Set FUTURE_UTC_DEADLINE to the current UTC time plus the session you want (for example, 1 hour), in the form 2026-01-01T13:00:00Z.

The default is to wait until a machine is claimed or the operation is
cancelled. Each attempt reports GPU, datacenter and result. Ordinary retries
are about five seconds apart; rate limits back off. Use `--capacity-timeout
10m` to bound the wait, or `--no-wait-for-capacity` for one observation and at
most one create attempt. `generate --no-wait` returns an admitted generation task to follow
separately; generation requires the claim to have succeeded first.

Capacity polling creates no new pod charges. A retained pod still costs
money, and an ambiguous create may already have allocated a pod. The paid
clock starts immediately before the create request that may succeed and
includes preparation, execution and idle time; it is not restarted when the
machine becomes ready. An uncertain create is reconciled, never retried as a
second create. If a timeout or cancellation leaves the result unknown, check
`astrid machines` and the original task before retrying. A host restart does
not resume a capacity wait.

The paid bound includes preparation and idle as well as execution; reuse
cannot extend the existing bound. A denied reuse must not postpone an
existing idle deadline. Unknown rates, insufficient limits, changed
preferences and replayed consent refuse the operation.

### Claim, inspect, keep and release

Claim reserves a machine without running a generation. It shows the policy
and asks for paid consent; `--consent paid` is the explicit unattended form.
For example, after approving the displayed terms:

```sh
astrid machines claim --gpu "RTX 4090" --gpu "RTX 5090" --max-hourly-usd 1 --max-usd 3 --deadline FUTURE_UTC_DEADLINE --for 30m --consent paid
astrid machines
astrid machines --json
```

Claim uses saved storage, with optional per-claim `--gpu` and `--datacenter`.
It supports the same `--capacity-timeout` and `--no-wait-for-capacity` controls.
Without an hourly ceiling it uses the current quote; no current quote means
it asks for `--max-hourly-usd`. `--for` is the initial idle retention after
claim, capped by the paid expiry. Without `--for`, it uses saved idle retention
or 30 minutes, disclosed before consent. The machine is idle once claimed; a
compatible generation can reuse it under the claim's existing authority and
expiry, without another consent or create.

A running host is required. A first claim may initialize its owner only when
custody is empty or confirmed ended. Existing or unresolved custody refuses
without releasing an old pod as a side effect. Follow the refusal's release
or reconciliation guidance before claiming again.

The machine table shows live and recently ended leases: ID, GPU, state,
scheduled release time in UTC, minutes left, estimated accrued USD cost,
hourly rate and end reason. The release time is the idle expiry while idle
and the paid expiry while active. Estimates are not a provider invoice.
A task's machine column records the placement kind, pod ID or `local`, lease ID
and device. A provider-terminated or missing pod leaves warm reuse; a job pinned to
that machine is refused with its reason and the live leases.
After a failed job, a verified machine stays idle until that release time; inspect its last job outcome and reuse it or release it explicitly.

To authorize a separate extension for that exact live machine:

```sh
astrid machines keep MACHINE_ID --for 2h --max-usd 3
astrid machines keep MACHINE_ID --until-release --max-usd 3
astrid machines release MACHINE_ID
```

Both keep forms require a fresh additional spend cap. `--for` keeps for up to
that duration, shortened by the cap. `--until-release` disables idle release
until explicit release or the cap's expiry; it is never uncapped. Keep needs
the live owner and refuses an ended machine, a different pod, a stale owner,
a release already in progress or replayed consent. If its result is unknown,
check `astrid machines` before sending another keep.

**Automatic release only happens while the owning Astrid host is running.**
If it stops or the computer sleeps, the timer cannot enforce the cap on time
and the pod can keep billing. Do not promise a provider-side spending cap.
Release explicitly before stopping the host when the machine is no longer
needed. Recovery must preserve the original custody and release receipt.
Release is complete only when the exact pod GET returns 404 and a successful
account list confirms its absence; unresolved cleanup blocks reuse. Releasing a pod preserves its network volume.

After a generation, list the machines and tell the user that a kept-warm machine
keeps costing money until it is released. Unless the user chooses to keep it
warm, release it by its ID with `astrid machines release MACHINE_ID`.

**What warm means here.** A job's models stay loaded on the claimed pod between
jobs until ComfyUI needs the room for a different model. Warmth ends when the
claim's idle time runs out and the pod is released: the next job on a new claim
loads its models again. Plan around the claim's idle time, not around the models
staying loaded.

### Runtime and storage

RunPod generation needs an installed Astrid layout. Pinned Runtime,
VibeComfy and other worker sources come from the installed release manifest
and are cached at `<data-root>/runpod/sources`; a developer checkout or ambient
`PYTHONPATH` is not a worker installation. The default placement remains
`auto`; saving RunPod preferences does not replace a configured human
ComfyUI target.

The lease journal owns only the exact pod Astrid created. Never adopt or
terminate another owner's pod, delete custody evidence to unblock a claim,
or create a replacement around uncertain cleanup. See the
[RunPod storage lifetimes](../../../../docs/packs/data-storage.md#runpod-machine-storage)
for settings, journals, model caches and temporary files.

A prepared generation is one Runtime task. Follow its original receipt and
binding after a disconnect instead of submitting again. Runtime output
settlement verifies published hashes and durable readback; a successful
remote process alone is not delivery. Preserve the existing local spool if
publication is unavailable, and resume the same receipt. Caller-local output
copies default to `./outputs/<task-id>/`, or the explicit `--out` destination.

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
