---
name: pack-builder
description: "Design and implement reusable Astrid packs and capabilities. Use when deciding whether a missing idea belongs in an existing executor, a new executor, an orchestrator, a visual element, or a timeline rendering extension, and when authoring the corresponding manifests and entrypoints."
metadata:
  short-description: "Build reusable Astrid extension packs"
---

# Pack Builder

Use this skill when a maker wants to turn a repeatable capability into an
Astrid extension. Start from the existing catalog and compose what is already
available. One pack is the distribution and namespace unit: `pack.yaml`
declares its public actions, UI, rendering, and resources. A pack is not a
database schema, a second project store, or a new gateway command.

The detailed contracts are maintained in these authoritative guides:

- [When and how to create a pack](../../../../../docs/guides/create-a-pack.md) —
  the current v3 authoring route, starter journeys, roles, documentation
  pointer, validation, and inspect commands.
- [Creating Astrid Packs (legacy v1 reference)](../../../../../docs/packs/creating-packs.md) —
  retained protocol and migration material for existing packs; it is not the
  current new-pack walkthrough.
- [Creating Tools](../../../../../docs/guides/creating-tools.md) — the decision
  rule for composing existing capabilities and choosing an executor,
  orchestrator, element, library, or rendering extension.
- [Pack Contract](../../../../../docs/packs/contract.md) and
  [Pack Taxonomy](../../../../../docs/packs/pack-taxonomy.md) — identity,
  ownership, enablement, maturity, and trust metadata.
- [Discovery for Agents](../../../../../docs/guides/discovery-for-agents.md) —
  the manifest-backed SDK discovery surface.
- [SDK reference](../../../../../docs/reference/sdk.md) — invocation and
  rendering APIs.
- [Capability reference](../references/capabilities.md) — generated inventory
  for quick orientation; when executor, orchestrator, or element manifests
  change, regenerate it with
  [`scripts/gen_capability_index.py`](../../../../../scripts/gen_capability_index.py).
- [Timeline editing skill](../../../timeline/docs/SKILL.md) — the current route
  for finding, inspecting, editing, previewing, and publishing an existing
  timeline; rendering remains a downstream evidence step.

The runnable starter source is
[`astrid/core/pack/cli_basic.py`](../../../../../astrid/core/pack/cli_basic.py);
its focused contract tests are
[`tests/core/pack/test_cli_scaffold_f08.py`](../../../../../tests/core/pack/test_cli_scaffold_f08.py).
Link to those sources rather than maintaining another template or schema here.

Read only the linked guide needed for the current choice. Do not copy its
schemas or command catalog into this skill; those documents are the source of
truth.

Use the contribution roles narrowly: `action` exposes callable functions,
`ui` binds a contribution to a receiving host, `rendering` declares renderer
or rendering support, and `shared` holds support reused by the pack's own
contributions. Declare the public surface in `pack.yaml`; implementation and
private helpers belong under only the applicable role folders.

## Choose the contribution shape

Use this decision map when an idea is underspecified:

1. **Existing capability** — discover and inspect the catalog with
   `astrid.sdk.discover()` and `astrid.sdk.get_capability(...)`. If existing
   executors can be wired to satisfy the request, add no new implementation.
2. **Action** — a callable public function. Keep its implementation and
   action-specific helpers under `actions/`, and declare the public entry in
   `pack.yaml`.
3. **UI** — a contribution received by a host application. Keep host-specific
   code under `ui/`, and declare the public contribution in `pack.yaml`.
4. **Rendering** — renderer or rendering support owned by the pack. Declare it
   under `rendering:` in `pack.yaml` and follow the current authoring steps in
   the [pack guide](../../../../../docs/guides/create-a-pack.md); the rendering
   skill remains the downstream compatibility layer.
5. **Shared library** — support with no public runtime of its own. Keep it
   under `shared/` and use it only from the pack's declared contributions.
6. **No new pack** — keep one-off experiments in a run directory and do not
   create a discoverable capability until the behavior is reusable.

For a one-off experiment, keep scratch output under a run directory and do
not create a discoverable capability until the behavior is reusable.

## Build a pack

For a new reusable pack, follow the supported authoring path in
[When and how to create a pack](../../../../../docs/guides/create-a-pack.md):

1. Scaffold with `python3 -m astrid.core.pack.cli new <pack_id> --starter
   standalone|wrapper|nested`. Repeat `--role` for the applicable `action`,
   `ui`, `rendering`, or `shared` roles; unused roles are omitted.
2. Declare every public function or contribution in `pack.yaml`. Put its
   implementation and private support under only the applicable role folder;
   a path or folder is not an export by itself.
3. Author exactly one ordinary `docs/SKILL.md` with YAML `name` and
   `description` frontmatter and point to it from
   `documentation: {kind: skill, path: docs/SKILL.md}`. Link adjacent guides,
   references, templates, and assets normally; they are not extra skills.
4. For a standalone pack, keep code and declaration in the pack. For a
   wrapper, keep the external repository unchanged and use a thin adapter plus
   its normal dependency/install route; do not vendor arbitrary upstream code.
   For a nested integration, preserve the parent repository layout, place only
   Astrid integration under the documented `integrations/<pack-id>/` subpath,
   and record a pinned source declaration. `pack_subpath` does not install
   dependencies.
