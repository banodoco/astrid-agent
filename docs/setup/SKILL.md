---
name: astrid-setup
description: Install or repair Astrid, check that it is connected, and make a first image with the user's own ComfyUI.
---
<!-- Generated from astrid/packs/setup/docs/SKILL.md; regenerate with python scripts/generate_setup_skill_mirror.py. -->


# Set up Astrid

Connect Astrid to the ComfyUI the user already runs, and make a first image in a
few minutes. Their ComfyUI stays as it is. The editor is
[astrid.haus](https://astrid.haus); to run everything on this computer instead,
use the last section. Report only the checks you ran. Never say a check passed
if it could not run.

Every way in ends with this sentence, word for word:
*Next: set up generation with the astrid-setup skill.*

## Launcher paths

Run every Astrid command, here and in the generation skill, through the
launcher. It needs no PATH entry. Code blocks use the Linux path; swap in the
one for the user's OS:

- Linux: `"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid"`
- macOS: `"$HOME/.local/share/astrid/run-astrid"`
- Windows PowerShell: `& "$env:LOCALAPPDATA\Astrid\run-astrid.cmd"`

A Pinokio-managed install keeps its launcher in its Pinokio app folder. Pinokio's
home is `$HOME/pinokio` (Windows: `$env:USERPROFILE\pinokio`) unless the scan's
`pinokio.home` names another. Set `PINOKIO_HOME` to that home, then find the
launcher with the command below. Pinokio chooses the app folder's name, so it can
hold spaces or quotes. Never assume a name, and quote every path:

- Pinokio, macOS and Linux: `"$PINOKIO_HOME"/api/*/app/astrid/run`
- Pinokio, Windows PowerShell: `"$env:PINOKIO_HOME\api\*\app\astrid\run.cmd"`

A launcher counts as Pinokio's only when the `install.json` beside it (in the
same `app/astrid` folder) has `"owner": "haus.astrid.runtime"` and
`"managed_by": "pinokio"`. Pinokio writes that record at install. The command
prints the launcher of each match, so use the one it prints.

```sh
for dir in "$PINOKIO_HOME"/api/*/app/astrid; do
  if [ -f "$dir/run" ] \
    && grep -Eq '"owner"[[:space:]]*:[[:space:]]*"haus.astrid.runtime"' "$dir/install.json" \
    && grep -Eq '"managed_by"[[:space:]]*:[[:space:]]*"pinokio"' "$dir/install.json"; then
    printf '%s\n' "$dir/run"
  fi
done
```

## 0. Work out the entry route

Take the first route that fits, and only that one:

1. **App brief**: the brief lists the setup choices and a command. Show those
   values back as "your choices from the App". Recommend keeping them.
   Ask: "Install with these choices, or change one?"
   If they keep them, run the brief's command exactly as given. If they change
   one, go to step 1. Then check the result as in step 2.
2. **Pinokio install**: there is a launcher at the Pinokio path (Launcher paths), and its
   `install.json` marks it as managed by Pinokio.
   Pinokio manages this install and its data. Never run the installer for it,
   and do not offer a second install. It is updated and repaired from the
   Pinokio app. Use that launcher for every command, and go to step 3.
3. **Fresh install**: there is no launcher at either path. Ask for consent
   once, unless the user already gave it. Recommend Astrid defaults.
   Then go to step 1.
4. **Existing install**: the launcher exists. Ask for consent, unless the user
   already approved this run. Recommend "keep your current settings".
   Keep the saved choices and say so: "Your saved setup choices stay as they
   are. Tell me if you want to change one." Run the plain line for their OS, with
   no `--yes`: `curl -fsSL https://astrid.haus/install | sh` on macOS or Linux,
   or `& ([scriptblock]::Create((irm https://astrid.haus/install.ps1)))` in
   Windows PowerShell. Add a flag only for a choice the user names, spelled as
   in step 2. It repairs or restarts Astrid and keeps existing work. Then check
   the result as in step 2.

## 1. The choices

Show the user the setup choices before you run anything.
Astrid defaults are:

- Start at sign-in: on (`--autostart on|off`)
- the `astrid` terminal command: on (`--command on|off`)
- Astrid skills: on (`--skills on|off`)
- skill copies: off (`--skill-copies on|off`)

Ask whether they want to change any choice. A change replaces that one value.
With an approved App brief, keep the brief's other values. Settle the choices
before you install. Then go to step 2.

## 2. Install

Read the installer first. The commands below are
for a fresh install, after step 1; an existing install uses route 4 in step 0.
They show Astrid defaults.
After consent, run the line for the user's OS with the values from step 1.
`--yes` records that consent. Never use it to skip the consent question.

**macOS or Linux:**

```sh
curl -fsSL https://astrid.haus/install | sh -s -- --yes --autostart on --command on --skills on --skill-copies off
```

**Windows PowerShell:**

```powershell
& ([scriptblock]::Create((irm https://astrid.haus/install.ps1))) --yes --autostart on --command on --skills on --skill-copies off
```

