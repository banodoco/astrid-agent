# RunPod lifecycle: launch, run, deliver, terminate

This is the operator checklist for an Astrid/VibeComfy GPU job. It is
deliberately conservative: a pod is a paid, mutable machine. Do not start one
until the authority, budget, storage, and ownership checks below are true.

For the practical existing-pod upload/execute/fetch/keep-running path, start
with [Spinning up and executing tasks on RunPod](spinning-up-and-executing-tasks-on-runpod.md).
That lifecycle path is live-verified; automatic scheduler-backed remote
`vibecomfy.run` placement is a separate, incomplete integration. The H3 gates
below apply to H3 generation, not to a small transport-only smoke test.

The continuation-specific execution sequence is documented in the linked
guide's [continuation-guide test](spinning-up-and-executing-tasks-on-runpod.md#3a-run-the-continuation-guide-test).
It deliberately separates the one-step capacity smoke from the creative
175-frame starter plus 260-frame native-context continuation. The smoke can
prove that the graph reaches sampling; only the subsequent raw-output and seam
review can prove that the continuation is usable.

## H3 fail-closed preflight (before creative downloads)

For the pinned MiniMax H3 chain, treat the canonical CUDA 13 stack as a
compatibility contract: the pinned Torch CUDA build, `comfy-kitchen` revision,
ComfyUI backend, and the provider host driver must agree. A container image tag
only describes user-space libraries; it does not upgrade the host driver.

Before downloading the full model/creative set, require all of these gates:

