---
name: astrid-setup
description: Set up Astrid on a new or existing machine, verify its Runtime connection, and help the user begin.
---
<!-- Generated from astrid/packs/setup/docs/SKILL.md; regenerate with python scripts/generate_setup_skill_mirror.py. -->


# Set up Astrid

Help the user install Astrid on this computer and reach a first image. Use
[astrid.haus](https://astrid.haus) by default. If they choose to run everything
on their computer so they can change the editor itself, follow the fully local
path below. Both paths use the same installed Runtime and workspace.

The journey has five steps, in order: 0 entry route, 1 choices, 2 install,
3 connected, 4 generation.
Carry out requested checks, report their actual results, and do not claim a
check passed when it could not be run.

## Launcher paths

Every Astrid command uses the stable launcher's absolute path, so it needs no
PATH entry. Use the path for the user's OS:

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
   list. Run
   the install command for the user's OS with no choice flags:
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

Before Astrid is installed, offer to install this setup skill by itself. Ask
which harness they are using if it is not clear. The command prompts for
Claude Code, Codex, or Hermes and writes one copy in that harness's supported
skills folder. It also keeps a source under the user's data directory and
writes the exact `.astrid-managed-skill.json` ownership marker. If the target
already exists, the command preserves it and stops. This installs the skill
only; Astrid and its Runtime are not installed.

- macOS or Linux: `python3 -c 'import urllib.request; exec(urllib.request.urlopen("https://raw.githubusercontent.com/banodoco/astrid-agent/main/scripts/preinstall_setup_skill.py").read())'`
- Windows PowerShell: `python -c "import urllib.request; exec(urllib.request.urlopen('https://raw.githubusercontent.com/banodoco/astrid-agent/main/scripts/preinstall_setup_skill.py').read())"`

The current default sync composes this skill into Astrid's writable view and
preserves the preinstalled harness copy. It does not yet adopt or refresh that
copy automatically. After Astrid is installed, the user can select the setup
skill through Astrid's skills controls to have sync manage it. Unmanaged skill
folders are preserved. The command downloads only the setup skill source and
helper; it does not request credentials.

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
by that app can inherit its filesystem redirection. Files written under
`%LOCALAPPDATA%` may then land in the app's private cache, where the Astrid
scheduled task cannot find them.

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

## 3. Connected

The installer checks that Astrid is healthy. Then it tries to open astrid.haus
in the browser. If no browser opens, ask the user to open https://astrid.haus.
If Chrome or Edge asks to reach this computer, tell the user to choose Allow.
Confirm that astrid.haus shows Connected.
If it does not, follow [Troubleshooting](https://github.com/banodoco/astrid-agent/blob/main/docs/setup/troubleshooting.md#asking-for-help).

## 4. Set up generation

Use the **Set up my generation** section of the `generation` skill first
(installed name: `generation`;
[public copy](https://github.com/banodoco/astrid-agent/blob/main/astrid/packs/generation/docs/SKILL.md#set-up-my-generation)).
It discovers existing tools and settings, asks before each change, and reports
changes and undo. Keep its discovery and handoff; do not repeat its scan or
change its read-only rules. If it finds a working ComfyUI, continue that handoff.
If it finds only Wan2GP, explain that Plan 12 will support Wan2GP and continue
the handoff; do not install or alter it. If nothing is found, assess this
computer before offering a route below.

On Linux or Windows run this once:

```sh
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
```

Success is GPU names and memory. On macOS run:

```sh
uname -m
```

Success is an architecture. On failure use the installer's `--doctor` fallback
from step 2, report the result and treat capability as unconfirmed. A capable
Linux or Windows computer has one NVIDIA GPU with at least 12 GB VRAM, or meets
the declared default generation requirement if available. Do not add GPU
memory together. macOS has no managed engine.

On a capable computer, check the engine recipe and read-only plan. On Linux:

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" engines install comfyui --dry-run
```

On Windows, substitute an absolute Windows engine root:

```powershell
& "$env:LOCALAPPDATA\Astrid\run-astrid.cmd" engines install comfyui --platform windows-x86_64-cu130 --root ABSOLUTE_ENGINE_ROOT --dry-run
```

Each succeeds on exit 0 with a plan and disk check; otherwise use `--doctor`.
A recipe alone does not establish execution support: Windows execution is not
yet available. If a managed engine is available for this platform, show the
plan and disk requirement. Offer installation and run the following Linux
command only on its own yes; consent is an agent gate, not a CLI flag:

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" engines install comfyui
```

Success is exit 0 with installed status; otherwise use `--doctor`. Return to
the generation handoff to select placement and defaults. Never install into or
restart the user's existing ComfyUI. If capable but no managed engine is
available, point to the engine's own installer or offer RunPod.

If the computer is not capable or capability is unconfirmed, offer RunPod and
explain that it rents a GPU and costs money. Guide the user to enter their key
locally using the hidden prompt. Linux:

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" credential set runpod
```

macOS:

```sh
"$HOME/.local/share/astrid/run-astrid" credential set runpod
```

Windows PowerShell:

```powershell
& "$env:LOCALAPPDATA\Astrid\run-astrid.cmd" credential set runpod
```

Each succeeds on a successful setter result; otherwise use `--doctor`. Never
ask for the key in chat, a command argument, or shell history. Set up the
retained RunPod owner using [runpod.setup](https://github.com/banodoco/astrid-agent/blob/main/astrid/packs/runpod/docs/SKILL.md#replacing-preferences),
with the user's approved policy, positive `budget_cap_usd` ceiling and credential
reference, not the key. The policy needs the account reference, ordered GPU
types, an image pinned by digest and a positive cap; use the user's actual
choices rather than inventing them. It succeeds when the result is ok and
`target_operation.state` is `saved`; otherwise use `--doctor`. Saving preferences
does not authorize spending.

With a separate yes, set `placement` to `{"kind": "runpod"}` in the generation
settings, preserving the other settings. Substitute a safe absolute file path.
On Linux, checkout and checkin use respectively:

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate settings checkout --file /absolute/path/generation.json
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate settings checkin /absolute/path/generation.json
```

On macOS:

```sh
"$HOME/.local/share/astrid/run-astrid" generate settings checkout --file /absolute/path/generation.json
"$HOME/.local/share/astrid/run-astrid" generate settings checkin /absolute/path/generation.json
```

On Windows:

```powershell
& "$env:LOCALAPPDATA\Astrid\run-astrid.cmd" generate settings checkout --file C:\absolute\path\generation.json
& "$env:LOCALAPPDATA\Astrid\run-astrid.cmd" generate settings checkin C:\absolute\path\generation.json
```

Edit the checked-out file between the commands. Checkout succeeds on exit 0
with the file; checkin succeeds on exit 0. On failure use `--doctor`; on stale
checkin checkout again and merge. Return to the generation handoff for defaults
and undo.

Before each paid generation, obtain the current offer without claiming a pod.
On Linux:

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate describe --placement runpod --engine comfyui --json
```

On macOS:

```sh
"$HOME/.local/share/astrid/run-astrid" generate describe --placement runpod --engine comfyui --json
```

On Windows:

```powershell
& "$env:LOCALAPPDATA\Astrid\run-astrid.cmd" generate describe --placement runpod --engine comfyui --json
```

Success is exit 0 with an offer and `offer_digest`; otherwise use `--doctor`.
Disclose the account, offer, rate, image, optional volume, estimated cost,
preparation, execution and idle times, cap and absolute UTC deadline. Obtain a
fresh paid consent for that exact offer and bounds; changed prices or offers
need another yes. Suggest a $2 cap within the saved ceiling. Substitute the
approved entry, input file, digest, future UTC deadline and time bounds below;
validate the entry and inputs first. Linux:

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" generate image --entry ENTRY --inputs-json /absolute/path/inputs.json --placement runpod --consent paid --max-usd 2 --deadline FUTURE_UTC_DEADLINE --offer-digest OFFER_DIGEST --preparation-seconds PREPARATION_SECONDS --execution-seconds EXECUTION_SECONDS --idle-seconds IDLE_SECONDS --json
```

macOS:

```sh
"$HOME/.local/share/astrid/run-astrid" generate image --entry ENTRY --inputs-json /absolute/path/inputs.json --placement runpod --consent paid --max-usd 2 --deadline FUTURE_UTC_DEADLINE --offer-digest OFFER_DIGEST --preparation-seconds PREPARATION_SECONDS --execution-seconds EXECUTION_SECONDS --idle-seconds IDLE_SECONDS --json
```

Windows:

```powershell
& "$env:LOCALAPPDATA\Astrid\run-astrid.cmd" generate image --entry ENTRY --inputs-json C:\absolute\path\inputs.json --placement runpod --consent paid --max-usd 2 --deadline FUTURE_UTC_DEADLINE --offer-digest OFFER_DIGEST --preparation-seconds PREPARATION_SECONDS --execution-seconds EXECUTION_SECONDS --idle-seconds IDLE_SECONDS --json
```

Success requires a successful generation result and delivered artifact, not
merely a pod claim; on failure use `--doctor`. Afterward list leases and, unless
the user chooses to keep it warm, release the owned lease by its ID. Linux:

```sh
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" machines --json
"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" machines release ID --json
```

macOS:

```sh
"$HOME/.local/share/astrid/run-astrid" machines --json
"$HOME/.local/share/astrid/run-astrid" machines release ID --json
```

Windows:

```powershell
& "$env:LOCALAPPDATA\Astrid\run-astrid.cmd" machines --json
& "$env:LOCALAPPDATA\Astrid\run-astrid.cmd" machines release ID --json
```

Listing succeeds on exit 0 with the lease list; release succeeds on exit 0 and
an ok result. Verify the lease ended and follow the RunPod skill's independent
provider cleanup checks; use `--doctor` on failure. Never create a pod during
setup without paid consent.

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

If a clone already exists, keep its changes and update to `main` without
discarding work. The supported start command is `npm run dev:local`. It uses
the installed Runtime, checks compatibility, and starts a source editor at
http://127.0.0.1:5180/tools/video-editor?connect=1. Open that address in the browser and
confirm it shows Connected. No browser local-network permission is needed;
the local dev server proxies requests and holds the credential. The hosted
door stays available for astrid.haus. The installer may have opened that site;
finish this path in the local editor.

Tell the user where the clone lives. Source edits appear through Vite's normal
development workflow. To start later, run `npm run dev:local` in the clone.
Ctrl+C stops the editor, its proxy, and any Runtime it started; a Runtime it
attached to keeps running. To update the editor, stop it, save or commit local
changes, then run `git pull --ff-only origin main`, `npx -y npm@11 ci`, and
`npm run dev:local`. Resolve Git conflicts without discarding the user's work.
Update the Runtime and agent through the official installer with `--update`,
then restart the editor. Follow the
[App README](https://github.com/banodoco/astrid-app#run-astrid-fully-on-your-computer)
for the complete commands. Report actual verification results, including any
check you could not run.

## Repair and next steps

If the installer fails, read the log at the printed `Details:` path. For help,
run the same install line with `--doctor`, attach the bundle, and follow
[Troubleshooting](https://github.com/banodoco/astrid-agent/blob/main/docs/setup/troubleshooting.md#asking-for-help). For updates, see
[Troubleshooting](https://github.com/banodoco/astrid-agent/blob/main/docs/setup/troubleshooting.md#update-astrid).

Once setup is complete, give outside agents the launcher path for their OS
listed above. Then follow [Astrid’s core skill](https://github.com/banodoco/astrid-agent/blob/main/astrid/packs/_core/docs/SKILL.md) and
the relevant pack for the work they want to do. Guide any provider credential
entry locally; never ask the user to paste a secret into chat.
