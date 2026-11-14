---
name: astrid-setup
description: Set up Astrid on a new or existing machine, verify its Runtime connection, and reach a first image from the user's own ComfyUI.
---

# Set up Astrid

Point Astrid at the ComfyUI the user already runs and get an image from it in a
few minutes, without changing anything on it. Use [astrid.haus](https://astrid.haus)
by default. If the user wants everything on their computer, use the last section.

Every door ends with this sentence, quoted exactly:
*Ask your agent: set up my generation with the astrid-setup skill.*

The journey has five steps, in order: 0 entry route, 1 choices, 2 install,
3 connected, 4 set up generation. Carry out requested checks, report their
actual results, and do not claim a check passed when it could not be run.

## Launcher paths

Astrid's commands run through its stable launcher, so no PATH entry is needed.
Every command in this skill and in the generation skill uses the launcher for the
user's OS. The blocks below show the Linux path; on macOS and Windows substitute:

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
   Windows PowerShell. Add a choice flag only for a choice the user names,
   using step 2's spelling. `--yes` is not needed here: the installer asks for
   consent only on a fresh install. The install command will repair or restart
   Astrid and keep existing work, then follow step 2's success and failure
   rules.

## 1. The choices

Show the user the setup choices before you run anything.
If there is an App brief, recommend keeping the brief's values.
For a fresh install without an App brief, recommend Astrid defaults.
Astrid defaults are:

- Start at sign-in: on (`--autostart on|off`)
- the `astrid` terminal command: on (`--command on|off`)
- Astrid skills: on (`--skills on|off`)
- skill copies: off (`--skill-copies on|off`)

Ask which harness they are using if it is not clear. The setup skill can be
installed by itself before Astrid, with the command below. It prompts for Claude
Code, Codex, or Hermes, writes one copy in that harness's skills folder, and
installs the skill only. Astrid and its Runtime are not installed.

- macOS or Linux: `python3 -c 'import urllib.request; exec(urllib.request.urlopen("https://raw.githubusercontent.com/banodoco/astrid-agent/main/scripts/preinstall_setup_skill.py").read())'`
- Windows PowerShell: `python -c "import urllib.request; exec(urllib.request.urlopen('https://raw.githubusercontent.com/banodoco/astrid-agent/main/scripts/preinstall_setup_skill.py').read())"`

Ask the user whether they want to change any choice. If they change one, use
its new value in step 2. If the user approved an App brief, keep the brief's values,
and when the user changes one, use the new value for that choice only. Do not run the install until the choices are settled. Then
go to step 2.

## 2. Install

Run one install command for the user's OS, after consent. The full-flag commands below are for a fresh install, or after the choices step only. Existing installs follow route 3 in step 0. The commands use Astrid defaults. Use the approved values from step 1 for every choice. On a
new machine, the command installs Astrid. On an existing install, it repairs
or restarts Astrid and keeps existing work. Open Terminal on macOS or Linux,
or PowerShell on Windows. Read the installer before you run it. `--yes`
records consent the user already gave. Never use `--yes` to skip the consent
question.

**macOS or Linux:**

```sh
curl -fsSL https://astrid.haus/install | sh -s -- --yes --autostart on --command on --skills on --skill-copies off
```

**Windows PowerShell:**

```powershell
& ([scriptblock]::Create((irm https://astrid.haus/install.ps1))) --yes --autostart on --command on --skills on --skill-copies off
```

If you use another shell on Windows (such as Git Bash), run this command
after consent, with the user's approved choice values:
`powershell -NoProfile -ExecutionPolicy Bypass -Command "& ([scriptblock]::Create((irm https://astrid.haus/install.ps1))) --yes --autostart on --command on --skills on --skill-copies off"`

If you run inside an MSIX-packaged Windows desktop app, install from a
standalone PowerShell window opened from the Start menu. A PowerShell started
by that app can inherit its filesystem redirection, so files written under
`%LOCALAPPDATA%` may land where the Astrid scheduled task cannot find them.

Succeeds when the first line of output starts with `Astrid installed:`,
`Astrid updated:`, `Astrid is installed and running`, or `Astrid is already up to date`. Any other
first line, including `Astrid was NOT installed`, means the install failed. On
success, go to step 3.

If the installer stops because consent is required (exit code 2), show the
user the plan it prints. Run the printed rerun command only after the user
agrees.

If the install fails, read the log at the printed `Details:` path. To get help,
run the install command for the user's OS with `--doctor` added. Attach the
bundle it makes, and follow [Troubleshooting](https://github.com/banodoco/astrid-agent/blob/main/docs/setup/troubleshooting.md#make-a-support-bundle).
If the install line fails behind a proxy that blocks GitHub downloads, see
[Troubleshooting](https://github.com/banodoco/astrid-agent/blob/main/docs/setup/troubleshooting.md).

## 3. Connected

The installer checks that Astrid is healthy. Then it tries to open astrid.haus
in the browser. If no browser opens, ask the user to open https://astrid.haus.
If Chrome or Edge asks to reach this computer, tell the user to choose Allow.
Confirm that astrid.haus shows Connected.
If it does not, follow [Troubleshooting](https://github.com/banodoco/astrid-agent/blob/main/docs/setup/troubleshooting.md#asking-for-help).

## 4. Set up generation

Setup reads first and asks before each change. Never launch, restart or install
anything into the user's ComfyUI. Its only download is a default's missing models,
and only on the user's yes. If a read fails, use the `--doctor` fallback from step 2.

If the scan finds a working ComfyUI, tell the user it can use that installation.
If it finds only Wan2GP, tell the user that Wan2GP support is planned in Plan 12,
and continue without installing or altering Wan2GP.

**See what you have.** Run these read-only commands and report what they show:

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate defaults --json
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate targets --json
python3 -m astrid.packs.vibecomfy.local scan
```

`defaults` lists each slot with `ready` and `reason`, and `placement` shows
where it resolves. The scan lists running ComfyUI servers and their saved
workflows, newest first. The scan is a pack module, so it is not an Astrid
command and the launcher does not run it. Success is exit 0 with that output.
A ComfyUI that is not running is a finding: say so, and ask the user to start it.

**Use it.** If a ComfyUI is running, ask: "Use the ComfyUI at this address to
generate?" On that yes, save the connection. A project is created for them if
none exists, and generation prints one line saying so:

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate settings set placement http://127.0.0.1:PORT
```

Undo it with `"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate settings unset placement`.

**First image.** Astrid's default image is the first success. Read its entry
from `defaults` (slot `image.t2i`). If its models are missing on their ComfyUI,
show the plan. This writes nothing:

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate fetch-models ENTRY t2i --comfy-url URL
```

The plan lists the files, sizes, folder and total. On the person's one yes,
run the same command with the plan's digest. This is the only write setup makes
to their ComfyUI:

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate fetch-models ENTRY t2i --comfy-url URL --apply PLAN_SHA256
```

Then ask for a yes to the first image and run it with inputs from a file:

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate image --entry ENTRY --inputs-json /absolute/path/inputs.json --json
```

If the user declines the downloads, use their own saved workflow instead. Fetch
it read-only, describe it, then run it; its fixed saved seed is kept:

```sh
curl -fsS "http://127.0.0.1:PORT/userdata/workflows%2FWORKFLOW" -o "$WORK/workflow.json"
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate describe "$WORK/workflow.json" --placement URL
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate workflow "$WORK/workflow.json" --placement URL --inputs-json /absolute/path/inputs.json
```

A completed generation's JSON result carries `open_url`. Give that link once,
and say: the new image is in the gallery at the bottom of the editor (the
"Open Gallery" tab). If a workflow reports a missing node, say what the message names: an attached
ComfyUI names a core node to update, and a custom node names its pack for the
user to install. Setup never installs nodes.

**Make it yours.** Each default slot gets one question: keep Astrid's, use one
of theirs, pick another, or create a new one from their workflow. Follow the
[defaults step](https://github.com/banodoco/astrid-agent/blob/main/astrid/packs/generation/docs/SKILL.md#set-up-my-generation)
of the generation skill; do not repeat it here.

**No local GPU or ComfyUI?** Check the hardware. On Linux or Windows run this
once, and on macOS run `uname -m` instead:

```sh
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
```

A capable Linux or Windows computer has one NVIDIA GPU with at least 12 GB VRAM,
or meets the declared default generation requirement if available. Do not add
GPU memory together. Report the GPU and memory or architecture found, and say
whether capability is still unconfirmed. Then offer one of these, each on its
own yes:

**Astrid's managed engine**, on a capable computer where it is available: see the
[placement section](https://github.com/banodoco/astrid-agent/blob/main/astrid/packs/generation/docs/SKILL.md#placement).
Check the plan first, which installs nothing:

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" engines install comfyui --dry-run
```

Run the following Linux command only on its own yes. It installs ComfyUI on this
computer; it never touches the user's existing ComfyUI:

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" engines install comfyui
```

**RunPod**, which rents a GPU and costs money. Set it up with the
[RunPod machine skill](https://github.com/banodoco/astrid-agent/blob/main/astrid/packs/runpod/docs/SKILL.md#runpod-generation-machines)
(installed name: `runpod`). Its key prompt, preferences, paid consent and
release steps are there; setup does not claim a machine. Never ask for a key in
chat.

The user can decline either and leave generation setup for later.

## Run everything on this computer

After the official install, get the editor from
[github.com/banodoco/astrid-app](https://github.com/banodoco/astrid-app).
Use Git and Node.js 20.19 or newer in the Node 20 release line:

```sh
git clone https://github.com/banodoco/astrid-app.git
cd astrid-app
npx -y npm@11 ci
npm run dev:local
```

If a clone already exists, keep its changes and update to `main` without discarding the user's work. The start command `npm run dev:local` uses the
installed Runtime and opens a source editor at
http://127.0.0.1:5180/tools/video-editor?connect=1, which should show Connected.
Ctrl+C stops the editor and its proxy. Follow the
[App README](https://github.com/banodoco/astrid-app#run-astrid-fully-on-your-computer)
for updates and the complete commands. Report actual verification results.

## Repair and next steps

If the installer fails, read the log at the printed `Details:` path. For help,
run the same install line with `--doctor`, attach the bundle, and follow
[Troubleshooting](https://github.com/banodoco/astrid-agent/blob/main/docs/setup/troubleshooting.md#asking-for-help). For updates, see
[Troubleshooting](https://github.com/banodoco/astrid-agent/blob/main/docs/setup/troubleshooting.md#update-astrid).

Once setup is complete, give outside agents the launcher path for their OS
listed above. Then follow [Astrid's core skill](https://github.com/banodoco/astrid-agent/blob/main/astrid/packs/_core/docs/SKILL.md) and
the relevant pack for the work they want to do. Guide any provider credential
entry locally; never ask the user to paste a secret into chat.
