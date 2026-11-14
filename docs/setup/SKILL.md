---
name: astrid-setup
description: Set up Astrid on a new or existing machine, verify its Runtime connection, and reach a first image from the user's own ComfyUI.
---
<!-- Generated from astrid/packs/setup/docs/SKILL.md; regenerate with python scripts/generate_setup_skill_mirror.py. -->


# Set up Astrid

Point Astrid at the ComfyUI the user already runs and get an image from it in a
few minutes, without changing it. Use [astrid.haus](https://astrid.haus) by
default; for everything on their computer, use the last section. Report actual
check results; never claim a check passed that could not be run.

Every door ends with this sentence, quoted exactly:
*Ask your agent: set up my generation with the astrid-setup skill.*

## Launcher paths

Every Astrid command here and in the generation skill runs through the stable
launcher, so no PATH entry is needed. Blocks show the Linux path; substitute:

- Linux: `"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid"`
- macOS: `"$HOME/.local/share/astrid/run-astrid"`
- Windows PowerShell: `& "$env:LOCALAPPDATA\Astrid\run-astrid.cmd"`

## 0. Work out the entry route

Check the routes in this order. Use the first one that fits, and only that one:

1. **App brief**: the brief lists the setup choices and a command. Show those
   values back as "your choices from the App". Recommend keeping them.
   Ask "Install with these choices, or change one?"
   On no change, run the brief's command exactly as given. If they change one,
   go to step 1. Then follow step 2's success and failure rules.
2. **Fresh install**: no launcher exists at its path above. Ask the user for
   consent once, unless they have already given it. Recommend Astrid defaults.
   Then go to step 1.
3. **Existing install**: the launcher exists. Ask the user for consent, unless
   they have already approved this run. Recommend "keep your current settings".
   Do not guess saved choices. Tell the user: "Your saved setup choices
   stay as they are. Tell me if you want to change one." Do not show the choices
   list. Run the install command for the user's OS with no choice flags:
   `curl -fsSL https://astrid.haus/install | sh` on macOS or Linux, or
   `& ([scriptblock]::Create((irm https://astrid.haus/install.ps1)))` in
   Windows PowerShell. Add a flag only for a choice the user names, in step 2's
   spelling. `--yes` is not needed here: the installer asks for consent only on
   a fresh install. It repairs or restarts Astrid and keeps existing work; then
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

To install only this skill first (no Astrid or Runtime), run one of these. It
prompts for Claude Code, Codex, or Hermes and writes one copy in that harness's
skills folder.

- macOS or Linux: `python3 -c 'import urllib.request; exec(urllib.request.urlopen("https://raw.githubusercontent.com/banodoco/astrid-agent/main/scripts/preinstall_setup_skill.py").read())'`
- Windows PowerShell: `python -c "import urllib.request; exec(urllib.request.urlopen('https://raw.githubusercontent.com/banodoco/astrid-agent/main/scripts/preinstall_setup_skill.py').read())"`

Ask whether they want to change any choice. A changed choice replaces that
value only; with an approved App brief, keep the brief's other values. Do not
install until the choices are settled. Then go to step 2.

## 2. Install

Run one install command for the user's OS, after consent. The full-flag commands
below are for a fresh install, or after the choices step only; existing installs
follow route 3 in step 0. They show Astrid defaults. Use the approved values from step 1 for every choice.
Read the installer first. `--yes` records consent
the user already gave; never use it to skip the consent question.

**macOS or Linux:**

```sh
curl -fsSL https://astrid.haus/install | sh -s -- --yes --autostart on --command on --skills on --skill-copies off
```

**Windows PowerShell:**

```powershell
& ([scriptblock]::Create((irm https://astrid.haus/install.ps1))) --yes --autostart on --command on --skills on --skill-copies off
```

In another Windows shell (such as Git Bash), after consent and with the
approved choice values:
`powershell -NoProfile -ExecutionPolicy Bypass -Command "& ([scriptblock]::Create((irm https://astrid.haus/install.ps1))) --yes --autostart on --command on --skills on --skill-copies off"`

Inside an MSIX-packaged Windows desktop app, install from a standalone
PowerShell window opened from the Start menu: one started by that app can
redirect `%LOCALAPPDATA%` where the Astrid scheduled task cannot find it.

Succeeds when the first line of output starts with `Astrid installed:`,
`Astrid updated:`, `Astrid is installed and running`, or `Astrid is already up to date`. Any other
first line, including `Astrid was NOT installed`, means the install failed. On
success, go to step 3.

If the installer stops because consent is required (exit code 2), show the
user the plan it prints. Run the printed rerun command only after the user
agrees.

If the install fails, read the log at the printed `Details:` path. For help,
rerun the install command with `--doctor` added, attach the bundle, and follow
[Troubleshooting](https://github.com/banodoco/astrid-agent/blob/main/docs/setup/troubleshooting.md#make-a-support-bundle).
Behind a proxy that blocks GitHub downloads, see
[Troubleshooting](https://github.com/banodoco/astrid-agent/blob/main/docs/setup/troubleshooting.md).

## 3. Connected

The installer checks Astrid, then opens astrid.haus; if no browser opens, ask the
user to open https://astrid.haus. If Chrome or Edge asks to reach this computer,
tell them to choose Allow. Confirm astrid.haus shows Connected. If not, follow
[Troubleshooting](https://github.com/banodoco/astrid-agent/blob/main/docs/setup/troubleshooting.md#asking-for-help).
For updates, see [Troubleshooting](https://github.com/banodoco/astrid-agent/blob/main/docs/setup/troubleshooting.md#update-astrid).

## 4. Set up generation

Read first, ask before each change. Never launch, restart or install anything
into the user's ComfyUI; its only download is a default's missing models, on the
user's yes. If a read fails, use step 2's `--doctor` fallback. If the scan finds
only Wan2GP, say Wan2GP support is planned in Plan 12 and leave it alone.

**See what you have.** Run these read-only commands and report the output:

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate defaults --json
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate targets --json
python3 -m astrid.packs.vibecomfy.local scan
```

`defaults` lists each slot with `ready` and `reason`; `targets` shows where
placement resolves. The scan (a pack module, not run by the launcher) lists
running ComfyUI servers and their saved workflows, newest first. A ComfyUI that
is not running is a finding: ask the user to start it.

**Use it.** If a ComfyUI is running, ask: "Use the ComfyUI at this address to
generate?" On that yes, save the connection (a project is created if none
exists). Undo with `settings unset placement`:

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate settings set placement http://127.0.0.1:PORT
```

**First image.** Read the entry for slot `image.t2i` from `defaults`. If its
models are missing on their ComfyUI, show the plan (writes nothing). It lists
files, sizes, folder and total:

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate fetch-models ENTRY t2i --comfy-url URL
```

Stop and ask for the person's one yes. On the person's one yes, rerun with the
plan's digest; this is the only write setup makes to their ComfyUI:

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate fetch-models ENTRY t2i --comfy-url URL --apply PLAN_SHA256
```

Then ask for a yes to the first image and run it:

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate image --entry ENTRY --inputs-json /absolute/path/inputs.json --json
```

If they decline the downloads, use their saved workflow instead (fetched
read-only; its saved seed is kept):

```sh
curl -fsS "http://127.0.0.1:PORT/userdata/workflows%2FWORKFLOW" -o "$WORK/workflow.json"
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate describe "$WORK/workflow.json" --placement URL
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate workflow "$WORK/workflow.json" --placement URL --inputs-json /absolute/path/inputs.json
```

A completed generation's JSON result carries `open_url`. Give that link once,
and say: the new image is in the gallery at the bottom of the editor (the
"Open Gallery" tab). For a missing node, say what the message names: a core node
to update on an attached ComfyUI, or a custom node's pack for the user to
install. Setup never installs nodes.

**Make it yours.** Follow the
[defaults step](https://github.com/banodoco/astrid-agent/blob/main/astrid/packs/generation/docs/SKILL.md#set-up-my-generation)
of the generation skill.

**No local GPU or ComfyUI?** On Linux or Windows run this once; on macOS run
`uname -m` instead:

```sh
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
```

A capable Linux or Windows computer has one NVIDIA GPU with at least 12 GB VRAM,
or meets the declared default generation requirement if available; never add GPU
memory together. Report what you found and whether capability is unconfirmed.
Then offer one of these, each on its own yes:

**Astrid's managed engine**, on a capable computer where available
([placement section](https://github.com/banodoco/astrid-agent/blob/main/astrid/packs/generation/docs/SKILL.md#placement)).
Check the plan first (installs nothing):

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" engines install comfyui --dry-run
```

Run the following Linux command only on its own yes. It installs ComfyUI on this
computer and never touches the user's existing one:

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" engines install comfyui
```

**RunPod** rents a GPU and costs money. Use the
[RunPod machine skill](https://github.com/banodoco/astrid-agent/blob/main/astrid/packs/runpod/docs/SKILL.md#runpod-generation-machines)
(installed name: `runpod`) for its key prompt, paid consent and release; setup
does not claim a machine. Never ask for a key in chat.

The user can decline either and leave generation setup for later.

## Run everything on this computer

After the official install, get the editor from
[github.com/banodoco/astrid-app](https://github.com/banodoco/astrid-app), with Git
and Node.js 20.19 or newer in the Node 20 line:

```sh
git clone https://github.com/banodoco/astrid-app.git
cd astrid-app
npx -y npm@11 ci
npm run dev:local
```

If a clone already exists, keep its changes and update to `main` without discarding the user's work. `npm run dev:local` uses the
installed Runtime and opens a source editor at
http://127.0.0.1:5180/tools/video-editor?connect=1, which should show Connected;
Ctrl+C stops it. The
[App README](https://github.com/banodoco/astrid-app#run-astrid-fully-on-your-computer)
has updates and the complete commands.

Once setup is done, give outside agents the launcher path for their OS and follow
[Astrid's core skill](https://github.com/banodoco/astrid-agent/blob/main/astrid/packs/_core/docs/SKILL.md).
Guide credential entry locally; never ask for a secret in chat.
