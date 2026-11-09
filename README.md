# Astrid

Open tools and knowledge for humans and agents to make art.

## Get started

<!-- BEGIN GENERATED AGENT BANNER -->

### Give this to your agent (recommended)

```text
Help me set up Astrid on this computer.

Read this setup guide and follow its steps in order:
https://github.com/banodoco/astrid-agent/blob/main/docs/setup/SKILL.md
Do not install the guide itself as a skill.

Before you run anything, show me the setup choices and let me change them.
Keep my existing work and credentials.
Confirm that the Runtime is connected.
Then follow its "Set up generation" section, and help me begin at
https://astrid.haus.
```

<!-- END GENERATED AGENT BANNER -->

### Set it up yourself

Follow the [manual setup guide](docs/getting-started.md). You can also follow
the setup guide at [docs/setup/SKILL.md](docs/setup/SKILL.md). Its step 1
shows the setup choices before anything is installed. Read the installer
before you run it.

The recommended setup starts Astrid at sign-in, adds the `astrid` terminal
command, installs Astrid skills, and leaves skill copies off. Each choice has a
flag: `--autostart on|off`, `--command on|off`, `--skills on|off`, and
`--skill-copies on|off`. On a fresh install, Astrid asks for your consent once,
unless you have already given it. After consent, run one installer command
with your approved values. If you're updating or repairing an existing
install, your saved choices stay as they are.
An existing install needs no second consent question.

macOS or Linux (Terminal):

```sh
curl -fsSL https://astrid.haus/install | sh
```

Windows (PowerShell; support is in beta):

```powershell
irm https://astrid.haus/install.ps1 | iex
```

Then use the app at **[astrid.haus](https://astrid.haus)** (recommended). Astrid Runtime runs locally and keeps your projects and media on your computer. You do **not** need to clone `astrid-app` to use Astrid; that repository is for contributors who want to modify the editor. You can also [run the editor from source](docs/getting-started.md#run-everything-on-your-computer).

If installation fails, see [troubleshooting](docs/setup/troubleshooting.md).

## Learn and contribute

- [How Astrid works](docs/guides/how-it-works.md)
- [Create a pack](docs/guides/create-a-pack.md)
- [Contribute knowledge](docs/guides/contributing-knowledge.md)
- [Contribute to Astrid development](https://github.com/banodoco/astrid-dev) (issues, plans and agent workflow)
- [Develop this repository](AGENTS.md)

Licensed under the [Apache License, Version 2.0](LICENSE). See [NOTICE](NOTICE).
