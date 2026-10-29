# Pod session setup

`scripts/pod_session.py` is the supported one-command entry point for a RunPod
session. Run it from the root of a clean `astrid-agent` Git checkout. It bundles
that checkout together with four sibling source checkouts, claims one pod, runs
the requested work, downloads and verifies results, writes progress, and releases
the pod.

The four sibling checkouts are supplied through `--sources-dir`; that directory
must contain clean Git checkout roots named `runpod-python`, `astrid-runtime`,
`runpod-lifecycle`, and `VibeComfy`. `astrid-runtime`'s HEAD must match
`runtime.lock.json` in the agent checkout. The runner rejects missing, nested,
dirty, or changed-during-bundle checkouts. It does not copy local Git remotes,
configuration, or credentials into the bundle.

The local `runpod-lifecycle` CLI must already be available beside the Python
interpreter or on `PATH`, and its normal provider authentication must already
be configured. No shell environment variables need to be set for the examples
below. Never print or copy provider credentials or the per-claim token into
logs. The runner creates the token in a mode-0600 file under
`~/.astrid-pod-tokens/` by default and removes it after verified release.

## Select inputs

Choose exactly one mode: the legacy single H3 request (`--request` plus
`--asset-map`) or a YAML `--jobs` manifest. The single request, and every H3
job request in a manifest, must have `output.duration: 9`. The runner derives
the one-second S0 smoke request for each H3 workload.

### Single H3 request

The request is a JSON object accepted by the H3 transform action. For example,
`job/request.json` can contain:

```json
{
  "version": 1,
  "operation": "generate",
  "source": null,
  "output": {"duration": 9},
  "content": {"prompt": "A blue room with a soft light."},
  "changes": {"video": [], "audio": []},
  "references": [{"asset": "reference", "purpose": "appearance"}],
  "overrides": {}
}
```

`job/assets.json` maps each referenced asset ID to a local file path, relative
to the directory where you run the command. Supply the referenced image or
video at that path, for example:

```json
{"reference": "job/reference.png"}
```

Run from the agent checkout root, with the four source checkouts in
`../pod-sources`:

```sh
python3 scripts/pod_session.py \
  --datacenters EUR-NO-1 \
  --sources-dir ../pod-sources \
  --request job/request.json \
  --asset-map job/assets.json \
  --workdir output/pod-session-single-01
```

Use a fresh `--workdir` for each run. For this mode, the local request and
asset map are saved under `workdir/inputs/`; the verified final video, receipt,
and composition manifest are downloaded under `workdir/download/`.

### YAML job list

A version 1 manifest must contain a non-empty `jobs` list. Job IDs must be
unique safe names (1–64 letters, digits, `.`, `_`, or `-`). Local paths in the
manifest are relative to the manifest file.

An `h3` job has `id`, `type: h3`, `request`, and either an `assets` map from
asset IDs to local paths or `asset-map` naming a JSON map file. It may include
`goal`, `prompt`, and positive `timeout` seconds (default 5400). Its request
must be nine seconds; the runner performs S0 and then the nine-second action.

A `cmd` job has `id`, `type: cmd`, `command`, and a non-empty
`expected-outputs` list. `command` may be an argv list or shell text.
`input-files` is optional and maps safe pod-relative destination paths to
local source paths. Each expected output may be a path string or an object with
`path`, `kind` (`png`, `json`, `mp4`, or `file`), and optional PNG dimensions.
When omitted, kind is inferred from `.png`, `.json`, and `.mp4`; other extensions
use non-empty file and SHA-256 checks. PNGs are decoded, JSON outputs must be
objects, and MP4s are probed. A `cmd` job runs in its own pod directory in the
installed H3 engine environment; it does not install a second engine.

Example `job/jobs.yaml` with one H3 and one command job:

```yaml
version: 1
jobs:
  - id: clip
    type: h3
    goal: Produce a nine-second audiovisual clip.
    request: request.json
    assets:
      reference: reference.png
    timeout: 5400
  - id: image
    type: cmd
    goal: Generate a PNG and JSON result receipt.
    prompt: A small blue rectangle.
    command: [python, generate.py]
    input-files:
      generate.py: generate.py
      data/reference.png: reference.png
    expected-outputs:
      - {path: generated.png, kind: png, width: 32, height: 24}
      - result.json
    timeout: 600
```

The paths above are relative to `job/jobs.yaml`: `job/request.json`,
`job/reference.png`, `job/generate.py`, and its other input files must exist
before the run. `generate.py` must write `generated.png` and `result.json` in
its working directory. Invoke the job-list run from the agent checkout root:

```sh
python3 scripts/pod_session.py \
  --datacenters EUR-NO-1 \
  --sources-dir ../pod-sources \
  --jobs job/jobs.yaml \
  --workdir output/pod-session-jobs-01
```

