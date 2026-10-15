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

## First declared component: build `vignette`

This is the **component** recipe. It uses the same pack-v3 resource admission as
operations and the returned Composition `declareModifier`/`composeModifiers`
host. It adds no renderer, operation, clock or App panel. The five kinds listed
above retain their separate consumers. Read this recipe as your authoring
instruction; a fresh exercise must not read the sample component or its tests.

Record UTC start, elapsed time to the first correct build, elapsed time through
handback, and each concrete stumble. Work in an ignored disposable path such as
`out/ext-component-fresh/packs/my_looks`; match the pack folder and ID. Use the
installed Python 3.11/Node environment. Do not install packages. The receiving
host must supply its actual admitted Composition/theme-api module and normal
trusted local loading boundary. Permission metadata is disclosure, and this
recipe does not confine arbitrary contributor code.

1. Write `pack.yaml` with `schema_version: 3`, `id: my_looks`, a name/version,
   and `documentation: {kind: skill, path: docs/SKILL.md}`. Write that skill
   with `name`/`description` frontmatter. Declare:

   ```yaml
   extensions:
     timeline:
       components: [extensions/timeline.component/vignette.yaml]
   resources:
     - {path: extensions/timeline.component/vignette.yaml, kind: component}
     - {path: extensions/timeline.component/vignette.tsx, kind: typescript}
   ```

   Only explicitly declared file resources are admitted. A folder, same-name
   neighbour, directory resource or basename is not an export. No Python
   contribution executes during validation, discovery or binding.

2. Write the component descriptor at the first resource path:

   ```yaml
   point: timeline.component
   id: my_looks.vignette
   kind: modifier
   label: Vignette
   description: Darken one clip's edges with fixed radial attenuation.
   params:
     type: object
     additionalProperties: false
     required: [strength]
     properties:
       strength: {type: number, minimum: 0, maximum: 1}
   hosts: [composition, app]
   bounds: preserve
   alpha: preserve
   support: preserve
   implementation:
     path: extensions/timeline.component/vignette.tsx
     export: bindVignette
   ```

   This declares a child-content modifier, not an overlay/effect/animation.
   The exact element key is `timeline.component/my_looks.vignette`. Schemas
   use JSON Schema 2020-12 with only local `#` references. Omitting bounds
   is admitted for inspection but shared evaluation must return `unknown`
   and finding `unknown-modifier-bounds`; it cannot be rendered as preserving.

3. Write the TypeScript export `bindVignette(host, declaration)`. The caller
   supplies the actual `@banodoco/timeline-composition/theme-api` host, plus
   the freshly admitted binding's descriptor. Call its `declareModifier` with:
   `kind: 'modifier'`, exact `element: declaration.point + '/' + declaration.id`,
   declared hosts, and `bounds`, `alpha`, `support` all `'preserve'`.
   Validate descriptor identity/subtype/hosts/preservation before binding.
   Bind a React component and `resolveParams(params, context)`. Resolve only
   the declared finite static numeric strength in [0,1], refusing extra keys,
   booleans, NaN, objects and unsupported animated carriers. This first
   component introduces no animation format.

   The component receives `instanceId`, `params`, `context`, `children` from
   that host. Return exactly `children` when strength is zero. Use no wall
   clock or `useCurrentFrame`; the owning clip already supplies evaluated
   `frame`, `keyframeFrame`, `keyframeTime`, `globalFrame`, `fps`, bounds and
   theme. Unknown target bounds refuse through the existing host callback.

   Freeze this first falloff: white from the center through 50% of an ellipse's
   radius, linearly falling to RGB `(1-strength)` at its edge. Implement a
   local SVG filter, not an overlay that paints transparent regions. Use an
   opaque inline SVG data image with a white 50% radial-gradient stop and an
   RGB `(1-strength)*100%` 100% stop. Stretch the square image to the target's
   exact known rectangle with `preserveAspectRatio="none"`. Use `feImage`
   result `attenuation`, followed by `feComposite` with `in="SourceGraphic"`,
   `in2="attenuation"`, `operator="arithmetic"`, `k1=1`, `k2=k3=k4=0`.
   Set filter/primitive units to `userSpaceOnUse`, region x/y/width/height to
   `context.bounds.rect`, and `colorInterpolationFilters="sRGB"`.
   Multiplication by this opaque image attenuates premultiplied source RGB
   and multiplies source alpha by exactly one. Zero RGB outside source support
   remains zero; the filter region never expands target bounds. Preserve the
   owning subtree once inside a position-absolute/inset-zero filter wrapper.
   Use React `useId` (sanitized for SVG) for collision-free filter IDs across
   repeated occurrences, without making hooks conditional on strength.
   Include only inline admitted data/resources; no I/O, scene capture, audio,
   independent timing window, timeline mutation or cross-clip wrapper.

4. Validate and discover using the actual tools:

   ```sh
   python3 -m astrid.core.pack.cli validate out/ext-component-fresh/packs/my_looks --json
   python3 -m astrid.core.pack.cli inspect my_looks --pack-root out/ext-component-fresh/packs
   ```

   ```python
   from pathlib import Path
   from astrid.sdk.timeline.components import (
       resolve_component, bind_component, component_instance, component_bounds,
   )
   component = resolve_component(
       'timeline.component/my_looks.vignette',
       project_root=Path('out/ext-component-fresh/empty-project'),
       extra_pack_roots=(str(Path('out/ext-component-fresh/packs').resolve()),),
   )
   binding = bind_component(component, host='composition')
   reference = component_instance(component, 'edge-darkening-1',
                                  {'strength': 0.5}, host='composition')
   assert reference['element'] == binding['identity']
   assert component_bounds(component, {'status': 'known', 'rect': {
       'x': 0, 'y': 0, 'width': 100, 'height': 50}})['status'] == 'known'
   print(component.to_json(), binding['implementation_digest'], reference)
   ```

   Binding re-admits identity, descriptor and all declared file-resource hashes.
   It returns the exact source closure (`sources[path]` has `text` and `sha256`)
   for the existing trusted local host/bundler. Use only the declared path/export
   from that closure. Rediscover after changes; a stale handle must refuse.
   Environment-only `ASTRID_PACKS_PATH` is inspect-only. Explicit roots retain
   existing pack trust precedence. This is source admission, not automatic
   registration in every consumer or an untrusted-code security boundary.