From another Windows shell, such as Git Bash, run:
`powershell -NoProfile -ExecutionPolicy Bypass -Command "& ([scriptblock]::Create((irm https://astrid.haus/install.ps1))) --yes --autostart on --command on --skills on --skill-copies off"`

Inside an MSIX-packaged Windows desktop app, install from a PowerShell window
opened from the Start menu. A shell the app starts can redirect `%LOCALAPPDATA%`,
and then Astrid's scheduled task cannot find the install.

Succeeds when the first line of output starts with `Astrid installed:`,
`Astrid updated:`, `Astrid is installed and running`, or `Astrid is already up to date`. Any other
first line, including `Astrid was NOT installed`, means it failed. On
success, go to step 3.

If the installer stops to ask for consent (exit code 2), show the user the plan
it prints. Run the rerun command it prints only after they agree.

If the install fails, read the log at the `Details:` path it prints. To get
help, rerun the same command with `--doctor` and follow
[Make a support bundle](https://github.com/banodoco/astrid-agent/blob/main/docs/setup/troubleshooting.md#make-a-support-bundle).
If a proxy blocks GitHub downloads, see
[Troubleshooting](https://github.com/banodoco/astrid-agent/blob/main/docs/setup/troubleshooting.md).

## 3. Connected

The installer checks Astrid and opens astrid.haus. If no browser opens, ask the
user to open https://astrid.haus. If Chrome or Edge asks to reach this computer,
they choose Allow. Confirm that astrid.haus shows Connected. If it does not, see
[Asking for help](https://github.com/banodoco/astrid-agent/blob/main/docs/setup/troubleshooting.md#asking-for-help).
To update later, see [Update Astrid](https://github.com/banodoco/astrid-agent/blob/main/docs/setup/troubleshooting.md#update-astrid).

## 4. Set up generation

Read first, and ask before each change. Never launch, restart or install
anything in the user's ComfyUI. The one thing setup adds to it is a default's
missing models, after the user says yes. If a read fails, use the `--doctor`
fallback from step 2.

**See what you have.** Run these read-only commands and report what they show:

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate defaults --json
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate targets --json
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate scan --json
```

`defaults` lists each slot with `ready` and `reason`. `targets` shows where
generation would run. The scan lists running ComfyUI servers and their saved
workflows, newest first. If their ComfyUI is not running, ask the user to start it.

**Use it.** If a ComfyUI is running, ask: "Use the ComfyUI at this address to
generate?" On that yes, save the connection with the command below. Saving it
creates no project; the first generation creates one if there is none. Undo it
with `settings unset placement`.

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate settings set placement http://127.0.0.1:PORT
```

**First image.** Find the entry for slot `image.t2i` in the `defaults` output.
If its models are missing from their ComfyUI, show the download plan. The plan
writes nothing. It lists the files, their sizes, the folder and the total:

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate fetch-models ENTRY t2i --comfy-url URL
```

Ask for one yes. On that yes, rerun with the plan's digest.
This is the only write setup makes to their ComfyUI:

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate fetch-models ENTRY t2i --comfy-url URL --apply PLAN_SHA256
```

Then ask for a yes to the first image, and run it:

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate image --entry ENTRY --inputs-json /absolute/path/inputs.json --json
```

If they say no to the downloads, use one of their saved workflows instead
(fetched read-only; its saved seed is kept), as in the generation skill's
[Set up my generation](https://github.com/banodoco/astrid-agent/blob/main/astrid/packs/generation/docs/SKILL.md#set-up-my-generation).

A finished generation's JSON result has an `open_url`. Give that link once,
and say: the new image is in the gallery: the image tab at the bottom of the
editor. If a node is missing, pass on what the error names: a core
node to update on an attached ComfyUI, or a custom node pack for the user to
install. Setup never installs nodes.

**Make it yours.** Follow the generation skill's
[Set up my generation](https://github.com/banodoco/astrid-agent/blob/main/astrid/packs/generation/docs/SKILL.md#set-up-my-generation)
step.

**No local GPU or ComfyUI?** Follow the generation skill's
[Placement](https://github.com/banodoco/astrid-agent/blob/main/astrid/packs/generation/docs/SKILL.md#placement): Astrid's managed engine, or a RunPod GPU
through the [RunPod machine skill](https://github.com/banodoco/astrid-agent/blob/main/astrid/packs/runpod/docs/SKILL.md#runpod-generation-machines).

## Run everything on this computer

After the official install, run the editor from source with `npm run dev:local`.
The [App README](https://github.com/banodoco/astrid-app#run-astrid-fully-on-your-computer)
has the complete commands, including updating a clone without discarding the user's work.

When setup is done, give other agents the launcher path for their OS, and follow
[Astrid's core skill](https://github.com/banodoco/astrid-agent/blob/main/astrid/packs/_core/docs/SKILL.md).
Help the user enter credentials on their own computer; never ask for a secret
in chat.
