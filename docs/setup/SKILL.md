---
name: astrid-setup
description: Set up Astrid on a new or existing machine, verify its Runtime connection, and help the user begin.
---

# Set up Astrid

Help the user install Astrid on this computer and reach a first image. Use
[astrid.haus](https://astrid.haus) by default. If they choose to run everything
on their computer so they can change the editor itself, follow the fully local
path below. Both paths use the same installed Runtime and workspace.

The journey has six steps, in order: 0 entry route, 1 choices, 2 install,
3 connected, 4 what this machine can generate with, and 5 set up generation.
Carry out requested checks, report their actual results, and do not claim a
check passed when it could not be run.

## Launcher paths

Every Astrid command uses the stable launcher's absolute path, so it needs no
PATH entry. Use the path for the user's OS:

- Linux: `"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid"`
- macOS: `"$HOME/.local/share/astrid/run-astrid"`
- Windows PowerShell: `& "$env:LOCALAPPDATA\Astrid\run-astrid.cmd"`

## 0. Work out the entry route

Pick exactly one route, checking them in this order:

1. **App brief**: the brief gives a yes/no line about installing with the
   recommended choices. A yes means the choices are supplied. Go to step 1 and
   ask, "install with these choices, or change one?"
2. **Fresh install**: no launcher exists at its path above. Ask for consent
   once if it has not already been given, then go to step 1.
3. **Existing install**: the launcher exists. Skip the consent question only
   if the user has already approved the run. Go to step 1; the install line
   repairs or restarts it and keeps existing work.

## 1. The choices

Show the setup choices and offer a change. The recommended choices are startup
at sign-in on, the `astrid` terminal command on, Astrid skills on, and skill
copies off. Each choice has a flag: `--autostart on|off`, `--command on|off`,
`--skills on|off`, and `--skill-copies on|off`. If the user changes one, use
its value in step 2. An approved handoff keeps its selected values unchanged.
Do not run the install until the choices are settled.

## 2. Install

Run one install command for the user's OS after consent. It installs Astrid on
a new machine, or repairs or restarts an existing installation, and keeps
existing work. Open Terminal on macOS or Linux, or PowerShell on Windows.
Read the installer before running it. `--yes` records consent that was already
given and must not be used to bypass the consent question.

**macOS or Linux:**

```sh
curl -fsSL https://astrid.haus/install | sh -s -- --yes --autostart on --command on --skills on --skill-copies off
```

**Windows PowerShell:**

```powershell
& ([scriptblock]::Create((irm https://astrid.haus/install.ps1))) --yes --autostart on --command on --skills on --skill-copies off
```

For an agent using another shell on Windows (such as Git Bash), run
`powershell -NoProfile -ExecutionPolicy Bypass -Command "& ([scriptblock]::Create((irm https://astrid.haus/install.ps1))) --yes --autostart on --command on --skills on --skill-copies off"`
after consent, or preserve the user's approved choice values in that command.

If the agent runs inside an MSIX-packaged Windows desktop app, use standalone
PowerShell opened from the Start menu for installation. Launching PowerShell
as a child of that app can inherit filesystem redirection: files written under
`%LOCALAPPDATA%` may land in the app's private cache, where the Astrid scheduled
task cannot find them.

Succeeds when the first line of output starts with `Astrid installed:`,
`Astrid is installed and running`, or `Astrid is already up to date`. It fails
on `Astrid was NOT installed`, and on any other first line.

If the installer stops with consent required (exit code 2), show the plan it
prints and run the printed rerun command only after the user agrees.

If the install fails, read the log at the printed `Details:` path. For help,
run the install line for the user's OS with `--doctor` added, attach the
bundle, and follow [Troubleshooting](troubleshooting.md#make-a-support-bundle).

## 3. Connected

The installer checks that Astrid is healthy and opens astrid.haus itself. Tell
the user to choose Allow if Chrome or Edge asks to reach their computer, then
confirm astrid.haus shows Connected. Setup is connected at that point.

## 4. What this machine can generate with

This step is read-only. Install nothing. Run the probe for the user's OS and
report what it prints.

**Linux:**

```sh
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
```

**macOS:**

```sh
uname -m
```

**Windows:**

```powershell
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader
```

Succeeds when the Linux or Windows probe prints one line per GPU with a name
and a memory total, or the macOS probe prints the machine type. The local
engines need an NVIDIA GPU with at least 12 GB of memory.

Doctor fallback: if a probe is missing or fails, say that it could not be
checked and install nothing. Run the install line for the user's OS with
`--doctor` added, as in [Troubleshooting](troubleshooting.md#make-a-support-bundle),
and attach the bundle it prints.

Managed local engines by platform:

| Platform | Engine |
| --- | --- |
| `linux-x86_64-cu130` | `comfyui` |
| `windows-x86_64-cu130` | `vibecomfy` |
| `windows-x86_64-cu128` | `wan2gp` |

No managed local engine is listed for macOS yet.

## Set up generation

Pick the path that matches this machine:

- A ComfyUI is already running here: follow
  [Find their ComfyUI](../../astrid/packs/vibecomfy/docs/SKILL.md#find-their-comfyui).
- No ComfyUI on this machine: use the
  [RunPod skill](../../astrid/packs/runpod/docs/SKILL.md) for a cloud GPU.
- A managed engine install is coming. This setup does not run one yet.

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