5. Load the declared export through the receiving host's existing trusted local
   boundary, call `bindVignette(actualHost, binding.descriptor)`, and supply
   `{[binding.identity]: returnedDeclaration}` as its `modifierRegistry`.
   Invoke the actual host with the validated `reference` in the owning existing
   `clip.modifiers` list. Verify `[A,B]` becomes `B(A(content))`, stable IDs
   survive serialization/reorder, every wrapper receives the exact same evaluated
   clock/bounds, zero preserves child identity, invalid params refuse, and an
   unknown target emits the named finding. Prove `host='remote'` explicitly
   refuses. A descriptor with no bounds must give coverage `unknown` with the
   named finding and refuse drawing; never synthesize bounds. Preserve source
   alpha/support, existing crop/fade/transition placement and ordinary audio.

   The returned I003 host supports Composition and App native media when a
   registry is explicitly injected. App element/generated/text/audio/other
   unsupported targets refuse. In the current assigned sources, CLI `edit --add`
   for modifier instances and the App add menu lack their owner consumers;
   constructing a reference or injecting a registry does not prove those UI
   surfaces. Report the missing D/App integrations. A/G must admit and consume
   this binding through their existing producer; this recipe adds no loader.
   Actual Player/export event-centred frame agreement, the RENDER-PARITY harness
   and planted 1 px failure require G's actual renderer/query/harness inputs and
   the compatible A lease. Structural/SSR evidence does not prove pixels.

Hand back exact pack paths and hashes, validation/discovery/binding/host results,
source/dependency tuples, elapsed timings and concrete stumbles. Retain failed
attempts. The independent fresh author must create their own pack from this
recipe, without opening the implementation sample, implementation tests or
XHARD witness; their exercise is a separate dispatch after recipe freeze.


## Read-only timeline view recipe (EXT-VIEW)

Build a V3 pack in a disposable directory whose folder name matches its `id`.
Record build start/end and stumbles; the independent fresh-agent exercise belongs
to the manager. A view contains **data only**, with no Python or browser entry.

1. Add `extensions.timeline.views: [extensions/timeline.view/story_overview.yaml]`
   to `pack.yaml` and declare that exact path as a file in `resources`. Supply
   ordinary V3 pack metadata and `documentation: {kind: skill, path: docs/SKILL.md}`.
   Use a small dedicated pack (for example `story_views`), so its admitted closure
   does not contain the generated editor catalog that will project its digest.
2. In the descriptor declare `point: timeline.view`, an owner-qualified
   `id: story_views.story_overview`, `label`, `description` (help),
   `read_model: story_overview`, and exactly
   `reads: [project, timeline, revision_id]`. `schema` is an inline JSON Schema
   for the producer result; references must remain local (`#...`). Declare every
   result field without inventing derived rows. I's producer returns
   `{project_id,timeline_id,revision_id,content_digest,fps,ranges,legacy_chapters,findings}`.
   Each range has `{range,start_frame,end_frame}`; `range` is the original
   `{name,kind,from,to}` (including malformed originals when findings explain them),
   frame edges may be null, and findings have `{code,path,message}`.
3. Validate using `python3 -m astrid.core.pack.cli validate <pack-dir> --json`.
   Discover with `astrid.sdk.timeline_views.discover_views(extra_pack_roots=(... ,))`
   or `resolve_view("timeline.view/<pack>.story_overview", ...)`. Bare names and
   cross-point names refuse. Discovery never imports contribution code.
4. `read_view(view, project=P, timeline=T, revision_id=R, host=...)` re-admits
   the closure and validates the returned schema, revision and content identity.
   The local `astrid.core.pack.views.TimelineReadModels(client)` host calls the
   actual I `open_story_overview` API; an unavailable producer/client refuses
   with `unsupported`. A browser cannot invoke Python or open a substitute host.
5. The generic CLI routes are `timelines show T --project P --as <full-view-id>`
   and `timelines visualize T --project P --view <full-view-id>`; both accept
   `--revision-id R` and `--json`. `--list-views` lists declarations without
   starting Runtime. They resolve aliases to the host's canonical IDs and pin
   the returned revision before the producer read. These schema-driven views
   print structured/text inspection; they request no thumbnails or media reads.
6. Regenerate the existing editor catalog with
   `python3 scripts/gen_editor_extension_catalog.py`. Its
   `ASTRID_TIMELINE_VIEW_CATALOG` projects the same declaration and digest for
   the App's generic extension panel. It lists schema/help/read inputs and
   reports typed `unsupported` on execution until A/C provide a real exact-revision
   read transport. Fixtures are not Runtime transport evidence.

Prove valid chapters/beats, unknown/invalid anchors, exact identity and stale
refusals, closure changes, discovery without execution, and unchanged document
plus zero journal/write effects for every admitted view. Keep results and
findings intact. Do not register a second evaluator/registry, invent row IDs,
read arbitrary files, write a timeline, save, publish or launch a service.
