# Generation on a fresh installation

Use the [ordinary installer](local-source-install.md) in a terminal before
starting generation. It selects the Runtime workspace, verifies the installed
service and adds Astrid's skills. Repeat the same install line to repair an
existing installation. A kept-data reinstall attaches the recorded workspace.
Use `--update` when qualifying different program commits; ordinary repair
retains installed code.

After installation, use the installed product gateway:

```sh
astrid-tools status --json
astrid-tools generate list --json
astrid-tools generate targets --json
astrid-tools generate describe --entry z-image --mode t2i --json
```

`python -m astrid` exposes the same product gateway from the selected
interpreter. The shorter `astrid` command opens the named agent. Listing entries,
describing inputs and reading target configuration do not create a workspace,
install an engine, launch ComfyUI or claim a paid worker. A listed entry still
needs execution readiness before it can run.

If workspace inspection reports `workspace_missing`, run the install line again
outside chat, using the original program/data paths. Inspect its `Details:` log
if setup does not finish. Starting generation or repeating metadata probes is
not workspace setup. Runtime owns explicit workspace create/attach operations;
Astrid's storage resolver only reads the chosen root. For an intentionally
separate developer workspace, select an absolute `ASTRID_RUNTIME_DATA_ROOT`
before using Runtime's explicit workspace setup. Keep that choice consistent
across commands and do not move an existing realm by copying catalog files.

Root selection is independent of the current directory. An explicit
`ASTRID_RUNTIME_DATA_ROOT` wins; otherwise Astrid reads the installer selection
(including kept-data uninstall records), then persisted setup selection, then
the checkout's `config/astrid-runtime.json`. A wheel without any of these uses
`~/.astrid-data`. Resolving a path creates no folders. A linked install record
is refused so an invalid record cannot silently choose a blank fallback root.
Repair the installation record through the installer; do not delete it to
silence the diagnostic. Existing conflicting realms require an explicit choice.

For unpublished commits, follow [local Git source qualification](local-source-install.md#developer-qualification-with-local-git-sources).
Set each manifest source's absolute local `repo` and full lowercase commit
`sha`; retain the complete pinned tools section. The installer reads committed
bytes with `git archive`, ignoring dirty and untracked files. Use separate
program/data directories, a clean test home and an unrelated working directory.
On Windows retain `%LOCALAPPDATA%` for the chosen home; on Linux retain the
selected XDG paths. Repeat repair, then uninstall while keeping data and
reinstall; verify the same realm ID and project bytes each time. These native
installer qualifications belong to M-R1FIX/G-13/R-18.

For a human ComfyUI target, inspect `generate targets` and the entry's input
description, then use the explicit target when generating. Launching a stopped
human installation requires per-call `--consent user_env`; otherwise start it
manually. Readiness and version drift evidence must retain that target's
identity. Managed missing-model recovery requires the producer-supported fix
and exact consent; an unresolved fix remains blocked.

RunPod requires explicit selection and fresh paid authority. Entry/input
validation happens before a claim. Use the ordinary generation route with
`--target runpod --consent paid --max-usd <cap> --deadline <UTC-deadline>` and
follow the admitted task. Inspect with `generate describe --target runpod`;
request release with `generate release --engine comfyui --target runpod`.
A release task receipt may still say `cleanup_pending`. Completion requires
owned drain/stop evidence and independent exact-resource GET 404 plus successful
account-list absence, preserving the volume and human server PID/queue.

## CPU and installed fixture selectors

The shared [journey rows](../../tests/fixtures/generation/agent_journeys.json)
declare `cpu`, `installed`, `runpod-fake` and `runpod-installed-fake` selectors.
They use the existing `ASTRID_HOST_EXECUTION_PROFILE=source|installed` choice
and installed prerequisite in `tests.lanes.has_installed_astrid`. M-CI owns
workflow provisioning and final M05 selector wiring. There is no new lane or
automatic installation in these tests. Missing installed prerequisites fail.

From the candidate checkout, using an already provisioned interpreter:

```sh
python -m pytest -q -p no:cacheprovider -o addopts= -m 'not integration' \
  --basetemp=/absolute/task-local/a08-cpu \
  tests/test_installed_generation_journey.py tests/sdk/test_generation_first_run.py \
  tests/stage1/test_storage_root.py tests/test_skills_package_data.py \
  tests/sdk/test_runpod_front_door.py tests/packs/runpod/test_worker_release.py \
  tests/test_runpod_managed_worker_session.py
```

Delete the task-local basetemp after retaining the result. For the installed
selector, set `ASTRID_HOST_EXECUTION_PROFILE=installed`, retain the existing
lane's `ASTRID_REMOTION_PROJECT_DIR`, use its noneditable installed interpreter
and select these two nodes:

```text
tests/test_installed_generation_journey.py::test_generation_package_probe
tests/test_installed_generation_journey.py::test_runpod_package_fake_owner
```

The first checks imported package origin, shipped skills and non-writing CLI
metadata from an unrelated directory, using a small fake catalog and empty
target inventory in place of broad discovery and loopback probes. The real
CLI parser and readiness projection run. The second uses the real gateway/SDK and
a fake RunPod host owner for describe/release admission. A10 and pack fake tests
cover image/video/workflow/H3, consent, follow and release recovery. The
first-run tests use the existing installer transaction and local Git fixture,
with service/tool/Runtime boundaries stubbed. Linux/Windows selection paths
run on CPU; native Windows installation is a separate qualification.

The A09 and Pallando rows record required assertions, not completed live
journeys. H's approved C0 source/mask/request/asset-map digests are retained
without private media. The output must still prove 48 frames, 1024×576,
24 fps, preserved audio, full decode, hash-checked durable readback, setup,
cost and release. V08 owns exact bytes and V09 owns live execution. Phase 1
generation fixtures cover ComfyUI; Wan C6 is excluded.