5. Reuse existing SDK capabilities through `astrid.sdk.invoke(...)`; do not
   open the runtime database, create a local state authority, or invoke
   `run.py` directly.
6. Validate with `python3 -m astrid.core.pack.cli validate <path> --json`, then
   use `python3 -m astrid.core.pack.cli inspect <pack_id> --pack-root <root>`
   to read back the declared docs pointer and public role sections. Static
   validation checks manifests, roots, docs, and entrypoints; it does not
   execute pack code or install dependencies.

Use the manifest schemas and templates linked by the pack guide instead of
inventing fields. Declare network, files, subprocesses, environment, GPU,
and external-service needs in the pack permissions; declare specific secret
environment variables on the component manifest that reads them. Permission
metadata is disclosure-only in the current contract.

## Implementation invariants

- Discover capabilities through the SDK and manifests. Do not guess ids from
  source-tree names.
- Every invocation has an explicit capability `kind`; executor and
  orchestrator ids are qualified by their owning pack.
- Durable projects, media, timelines, tasks, runs, receipts, and events belong
  to the workspace runtime. Attempt-local files are delivery artifacts, not a
  parallel ledger.
- Keep executor work atomic and inspectable. Workflow shape, retries,
  conditional branches, and child calls belong in orchestrators.
- Register aliases on the pack that owns the canonical capability. Do not
  create shadow sources or compatibility sidecars.
- For elements, preserve the owning pack's element kind and manifest contract;
  an element is selected by the timeline and rendered through the normal
  rendering path.
- For rendering contributions, declare the public surface under `rendering:`
  in `pack.yaml` and follow the current pack guide. Do not add a renderer
  branch to the facade or ask callers to import a backend module.
- Do not add a schema pack, SQL migration, local schema registry, or new
  top-level gateway family as part of capability authoring.

The `_core` gateway is the bounded exception: it remains manifestless and is
composed by the existing skill sync. Its authored source is
`astrid/packs/_core/docs/`, not a new runtime pack or a second skill export.

## Verify the result

Before handoff, confirm that the pack's manifest and every contribution pass
the static validator, that all declared paths stay inside the pack, and that a
cold agent can discover and inspect the capability from the SDK. Run a smoke
invocation only when the requested change includes runtime behavior and the
required runtime/dependencies are available. For rendering contributions,
use the rendering-specific validation and smoke path linked from the [current
pack guide](../../../../../docs/guides/create-a-pack.md).

## First timeline operation: build `fit_to_beats`

This recipe owns the supported **operation** surface: a trusted local Python
SDK command macro admitted by pack V3. The five Release 3 kinds remain:
operation `fit_to_beats` (EXT-ONE), view `story_overview` (EXT-VIEW), component
`vignette` (EXT-COMPONENT), check `DAMAGED-ART` and generator `chiptune.compose`
(EXT-CHECK-GEN). The latter four are **unsupported by this operation host**;
their cards supply their actual consumers. Curves, transitions, music actions
and macros remain EXT-CURVES work. Do not register them as operations merely
to make them discoverable.

Build in a disposable directory such as
`out/ext-one-fresh/packs/beat_tools`, whose folder name must match the pack ID.
Record wall-clock start, end and stumbles. Author the following files yourself;
reading a sample implementation is unnecessary. Run from the Astrid checkout
with its existing Python 3.11 environment, without installing dependencies.

1. Write `pack.yaml`:

   ```yaml
   schema_version: 3
   id: beat_tools
   name: Beat Tools
   version: 0.1.0
   documentation: {kind: skill, path: docs/SKILL.md}
   extensions:
     timeline:
       operations: [extensions/timeline.operation/fit_to_beats.yaml]
   resources:
     - {path: extensions/timeline.operation/fit_to_beats.yaml, kind: operation}
     - {path: extensions/timeline.operation/fit_to_beats.py, kind: python}
   ```

   Write `docs/SKILL.md` with `name` and `description` frontmatter and a short
   explanation of replacing overlay anchors with existing music beat moments.
   No permissions are needed for a pure command macro. Declared pack resources
   are the admitted closure; a directory or a same-name neighbour is no export.

2. Write `extensions/timeline.operation/fit_to_beats.yaml`:

   ```yaml
   point: timeline.operation
   id: beat_tools.fit_to_beats
   label: Fit to beats
   description: Replace one cut's overlay start/end anchors with N+1 existing beat references.
   inputs:
     type: object
     additionalProperties: false
     required: [clips, beats]
     properties:
       clips:
         type: array
         uniqueItems: true
         items: {type: string, minLength: 1}
       beats:
         type: array
         items: {type: string, pattern: '^(beat|downbeat) '}
   implementation:
     path: extensions/timeline.operation/fit_to_beats.py
     function: build
   ```

   The schema uses JSON Schema 2020-12; references must stay within the schema
   (`#...`). The point-qualified identity is
   `timeline.operation/beat_tools.fit_to_beats`. Bare names and other points
   explicitly report `unsupported`. Validation and discovery never import code.

