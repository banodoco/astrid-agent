---
name: timeline_editing
description: Find, inspect, edit, preview, validate, and publish an existing runtime timeline through Astrid's current CLI and SDK.
---

# Timeline editing

For an existing timeline, follow **find → show → edit → see → publish**. Runtime
owns the project and published timeline; the CLI edits a detached working copy.
Examples below use the disposable `demo/main` command fixture; substitute IDs
from the connected workspace when working on a real project.

## Find and inspect

Search for the phrase, visible text, or asset in the request. Paste a returned
address into `show` to inspect its complete record; `--summary` gives the cut
overview. `find` and `show` read the selected working copy when one exists;
`--published` selects the saved head. The [orientation subprocess scenario](../../../../tests/packs/timeline/skill_cli_scenarios.yaml)
executes these commands through the built CLI against a disposable fixture.

```bash
python3 -m astrid timelines find main "went" --project demo
python3 -m astrid timelines show main c1.rocket --project demo
python3 -m astrid timelines show main --project demo --summary
```

## Edit, see, and publish

Choose a draft name and check it out. This structured edit has a matching
[editing scenario](../../../../tests/editing_scenarios/code/code.skill.structured-edit.yaml).
Check the result and inspect a render-free placement plan before publishing.
The [edit-loop subprocess scenario](../../../../tests/packs/timeline/skill_cli_scenarios.yaml)
uses a local fake transport. When ready, submit the saved draft with
`timelines publish <timeline> --project <project> --draft <draft> -m "Describe the edit"`;
publishing sends a Runtime mutation.

```bash
python3 -m astrid timelines checkout main --project demo --draft working
python3 -m astrid timelines edit main --project demo --draft working --command '{"op":"clip.set","select":"c1.rocket","args":{"x":120}}'
python3 -m astrid timelines check main --project demo --draft working
python3 -m astrid timelines visualize main --project demo --draft working --mode inputs --plan
```

If the head moved or validation fails, keep the draft, inspect the current head,
and reapply the intended change to a fresh checkout. For other operations, use
`timelines edit --help`. The [JSON checkout reference](../../rendering/docs/references/document-checkout.md)
covers the separate SDK workflow for editing the complete authoring bundle.

## History and local edits

`history` lists Runtime revisions newest first; when Runtime cannot list them,
it may show only the head. `log` reads the local edit journal; `--verify` checks
its hash chain. Local undo/redo acts on the detached working copy. On a clean
copy after its own publish, `undo` (also spelled `revert`) stages that
publication's inverse as guarded commands; publish that staged edit normally.
The [history/revert subprocess scenario](../../../../tests/packs/timeline/skill_cli_scenarios.yaml)
also exercises local undo and redo.

```bash
python3 -m astrid timelines history main --project demo --limit 10
python3 -m astrid timelines log main --project demo --verify
python3 -m astrid timelines undo main --project demo --draft working --steps 1
python3 -m astrid timelines redo main --project demo --draft working --steps 1
```

Exact publication undo uses verified local publication commands and Runtime
closures. Overlapping authored writes refuse with `same-property`; use whole
revision restore when replacement is intended. Restore is a Runtime mutation
that replaces the whole revision, leaving detached working copies unchanged.
`<revision-id>`, `@prev`, and `@-N` select its source:

```bash
python3 -m astrid timelines restore main --project demo --from @prev --idempotency-key restore-demo
```

## Structured edits and explicit clears

`--command` accepts the existing OpCall vocabulary. This dry run writes no
document, history step, or journal record. The [structured-edit scenario](../../../../tests/editing_scenarios/code/code.skill.structured-edit.yaml)
exercises the same operation across CLI, sheet, and Python.

```bash
python3 -m astrid timelines edit main --project demo --draft working --command '{"op":"clip.set","select":"c1.rocket","args":{"x":120}}' --dry-run
```

The SDK entry point is `astrid.sdk.timeline.commands.run(checkout, command)`;
`dry_run=True` returns the same preview. `timeline.batch` applies ordered
commands atomically as one undo step. Preview a batch, then run the same command
without `--dry-run` to apply it as one edit. The [batch preview scenario](../../../../tests/editing_scenarios/code/code.skill.batch-edit.yaml)
and [batch apply scenario](../../../../tests/editing_scenarios/code/code.skill.batch-edit.yaml)
cover the command across CLI, sheet, and Python:

```bash
python3 -m astrid timelines edit main --project demo --draft working --command '{"op":"timeline.batch","select":null,"args":{"commands":[{"op":"clip.set","select":"c1.rocket","args":{"x":125}},{"op":"clip.set","select":"c1.rocket","args":{"y":40}}]}}' --dry-run
python3 -m astrid timelines edit main --project demo --draft working --command '{"op":"timeline.batch","select":null,"args":{"commands":[{"op":"clip.set","select":"c1.rocket","args":{"x":125}},{"op":"clip.set","select":"c1.rocket","args":{"y":40}}]}}'
```

Use `--clear italicWord` to restore the element default; `--set
italicWord=null` supplies null and is refused when the schema requires a string.
Canvas positions are pixels at every authoring entry point. Sheets can include
`edit {"op":...,"select":...,"args":...}` lines for
operations outside their table, including independent audio clips and source
trim. A take `gap 0` preserves the next take's lead-in; `timeline.close-gap`
closes silence before a spoken word.

See the [generated flag-to-operation reference](references/edit-command-flags.md),
built from the canonical CLI inventory and SDK registry and checked against the
parity fixture. Explicit `with checkout.edit(label, dry_run=True)` retains its
existing audit record; executable command previews write none.

## Observe and refresh

`status --watch` polls the existing public metadata head route. It reports the
observed revision separately from the last accepted revision; publication ID
and update time remain unknown, and external revisions/conflicts remain
unknown in watch output. This route does not expose the internal three-field
Runtime head endpoint. `checkout --refresh` fetches the exact observed head and
rebases recorded commands; it does not substitute that head for the accepted
revision receipt. The [sync subprocess scenario](../../../../tests/packs/timeline/skill_cli_scenarios.yaml)
checks that accepted R1 remains distinct from observed R2.

```bash
python3 -m astrid timelines status main --project demo --draft working --watch --poll-seconds 2 --timeout-seconds 3600 --json
python3 -m astrid timelines checkout main --project demo --draft working --refresh --json
```

The [generated cut index](../../../../docs/cut-index.md) documents cut
addresses. Split with `timelines edit ... --split c1 --on MOMENT`; lift with
`timelines lift ... c2 --layers stay` (or `remove`). A lifted span holds the
preceding picture and chapter starts move to the preceding cut. Picture
`--remove` is refused with the lift spelling. Narrated removal accepts
`--remove-line LINE --visuals remove` to lift picture cuts, or
`--visuals orphan` to retain and flag them. Display ordinals remain valid
inputs; commands store stable cut IDs.
