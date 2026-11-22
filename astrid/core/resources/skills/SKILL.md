---
name: astrid
short_description: Astrid creative work and workspace commands.
description: Inspect projects, media, timelines, tasks and runs, and use selected capability guides for creative work.
---

# Astrid

Astrid is a client and pack toolkit for making creative work with a human.
The Runtime owns projects, media, timelines, tasks, runs, receipts and events.

Read [Creative work](creative-work/SKILL.md) to choose a selected capability,
[Pack builder](pack-builder/SKILL.md) to author a reusable capability, or
[Pack testing](pack-testing/SKILL.md) to validate one. The
[selected pack directory](creative-work/references/packs.md) and
[capability index](references/capabilities.md) are composed from declarations.
If no optional pack is selected, those directories contain no optional routes.
A missing capability is unavailable; do not acquire dependencies to guess one.

For execution location details, see the [platform and execution matrix](https://github.com/banodoco/astrid-agent/blob/main/docs/reference/where-things-run.md).

## Setup

On macOS or Linux, install or repair Astrid with:

```sh
curl -fsSL https://astrid.haus/install | sh
```

On Windows, use PowerShell:

```powershell
irm https://astrid.haus/install.ps1 | iex
```

The installer opens astrid.haus. Allow the browser to reach the local computer,
then confirm the page shows Connected. Preserve existing work.

## Find and inspect work

Start with the user's request and establish the current project explicitly.
Inspect help when a command's inputs are uncertain:

```sh
python3 -m astrid --help
python3 -m astrid projects list --json
python3 -m astrid projects select <project>
python3 -m astrid timelines list --project <project> --json
python3 -m astrid tasks list --project <project> --json
python3 -m astrid runs list --project <project> --json
```

Read the selected capability guide before creative execution. Discover
available skills with `python3 -m astrid.skills list --json`. Use the exact
qualified capability id, kind, inputs and project binding from its declaration.
Use the SDK through a connected Runtime; do not invoke pack scripts directly.
Report results and evidence from the admitted Runtime task. A local output
file alone does not prove completion or publication.

`astrid tasks logs <task>` lists a task's logs; add `--name <log>` to print one.
`astrid generate failure` shows the last generation failure; add `--logs` to print its logs.

## Preferences and documents

Current explicit instructions precede project preferences, then user defaults.
Save lasting preferences only when requested; keep credentials out of them.
Use the preferences and documents command help to read, check out and check in
Runtime documents. Do not edit Runtime storage or invent filesystem side channels.

## Safety and ownership

Use Runtime-owned storage for outputs. Alternate exports need an explicit
request. Preserve project/run evidence. Do not commit source media, rendered
videos, local environments or secrets. Preview public writes and obtain the
user's explicit publication authorization before submitting them.

## If something goes wrong

- If you hit a bug, a confusing step or a failure in Astrid, tell the user plainly what happened.
- Offer to fix it. With the user's OK, try to fix it and keep their work and data safe.
- With the user's OK, file an issue at https://github.com/banodoco/astrid-dev/issues/new
  with what happened, the exact steps, the release and the version
  (run the launcher with `--version`), the OS, and the error output.
- Remove credentials, tokens and personal data (names, home paths) from everything
  you share, logs included. Show the user the text before you post it.
- If you fixed it, offer to open a PR against the Astrid repo that owns the code
  (under github.com/banodoco), from a fork or branch the user can push to, and link it
  from the issue. Never push to a repo without the user's OK.
- If you can't fix it, the issue alone is still valuable. Don't give up silently.
