---
name: astrid-setup
description: Set up Astrid on a new or existing machine, verify its Runtime connection, and help the user begin.
---

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
bundle it makes, and follow [Troubleshooting](troubleshooting.md#make-a-support-bundle).

## 3. Connected

The installer checks that Astrid is healthy. Then it tries to open astrid.haus
in the browser. If no browser opens, ask the user to open https://astrid.haus.
If Chrome or Edge asks to reach this computer, tell the user to choose Allow.
Confirm that astrid.haus shows Connected.
If it does not, follow [Troubleshooting](troubleshooting.md#asking-for-help).

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

On Linux or Windows run `nvidia-smi --query-gpu=name,memory.total --format=csv,noheader`
once. It succeeds when it returns GPU names and memory; an unavailable command
or unusable output means capability is unconfirmed. On macOS run `uname -m`
(in Terminal); it succeeds when it returns the architecture. macOS has no
Astrid managed engine. A capable Linux or Windows computer has an NVIDIA GPU
with at least 12 GB VRAM, or meets the declared default generation requirement
if one is available. Do not add GPU memory together. Treat a failed probe as
unconfirmed, not a capable GPU. For command failures use the installer's
`--doctor` fallback from step 2 and report the actual result.

On a capable computer, check whether a managed engine is available for its OS
and platform using the engine recipe and a read-only plan. On Linux run
`"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" engines install comfyui --dry-run`.
On Windows, use the absolute launcher with `engines install comfyui --platform windows-x86_64-cu130 --root ABSOLUTE_ENGINE_ROOT --dry-run`, replacing the root with an absolute Windows path. This read-only plan succeeds on exit 0
with a plan and disk check. If a supported managed engine is available, show the
plan and disk requirement, then offer to install it. Install only on its own yes,
by rerunning that exact plan command without `--dry-run`, using the same platform and
root. Windows execution is unavailable until its engine support lands; a recipe
alone does not establish execution support. Success is exit 0 and an installed status; otherwise use
`--doctor`. Then return to the generation skill's handoff to select placement
and defaults. Never install into or restart the user's existing ComfyUI.
If capable but no managed engine is available, point to the engine's own
installer or offer RunPod; do not claim that Windows or macOS has a managed
engine where its recipe or execution is unavailable.

If the computer is not capable or capability is unconfirmed, offer RunPod.
Explain that it rents a GPU and costs money. Guide the user to enter their key
locally using the hidden prompt: on Linux or macOS run
`"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid" credential set runpod`;
on Windows run `& "$env:LOCALAPPDATA\Astrid\run-astrid.cmd" credential set runpod`.
Never ask for the key in chat, a command argument, or shell history. Success
is a successful setter result; on failure use `--doctor`. Set up the retained
RunPod owner through [runpod.setup](../../astrid/packs/runpod/docs/SKILL.md#replacing-preferences)
with the user's approved policy, positive `budget_cap_usd` ceiling and credential reference, not the key. It succeeds
when the result is ok and `target_operation.state` is `saved`; otherwise use
`--doctor`. Saving preferences does not authorize spending.

With a separate yes, use the generation skill's settings checkout and checkin
through the absolute launcher to set `placement` to `{"kind": "runpod"}`;
preserve the other settings. Checkout succeeds on exit 0 with the settings file;
checkin succeeds on exit 0. On failure use `--doctor`; on stale checkin checkout
again and merge. Return to the generation handoff for defaults and undo.
For each paid generation disclose the offer, rate and expected cost, obtain a
fresh paid consent, and set a cap (suggest $2): pass `--consent paid` and
`--max-usd 2` to the generation command. Success requires the actual generation
result, not merely a pod claim; on failure use `--doctor`. Afterward list leases
with the absolute launcher and `machines --json`; success is exit 0 with the
lease list. Unless the user chooses to keep it warm, release the owned lease
with the launcher and `machines release ID`, then verify it ended; use
`--doctor` on failure. Follow the RunPod skill for the full paid authorization
and cleanup checks. Never create a pod during setup without that consent.

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
[Troubleshooting](troubleshooting.md#asking-for-help). For updates, see
[Troubleshooting](troubleshooting.md#update-astrid).

Once setup is complete, give outside agents the launcher path for their OS
listed above. Then follow [Astrid’s core skill](../../astrid/packs/_core/docs/SKILL.md) and
the relevant pack for the work they want to do. Guide any provider credential
entry locally; never ask the user to paste a secret into chat.
