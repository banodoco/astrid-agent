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
*Ask your agent: set up my generation with the astrid-setup skill.*

## Launcher paths

Run every Astrid command, here and in the generation skill, through the
launcher. It needs no PATH entry. Code blocks use the Linux path; swap in the
one for the user's OS:

- Linux: `"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid"`
- macOS: `"$HOME/.local/share/astrid/run-astrid"`
- Windows PowerShell: `& "$env:LOCALAPPDATA\Astrid\run-astrid.cmd"`

A Pinokio-managed install keeps its launcher at a different root. Pinokio's
home is `~/pinokio` unless the scan's `pinokio.home` names another. If the
launcher is there, use it in place of the one above for every command:

- Pinokio, macOS and Linux: `"$HOME/pinokio"/api/*/app/astrid/run-astrid`
- Pinokio, Windows PowerShell: `"$env:USERPROFILE\pinokio\api\*\app\astrid\run-astrid.cmd"`

## 0. Work out the entry route

Take the first route that fits, and only that one:

1. **App brief**: the brief lists the setup choices and a command. Show those
   values back as "your choices from the App". Recommend keeping them.
   Ask: "Install with these choices, or change one?"
   If they keep them, run the brief's command exactly as given. If they change
   one, go to step 1. Then follow step 2's success and failure rules.
2. **Pinokio install**: there is a launcher at the Pinokio path (Launcher paths).
   Pinokio manages this install and its data. Never run the installer for it,
   and do not offer a second install. It is updated and repaired from the
   Pinokio app. Use that launcher for every command, and go to step 3.
3. **Fresh install**: there is no launcher at either path. Ask for consent
   once, unless the user already gave it. Recommend Astrid defaults.
   Then go to step 1.
4. **Existing install**: the launcher exists. Ask for consent, unless the user
   already approved this run. Recommend "keep your current settings".
   Do not guess saved choices. Tell the user: "Your saved setup choices
   stay as they are. Tell me if you want to change one." Do not show the choices
   list. Run the plain install command for their OS:
   `curl -fsSL https://astrid.haus/install | sh` on macOS or Linux, or
   `& ([scriptblock]::Create((irm https://astrid.haus/install.ps1)))` in
   Windows PowerShell. Add a flag only for a choice the user names, spelled as
   in step 2. `--yes` is not needed here: the installer asks for consent only on
   a fresh install. It repairs or restarts Astrid and keeps existing work. Then
   follow step 2's success and failure rules.

## 1. The choices

Show the user the setup choices before you run anything.
If there is an App brief, recommend keeping the brief's values.
For a fresh install without an App brief, recommend Astrid defaults.
Astrid defaults are:

- Start at sign-in: on (`--autostart on|off`)
- the `astrid` terminal command: on (`--command on|off`)
- Astrid skills: on (`--skills on|off`)
- skill copies: off (`--skill-copies on|off`)

To install only this skill (no Astrid, no Runtime), run one of these. It asks
which harness you use (Claude Code, Codex or Hermes) and copies the skill into
that harness's skills folder.

- macOS or Linux: `python3 -c 'import urllib.request; exec(urllib.request.urlopen("https://raw.githubusercontent.com/banodoco/astrid-agent/main/scripts/preinstall_setup_skill.py").read())'`
- Windows PowerShell: `python -c "import urllib.request; exec(urllib.request.urlopen('https://raw.githubusercontent.com/banodoco/astrid-agent/main/scripts/preinstall_setup_skill.py').read())"`

Ask whether they want to change any choice. A change replaces that one value.
With an approved App brief, keep the brief's other values. Settle the choices
before you install. Then go to step 2.

## 2. Install

Run one install command for the user's OS, after consent. Read the installer
first. The commands below are for a fresh install, or after the choices step only;
an existing install uses route 4 in step 0. They show Astrid defaults. Use the approved values from step 1 for every choice.
`--yes` records consent the user already gave. Never use it to skip the
consent question.

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
fallback from step 2. If the scan finds only Wan2GP, say Wan2GP support is
planned (Plan 12) and leave it alone.

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
generate?" On that yes, save the connection. This creates a project if there is
none. Undo it with `settings unset placement`:

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate settings set placement http://127.0.0.1:PORT
```

**First image.** Find the entry for slot `image.t2i` in the `defaults` output.
If its models are missing from their ComfyUI, show the download plan. The plan
writes nothing. It lists the files, their sizes, the folder and the total:

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate fetch-models ENTRY t2i --comfy-url URL
```

