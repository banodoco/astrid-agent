# Astrid

Tools and knowledge for agents and humans to make art with open models.

<!-- BEGIN GENERATED AGENT BANNER -->

### Give this to your agent

```text
╭──────────────────────────── ◇ ─────────────────────────────╮
│                                                            │
│                   A   S   T   R   I   D                    │
│                                                            │
│              agents harnessed, humans free —               │
│               open tools for what could be:                │
│              moving image, voice, and frame,               │
│       open Astrid, make something, share its flame.        │
│                                                            │
│                          ·  ·  ·                           │
│                                                            │
│          Agent instructions: docs/setup/SKILL.md           │
│                                                            │
│       $ curl -fsSL https://astrid.haus/install | sh        │
│        $ irm https://astrid.haus/install.ps1 | iex         │
│             open https://astrid.haus to begin.             │
│                                                            │
│                          ·  ·  ·                           │
│                                                            │
│                 ask the maker what to do,                  │
│             bring a spark and see it through —             │
│              no map, no plan, no perfect day:              │
│         begin, hold fast  — you'll find your way.          │
│                                                            │
╰──────────────────────────── ◇ ─────────────────────────────╯
```

<!-- END GENERATED AGENT BANNER -->

**Set up Astrid:** open Terminal on macOS or Linux, or PowerShell on Windows;
run the installer for your operating system, then open
[astrid.haus](https://astrid.haus).

**Windows support is in beta.** It has had less testing than macOS and Linux;
if something goes wrong, see [Get help](docs/setup/troubleshooting.md).

**Want step-by-step instructions?** [Set up Astrid →](docs/getting-started.md)

**Testing the installer?** Follow the [tester guide](docs/testing/TESTING.md), which records a known chat-history
gap in the currently published agent engine.

macOS and Linux:

```sh
curl -fsSL https://astrid.haus/install | sh
```

Windows PowerShell:

```powershell
irm https://astrid.haus/install.ps1 | iex
```

Astrid is used at [astrid.haus](https://astrid.haus). Do not install
`astrid-app` to use Astrid; that repository is for app developers.

The CI default lane uses the committed `tests/.test_durations` file to balance its eight pytest shards. After a full default-lane run, refresh it with `python -m pytest -q --store-durations --durations-path tests/.test_durations`, then commit the updated file.

### More info

- [How Astrid works](docs/guides/how-it-works.md)
- [When and how to create a pack](docs/guides/create-a-pack.md)
- [Contributing knowledge](docs/guides/contributing-knowledge.md)
- [Get help](docs/setup/troubleshooting.md)

---

Licensed under the [Apache License, Version 2.0](LICENSE).
See [NOTICE](NOTICE) for copyright information.
