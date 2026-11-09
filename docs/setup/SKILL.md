---
name: astrid-setup
description: Set up Astrid on a new or existing machine, verify its Runtime connection, and help the user begin.
---

# Set up Astrid

Help the user install Astrid on this computer and reach a first image. Use
[astrid.haus](https://astrid.haus) by default. If they choose to run everything
on their computer so they can change the editor itself, follow the fully local
path below. Both paths use the same installed Runtime and workspace.

The journey has four steps, in order: 0 entry route, 1 choices, 2 install,
3 connected.
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
   values back, ask "Install with these choices, or change one?", and on no
   change run the brief's command exactly as given. If they change one, go to
   step 1.
2. **Fresh install**: no launcher exists at its path above. Ask the user for
   consent once, unless they have already given it. Then go to step 1.
3. **Existing install**: the launcher exists. Ask the user for consent, unless
   they have already approved this run. Your saved setup choices stay as they
   are. Tell me if you want to change one. Do not show the choices list. Run
   the install command for the user's OS with no choice flags:
   `curl -fsSL https://astrid.haus/install | sh` on macOS or Linux, or
   `& ([scriptblock]::Create((irm https://astrid.haus/install.ps1)))` in
   Windows PowerShell. Add a choice flag only for a choice the user names,
   using step 2's spelling. `--yes` is not needed here: the installer asks for
   consent only on a fresh install. The install command will repair or restart
   Astrid and keep existing work, then follow step 2's success and failure
   rules.

## 1. The choices

Show the user the setup choices before you run anything. The recommended
choices are:

- Start at sign-in: on (`--autostart on|off`)
- the `astrid` terminal command: on (`--command on|off`)
- Astrid skills: on (`--skills on|off`)
- skill copies: off (`--skill-copies on|off`)

Ask the user whether they want to change any choice. If they change one, use
its new value in step 2. If the user approved an App brief, keep its selected
values unchanged. Do not run the install until the choices are settled. Then
go to step 2.

## 2. Install

Run one install command for the user's OS, after consent. The commands below
use the recommended values. Replace any value the user changed in step 1. On a
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
`Astrid is installed and running`, or `Astrid is already up to date`. Any other
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
Confirm that astrid.haus shows Connected. When it does, setup is complete.
(Connecting local engines such as ComfyUI or Wan2GP is not covered here yet.)
If it does not, follow [Troubleshooting](troubleshooting.md#asking-for-help).

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