Stop and ask for one yes. On the person's one yes, rerun with the plan's digest.
This is the only write setup makes to their ComfyUI:

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate fetch-models ENTRY t2i --comfy-url URL --apply PLAN_SHA256
```

Then ask for a yes to the first image, and run it:

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate image --entry ENTRY --inputs-json /absolute/path/inputs.json --json
```

If they say no to the downloads, use one of their saved workflows instead
(fetched read-only; its saved seed is kept):

Make a work folder first, and percent-encode `WORKFLOW`, the workflow's scan
name: a space is `%20`, and a nested name keeps its `/` as `%2F`. In
PowerShell, make the folder as below and fetch with `curl.exe -fsS`, since
PowerShell 5.1 aliases `curl`:

```powershell
$WORK = New-Item -ItemType Directory -Path (Join-Path $env:TEMP ([guid]::NewGuid()))
curl.exe -fsS "http://127.0.0.1:PORT/userdata/workflows%2FWORKFLOW" -o "$WORK\workflow.json"
```

```sh
WORK=$(mktemp -d)
curl -fsS "http://127.0.0.1:PORT/userdata/workflows%2FWORKFLOW" -o "$WORK/workflow.json"
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate describe "$WORK/workflow.json" --placement URL
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate workflow "$WORK/workflow.json" --placement URL --inputs-json /absolute/path/inputs.json
```

A finished generation's JSON result has an `open_url`. Give that link once,
and say: the new image is in the gallery at the bottom of the editor (the
"Open Gallery" tab). If a node is missing, pass on what the error names: a core
node to update on an attached ComfyUI, or a custom node pack for the user to
install. Setup never installs nodes.

**Make it yours.** Follow the generation skill's
[Set up my generation](https://github.com/banodoco/astrid-agent/blob/main/astrid/packs/generation/docs/SKILL.md#set-up-my-generation)
step.

**No local GPU or ComfyUI?** On Linux or Windows, run this once. On macOS, run
`uname -m` instead:

```sh
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
```

A capable Linux or Windows computer has one NVIDIA GPU with at least 12 GB of
VRAM, or meets the declared requirement of the default generation, if there is
one. Do not add memory across GPUs. Report what you found, and say if capability
is unconfirmed. Then offer one of these, each on its own yes:

**Astrid's managed engine**, on a capable computer where it is available (see
[Placement](https://github.com/banodoco/astrid-agent/blob/main/astrid/packs/generation/docs/SKILL.md#placement)).
Check the plan first. This installs nothing:

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" engines install comfyui --dry-run
```

Run the following Linux command only on its own yes. It installs a new ComfyUI
on this computer and leaves the user's own one alone:

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" engines install comfyui
```

**RunPod** rents a GPU and costs money: hand over to the
[RunPod machine skill](https://github.com/banodoco/astrid-agent/blob/main/astrid/packs/runpod/docs/SKILL.md#runpod-generation-machines)
(installed name: `runpod`), and never ask for a key in chat.

The user can say no to both and set up generation later.

## Run everything on this computer

After the official install, get the editor's source from
[github.com/banodoco/astrid-app](https://github.com/banodoco/astrid-app). It needs
Git and Node.js 20.19 or a later 20.x release:

```sh
git clone https://github.com/banodoco/astrid-app.git
cd astrid-app
npx -y npm@11 ci
npm run dev:local
```

If a clone already exists, update it to `main` without discarding the user's work. `npm run dev:local` uses the
installed Runtime and opens the editor at
http://127.0.0.1:5180/tools/video-editor?connect=1, which should show Connected.
Ctrl+C stops it. For updates and the full commands, see the
[App README](https://github.com/banodoco/astrid-app#run-astrid-fully-on-your-computer).

When setup is done, give other agents the launcher path for their OS, and follow
[Astrid's core skill](https://github.com/banodoco/astrid-agent/blob/main/astrid/packs/_core/docs/SKILL.md).
Help the user enter credentials on their own computer; never ask for a secret
in chat.
