---
name: vibecomfy
description: >
  Run the user's own ComfyUI and saved workflows on their machine (local
  generation), and import, inspect, edit, validate, and run ComfyUI workflows
  through VibeComfy's canonical Python/companion/source bundle and Astrid task
  history.
---

# VibeComfy

## Run on your own ComfyUI

Use this when the user wants to generate with their own ComfyUI: "use my
ComfyUI", "with my portrait workflow", "my saved workflow", "run it locally",
"on my GPU", or when their `## Local generation` preferences say to prefer
local and they didn't ask for the cloud this time. Their ComfyUI is already
running; you attach to it. You never launch, restart or manage it, never
unload its models, and never touch its queue beyond your own prompt. No API
key is involved.

The flow is: find their ComfyUI → pick a saved workflow → read it → run
`vibecomfy.run` with `comfy_url` → follow → show the result → remember the
setup. Ask the user only on real ambiguity, and before installing anything.

### Find their ComfyUI

If the injected preferences already have `## Local generation`, treat it as a
hint and check it with one call. Otherwise discover. Both use the same
read-only helper, run with Astrid's Python (the one that runs
`python3 -m astrid`):

```sh
python3 -m astrid.packs.vibecomfy.local scan                 # ports 8188 and 8000
python3 -m astrid.packs.vibecomfy.local scan --ports 8188    # health-check a saved URL
```

It reads `/system_stats`, `/internal/folder_paths` (model folders and
`custom_nodes`, whose parent is the ComfyUI base) and `/userdata?dir=workflows`,
and prints one JSON object: `servers[]` with `url`, `base_dir`, `output_dir`
(`--output-directory` if set, else `<base>/output`), `workflows` (saved
names), `manager_present` and `python` (their ComfyUI's interpreter, if
found); `errors[]` per port and endpoint; and `pinokio.available` plus
`pinokio.home` when Pinokio's optional scan completes within its short budget.
It probes up to eight extra loopback HTTP ports from running scripts'
`local.url` values; extra-port matches must have a ComfyUI-shaped
`/system_stats` response. A matching server includes its `pinokio_apps`.

If no ComfyUI is found and `pinokio.available` is true, ask the person to
start ComfyUI in Pinokio, then rescan. Its ComfyUI tree is
`<pinokio.home>/api/comfy.git/app`, Python is `<tree>/env/bin/python` (on
Windows, `<tree>\env\Scripts\python.exe`), and output is `<tree>/output`.
These paths come from `/pinokio/info`'s `home`, so save them in preferences
after a successful scan as usual.

- Saved setup and scan agree: carry on. They disagree (other folder, workflow
  gone, nothing on that port): rediscover, use what's true now, and update
  preferences after the next success.
- `Operation not permitted` / `Permission denied`: the agent's sandbox may
  have blocked loopback access. Retry the same command through the agent's
  normal permission-request flow; don't change permission defaults or tell
  the user to restart ComfyUI. A denied request means report the access block.
  Astrid CLI calls also need access to Runtime storage outside the sandbox;
  a `runtime_acquisition_lock_unavailable` there can need the same approval,
  rather than an Astrid reinstall.
- No server (connection refused or timed out): ComfyUI isn't running, or it's
  on another port. Ask them to start it, or for the port
  (`--ports 8188,8000,PORT`). ComfyUI Desktop's port varies.
- Several servers: ask which one.
- VibeComfy package missing: don't install it. It ships with Astrid, so tell
  the user to run the normal Astrid update in a terminal outside chat
  (`curl -fsSL https://astrid.haus/install | sh -s -- --update`; in PowerShell
  `& ([scriptblock]::Create((irm https://astrid.haus/install.ps1))) --update`).
  Never pip-install it, and never restart the Astrid service from chat: that
  kills this chat.

### Pick and read the workflow

Match the request to a saved workflow name from `workflows`. One clear match:
use it and say which. Several plausible ones: ask with the short list. None:
show the list.

Fetch it read-only into a scratch folder of your own (`$WORK` below), e.g.
`curl -s "http://127.0.0.1:8188/userdata/workflows%2Fportrait_flux.json" -o "$WORK/portrait_flux.json"`,
or copy it from `<base>/user/default/workflows/`. Never write into their
ComfyUI folders.

Read the JSON yourself before running. `vibecomfy.run` auto-detects a few
inputs, and the detection is rough:

- `prompt` binds to the first `CLIPTextEncode` by node id, which may be the
  negative encoder. Follow the links into the sampler's `positive` input to
  check.
