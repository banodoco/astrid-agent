---
name: astrid-setup
description: Set up Astrid on a new or existing machine, verify its Runtime connection, and help the user begin.
---

# Set up Astrid

Help the user install Astrid on this computer. Use
[astrid.haus](https://astrid.haus) by default. If they choose to run everything
on their computer so they can change the editor itself, follow the fully local
path below. Both paths use the same installed Runtime and workspace.

Run the install line for this operating system. It installs Astrid on a new
machine, repairs or restarts an existing installation, and keeps existing
work. Open Terminal on macOS or Linux, or PowerShell on Windows. Use the line
from [Getting Started](../getting-started.md). Read the installer before running it:

- macOS or Linux: `curl -fsSL https://astrid.haus/install | sh`
- Windows PowerShell: `irm https://astrid.haus/install.ps1 | iex`

For an agent using another shell on Windows (such as Git Bash), run
`powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astrid.haus/install.ps1 | iex"`.

If the agent runs inside an MSIX-packaged Windows desktop app, use standalone
PowerShell opened from the Start menu for installation. Launching PowerShell
as a child of that app can inherit filesystem redirection: files written under
`%LOCALAPPDATA%` may land in the app's private cache, where the Astrid scheduled
task cannot find them.

## Use astrid.haus (default)

The installer checks that Astrid is healthy and opens astrid.haus itself. Tell
the user to choose Allow if Chrome or Edge asks to reach their computer, then
confirm astrid.haus shows Connected. That is the end of setup. (Connecting
local engines such as ComfyUI or Wan2GP is not covered here yet.) Carry
out requested checks, report their actual results, and do not claim a check
passed when it could not be run.

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

Once setup is complete, give outside agents the stable launcher path for their
OS. It is an absolute path and does not require adding Astrid to the caller's
PATH:

- Linux: `"${XDG_DATA_HOME:-$HOME/.local/share}/astrid/run-astrid"`
- macOS: `"$HOME/.local/share/astrid/run-astrid"`
- Windows PowerShell: `& "$env:LOCALAPPDATA\Astrid\run-astrid.cmd"`

When the user wants to connect ComfyUI or review generation defaults, follow
[Astrid onboarding](../../astrid/packs/_core/docs/onboarding/SKILL.md).
Then follow [Astrid’s core skill](../../astrid/packs/_core/docs/SKILL.md) and
the relevant pack for the work they want to do. Guide any provider credential
entry locally; never ask the user to paste a secret into chat.
