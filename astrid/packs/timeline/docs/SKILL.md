---
name: timeline_editing
description: Find, inspect, edit, preview, validate, and publish an existing runtime timeline through Astrid's current CLI and SDK.
---

# Timeline editing

Use one route for an existing timeline: **find → show → edit → see → publish**.
The workspace Runtime owns the project and published timeline. The CLI edits a
detached working copy; it does not save a local JSON document as the authority.

## Find and inspect

Use the project and timeline IDs or slugs from the connected workspace. Search
for the phrase, visible text, or asset in the user's request:

```bash
python3 -m astrid timelines find <timeline> "viral" --project <project>
```

Copy a returned address into `show` to inspect its complete record. Use a
bounded `show` without an address for the timeline's cut overview:

```bash
python3 -m astrid timelines show <timeline> <address> --project <project>
python3 -m astrid timelines show <timeline> --project <project> --summary
```

`find` and `show` read the selected working copy when one exists. Add
`--published` to inspect the saved head instead. Keep the project, timeline,
and exact address together as you move between commands.

## Edit, see, and publish

Choose a new draft name for this edit, then check it out. This timing recipe
has a matching disposable fixture in the [editing scenario
harness](../../../../tests/editing_scenarios/timing/timing.skill.on-word.yaml):

```bash
python3 -m astrid timelines checkout <timeline> --project <project> --draft <draft>
python3 -m astrid timelines edit <timeline> --project <project> --draft <draft> --clip <address> --on '"<word>"'
python3 -m astrid timelines check <timeline> --project <project> --draft <draft>
python3 -m astrid timelines visualize <timeline> --project <project> --draft <draft> --mode composed --format md --format png
python3 -m astrid timelines publish <timeline> --project <project> --draft <draft> -m "Describe the edit"
```

Replace `<address>` with the clip address from `find` and `<word>` with the
spoken word where it should start. Review the check result and composed preview
before publishing. Use `--mode inputs` for a render-free placement view. If the
head has moved or validation fails, keep the draft, inspect the current head,
and reapply the intended change to a fresh checkout; do not force a stale edit.

For a different edit, use `timelines edit --help` to select an existing
operation. The [JSON checkout reference](../../rendering/docs/references/document-checkout.md)
covers the separate SDK workflow for a request that specifically needs editing
the complete authoring bundle.

## Journal identity

Timeline edit journals record the actor from `ASTRID_ACTOR`. Set
`ASTRID_ACTOR=agent:<id>` or `ASTRID_ACTOR=human:<id>` to label the actor;
an unprefixed non-empty value is recorded as a human actor ID. `ASTRID_SESSION`
adds an optional session label. These fields identify journal entries and do
not grant access.
