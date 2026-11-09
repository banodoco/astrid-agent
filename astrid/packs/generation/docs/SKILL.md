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
user's ComfyUI or its queue; never download or install anything during setup;
never read or print an API key; never write a change without that change's own yes.

1. **Read what is set.** `python3 -m astrid generate defaults --json` (each slot and
   where its value came from) and `python3 -m astrid generate targets --json` (the placement).
2. **Discover, read-only.** Scan for the user's ComfyUI, with its saved workflows:
   `python3 -m astrid.packs.vibecomfy.local scan`. Check provider keys by presence
   only (for example `test -n "$FAL_KEY" && echo set`). For Wan2GP, ask for the
   folder if the user knows it, then check it:
   `python3 -m astrid.packs.vibecomfy.local wan2gp /abs/Wan2GP`. Show one list of
   what was found, and what was not.
3. **Ask once which tool is primary**: their ComfyUI, Wan2GP, RunPod, or this
   computer's built-in engine. Save the placement only on a yes:
   `python3 -m astrid generate settings checkout --file "$WORK/generation.json"`, edit
   `placement`, then `python3 -m astrid generate settings checkin "$WORK/generation.json"`.
   For a ComfyUI, `placement` is `{"kind": "attached", "endpoint": "http://127.0.0.1:PORT"}`.
4. **One question per default slot.** Use `python3 -m astrid generate list --json`
   for readiness, and ask with the evidence: "you have X's models; set it as your
   image default?" Save each yes with
   `python3 -m astrid generate defaults set <kind> <mode> <entry>`.
5. **A checklist of the other saved workflows** from the scan. For each one the
   user ticks, preview it first, then save it as an entry:
   `python3 -m astrid generate workflow FILE --as-entry NAME --dry-run --json`
   shows the inputs the entry would declare and writes nothing.
   Saving with `--as-entry NAME` (without `--dry-run`) also queues one run, so
   ask before saving.
6. **Show how to run each one** (the commands in the sections below), then offer one
   test generation. Run it only on a yes.
7. **List every change and its undo.** Undo a placement with `settings checkout`
   and restore the old value; a pinned default with
   `python3 -m astrid generate defaults unset <kind> <mode>`. Say which changes were
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
- **`runpod`**: a rented RunPod GPU. Pass `--placement runpod`; it needs paid consent
  (`--consent paid`), see below.

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
python3 -m astrid generate workflow FILE --placement runpod --consent paid --inputs-json INPUTS.json
```

`INPUTS.json` is a JSON object of the workflow's declared inputs. List them with
`python3 -m astrid generate describe FILE --json`; a prompt is usually a node input such as
`node:20.text`. A reference image is passed with `--edit FILE`; it is shipped with the
run, so the workflow does not need a path on the target. Add `--dry-run` first on any placement.

To turn a workflow into a named entry the user can run again, use
`--as-entry NAME` (it validates the inputs as it saves). `--replace` overwrites an
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

- **Consent.** RunPod costs money. Each job that uses it needs `--consent paid`, and
  the budget cap (`--max-usd`) applies. Say what a job will cost before the yes.
- **Controller.** Astrid's controller starts the pod, drives the job and pulls the result
  back. The user's files are never uploaded except as the job's inputs.
- **Lease and warm reuse.** A pod stays warm after a job, for its idle timeout, so the
  next default job reuses it. The lease is state, not a setting.
- **A pod that dies.** If the provider reports it terminated, exited or missing, its
  lease ends with `ended: provider_terminated`, it stops being charged and leaves warm
  reuse. The next default job claims a fresh pod. A job pinned to a `machine` is
  refused with the reason and the live leases.
- **`astrid machines`** lists live and recently ended leases (id, kind, GPU, state, idle
  expiry, cost so far, end reason). `astrid machines release ID` ends one.
- **The volume.** An optional network volume keeps models between pods; the user chooses
  its name in the settings. Deleting it is the user's decision, never yours.
- **Which machine ran a job.** The task shows a machine column: the placement kind, the
  pod id or `local`, the lease id and the device.
- **Preferences.** GPU types, datacenter, image, volume, idle timeout and budget cap are
  placement policy under `machines.runpod` in the settings document.

## Provider APIs

Hosted provider APIs (fal, OpenAI, Wavespeed) are a separate route. They are not part of
this placement model until the providers follow-up lands, so do not present them as a
placement.

## Limits

- A workflow runs only where its models and nodes are present. Missing ones fail
  before queueing, with the list. Install them with the steps in the VibeComfy skill
  ([Missing nodes and models](../../vibecomfy/docs/SKILL.md#missing-nodes-and-models)).
- Never put a key, token or password in an input or a settings file.
