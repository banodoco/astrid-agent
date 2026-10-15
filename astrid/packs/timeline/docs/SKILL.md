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

## Structured edits and explicit clears

`timelines edit TL --project P --command '<JSON>'` accepts the existing OpCall
vocabulary. For example, preview a parameter edit without writing a document,
history step, or journal record:

```bash
python3 -m astrid timelines edit TL --project P --command '{"op":"clip.set","select":"c1.rocket","args":{"x":120}}' --dry-run
```

The JSON preview contains the executed canonical `calls`, affected clips/cuts,
digests and change description. Omit `--dry-run` to apply that same command.
The SDK entry point is `astrid.sdk.timeline.commands.run(checkout, command)`;
`dry_run=True` returns the same preview. `timeline.batch` takes an ordered
`commands` array and applies it atomically as one undo step. The registry in
`astrid/sdk/timeline/commands.py` lists the supported operations; this producer
uses the existing `schemas/change-contract/opcall.schema.json` envelope.

Use `--clear italicWord` to remove the parameter's presence and use the element
default. `--set italicWord=null` supplies a null value and is refused when the
element schema requires a string. In Python, use `clip.clear("italicWord")`.
Canvas positions remain canvas pixels at every authoring entry point.

A sheet can contain `edit {"op":...,"select":...,"args":...}` lines for edits
that do not fit its table, including independent audio clips and source trim.
They use the same registry and join the sheet's single atomic edit. Existing
cut/layer rows retain their syntax. A take `gap 0` preserves the next take's
lead-in; use a `timeline.close-gap` command to close silence before a spoken word.
The generated flag-to-operation table is `tests/parity/edit-command-flags.json`.

The older explicit `with checkout.edit(label, dry_run=True)` audit API retains
its existing dry-run audit record. Executable command/CLI previews write none.

## Publication undo and restore

On a clean working copy after its own publish, `timelines undo TL --project P`
stages that publication's inverse as guarded commands. `revert` is an alias;
`timelines revert TL PUBLICATION_ID --project P` selects an exact publication
from the verified local journal. Publish the staged edit normally with a named
message. Later disjoint edits survive COMMIT's rebase; an overlapping authored
write refuses with `same-property`, names the later publication, and points to
`restore`. The guard includes edits that changed a property and changed it back.

Inverse commands require verified local publication commands and exact Runtime
closures. Recorded add/remove, split, duplicate, detach and compound command
identities can be reversed; deleted payloads come from their tagged command
preconditions, and IDENTITY reservations survive reversal. Explicit remapping
preserves dependent commands. Materializer effects use recorded causal writes,
then pass through the compiler. Media acquisition still requires its admitted
COMMIT adapter. Unknown publication/evidence links remain conflicts; revision IDs,
snapshots, prefixes and timestamps do not establish publication ancestry.

For an explicit whole-timeline replacement, use:

```bash
python3 -m astrid timelines restore TL --project P --from @prev --idempotency-key restore-review-1
```

This invokes Runtime's existing expected-head restore producer. Runtime records
the source in `restored_from_revision_id`; it can publish the same immutable
revision again under a new publication ID. A restore leaves detached working
copies as they were; check out the new head before making further edits. This
route does not restore a single cut. History uses Runtime publication provenance
for source and verified local publication labels for messages when available;
the pinned public Runtime history resource does not expose remote commit labels.