1. **Constrain placement at the provider.** Use RunPod GraphQL
   `podFindAndDeployOnDemand` with the actual `allowedCudaVersions` (or the
   schema's `minCudaVersion`) required by the pinned CUDA 13 stack. Verify the
   returned machine, not just the requested image tag. The current inspected
   lifecycle checkout supports `--allowed-cuda-versions` and the Python
   `allowed_cuda_versions` field; verify the installed version with CLI help.
   Older 18 September examples predate that support. See the
   [official placement filter](https://docs.runpod.io/sdks/graphql/manage-pods#filter-by-cuda-version)
   and the [dated compatibility evidence](astrid-intro-regeneration-20260918.md).
2. **Verify the live stack.** Record `nvidia-smi` driver/CUDA capability,
   `torch.version.cuda`, the pinned `comfy-kitchen` version, ComfyUI revision,
   and the active attention/VAE backend. Run the canonical backend probe and
   confirm the CUDA 13 CK path is actually selected; an import or successful
   sampler alone is insufficient. Any mismatch or unplanned fallback is a hard
   stop before creative generation.
3. **Run one exact capacity diagnostic.** With the production graph and model,
   queue exactly one **1920x1088, 260-native-frame, one-step** diagnostic and
   decode its output. This is a memory/backend/capacity gate, not visual
   acceptance. Do it before admitting the creative chain; the longest clip is
   the required capacity test, while each real segment still needs its own raw
   visual and seam review.
4. **Prove native continuation wiring.** Retain the graph's **39 native
   context frames** from the raw latent edge. Do not decode an MP4 and
   re-encode it as continuation context. Record the context edge and first
   unprotected frames in the run evidence.
5. **Classify failures correctly.** A CUDA/driver/backend mismatch means stop,
   preserve evidence, and reprovision on a compatible host; do not enter a
   math-patch spiral. A later OOM on a matched stack is a separate memory
   capacity problem: use supported memory controls and a bounded retry, without
   claiming CUDA mismatch caused every OOM. Passing the 260-frame diagnostic
   still does not approve any creative take.

### Use a validated H3 golden image, not the provider Torch default

The current RunPod `runpod-torch-v240` template/default is a Torch 2.4,
CUDA 12.4 environment. It does **not** satisfy the canonical H3 contract of
Torch 2.10 with CUDA 13.0, the pinned ComfyUI revision, `comfy-kitchen` and
`comfy-aimdo` versions, exact H3/VHS custom-node commits, and the matching
`custom_nodes.lock`. A CUDA-13-capable host does not make that older userspace
runtime compatible.

Maintain a verified golden image/template containing that pinned runtime,
custom nodes, and lock. It may also contain model-verification receipts when
the corresponding model bytes and receipt fingerprints are part of the same
controlled artifact. The 18 September fresh-machine path otherwise had to
download the large PyTorch CUDA wheel set serially before downloading models;
that paid bootstrap was slow and repeated work already proven on the prior
machine.

A golden artifact is an acceleration mechanism, not self-authenticating proof.
Before creative admission, verify its image identity, runtime and node pins,
lock hash, model hashes or valid receipts, live CUDA/CK backend, and the
five-frame smoke. Then run the production-shape capacity diagnostic above.
If any check differs, fail closed and rebuild/promote a new golden artifact;
do not silently mutate the validated image in place.

The existing manually prepared H3 candidate uses this placement contract:

```text
image:              runpod/base:1.0.3-dev-fix-pytorch-version-verification-cuda1300-ubuntu2404
allowed host CUDA:  13.0
release root:       /workspace/h3-golden/releases/h3-cu130-v1-candidate
python:             runtime/venv/bin/python
Comfy entrypoint:   runtime/launch-comfy.sh
```

New builds use the content-derived root reported by `build-h3-release` below;
they do not overwrite that existing candidate. The image, host-CUDA constraint,
and mounted release are one contract. A pod
that merely has an RTX 5090 or a mounted `/workspace` is not compatible unless
the direct release-venv CUDA preflight passes.

### Build an H3 release on an existing network volume

Before creating a volume on a fresh account, use the optional lifecycle
availability helper to check the GPU and data center without allocating anything:
`runpod-lifecycle availability --gpu "RTX 5090" --min-cuda-version 13.0`.
This requires a lifecycle checkout with the `availability` command. Python callers
can use `await astrid.core.integrations.runpod.storage.storage_availability(
"RTX 5090", min_cuda_version="13.0")`; it uses Astrid's credential resolver and
returns the same JSON-shaped evidence. Require `status: "available"` on the
chosen data-center row and record its exact ID for both the volume and later pod.
An `unknown` result does not establish capacity. The minimum CUDA filter permits
newer compatible host capability; `--allowed-cuda-versions` is an exact allow-list.
This snapshot does not qualify the eventual host or reserve stock.

Install Astrid with this checkout's patch and the pinned `runpod-lifecycle`
dependency. Select the volume's data center before creating that volume; this
command accepts an existing volume of at least **100 GB** and attaches it without
creating or resizing it.
Load `RUNPOD_API_KEY`, `RUNPOD_SSH_PUBLIC_KEY` (the public key text), and
`RUNPOD_SSH_KEY_PATH` (the matching local private key path) in the invoking
process, then run:

```bash
build-h3-release --volume VOLUME_ID --pod-type cpu --max-seconds 10800 \
  --max-hourly 0.30 --handle ./h3-build.json
```

`python -m astrid.packs.runpod.h3_release` exposes the same entrypoint.
`--plan` prints the exact recipe, model lock, release ID and release root with
no provider calls. `--claim-seconds` defaults to 1800 and cannot exceed it;
the total cap includes a 120-second cleanup reserve. CPU placement requests
two vCPUs, the price-prioritized compute CPU flavors, the exact volume/data
center and only SSH. There is no automatic GPU fallback. To build on a GPU,
pass its exact RunPod GPU type ID as `--pod-type` and an explicit suitable
`--max-hourly`; the request then requires host CUDA 13.0.

The builder saves the allocation intent before creation and the exact pod ID
immediately after the response, before waiting for SSH. It verifies the
provider's volume attachment, mount, image, data center and hourly price.
On success, failure, timeout, SIGINT or SIGTERM it deletes that exact pod and
requires an independent GET to return 404. An unresolved cleanup stays pending
in the handle and exits unsuccessfully. After a host crash or SIGKILL, recover
with the same handle:

```bash
build-h3-release --volume VOLUME_ID --handle ./h3-build.json --cleanup-only
# Or rerun the original command to clean the old pod and resume on a new one.
```

A lost create response is reconciled by the unique request name before another
allocation. If the outcome cannot be resolved, the command refuses a replacement;
retain the handle and reconcile it against the provider account.

The recipe is packaged as `astrid/packs/runpod/environment.linux-cu130.json`;
`models.lock.json` pins the five generate-path assets to an immutable HF
revision with exact bytes and SHA-256s. The release directory is
`/workspace/h3-golden/releases/h3-cu130-<input-sha256>`, covering the recipe,
model lock and provisioner implementation. Python 3.12.10, the managed Python
installation and the venv all live on the volume. Pinned ComfyUI, VibeComfy,
H3 and VideoHelperSuite sources are fetched directly at their commits.

Each source/runtime/model step probes existing state before doing work. The
supporting dependencies resolve against the dated PyPI cutoff into a hash lock;
VibeComfy installs with `--no-deps`, and the exact official Linux CUDA 13
Torch 2.10.0, TorchVision 0.25.0 and TorchAudio 2.10.0 wheels install last.
The builder checks their CUDA local versions, `torch.version.cuda`, the pinned
Comfy helper distributions, OpenCV imports and dependency closure. The preserved
`runtime/requirements.lock`, `runtime/environment-receipt.json` and
`runtime/build.log` record that environment.

One volume flock serializes writers. Downloads use validated byte ranges and
resumable `.part` files; only a matching byte count and SHA-256 permits atomic
promotion to `models/`. The builder writes `release-manifest.json`,
`models.lock.json`, `model-inventory.json` and `build-receipt.json` before
writing `READY` last. The local handle, `.manifest.json` and `.receipt.json`
record the release root, exact manifest bytes/digest, observed pod price and
GET-verified cleanup. Retain these for P2 staging.

Attach the volume to an inference pod using the recorded image, same data
center and a CUDA-13-compatible host. Use the reported release root's
`runtime/venv/bin/python`, `runtime/ComfyUI`,
`runtime/vibecomfy/custom_nodes.lock` and `runtime/launch-comfy.sh` when
constructing staging inputs. The release's `models/` directory is the model
root. Storage preparation records `gpu_qualified: false`, even when built on
a GPU: GPU admission and a real generation smoke remain separate gates.
Storage modes and generation-selection builders remain follow-up work.

For non-UI CLI and worker startup, export `VIBECOMFY_HEADLESS=1` before
invoking `vibecomfy session start` or `vibecomfy run`. The packaged launcher
sets it automatically.

#### Lock custody and portable Python IR

Current VibeComfy IR can carry structured, resolved `custom_node_refs` with
repository, commit/version, class-set and evidence fields; those workflow
pins are authoritative and may be enriched by a legacy lock only when they do
not already identify the pack. Older bundles and the compiler's compatibility
path still require the checkout-adjacent `custom_nodes.lock`, so ship it with
the runtime and verify its SHA-256 (the H3 release lock is
`7741945b99176bedf97fd14009539156ccccde41c3cbe4f03b9f26df0ccead42`). Never
synthesize a lock from whatever happens to be installed. A missing or
different lock is a transport/setup failure, not a reason to weaken the
workflow pin.

## What is actually supported here

Use Astrid's RunPod pack, backed by `runpod-lifecycle>=0.3`:

- `runpod.session` is for disposable one-shot jobs. It provisions, executes,
  and tears down in `try/finally`; do not use it when the pod must stay running.
- For an existing pod, `runpod-lifecycle run POD_ID --script <job-script>
  --keep-pod` already ships, executes and fetches results without a remote
  Astrid worker. Its outputs are local files, not automatically managed media.
- `runpod.exec` wraps that runner through Astrid and leaves the pod alive.
  It currently requires the full handle from `runpod.provision`, not the
  claim waiter's smaller handle. Verify the runtime, executor dependencies,
  SSH transport admission and managed settlement before claiming native success.
- Use `runpod.provision` → `runpod.exec`/`runpod.pull` → `runpod.teardown`
  when several sequential GPU stages must share a warm ComfyUI process/cache.
- Provisioning writes `pod_handle.json` immediately. It contains the pod ID,
  SSH details, configuration snapshot, expiry/cost information, and the name
  of the credential variable—not the API key.

The supported Python shape is (the runtime owns admission and receipts):

```python
from astrid.sdk import AstridClient

with AstridClient.open_from_launcher() as client:
    result = client.invoke_result(
        "runpod.provision",
        kind="executor",
        project="<selected Astrid project>",
        inputs={"gpu_type": "<explicit GPU type>"},
        wait=True,
    )
    if not result.ok:
        raise RuntimeError(result.error)
# The runtime result contains the managed pod_handle artifact. Pass that exact
# handle to runpod.exec/runpod.pull, then invoke runpod.teardown through the
# same connected runtime.
```

Do not invent a standalone `out=` argument or call the module-level SDK helper
without a connected client. For the exact executor input contract, inspect the
[RunPod pack skill](../astrid/packs/runpod/skill/SKILL.md).

Do not turn `workflows/astrid_intro_h3_anchor_chain/run_pod.sh` into a launch
command. It assumes a pod already exists, changes to the pinned remote
VibeComfy checkout, and queues a workflow against
`http://127.0.0.1:8188`. Its refusal to overwrite an existing attempt directory
is part of the safety boundary.

### Optional worker-backed Astrid task submission: incomplete integration

The following describes the worker-backed acceptance contract, not a working
automatic pod-attach command in the inspected checkout. `execution_request`
is currently stored as task metadata; it does not attach a pod or enforce
placement. Do not block ordinary `runpod.exec`/lifecycle jobs on implementing
this integration. See the [practical operator path](spinning-up-and-executing-tasks-on-runpod.md).

For worker-backed generation, submit through Runtime's task queue rather than
calling Comfy's `/prompt` endpoint directly. Carry the exact capability digest,
input-object manifest, RunPod pod/account target, bounded queue/runtime limits,
and `leave_running` lifecycle. The worker owns the sequence
`admitted -> claimed -> running -> completed`; retain task/events JSON, the
executor/fence/runtime epoch, host and Comfy logs, workflow/input hashes, and
the raw output manifest.

`vibecomfy.run` is a local-generation adapter: its `network: true` declaration
covers the host-owned loopback Comfy daemon and emits diagnostic socket evidence;
it is not provider egress. Provider adapters still require the host-managed
broker and signed route evidence. After the command returns, managed VibeComfy
outputs are copied into the attempt-local `outputs/` spool before harvesting;
settlement must not depend on a managed checkout path that will be cleaned up.

Before teardown, replay the same idempotency key (it must return the original
task/run without a new claim), and attempt a claim with a worker credential
bound to a different executor (it must be rejected). Decode each selected MP4
and compare dimensions, frame count, and SHA-256 with the result manifest. Only
then terminate the exact pod ID and verify it is absent; the network volume is a
separate resource and remains intact.

On a persistent network volume, never treat an old `generic-host.ready.json`,
PID file, result, or “newest MP4” as evidence for the current launch. Remove
stale readiness witnesses before starting the host, require the new marker to
be newer than the launch and to name a live PID/executor with
`vibecomfy.run`, and bind smoke output to the current prompt’s reported path
under the attempt-local output directory. A non-zero VibeComfy exit is only a
diagnostic warning when the result is `completed`, the runner contains only the
known external-runtime observation warning, the Comfy checkout’s Git HEAD
matches the release manifest and is clean, and a full `ffmpeg -xerror` decode
passes; otherwise it is a hard failure. `ffprobe` alone is not a decode test,
and an API field reporting `comfy_commit: null` must be distinguished from an
actual source mismatch by recording an independent checkout identity proof.

## Before spending anything

1. **Authority and key.** Confirm the person/account authorized to spend and
   terminate this pod, and the exact project/run that owns the outputs. Keep
   `RUNPOD_API_KEY` in Astrid's shared credential store (`astrid-credential set
   runpod`) or an approved secret manager; never put it in a profile, workflow,
   shell argument, log, or `pod_handle.json`. A read-only authentication check
   is:

   ```bash
   astrid-with-credential --provider runpod -- runpod-lifecycle list --json
   ```

   A 401 is a rejected key, not a GPU or workflow failure. Fix the selected
   credential and repeat the read-only check.

2. **Spend cap.** Write down a hard dollar/time cap before launch. Set the
   executor's `max_runtime_seconds`; record the expected hourly rate and keep a
   human-visible timer. For manual `runpod.provision`, a library/runtime
   timeout is not proof that the provider pod was terminated; retain the handle
   and perform explicit teardown plus provider verification. A timeout is a
   backstop, not a substitute for teardown. Stop admitting stages if the cap or
   remaining time is unclear.

3. **Storage.** Decide whether the job is storage-free, uses the pod's local
   volume, or requires an existing named network volume. Astrid never creates a
   network volume implicitly: preflight the exact `storage_name`, data center,
   mount path, free space, and whether the data must survive pod termination.
   Pod volume data can persist across restarts but is tied to its machine;
   network volumes are the portable handoff. Deleting a pod is not a request to
   delete a network volume, so audit volume ownership separately.

4. **Pins and security.** Pin the image, VibeComfy checkout, custom-node
   revision, model revisions/checksums, workflow/prompt digests, ports, SSH
   key, and (where supported) the compute profile. Expose only the required
   ports; keep ComfyUI bound to loopback when using the remote script. Do not
   infer GPU suitability from a filename or from a previous job.

   When staging a VibeComfy source checkout, include its authoritative
   `custom_nodes.lock`, verify the transport hash, and compare the required
   node commits with the workflow declarations before compile. Copying Python
   source alone is incomplete: the CUDA 13 relaunch initially stopped at this
   missing-lock gate. Restore the matching lock; do not bypass the check or
   synthesize different node revisions.

   **Compatibility note:** current VibeComfy IR carries structured resolved
   `custom_node_refs` (including exact commit/version and optional evidence),
   so a portable workflow can carry its own node identity. The compiler still
   accepts the checkout-adjacent lock for legacy bundles and for enrichment
   when a workflow ref lacks identity; ship and verify that lock until all
   consumers have migrated. Workflow declarations are not proof of the
   remote installation: compare the effective refs with the installed commits
   and lock hash before compile.

5. **Right-size by evidence.** GPU choice, resolution, frame count, model
   family, and context length are workload-specific. Do not universalize the
   5090 or the 1920×1088 result below. Confirm VRAM, system RAM, disk, CUDA,
   node/model compatibility, and expected runtime for this exact workflow.

### Request host CUDA compatibility, not just a GPU name

RunPod's GraphQL `podFindAndDeployOnDemand` supports `allowedCudaVersions`.
For a workflow requiring CUDA 13, request the corresponding allowed host
version (for example `allowedCudaVersions: ["13.0"]`) instead of assuming every
5090 host meets the requirement. Still verify the observed host and runtime
after launch. The current lifecycle CLI exposes this as `--allowed-cuda-versions`.
See the official [CUDA placement filter](https://docs.runpod.io/sdks/graphql/manage-pods#filter-by-cuda-version).

The 2026-09-21 inspected checkout passes `allowed_cuda_versions` through launch,
fallback and probe. Check installed CLI help rather than relying on the package
version string alone. Earlier revisions lacked that support; this missing
placement constraint was a major cause of the 18 September compatibility detour.
The earlier unfiltered attempt did not
enforce it; a subsequent direct-provider relaunch created pod
`t2bwv16uualmsr` on 18 September 2026 with
`allowedCudaVersions: ["13.0"]` at the recorded `$0.99/hour` rate. The host
driver/runtime verification and creative-readiness gates remain pending; see
the [CUDA 13 relaunch record](astrid-intro-cuda13-relaunch-20260918.md).

## Launch and readiness gates

Use the named compute profile or explicit executor inputs. A profile may record
`datacenter_id`, but the currently targeted lifecycle library does not use it
as a launch constraint; verify the observed location in the receipt. A profile
also does not create or resize storage.

After provisioning, do not queue creative work merely because an SSH address
exists. Gate the run in this order:

1. The returned handle has one exact pod ID, expected image/GPU/storage, and a
   known expiry. Confirm it is yours in the provider list; do not operate on a
   name match alone.
2. `wait_ready` has passed and SSH works with the intended identity. ComfyUI
   startup can spend several minutes importing the pinned custom nodes before
   it binds port 8188; use a bounded readiness deadline of at least 15 minutes,
   poll `/system_stats` with a short HTTP timeout, and preserve the process
   table plus the final Comfy log tail when that deadline expires. A readiness
   timeout is a failed qualification, not evidence that the CUDA stack is bad.
3. On the pod, record `nvidia-smi`, CUDA/PyTorch versions, GPU memory, CPU/RAM,
   free disk, mounted volume, ComfyUI revision, custom-node revisions, and
   model file checksums. Treat every mismatch as a stop condition.
4. Run VibeComfy's offline gates before GPU admission:

   ```bash
   vibecomfy inspect <workflow>
   vibecomfy validate <workflow>
   vibecomfy doctor <workflow> --json
   ```

5. Run one bounded smoke at a time: one short output, one known seed, explicit
   output directory, and a timeout. Verify it can be downloaded and decoded.
   Only then admit the real chain. A failed smoke is not permission to queue
   the full job: diagnose it, preserve the failed attempt, make an explicit
   correction, and repeat the bounded smoke.

### Fresh-machine pitfalls observed on 18 September 2026

These are observed compatibility/transport issues, not defaults to copy into
every future run. See the [dated execution record](astrid-intro-regeneration-20260918.md)
for exact revisions, attempts, and receipts.

- **Connect to the existing Astrid owner.** If SDK startup reports a different
  active runtime owner, use `AstridClient.open_from_launcher(start_pack_host=False)`
  to connect to it; do not start a competing pack host.
- **Do not assume the wrapper is on PATH.** In this checkout the available
  credential wrapper is `.venv/bin/astrid-with-credential`. Resolve the executable
  before launch. The Astrid provisioning admission hit an `os.uname()` sandbox
  error before contacting RunPod; the same documented lifecycle substrate worked
  through that wrapper. Verify provider state before retrying any failed launch.
- **Choose an existing volume explicitly.** A profile/example named `Peter`
  did not match this account's `backup` volume. Listing storage comes before
  launch; omitting a storage argument did not reliably mean storage-free.
- **Disk free space is not a quota guarantee.** The mounted network filesystem
  reported space but rejected large model writes at its quota. Check actual
  write capacity and quota. Moving disposable models/runtime to container disk
  makes them termination-sensitive; preserve outputs separately.
- **Record the driver, not just installed CUDA packages.** This fresh 5090
  supported CUDA 12.8, while the historical recipe requested cu130. Installation
  success was not runtime compatibility. An inherited `PIP_CONSTRAINT` also
  affected installs inside a new virtualenv. Inspect it before dependency setup.
  Resolve this mismatch before downloading large models: a different container
  image does not upgrade the provider's host driver. A compatible host is the
  preferred way to retain the original runtime contract. If adapting the
  runtime instead, declare and validate the new target and preserve every
  patch; do not call that result an unmodified canonical environment.
- **Exercise the decoder in the smoke.** A per-model PyTorch attention selector
  did not govern all Minimax calls. Explicit global PyTorch attention passed
  sampling, but the quantized VAE still directly selected a CUDA 13 kitchen
  kernel. A narrowly recorded runtime patch made that branch honor the existing
  PyTorch fallback; the five-frame sampling-and-decode smoke then passed. This
  is a base revision plus a patch, not an unmodified pin. Preserve the diff and
  before/after hashes; do not silently rewrite the historical workflow bundle.
- **A tiny smoke does not prove full-size memory feasibility.** The five-frame
  test passed, but the first 1920×1088/175-frame stage failed in the eager INT8
  linear fallback while concatenating large activation buffers. `--lowvram`
  alone did not fix it. Record both tests separately; never describe a small
  smoke as proof that the production frame count fits. Backend-specific
  activation memory can matter as much as model weight offloading.
  The isolated preallocation patch later passed 175 frames, but 260 frames
  still failed because the full INT32 intermediate remained resident. The
  longest admitted continuation is a separate capacity gate. The tested
  Triton candidate also crashed during autotuning on the full-size tensor;
  capability discovery alone was not execution evidence.
- **Use the memory control for the active loading mode.** At this Comfy pin,
  NVIDIA/modern-PyTorch defaults to DynamicVRAM, whose CLI explicitly says
  `--lowvram` is ineffective. Its intended working-memory control is
  `--vram-headroom GB`, passed to aimdo device initialization. Inspect the
  actual CLI contract before treating an offload flag as a fix. The revision
  is testing 10 GB headroom against its largest observed allocation; that value
  is workload-specific, not a universal setting. Legacy loading requires
  explicitly disabling DynamicVRAM before legacy low/no-VRAM controls apply.
- **Transport scripts sequentially and verify hashes.** Concurrent tarball
  uploads mixed destination contents in this run. Complex `bash -lc` bodies also
  lost quoting through lifecycle `exec`. Use direct simple argv, or transfer a
  verified script and execute its exact path.
- **Verify the verifier's paths.** Explicit-server checks required
  `VIBECOMFY_CUSTOM_NODES_DIR` and `VIBECOMFY_MODELS_ROOT`; runtime/model CLI paths
  alone did not cover both checks. Do not confuse `_MODELS_ROOT` with `_MODELS_DIR`.
- **Distinguish verification time from queue time.** Each prefix invocation
  rehashed about 53.5 GB of model files in this run, taking minutes before
  Comfy received a prompt. An empty queue alone did not mean the operator was
  idle. Monitor the runner's phase/PID and then its admitted prompt ID. Retain
  integrity checks; repeated verified-file hashing is a tooling improvement
  opportunity, not a reason to silently bypass model verification.

Here, “reading a model” during this phase means a CPU-side sequential scan to
compute or verify its SHA-256 receipt. It is not the model being loaded into
GPU memory, and it is not evidence that sampling has begun. The first scan on a
fresh machine is an intentional integrity gate. Later stages should reuse the
receipt when the declared path, size, modification identity, and expected
digest still match; rescanning every large model for every prefix is a
measurable inefficiency to fix in the verifier, not something to hide by
disabling integrity checks.

The purpose of this check is exact-byte identity and corruption detection: the
workflow must use the model revision it declares, not merely a file with the
right name that happens to load. The fast path is to hash while acquiring the
asset, persist that receipt with the canonical path, expected digest, file
identity, and verifier version, then perform an O(1) stat/receipt comparison on
later runs. Rehash only when the receipt is absent, the file identity changes,
the expected digest changes, or the verifier changes. ComfyUI's normal loader
does not provide this cryptographic proof; it opens/deserializes tensors and
checks loader metadata for execution. Do not replace the first acquisition
hash with a filename or safetensors-header check, and do not silently disable
the integrity gate just to avoid the second read.

### Follow the live machine, not only the launcher

For every remote setup, smoke, diagnostic, or creative stage, keep a live
monitor attached to the exact pod handle until the stage has produced and
verified its receipt. A local `exec` or launcher returning is not evidence that
the remote process completed, failed, or stopped spending. At each poll record:

- the exact pod status and public SSH identity;
- the remote command PID and phase (setup, model transfer, Comfy startup,
  queue, sampling, export, or pull);
- the current Comfy/runner log tail and admitted prompt ID, when applicable;
- GPU utilization/memory/clock, disk growth, and the active output file; and
- the expected receipt, decode, and hash before advancing to the next gate.

Use the lifecycle status/exec route against the exact handle, for example:

```bash
astrid-with-credential --provider runpod -- runpod-lifecycle status <pod-id>
astrid-with-credential --provider runpod -- runpod-lifecycle exec <pod-id> \
  'ps -eo pid,etime,cmd; nvidia-smi; df -h / /workspace'
```

Keep long setup and generation commands in a foreground execution session or
write their remote stdout/stderr to a durable run evidence file and poll that
file. If the PID disappears without the expected receipt, treat the stage as a
failure and inspect the remote logs before retrying. Do not start a second pod
because a local client appears idle; first verify the first pod's exact remote
state. If a capacity probe allocates a candidate while another pod is active,
give it a bounded readiness deadline and terminate the exact probe unless it
passes the same host/runtime gates and an explicit handoff is recorded.

Do not arm a fixed wall-clock `sleep …; terminate <pod>` process copied from a
previous run. A deadline is a budget signal, not permission to interrupt active
sampling. Any autonomous teardown guard must begin its readiness timer only
after SSH is usable, poll the exact remote setup/runner/delivery PIDs and Comfy
queue, and treat sampling, pending prompts, output pulls, and QA as active work.
It may terminate only after the queue is empty, no generation or delivery
process remains, outputs are preserved, and an explicit teardown-eligibility
marker exists. Otherwise keep the pod under manual supervision and recalculate
the remaining budget from the current run's actual allocation time.

For consequential judgment calls, obtain an independent Astra second opinion
before acting. This applies to pod termination or relaunch, deadline or budget
changes, interpreting a smoke or capacity result, accepting a generated take,
and deciding between regeneration and a finishing transformation. Record the
question, evidence considered, Astra's recommendation, and the final decision
in the run evidence. A second opinion informs the decision; it does not replace
the exact-handle, custody, budget, and raw-quality gates above.

## Sequential generation and the warm-cache trap

For temporal continuation, preserve the same ComfyUI server, graph IDs,
inputs, seeds, and native latent/context edges. Run one segment at a time and
review it before admitting the next. Do not feed a cleaned/decoded derivative
back as native continuation context unless the recipe explicitly requires it.

The H3 opening run demonstrated the practical failure mode: the first native
1920×1088 starter completed in 363.20 seconds, and later continuation stages
reused a running Comfy cache. The record also shows that the cache is fragile:
the first sampler was reported cached, but a lost/restarted server requires an
explicit regeneration path. Save the exact workflow, seed, prompt head,
reference mode, context-frame count, and attempt directory for every stage.
Never “repair” a motion/style failure by silently changing the protected
context; make a new, immutable attempt and preserve the rejected take.

`run_pod.sh` records each attempt under the remote run directory and refuses to
overwrite one. Keep that behavior. Monitor queue state, Comfy log lines,
GPU utilization/memory, disk growth, elapsed time, and the spend timer. An idle
GPU can still incur pod cost.

## Delivery gate: download, verify, import

Before stopping or terminating anything:

1. Pull each selected output, metadata, workflow/recipe, prompt document,
   Comfy history, and review asset to local managed storage. Do not rely on a
   remote path or a contact sheet as delivery.
2. Verify file existence, decodability, dimensions, frame count, frame rate,
   and expected duration. Compute SHA-256 locally and compare it with the
   receipt/manifest; preserve raw and derived (for example, matte or conform)
   identities separately.
3. Import/register media through Astrid's runtime/SDK, not by dropping files
   into an ad-hoc `runs/` directory. Confirm the runtime can open the managed
   media and the exact run evidence. For a successful generation, select it in
   the intended timeline, render, and inspect the full result and seams before
   calling delivery done.
4. If generation failed, the budget/end gate was reached, or review cannot
   continue, preserve every rejected raw output and its receipt/evidence,
   record the failure and remaining work, and confirm there is no pending GPU
   work. That is enough to stop spend; do not keep an idle pod running forever
   while waiting for editorial acceptance. Only after preservation and the
   no-pending-work check may teardown begin.

## Stop versus terminate

RunPod's official REST control plane distinguishes `POST /v1/pods/{podId}/stop`
from `DELETE /v1/pods/{podId}`. Stop releases the GPU but keeps the pod's
machine-bound local volume; a restart may have zero GPUs if that machine is no
longer available. Termination destroys the pod. A network volume is separate
and remains until separately deleted. See the [official stop guidance](https://docs.runpod.io/pods/troubleshooting/zero-gpus),
[pod delete API](https://docs.runpod.io/api-reference/pods/DELETE/pods/podId),
and [network-volume API](https://docs.runpod.io/api-reference/network-volumes/POST/networkvolumes).

For a disposable completed job, use the exact `runpod.teardown` executor with
the exact `pod_handle.json`; it calls the lifecycle library's terminate path.
Treat a successful teardown receipt as necessary but not sufficient: perform a
read-only provider lookup for that exact ID and wait until it is absent. The
RunPod delete API documents HTTP 204 on success; a subsequent GET/list should
no longer return the pod.

### Ownership and locked-pod guard

Never terminate a pod found by a broad list, a familiar name, or a stale SSH
address. Before teardown, verify all of:

- exact pod ID matches the handle and the authorized run;
- account/credential is the owning account;
- no other operator or run has custody;
- outputs and persistent volume handoff are complete; and
- the pod is not locked, or an authorized owner has explicitly unlocked it.

RunPod documents that a locked pod disables stopping or resetting. If teardown
fails because it is locked, stop. Do not bypass the lock or try another ID.
Only the authorized owner may unlock that exact owned pod; then re-check the
ID, custody, and delivery gates before retrying teardown. If ownership cannot
be proven, leave it alone and escalate.

## What this repository actually tested

The original Astrid intro execution record tested a **supplied/reused** pod.
On 17 September 2026, pod `6vuwjqzv1c1qk6` was
terminated and verified with `DELETE 204` followed by `GET 404`. It was supplied
to the run and therefore is evidence for the terminate-and-verify procedure,
not evidence that Astrid's launch route, GPU availability, or sizing is
universal. The H3 run also left the pod running while outputs were being
reviewed, then required delivery verification before cleanup—the reason this
guide puts the download/import/hash gate before termination.

On 18 September 2026, the revision run launched a **fresh RTX 5090** pod,
`oh3fow3oows91z`, through the credential-wrapped lifecycle substrate. SSH,
GPU discovery, model preparation, and a corrected end-to-end smoke were
verified. Full-resolution creative output acceptance and teardown are separate
gates; their status is tracked in the dated execution record, not implied by
this setup result.

Related repository guidance: [Astrid intro execution record](astrid-intro-animation-execution.md),
[RunPod credentials](../docs/reference/runpod-credentials.md),
[compute profiles](../docs/reference/compute-profiles.md),
[VibeComfy execution skill](../../vibecomfy/docs/agent-skill/skills/run-comfy-workflow/SKILL.md),
and [RunPod pack skill](../astrid/packs/runpod/skill/SKILL.md).


## HTTPS-only RunPod clients

The H3 release builder and claim waiter accept `--transport https`. This starts
a single-file Python pod agent through the provider creation command and uses
`https://<pod-id>-8000.proxy.runpod.net`; an SSH identity is not required.
Install the companion runpod-lifecycle and RunPod SDK patches first.

```sh
build-h3-release --transport https --volume EXISTING_VOLUME_ID --handle ./h3-build.json
python scripts/claim_runpod_5090_backup.py --transport https --storage-name EXISTING_VOLUME_NAME --provider-account-ref YOUR_ACCOUNT_LABEL --release-root /workspace/h3-golden/releases/EXACT_RELEASE_ID --handle-path ./claim.json
```

Keep all existing budget, storage, provider identity and cleanup requirements.
SSH is still the default; proxy-configured SSH clients fail before allocation
with `SSH egress unavailable; use --transport https`. `--plan` and builder
`--cleanup-only` do not require pod-agent connectivity or an SSH key.

The builder keeps its random agent token in memory. The claim waiter writes a
random per-claim token to an owned 0600 sidecar; only its absolute path enters
the ordinary claim handle. Keep that sidecar private and available for the
claim lifetime. `--resume --transport https` reuses it; it never generates a
replacement token for an existing pod. New HTTPS attachment without the
original claim/token is refused. Prepared-worker composition reads the handle's
transport and token-file reference automatically, so the normal
`h3_av.transform --prepared-worker` selection format remains unchanged.
Staging commands, source uploads, session operations, worker-process operations
and pre-reserved worker credentials use the selected transport. A quiescent
provider-only release does not require the token file.

Claim transport/token custody also takes precedence over ambient reconnect
variables. Provider-only cleanup after durable quiescence ignores an ambient
token-file setting, so an unavailable sidecar cannot block exact provider
cleanup. Failure cleanup for newly allocated, unactivated pods retains the
Phase 0 release policy and preserves the existing network volume; unresolved
cleanup remains in the same handle for reconciliation.

Local transport tests cover staging probes and inode-preserving credential
transfer. No complete GPU transform has been live-qualified. The single capped
live smoke passed `uname -a` and a 50 MiB upload, but failed its original Range
download. JSON download envelopes were added and tested locally afterwards;
this remains a rollout limitation, not a successful live round trip.
