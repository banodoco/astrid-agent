---
name: vibecomfy
description: >
  Run the user's own ComfyUI and saved workflows on their machine (local
  generation), and import, inspect, edit, validate, and run ComfyUI workflows
  through VibeComfy's canonical Python/companion/source bundle and Astrid task
  history.
---

# VibeComfy

## Generate discovery

Use the shared Generate commands to list and inspect registered VibeComfy
catalog, workflow, and action entries. Authoring operations (`import`, `inspect`,
`edit`, and `validate`) support workflow preparation but are not Generate entries
by themselves. `run` executes a selected workflow; it does not make every pack
action or workflow an entry automatically.

```sh
astrid generate list --json
astrid generate describe --entry vibecomfy.video_enhance --json
astrid generate run --entry vibecomfy.video_enhance --inputs-json FILE
```

Use the selected entry's `describe` output to prepare `FILE`. The SDK invocation
routes documented below remain available as compatibility conveniences.

## Run on your own ComfyUI

Use this when the user wants to generate with their own ComfyUI: "use my
ComfyUI", "with my portrait workflow", "my saved workflow", "on my GPU". Their
ComfyUI is already running; Astrid attaches to it as the `attached` placement.
You never launch, restart or manage it, never unload its models, and never
touch its queue beyond your own prompt. No API key is involved. First-time
setup (finding it and choosing defaults) is the generation skill's
[Set up my generation](../../generation/docs/SKILL.md#set-up-my-generation).

The flow is: find their ComfyUI → pick a saved workflow → read it → run it on
`attached` → follow → show the result. Ask the user only on real ambiguity, and
before installing anything.

### Find their ComfyUI

If the generation settings already name a ComfyUI address, check it with one
scan. Otherwise discover. Both use the same read-only helper, run with Astrid's
Python (the one that runs `python3 -m astrid`):

```sh
python3 -m astrid.packs.vibecomfy.local scan                 # ports 8188 and 8000
python3 -m astrid.packs.vibecomfy.local scan --ports 8188    # health-check a saved address
```

It reads `/system_stats`, `/internal/folder_paths` (model folders and
`custom_nodes`, whose parent is the ComfyUI base) and `/userdata?dir=workflows`,
and prints one JSON object: `servers[]` with `url`, `base_dir`, `output_dir`
(`--output-directory` if set, else `<base>/output`), `workflows` (saved
names), `manager_present` and `python` (their ComfyUI's interpreter, if
found); `errors[]` per port and endpoint; and `pinokio.available` plus
`pinokio.home` when Pinokio's optional scan completes within its short budget.
A matching server includes its `pinokio_apps`. The scan only reads; it never
starts or changes anything.

If no ComfyUI is found and `pinokio.available` is true, ask the person to
start ComfyUI in Pinokio, then rescan. Its ComfyUI tree is
`<pinokio.home>/api/comfy.git/app`, Python is `<tree>/env/bin/python` (on
Windows, `<tree>\env\Scripts\python.exe`), and output is `<tree>/output`.

- Saved setup and scan agree: carry on. They disagree (other address, workflow
  gone, nothing on that port): rediscover, use what is true now, and update
  the settings after the next success.
- `Operation not permitted` / `Permission denied`: the agent's sandbox may
  have blocked loopback access. Retry the same command through the agent's
  normal permission-request flow; don't change permission defaults or tell
  the user to restart ComfyUI. A denied request means report the access block.
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

Read the JSON yourself before running, and check the inputs you pass land on
the right nodes. Follow the links into the sampler's `positive` input to see
which encoder a prompt reaches. Flux (`CLIPTextEncodeFlux`) and SDXL
(`CLIPTextEncodeSDXL`) encoders take no `prompt` by default.

Describe the workflow to see the inputs it declares, then pass values:

```sh
python3 -m astrid generate describe "$WORK/portrait_flux.json" --json
```

For anything the declared inputs don't cover (negative prompt, steps, size, LoRA
strength, a second sampler's seed), edit the widget values in a copy and run the
copy; don't also pass `prompt` if it would land on the wrong node. Each tweak is
a new file and a new content-addressed workflow, so history is kept. An unknown
input name fails before queueing and lists the valid names.
Don't infer a missing node from its name alone: let the run check the live
server before queueing and record any failure in task history.

### Run, follow, stop

Run the workflow file on the attached ComfyUI. Give the address as the
placement; the address must be loopback (`http://127.0.0.1:PORT`):

```sh
python3 -m astrid generate workflow "$WORK/portrait_flux.json" \
  --placement http://127.0.0.1:8188 --inputs-json "$WORK/inputs.json" \
  --project PROJECT --json
```

`inputs.json` is a JSON object such as `{"prompt": "a moody portrait of a lighthouse keeper"}`.
Add `--dry-run` to see the plan without queueing. The command prints the task
id and, unless `--no-wait` is given, waits for the result.

For stills, follow in the same turn with a limit, and tell the user they can
watch it in their ComfyUI window:

```sh
python3 -m astrid tasks follow TASK_ID --project PROJECT --timeout-seconds 600
```

Video can take longer: when the limit passes, the task keeps running; check it
later with `python3 -m astrid tasks show TASK_ID --project PROJECT --json`. "Stop" or
"cancel" means `python3 -m astrid tasks cancel TASK_ID --project PROJECT`, which removes
only Astrid's prompt from their queue.

Set `seed` in `inputs.json` to repeat an image. Without it, the result records
the seed it used; for "another one", set a fresh seed each time, or the run can
come back identical or cached. Workflows with several save nodes need a
`filename_prefix` on each, or the run fails closed.

### Image inputs

Pass a reference image with `--edit FILE`. The run uploads it to their ComfyUI
and binds the first `LoadImage`. A second image needs its own input name
declared by the workflow; check `generate describe` for the name.

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
you said it in the generation settings so it isn't repeated. A missing or
unrecognized GPU never blocks: VibeComfy uses their ComfyUI's GPU, not Astrid's
detection.

Wan2GP is manual: the user runs it themselves, then you import its results. To
check a Wan2GP folder without running anything, use
`python3 -m astrid.packs.vibecomfy.local wan2gp /abs/Wan2GP` (it reports whether
`wgp.py` is there and where `outputs/` is). Then import a result with
`python3 -m astrid media import /abs/Wan2GP/outputs/clip.mp4 --project PROJECT --json`.

### Remember the setup

After the first successful run, save the machine facts (the ComfyUI address as
the `attached` placement, its folders, their ComfyUI's Python, the saved
workflow names that worked) to the generation settings document without asking,
and say so in one line ("I saved your ComfyUI setup, so next time is instant").
Update them when a health check finds they've changed. Save taste (sizes,
defaults, local versus cloud) only when the user asks, or offer once and save
on yes. Store saved workflow names, not absolute workflow paths. Never store
secrets, tokens or API keys.

The settings document is edited as a file, the same way as preferences:

```sh
python3 -m astrid generate settings checkout --file "$WORK/generation.json"
python3 -m astrid generate settings checkin "$WORK/generation.json"
```

A conflict is refused with both versions shown; check out again and merge.
The shape is `{"placement": {"kind": "attached", "endpoint": "http://127.0.0.1:8188"},
"defaults": {...}}`, and the generation skill's Set up section explains each key.

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
- Use the `generation` skill (`generate(entry, inputs, placement)`) for standard
  entry-based generation when the user hasn't pointed at their own ComfyUI.
- Use `understanding` to explain existing media.
- Use `video_editing` to cut or render timelines.
