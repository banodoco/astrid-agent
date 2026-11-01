# Getting Started with Astrid

Windows support is in beta. It has had less testing than macOS and Linux; if
something goes wrong, see [Get help](setup/troubleshooting.md).

Install Astrid on this computer, then use it at
[astrid.haus](https://astrid.haus) by default. If you want to change the editor
itself, you can also run everything on your computer using the fully local
path below. Both paths keep projects, media, models and your agent on your
computer through Astrid Runtime.

Open Terminal on macOS or Linux, or PowerShell on Windows. Read the installer
before running it. It keeps your existing workspace and credentials.

On macOS or Linux, run:

```sh
curl -fsSL https://astrid.haus/install | sh
```

On Windows, open PowerShell and run:

```powershell
irm https://astrid.haus/install.ps1 | iex
```

Then open [astrid.haus](https://astrid.haus) and follow the on-screen steps.
For help, see [Troubleshooting](setup/troubleshooting.md).

Astrid Runtime runs on this computer and owns workspace projects, media,
timelines, tasks, and run records.

## Run everything on your computer

After the official install above, ask your agent to follow the
[fully local setup instructions](setup/SKILL.md#run-everything-on-this-computer),
or install Git and Node.js 20.19 or newer in the Node 20 release line and run:

```sh
git clone https://github.com/banodoco/astrid-app.git
cd astrid-app
npx -y npm@11 ci
npm run dev:local
```

Open **http://127.0.0.1:5180/tools/video-editor?connect=1** and confirm it shows Connected.
Keep the terminal open. The editor connects through its local server, so no
browser local-network permission is needed. astrid.haus remains available on
the same computer. Once dependencies are installed, the local editor can open
offline; online models and providers still need internet.

The `astrid-app` clone is your editor's code. Edit it while the server runs to
see changes. Start later with `npm run dev:local` in that folder. Stop with
Ctrl+C; an already running installed Runtime keeps running. To update, stop
the editor, save or commit source changes, then run `git pull --ff-only origin main`,
`npx -y npm@11 ci`, and `npm run dev:local`. If Git reports a conflict or divergence,
keep your changes and resolve it before continuing. Update the Runtime and
agent with the [official update command](setup/troubleshooting.md#update-astrid),
then restart the editor. See the
[App README](https://github.com/banodoco/astrid-app#run-astrid-fully-on-your-computer)
for details.

## Developer reference

The rest of this repository contains source code and developer documentation
for the Astrid toolkit, capability packs, and Runtime integration. These are
for building or contributing to Astrid; they are not user installation
instructions. Start with [AGENTS.md](../AGENTS.md) for repository guidance,
[creating packs](packs/creating-packs.md) for pack development, and the
[architecture documentation](architecture/) for system design.
