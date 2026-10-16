---
name: timeline_editing
description: Find, inspect, edit, preview, validate, and publish an existing runtime timeline through Astrid's current CLI and SDK.
---

# Timeline editing

For an existing timeline, follow **find → show → select → batch → check → publish**.
Runtime owns the published timeline; the CLI works in a detached draft. Examples
use the disposable `demo/main` fixture. Replace those IDs with the connected
workspace's IDs when working on a real project.

## Find and inspect

Search for a phrase, visible text, or asset. Use a returned address with `show`
to inspect its full record; `--summary` gives the cut overview. `find` and `show`
read the selected draft when one exists; `--published` selects the saved head.
The [orientation subprocess scenario](../../../../tests/packs/timeline/skill_cli_scenarios.yaml)
runs these examples through the built CLI on a disposable fixture.

```bash
python3 -m astrid timelines find main "went" --project demo
python3 -m astrid timelines show main c1.rocket --project demo
python3 -m astrid timelines show main --project demo --summary
```

## Select, batch, and inspect

Check out a named draft. Form one ordered batch from the selected addresses.
Preview it with `--dry-run`, then apply that same command. A successful batch is
one transaction and one undo step. The [preview scenario](../../../../tests/editing_scenarios/code/code.skill.batch-edit.yaml)
and [apply scenario](../../../../tests/editing_scenarios/code/code.skill.batch-edit.yaml)
exercise those CLI commands. Check the applied draft and inspect its render-free
placement plan before publishing.

```bash
python3 -m astrid timelines checkout main --project demo --draft working
python3 -m astrid timelines edit main --project demo --draft working --command '{"op":"timeline.batch","select":null,"args":{"commands":[{"op":"clip.set","select":"c1.rocket","args":{"x":125}},{"op":"clip.set","select":"c1.rocket","args":{"y":40}}]}}' --dry-run
python3 -m astrid timelines edit main --project demo --draft working --command '{"op":"timeline.batch","select":null,"args":{"commands":[{"op":"clip.set","select":"c1.rocket","args":{"x":125}},{"op":"clip.set","select":"c1.rocket","args":{"y":40}}]}}'
python3 -m astrid timelines check main --project demo --draft working
python3 -m astrid timelines visualize main --project demo --draft working --mode inputs --plan
```

The current signatures are `Selection.__call__(self, *preds: Any, **filters: Any) -> "Selection"`,
`commands.run(tl, value, *, dry_run=False)`, `Checkout.batch(self, commands: list[Mapping[str, Any]])`,
and `Checkout.edit(self, label: str, *, dry_run: bool = False, check: bool = True, quiet: bool = False, reason: str | None = None)`.
`draft.clips` returns a `Selection`; call it with filters, preview via `commands.run`,
then apply with `draft.batch(calls)` inside `draft.edit(label)` so operations share
one transaction. The focused fixture test executes this exact recipe.

```python
from astrid.sdk.timeline import commands

selected = draft.clips(element="am-sprite")
calls = [
    {"op": "clip.set", "select": clip.address,
     "args": {"x": 120}}
    for clip in selected
]
batch = {"op": "timeline.batch", "select": None, "args": {"commands": calls}}
preview = commands.run(draft, batch, dry_run=True)
with draft.edit("Set selected sprite positions"):
    draft.batch(calls)
```

A valid selection with no matches is empty. Treat it as a no-op; do not submit an
empty batch. If a batch command fails, the transaction restores the draft's
content and creates no undo step; its journal may record the rollback. For a
single structured edit, `--dry-run` also previews without writing. The
[structured-edit scenario](../../../../tests/editing_scenarios/code/code.skill.structured-edit.yaml)
executes this CLI example.

```bash
python3 -m astrid timelines edit main --project demo --draft working --command '{"op":"clip.set","select":"c1.rocket","args":{"x":120}}' --dry-run
```

## Publish and synchronize

Publish the saved draft with one explicit message. Keep the exact
`accepted_revision_id` returned by this command. A later status may report a
newer observed head, such as R2; that observed revision does not replace the
accepted receipt. These commands run in the [publish/sync subprocess scenario](../../../../tests/packs/timeline/skill_cli_scenarios.yaml)
against a local fake transport.

```bash
python3 -m astrid timelines publish main --project demo --draft working -m "Adjust the selected rocket" --json
python3 -m astrid timelines status main --project demo --draft working --watch --poll-seconds 2 --timeout-seconds 3600 --json
python3 -m astrid timelines checkout main --project demo --draft working --refresh --json
python3 -m astrid timelines status main --project demo --draft working --watch --poll-seconds 2 --timeout-seconds 3600 --json
```

Watch reads the existing public metadata head once per poll. It reports the
observed revision and the saved accepted receipt separately; publication/update
details, external ancestry, and conflicts may remain unknown on this route. A
refresh fetches the exact observed head and rebases recorded commands through
the existing sync API. It does not rewrite the accepted receipt. If refresh
reports a same-property or dependent conflict, inspect the returned paths and
choices, keep the draft, and decide which value to author against the latest
head. Do not force the write or present the observed head as an accepted
revision. A stale-head publish follows the same inspect-and-refresh path; publish
again only after resolving the actual conflict.

## History and local edits

`history` lists Runtime revisions newest first; when Runtime cannot list them,
it may show only the head. `log` reads the local edit journal; `--verify` checks
its hash chain. Local undo/redo acts on the detached working copy. On a clean
copy after its own publish, `undo` (also spelled `revert`) stages that
publication's inverse as guarded commands; publish that staged edit normally.
The [history/revert subprocess scenario](../../../../tests/packs/timeline/skill_cli_scenarios.yaml)
exercises local undo and redo.

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

Use `--clear italicWord` to restore the element default; `--set
italicWord=null` supplies null and is refused when the schema requires a string.
Sheets can include `edit {"op":...,"select":...,"args":...}` lines for
operations outside their table. A take `gap 0` preserves the next take's lead-in;
`timeline.close-gap` closes silence before a spoken word.

See the [generated flag-to-operation reference](references/edit-command-flags.md),
built from the canonical CLI inventory and SDK registry and checked against the
parity fixture. Explicit `with draft.edit(label, dry_run=True)` retains its
existing audit record; executable command previews write none.

The [generated cut index](../../../../docs/cut-index.md) documents cut
addresses. Split with `timelines edit ... --split c1 --on MOMENT`; lift with
`timelines lift ... c2 --layers stay` (or `remove`). A lifted span holds the
preceding picture and chapter starts move to the preceding cut. Picture
`--remove` is refused with the lift spelling. Narrated removal accepts
`--remove-line LINE --visuals remove` to lift picture cuts, or `--visuals orphan`
to retain and flag them. Display ordinals remain valid inputs; commands store
stable cut IDs.