The `--request` and `--jobs` modes are mutually exclusive. Do not pass
`--asset-map` with `--jobs`; put H3 assets in each job entry.

## Placement, sequence, and checks

The runner enforces the supported placement values:

- Datacenter: `EUR-NO-1`; `--datacenters EUR-NO-1` is required.
- Existing network volume: `storage`, 10 GB by default. The runner attaches it
  by name and does not create a replacement volume.
- GPU preference: `RTX 5090`, then `RTX PRO 4500` by default.
- CUDA allow-list: the full supported list from `13.0` through `13.3` is
  passed to the provider because its filter matches exactly.
- Container disk: computed from the engine recipe as at least 120 GB and at
  least 1.5 times its install footprint. `--container-disk-gb` can override
  this only when the value still fits the footprint plus 20 GB.
- Image: read from `astrid/packs/runpod/environment.linux-cu130.json` by
  default. `--image` can override it.
- Capacity wait: none by default; `--wait-capacity SECONDS` enables retries.
- Poll interval: 30 seconds by default. Polls and downloads are reads; they do
  not create agent job records. The runner refuses a new record before reaching
  its 100-record limit (the agent limit is 128).

Both modes follow the same setup order: claim, bundle, bootstrap, install,
rerun, then workload and verified downloads, followed by release. The install
sets `ASTRID_ENGINE_SANITY_CMD` for the engine installer. The installer first
constructs the primary engine virtual environment, runs bf16/fp16/fp32 GPU
sanity through that environment, and only then downloads models. The runner
does not run a standalone pre-install sanity command. Its rerun requires all
install steps to be skipped and `astrid engines status` to report `installed`
with healthy status before starting workloads. H3 jobs run S0 before the
nine-second action; each job's later stages stop at the first failure.

The command prints progress for each step and writes
`workdir/pod-session-progress.json`. While the pod is available, it also writes
and reads back `workdir/pod-session-progress.remote.json`; that readback ends
before release because the pod is then terminated. Successful job-list outputs
are organized as:

```text
workdir/
  pod-src.tgz
  pod-session-progress.json
  pod-session-progress.remote.json
  packets/<job-id>/
    job.json
    README.md
    machine-check.json
    inputs/       # preserved request/assets or command input files
    logs/         # per-job stdout and stderr
    outputs/      # downloaded, hashed, and type-checked files
```

Review `packets/<job-id>/README.md` and `machine-check.json`; a successful packet
has status `passed` and output hashes and sizes. H3 packets also contain the
verified publication receipt and composition manifest, and include
`outputs/side-by-side.mp4` when a visual reference is available. Legacy mode
keeps its outputs under `workdir/download/`.

A successful run exits 0 and prints `POD SESSION COMPLETE`. On any failed step,
the runner stops later work, records the error and failed packet when relevant,
and still attempts release. A release is verified only when termination
succeeds, GET for the claimed pod returns 404, and the account pod list is
empty. Release verification failure makes the overall run fail; follow the
printed pod ID and terminate it through the provider CLI, then verify GET 404
and an empty pod list. Do not retry against an uncertain pod. After a verified
release, start a new run with a fresh work directory. Do not delete
`$R/.astrid-engine` by hand; if using the engine CLI outside this runner,
inspect `astrid engines status` and follow
`docs/setup/engines-placement-generation.md` before recovery.

## Offline CPU rehearsal

The repository includes a fake-provider/ComfyUI rehearsal. It uses the
production loopback AgentServer and HTTPS transport, detached jobs, uploads,
downloads, hashes, media probes, packet checks, progress, and release code.
Only allocation, installation/GPU checks, and H3 model/action execution are
fixtures. It reads no private media and calls no real provider. It demonstrates
runner behavior; it does not demonstrate live provisioning, installation, GPU
sanity, inference, or H3 transform quality.

From the agent checkout root, with the existing project test environment and
`ffmpeg` available, run the success and injected-failure cases:

```sh
python -m pytest -q -o addopts='' -p no:cacheprovider \
  tests/scripts/test_pod_jobs.py::test_two_job_cpu_loopback_packets_progress_budget_and_release \
  'tests/scripts/test_pod_jobs.py::test_failed_two_job_rehearsal_stops_and_releases[command]' \
  -o tmp_path_retention_policy=all
```

