---
name: generation
description: >
  Generate images, video and audio with `generate(entry, inputs, placement)`: pick
  where it runs (this computer, the user's own ComfyUI, or RunPod), run a ComfyUI
  workflow file, and set up or change the user's generation defaults.
---

# Generation

One front door: `generate(entry, inputs, placement)`. The entry says what it makes
(image, video or audio); `placement` says where it runs. `image`, `video` and
`audio` are the same call with the kind pinned.

## Set up my generation

Run this section at first use, and whenever the user says "set up my
generation" or "connect my ComfyUI". Skip it when the user asked for one specific
generation and the defaults already work.

It only reads until a step asks the user. Never launch, restart or touch the
user's ComfyUI or its queue. The one download is a default's missing models, and
only on the user's yes in the astrid-setup skill's first-image step. Never read or
print an API key; never write a change without that change's own yes.

Astrid commands here use the launcher, the same form as the astrid-setup skill's
Launcher paths. The blocks show the Linux path; on macOS use
`$HOME/.local/share/astrid/run-astrid`, and on Windows
`& "$env:LOCALAPPDATA\Astrid\run-astrid.cmd"`.

1. **Read what is set.** `"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate defaults --json` (each slot and
   where its value came from) and `"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate targets --json` (the placement).
2. **Discover, read-only.** Scan for the user's ComfyUI, with its saved workflows:
   `python3 -m astrid.packs.vibecomfy.local scan`. Check provider keys by presence
   only (for example `test -n "$FAL_KEY" && echo set`). For Wan2GP, ask for the
   folder if the user knows it, then check it:
   `python3 -m astrid.packs.vibecomfy.local wan2gp /abs/Wan2GP`. Show one list of
   what was found, and what was not.
3. **Ask once which tool is primary**: their ComfyUI, Wan2GP, RunPod, or this
   computer's built-in engine. Save the placement only on a yes:
   `"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate settings checkout --file "$WORK/generation.json"`, edit
   `placement`, then `"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate settings checkin "$WORK/generation.json"`.
   For a ComfyUI, `placement` is `{"kind": "attached", "endpoint": "http://127.0.0.1:PORT"}`.
4. **One question per default slot, four answers.** Read readiness from
   `"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate list --json`, then ask with the evidence: "you have X's
   models; set it as your image default?" No answer runs anything. Each has one undo:
   - **Keep Astrid's**, with its readiness shown. Nothing is written. Undo: none needed.
   - **Use one of yours**. Show their saved workflows from the scan, most recently
     edited first (`workflows_detail`, by `modified`). On a yes, fetch the chosen one
     read-only to `$WORK/workflow.json`, then save and pin it below as `FILE`:
     `curl -fsS "http://127.0.0.1:PORT/userdata/workflows%2FWORKFLOW" -o "$WORK/workflow.json"`
     (`WORKFLOW` is its scan name; a nested name keeps its `/` as `%2F`). Undo:
     `"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate defaults unset KIND MODE`.
   - **Another shipped entry**, or leave the slot to the pack default. Pin one from
     `generate list --json` with `"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate defaults set KIND MODE ENTRY`,
     or drop the user's pin with `"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate defaults unset KIND MODE`.
     Undo: pin the entry step 1 showed, with `defaults set KIND MODE ENTRY` again.
   - **Create a new default when none of those fits**: a workflow of theirs that is a
     file on disk, not among the scan's. Save and pin it below as `FILE`. Undo:
     `"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate defaults unset KIND MODE`.
   - **Save and pin** (answers 2 and 4; `FILE` is the workflow, `NAME` the entry name).
     Preview first, which writes nothing:
     `"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate workflow FILE --as-entry NAME --dry-run --json`.
     On a yes, save, which queues nothing on any target:
     `"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate workflow FILE --as-entry NAME --no-run`.
     Then pin it: `"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate defaults set KIND MODE NAME`.
     The saved entry stays; delete `${VIBECOMFY_ENTRIES_DIR:-$HOME/.vibecomfy/entries}/NAME`
     only on a second yes.
5. **Show how to run each one** (the commands in the sections below), then offer one
   test generation. Run it only on a yes.
6. **List every change and its undo.** Undo a placement with `settings checkout`
   and restore the old value; a pinned default with
   `"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate defaults unset KIND MODE`. Say which changes were
   made and which were not.

A ComfyUI that is not running is a finding, not an error: say so, and ask the user
to start it.

## Generate

```python
from astrid.sdk import generate

img = generate.generate("image/flux2_klein_4b_t2i", {"node:20.text": "a lighthouse keeper"}, placement="local")
img.outputs["artifacts"]   # runtime artifact references
```

The CLI has the same shape:

```sh
python3 -m astrid generate list --json                  # entries and readiness
python3 -m astrid generate describe --entry ENTRY --json  # declared inputs, without checking readiness
python3 -m astrid generate image --entry image/flux2_klein_4b_t2i --inputs-json FILE --placement local --out outputs/
python3 -m astrid generate video --entry h3 --inputs-json FILE --dry-run
```

`--dry-run` prints the plan without running it. `--out` puts the result in a
folder you name; without it, files land in `./outputs/<task-id>/`.

## Placement

A placement is where a generation runs. Three kinds:

- **`local`**: this computer, through Astrid's managed ComfyUI. Optional `device` picks a GPU index.
  If that engine is not installed, the run reports the install and its download size.
  The download runs only on the user's yes; setup never starts it.
- **`attached`**: the user's own ComfyUI at a loopback address, given as the endpoint.
  Astrid never starts or stops it. Pass the address as `--placement http://127.0.0.1:8188`.
- **`runpod`**: a rented RunPod GPU. Pass `--placement runpod`; it uses an already claimed
  machine. Claims and paid limits are in the [RunPod machine skill](../../runpod/docs/SKILL.md#runpod-generation-machines).

Resolution, the same for every call: the request, then the user's default
placement, then the built-in default (`local`). `--placement auto` means "use the
default". Set the default in the generation settings, not per call, unless the user
asks for one place just this time.

Checking models: `--align check` (default) compares the workflow's models against
the target's inventory and refuses if one is missing. `--align trust` accepts the
workflow's declared models as present, unverified, and should be used only when the
user asks. `python3 -m astrid generate align --entry ENTRY` inspects locked managed
assets; `--apply --consent user_env` installs them and only with that explicit consent.

## Run a ComfyUI workflow file

A workflow file runs without any pack or entry:

```sh
python3 -m astrid generate workflow FILE --placement local --inputs-json INPUTS.json
python3 -m astrid generate workflow FILE --placement http://127.0.0.1:8188 --inputs-json INPUTS.json
```

For a rented GPU, claim a machine first in the [RunPod machine skill](../../runpod/docs/SKILL.md#runpod-generation-machines), then run the workflow with `--placement runpod`.

`INPUTS.json` is a JSON object of the workflow's declared inputs. List them with
`python3 -m astrid generate describe FILE --json`; a prompt is usually a node input such as
`node:20.text`. A reference image is passed with `--edit FILE`; it is shipped with the
run, so the workflow does not need a path on the target. Add `--dry-run` first on any placement.

To turn a workflow into a named entry the user can run again, use
`--as-entry NAME` (it validates the inputs as it saves). `--no-run` with it saves
without queueing a run, which is how setup saves. `--replace` overwrites an
entry with the same name.

Adding your own entry or shortcut is a pack contribution through the
`vibecomfy.workflows` point, not a file the user edits by hand. Declare the entry
there with its kind, modes and default slots; `astrid generate describe` then shows
it. Keep one contribution minimal: one workflow file, one entry, and a dry run that
passes.

## Defaults

Each `(kind, mode)` slot has one entry. Packs offer defaults as data
(`default_for`); the user's pin wins. Resolution: request, then user pin, then the
highest-priority pack default, then refused.

```sh
python3 -m astrid generate defaults --json            # every slot, its entry, and where it came from
python3 -m astrid generate defaults set image t2i flux-schnell
python3 -m astrid generate defaults unset image t2i
```

A pin naming an entry that does not exist, or lacks that kind and mode, is refused
and names the key.

## Settings document

Placement and defaults live in one document in the Runtime's user settings,
`generation`. It is editable as a file:

```sh
python3 -m astrid generate settings checkout --file "$WORK/generation.json"
python3 -m astrid generate settings checkin "$WORK/generation.json"
```

The shape is `{"placement": {...}, "defaults": {"<kind>.<mode>": {"pack": "...", "entry": "..."}}, "machines": {"runpod": {...}}}`.
A stale checkin is refused with both versions shown; check out again and merge.
Secrets are never stored there: the RunPod credential is a name, and the key lives
in the environment.

## RunPod

For RunPod setup, GPU and datacenter choices, capacity waiting, paid consent,
and the complete claim/keep/release workflow, use the
[RunPod machine skill](../../runpod/docs/SKILL.md#runpod-generation-machines)
(installed name: `runpod`). Start with `astrid machines setup` when the user
asks to configure RunPod; a missing preference refuses and never launches
setup automatically.

Each machine claim needs fresh paid consent. Generation uses a compatible
already-claimed machine within its existing paid limit; reuse cannot extend
that limit.
`astrid machines` shows the scheduled release time, minutes left, estimated
cost and hourly rate. `astrid machines keep` authorizes a separate bounded
extension, and `astrid machines release ID` ends the machine. Automatic
release only happens while its owning host is running.

An optional network volume keeps models between pods and is preserved on
release. The task's machine column records the placement kind, pod ID or
`local`, lease ID and device. A provider-terminated or missing pod leaves warm
reuse; a job pinned to that machine is refused with its reason and the live
leases.

## Provider APIs

Hosted provider APIs (fal, OpenAI, Wavespeed) are a separate route. They are not part of
this placement model until the providers follow-up lands, so do not present them as a
placement.

## Limits

- A workflow runs only where its models and nodes are present. Missing ones fail
  before queueing, with the list. Install them with the steps in the VibeComfy skill
  ([Missing nodes and models](../../vibecomfy/docs/SKILL.md#missing-nodes-and-models)).
- Never put a key, token or password in an input or a settings file.