3. Implement `build(ctx, inputs)` in the declared Python file. Return a list of
   existing `EditCommand`s, constructed by `ctx.command(op, select, **args)`.
   Use these read-only, detached context projections:

   | API | Value |
   | --- | --- |
   | `ctx.clip(address)` | Mapping with `identity`, `address`, `id`, `cut`, `is_audio`, `is_picture`, `element`, `track`, `start`, `end`, `moments`, `cut_start`, `cut_end` |
   | `ctx.time(reference)` | Current public timing resolver, including beat/downbeat moments |
   | `ctx.quantize(seconds)` / `ctx.fps` | Public frame quantization and frame rate |
   | `ctx.command("clip.on", address, moment=reference)` | Start timing OpCall |
   | `ctx.command("clip.until", address, moment=reference)` | End timing OpCall |

   Algorithm: accept an ordered unique clip list and exactly N+1 beat references.
   Empty clips plus empty beats returns `[]`; empty clips with beats refuses.
   Resolve all selected clips first; refuse duplicate identities (including
   aliases), more than one cut, no cut, audio, or `is_picture`. Parse each beat
   with `astrid.core.timeline.moments.parse` and require kind `beat` or `downbeat`.
   Convert `MomentError` into `astrid.sdk.timeline.commands.CommandError`.
   Resolve and quantize all boundaries with the context. Refuse missing,
   ambiguous, non-increasing or sub-frame intervals, or boundaries outside the
   cut. Raise `CommandError` for expected refusals. For clip i, append
   `clip.on(moment=beats[i])` then `clip.until(moment=beats[i+1])`, preserving
   reference strings. Existing anchors are deliberately replaced. Never write
   numerical timing, call a private compiler/reflow, detect beats, read media,
   publish, save, or open a nested transaction in contributor code.

4. Validate the pack, then discover and invoke the exact contribution. In the
   commands below, replace the fixture directory if you chose another one:

   ```sh
   python3 -m astrid.core.pack.cli validate out/ext-one-fresh/packs/beat_tools --json
   ```

   ```python
   import json
   from pathlib import Path
   from astrid.sdk.timeline.tools import resolve_operation, plan_operation, invoke_operation
   from astrid.sdk.timeline.checkout import Checkout

   pack_roots = (str(Path("out/ext-one-fresh/packs").resolve()),)
   op = resolve_operation("timeline.operation/beat_tools.fit_to_beats",
                          project_root=Path("out/ext-one-fresh/empty-project"),
                          extra_pack_roots=pack_roots)
   tl = Checkout(json.loads(Path("tests/editing_scenarios/fixtures/tiny_beats.json").read_text()))
   inputs = {"clips": ["c1.rocket", "c1.label"],
             "beats": ["beat 1 after c1", "beat 2 after c1", "beat 3 after c1"]}
   calls = plan_operation(tl, op, inputs)
   assert len(calls) == 4
   before = tl.document()
   preview = invoke_operation(tl, op, inputs, dry_run=True)
   assert preview.changed and tl.document() == before
   result = invoke_operation(tl, op, inputs)
   assert result.changed and len(result.calls) == 4
   assert not invoke_operation(tl, op, inputs).changed
   assert tl.clip("c1.rocket").start == 0.5
   assert tl.clip("c1.label").end == 1.5
   print(op.identity, op.implementation_digest, result.to_json())
   ```

   This disposable fixture was made with the current `clip.set-beats` producer
   using the current `music.beats` grid fields (`beats`, `downbeats`, `bpm`,
   in music-source seconds). It does not analyze audio. To retime the source grid,
   use the existing `clip.set-beats` OpCall with `source` set to a grid dict;
   the timing compiler follows the stored references. `Plan.diff`, `changes`,
   `direct_writes`, `consequences` and populated canonical `calls` are the
   runner's existing outputs. Dry-run performs no durable edit writes. A saved
   checkout records the complete macro as one undoable step; undo and redo use
   the existing checkout APIs.

5. Re-admission hashes the exact point identity, declaration and every admitted
   file resource, including Python. After editing implementation code, rediscover
   before invocation; an old handle refuses with `implementation changed`.
   Fresh source bytes are executed without a basename import or bytecode cache.
   The existing journal label and returned Plan label identify the operation
   and its implementation digest; Plan digests retain their existing before/after schema.

Only the local trusted SDK host is supported. `invoke_operation(..., host="app")`
(and `cli`, `sheet`, `remote`, or any host other than `sdk`) explicitly returns
an `unsupported` refusal; do not claim those integrations from this recipe.
The conformance harness's `extension` adapter is an SDK test host, not a CLI or
App surface. Environment-only `ASTRID_PACKS_PATH` discovery is inspect-only;
explicit pack roots use existing pack trust precedence. This host does not
confine untrusted Python: import side effects, filesystem/media/network access,
remote execution, Runtime grants, publication/concurrency acceptance and a
pinned-clip policy are **unsupported here**. Use the existing confinement and
host admission routes before making those claims. No alternate registry,
per-operation dispatch, service or App panel is needed.