The success case must produce the exact progress sequence
`claim, bundle, bootstrap, install, rerun, h3:clip:s0, job:clip,
download:clip, packet:clip, job:image, download:image, packet:image, release`,
two `passed` review packets, under 40 agent job records, and release evidence
with termination, GET 404, and account list `[]`. Its setup trace shows
bootstrap → install → sanity → models, then the idempotency rerun, one-second
S0, and nine-second H3 action. The failure case injects a command exit, confirms
later work stops, records a failed packet, and also verifies release. With `-o tmp_path_retention_policy=all`, pytest retains both session
directories, their `pod-session-progress.json`, packet trees, `harness-boundaries.json`, and
`setup-trace.txt` for inspection.

## Regression-guard troubleshooting

Use these entries when a guard fails during development or when the same
symptom appears in a source checkout. The guard names below identify code
contracts; they do not add runner behavior.

- **A1 engine-root registry — status or local run cannot find the installed engine.**
  Cause: the registered root points to an install without a valid receipt, or
  the requested command is resolving a different root. Fix: inspect
  `astrid engines status`; pass the intended `--root` to status/install, then
  run against that successfully installed root. The runner already supplies
  `/root/astrid/engines/vibecomfy-h3` explicitly. Guard:
  `tests/core/generation/test_engine_local_endpoint.py::test_run_path_uses_the_recorded_install_root_without_an_env_var`.

- **A2 bootstrap rerun — bootstrap reports missing `runpod_lifecycle`, repeats
  package work, or fails after an earlier partial attempt.** Cause: the managed
  installation or source identity check is incomplete. Fix: make sure all four
  source checkouts and the matching Runtime pin are present and clean, then
  rerun the complete pod session with a fresh work directory; do not remove
  engine state manually. Guard: `tests/scripts/test_pod_bootstrap.py::test_marker_rerun_still_verifies_installed_astrid` and
  `test_missing_lifecycle_is_installed_into_existing_managed_environment`.

- **A5 public generation route — a new public generation method fails the
  route guard or bypasses normal admission.** Cause: it calls a lower-level SDK
  path instead of its registered central generation route. Fix: route the
  public entry through `astrid.core.generation.verbs` and its registered
  capability, then rerun the public route guard. Guard:
  `tests/core/generation/test_public_route_guard.py::test_public_generation_routes_use_the_registered_central_paths`.

- **A6 H3 workflow model URLs — workflow validation finds a mutable or
  unrecognized model URL.** Cause: a workflow URL differs from the immutable
  model revision in `models.lock.json`, including a `resolve/main` URL. Fix:
  derive the URL from the existing lock entry and keep the character-swap
  revision pin; rerun the workflow URL guard. Guard:
  `tests/packs/h3_av/test_workflow_model_urls.py::test_h3_workflow_model_urls_match_lock`.

- **A7 packaged ready templates — `workflow_from_ready()` cannot find a
  built-in template from an installed package without a source checkout.**
  Cause: the wheel lacks the package-owned `ready_templates` corpus or the
  workflow is only in a checkout scratchpad. Fix: install VibeComfy through
  the normal non-editable package step and select a shipped ready template;
  checkout scratchpads and paths outside the package are intentionally not
  trusted. Guard: `../pod-sources/VibeComfy/tests/test_packaged_ready_trust.py::test_wheel_ready_templates_and_external_paths_from_neutral_cwd`.

- **A8 Windows workflow publication — import or publish raises `EACCES` while
  opening a directory to sync it.** Cause: Windows does not support that
  directory-open/fsync operation. Fix: use the shared durable replace helper,
  which treats only this unsupported Windows directory sync as a no-op while
  preserving file flush, atomic replacement, and recovery; rerun the Windows
  publication and recovery guard. Guard:
  `../pod-sources/VibeComfy/tests/test_windows_directory_fsync.py::test_import_succeeds_with_windows_directory_open_denied`.

- **GPU sanity reports `CUBLAS_STATUS_NOT_INITIALIZED` or `INVALID_VALUE`.**
  Cause: a system CUDA library path is shadowing the engine environment. Fix:
  do not add system CUDA paths to the pod environment; the engine child
  environment rebuilds `LD_LIBRARY_PATH` and drops `/usr/local/cuda*`. Check
  the libcublas paths printed by `gpu_sanity.py`. Guards:
  `tests/scripts/test_gpu_sanity.py` and
  `tests/core/generation/test_engine_child_env.py`.

- **Bootstrap reports that the existing workspace has no usable source profile.**
  Cause: the profile is absent or disagrees with the bundled source identity.
  Fix: check the source checkout roots and Runtime pin described above, verify
  release, then rerun the full session; bootstrap writes the source profile.
  Guard: `tests/scripts/test_pod_bootstrap.py::test_setup_runtime_writes_profile_and_is_idempotent`.

The installed model-root receipt checks are covered by
`tests/core/generation/test_engine_local_model_root.py`; a missing or
unverified model binding should be repaired by a successful engine install,
not by deleting `.astrid-engine` state.
