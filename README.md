# Astrid

Open tools and knowledge for humans and agents to make art.

## Get started

<!-- BEGIN GENERATED AGENT BANNER -->

### Give this to your agent (recommended)

```text
Help me set up Astrid on this computer. Follow the onboarding skill at
https://github.com/banodoco/astrid-agent/blob/main/docs/setup/SKILL.md
without installing it. Show me the setup choices and let me change them
before you run anything.

Preserve my existing work and credentials, confirm that the
Runtime is connected, and help me begin at https://astrid.haus.
```

<!-- END GENERATED AGENT BANNER -->

### Set it up yourself

Follow the [manual setup guide](docs/getting-started.md), or follow the
onboarding skill at [docs/setup/SKILL.md](docs/setup/SKILL.md) and use its
choices step to review the setup choices before anything is installed. Review
the installer before running it.

The recommended setup starts Astrid at sign-in, adds the `astrid` terminal
command, installs Astrid skills, and leaves skill copies off. The four choices
are controlled by `--autostart on|off`, `--command on|off`, `--skills on|off`,
and `--skill-copies on|off`. Ask for consent once for a fresh install when it
has not already been given. After consent, use one installer command with the
approved values. An already-approved handoff needs no second consent question;
preserve all its choices.

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