- Flux (`CLIPTextEncodeFlux`) and SDXL (`CLIPTextEncodeSDXL`) encoders get no
  `prompt` at all.
- Only the first `LoadImage` (`image`) and the first seed (`seed`) are
  detected.

When detection fits, pass values in `workflow_inputs`. For anything else
(prompt on a Flux/SDXL or misdetected node, negative prompt, steps, size,
LoRA strength, a second sampler's seed), edit the widget values in a copy and
run the copy; don't also pass `prompt` if it would land on the wrong node. Each
tweak is a new file and a new content-addressed workflow, so history is kept.
An unknown `workflow_inputs` name fails before queueing and lists the valid
names, which also tells you what was detected.
Don't infer a missing node from its name alone: let `vibecomfy.run` check the
live server before queueing and record any failure in task history.

### Run, follow, stop

Use the selected project (editor context, or `python3 -m astrid projects current`).
Import the workflow file, and the image ZIP if any; the `digest` in each
response is what the run's file inputs take:

```sh
python3 -m astrid media import "$WORK/portrait_flux.json" --project PROJECT --json
```

Then admit `vibecomfy.run`. Astrid's own agent uses the `astrid_invoke` tool
with these fields; Claude Code, Codex and Hermes run
`python3 -m astrid.omp_invoke '<json>'` with Astrid's Python:

```json
{"capability_id": "vibecomfy.run", "kind": "executor", "project": "PROJECT",
 "inputs": {
   "workflow": "sha256:WORKFLOW_DIGEST",
   "workflow_inputs": "{\"prompt\": \"a moody portrait of a lighthouse keeper\"}",
   "comfy_url": "http://127.0.0.1:8188",
   "comfy_output_dir": "/abs/ComfyUI/output"}}
```

- `workflow_inputs` is a JSON object encoded as a string.
- `comfy_url` is loopback only (`http://127.0.0.1:PORT`); `comfy_output_dir`
  is the scan's absolute `output_dir`.
- `seed` defaults to a new random value on every run when the workflow has a
  detected seed, and the result records the one used. Pass `"seed": N` to
  repeat an image. If the seed isn't detected, set a fresh one in your copy
  each time, or "another one" can come back identical or cached.
- Workflows with several save nodes need a `filename_prefix` on each, or the
  run fails closed.

Admission returns the task id. If the shell tool reports a still-running
process/session, wait on that same handle; don't submit the invocation again.
For stills, follow in the same turn with a limit, and tell the user they can
watch it in their ComfyUI window:

```sh
python3 -m astrid tasks follow TASK_ID --project PROJECT --timeout-seconds 600
```

Video can take longer: when the limit passes, the task keeps running; check it
later with `python3 -m astrid tasks show TASK_ID --project PROJECT --json`. "Stop" or
"cancel" means `python3 -m astrid tasks cancel TASK_ID --project PROJECT`, which removes
only Astrid's prompt from their queue.

### Image inputs

Build the `managed_assets` ZIP, import it, and pass its digest as
`managed_assets`. The run uploads the images to their ComfyUI (input
subfolder `astrid/<task id>`) and binds the first `LoadImage`:

```sh
python3 -m astrid.packs.vibecomfy.local pack-images "$WORK/images.zip" /abs/ref.png
```

It prints `{"zip", "names"}`; `names` are the `LoadImage` values. A second
image needs its own `LoadImage` value set in your copy, pointing at a file
already in their ComfyUI input folder.

### Missing nodes and models

The run checks the live server before queueing. If something is missing it
fails with a list like `missing nodes: X (pack SLUG); missing models: FILE
(Loader.field, node N)`. Tell the user what's missing and what you'll install
in one message, and go ahead when they agree. Always install into their
ComfyUI, with their ComfyUI's Python (the scan's `python`; ask if it's null).
If the owning pack is unknown, offer to identify it before installing; don't
invent an install slug.

- Nodes, when `manager_present` is true: ask them to use ComfyUI → Manager →
  Install Missing Custom Nodes → Restart. Manager installs into the right
  Python.
- Nodes otherwise: point VibeComfy at their ComfyUI (safe to repeat), install
  the pack, install its requirements with their Python, then ask them to
  restart ComfyUI and tell you when it's back.
- Models: `vibecomfy fetch "$WORK/portrait_flux.json" --dry-run` shows what
  goes where; then run it again without `--dry-run`. After `config init`,
  files land in `<base>/models`. Models need no restart. If fetch doesn't know
  a file, tell the user its name and the folder it belongs in.

```sh
COMFYUI_PATH=/abs/ComfyUI vibecomfy config init --yes
vibecomfy nodes install SLUG --yes
/abs/ComfyUI/venv/bin/python -m pip install -r /abs/ComfyUI/custom_nodes/PACK_FOLDER/requirements.txt
vibecomfy fetch "$WORK/portrait_flux.json" --yes
```

In PowerShell, set `$env:COMFYUI_PATH = "C:\path\ComfyUI"` first; on the
portable build their Python is `python_embeded\python.exe`. If `vibecomfy`
isn't on PATH, run `python3 -m vibecomfy.cli` with Astrid's Python. Never
pip-install a pack's requirements into Astrid's own Python. When the user says
ComfyUI is back, health-check with `scan`, then rerun.

Never use `vibecomfy prepare`, `vibecomfy run --ensure-*` or
`vibecomfy nodes ensure` here: they install into the wrong folders.

### Show the result

The outputs settle as normal task outputs in the project: in Astrid App they
appear in chat and Project media; elsewhere name them from
`python3 -m astrid tasks show TASK_ID --project PROJECT --json`.
Report the seed and the time taken. Place results on the timeline only when
asked. "Another one" means the same workflow with a new seed; "same but X"
means edit the copy and pass the previous seed unless asked otherwise.

On Windows with NVIDIA this setup is untested, and Astrid may show a warning
saying so. Run anyway, say it once ("untested on Windows NVIDIA; please tell
us how it went, with the GPU and driver from `nvidia-smi`"), and record that
you said it in preferences so it isn't repeated. A missing or unrecognized GPU never
blocks: VibeComfy uses their ComfyUI's GPU, not Astrid's detection.

Wan2GP is manual for now: the user runs it themselves, then you import the
results from its output folder (usually `outputs/` in their Wan2GP folder)
with `python3 -m astrid media import /abs/Wan2GP/outputs/clip.mp4 --project PROJECT --json`.

### Remember the setup

After the first successful run, save the machine facts (URL, folders, their
ComfyUI's Python, the saved workflows that worked) to the user's preferences
without asking, and say so in one line ("I saved your ComfyUI setup to your
preferences, so next time is instant"). Update them when a health check finds
they've changed. Save taste (sizes, defaults, local versus cloud) only when
the user asks, or offer once ("Want 1280x720 as
your default from now on?") and save on yes. Store saved workflow names, not
absolute workflow paths. Never store secrets, tokens or API keys: preferences
go into model prompts.

Read, check out to a new file, edit, then check in. A conflict keeps your
file; check out again to another new file and merge:

```sh
python3 -m astrid preferences user
python3 -m astrid preferences checkout --scope user --file /tmp/astrid-prefs.md
python3 -m astrid preferences checkin /tmp/astrid-prefs.md
```

Keep one section in this shape; the "Defaults" and "Prefer" lines are taste,
written only when the user said so:

```markdown
## Local generation
- ComfyUI: http://127.0.0.1:8188 · folder /abs/ComfyUI · python /abs/ComfyUI/venv/bin/python
- Output folder: /abs/ComfyUI/output · models: /abs/ComfyUI/models (+ extra_model_paths.yaml)
- Workflows (saved names): portraits "portrait_flux"; motion "wan_i2v"; upscale "upscale_4x"
- How to run these: see "Run on your own ComfyUI" in packs/vibecomfy/docs/SKILL.md
- Defaults: 1280x720 finals, 768x432 previews; random seed unless I give one
- Prefer local; cloud only when I ask
- Updated: YYYY-MM-DD
```

On Windows NVIDIA add `- Windows NVIDIA untested note given: YYYY-MM-DD`.

## Workflow authoring

Use the rest of this pack for ComfyUI workflows that need graph inspection or
edits beyond the standard `generation` contracts, including LoRAs, IP-adapter,
ControlNet, custom samplers, and graph composition. The canonical bundle
contains three separate members: editable `workflow.py`, its
`workflow.vibe.json` revision and UI companion, and byte-identical original
`source.json`. Testers provisioning a Windows NVIDIA environment for Astrid's
own engine: see
[`docs/testing/vibecomfy-windows-provisioning.md`](../../../../docs/testing/vibecomfy-windows-provisioning.md).

## Executors

| Executor | Inputs and result |
|---|---|
| `vibecomfy.import` | Admitted raw `source` JSON plus a stable `workflow_id`; emits `python`, `companion`, `source`, and an origin `report`. |
| `vibecomfy.inspect` | One UI `workflow` JSON or the complete `python`/`companion`/`source` trio; emits only a read-only `projection` and `inspection`. Canonical Python requires `python_execution_consent="confirmed"`; UI JSON is inspected statically without it. |
| `vibecomfy.edit` | A canonical parent trio plus required `python_execution_consent="confirmed"`, and `operations` for one atomic typed batch or one separate `capture_python`/`capture_graph` candidate; emits the full successor trio and audited `report`. |
| `vibecomfy.validate` | A UI `workflow` JSON or the canonical trio; checks structure offline. Canonical Python requires `python_execution_consent="confirmed"`; UI JSON is validated statically without it. Runtime and target-schema validation are deferred to `vibecomfy.run`. |
| `vibecomfy.run` | A UI `workflow` JSON or the canonical trio; executes the workflow and settles its result artifacts. |

Canonical bundle members are immutable Astrid artifacts. Each edit or capture
consumes the exact prior trio and reports its `workflow_id`, `transition_kind`,
`parent_revision`, `parent_task_id`, and `origin_task_id`. Astrid's existing
task lifecycle records admission, completion/failure, and output digests. Read
the detailed import → inspect → edit/capture → validate → run journey in
[`docs/guides/cli-journeys.md`](../../../../docs/guides/cli-journeys.md).

`vibecomfy.inspect` is read-only: it does not create a revision or canonical
bundle. Its Python-like projection is for reading, never for mutation input.
`vibecomfy.edit` is the accepted mutation path. Typed edits and manual captures
produce a successor and a structured transition report. A ComfyUI canvas Apply
does not enter Astrid task history; use explicit project-bound capture to
record a candidate.

For canonical Python bundles, include the scalar input
`python_execution_consent: "confirmed"` in `spec.inputs` for inspect, edit,
and validate. It has no default, is validated as an exact literal, and must be
explicitly present on every task that loads canonical Python. The adapter maps
it to VibeComfy's existing audited non-interactive `--yes` GateContext; task
admission and `authority_context` do not stand in for this input. Inspect,
edit, and validation artifacts include the consent value and gate audit. UI
JSON inspection and validation stay on the static ingestion path and need no
consent. `vibecomfy.run` is a separate generation capability.

Canonical validation uses `--no-schema` and reports
`validation_mode="canonical_bundle_structural"`, with `runtime_validation.status`
set to `deferred` and its executor set to `vibecomfy.run`. It preserves canonical
bundle and structural graph checks without contacting or managing Comfy. Passing
this step does not establish runtime readiness: the run adapter must attest its
session and compile against fresh target schemas before queueing generation.

## Typed edit document

The `operations` input is JSON with one ordered list of leaf operations. The
executor lowers the list to one VibeComfy `edit_batch`, so any rejected leaf
rejects the whole batch and no successor artifacts are published.

```json
{
  "schema_version": 1,
  "expected_revision": 0,
  "ops": [
    {"op": "edit_node", "target": "ksampler", "field": "steps", "value": 24}
  ]
}
```

The supported leaf tools are `edit_node`, `add_node`, `remove_node`,
`upsert_link`, `remove_link`, and `set_node_mode`. Node targets may be rendered
bindings or stable UIDs. Explicitly named new-node UIDs can be referenced by a
later operation in the same batch. For field names, node classes, and tool
schemas, use the installed `vibecomfy node` command before editing.

For task invocation, place scalar ports under `spec.inputs`; name each
immutable file port in `spec.input_digests` and authorize its media object in
`input_manifest`. Keep the same `workflow_id`, `transition_kind`,
`parent_revision`, `parent_task_id`, and `origin_task_id` in the task spec so
the report and task history describe one lineage. Import, edit, capture,
validation, and execution are separate tasks when using Astrid. Do not pass a
`--project` option to an Astrid-native executor: its admitted task already
supplies project context.

## When not to use

- Own-ComfyUI requests ("my ComfyUI", "my saved workflow", local generation)
  belong in [Run on your own ComfyUI](#run-on-your-own-comfyui) above, not in
  `generation`.
- Use `generation.generate_image` for standard model-by-name image generation
  (cloud, Codex, or Astrid's built-in engine) when the user hasn't pointed at
  their own ComfyUI.
- Use `understanding` to explain existing media.
- Use `video_editing` to cut or render timelines.
