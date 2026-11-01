---
name: pack-builder
description: "Design and implement reusable Astrid packs and capabilities. Use when deciding whether a missing idea belongs in an existing executor, a new executor, an orchestrator, a visual element, or a timeline rendering extension, and when authoring the corresponding manifests and entrypoints."
metadata:
  short-description: "Build reusable Astrid extension packs"
---

# Pack Builder

Package a repeatable capability as an Astrid extension. Start with the
catalog and reuse what already exists. For a new pack, follow the
[authoring guide](../../../../../docs/guides/create-a-pack.md) and
[manifest reference](../../../../../docs/packs/creating-packs.md).

## Choose what to build

1. **Existing capability:** inspect `astrid.sdk.discover()` and
   `astrid.sdk.get_capability(...)`; compose what fits.
2. **Action or UI:** declare the public contribution in `pack.yaml` and put
   its implementation under the matching role folder.
3. **Rendering:** declare it under `rendering:` and follow the authoring guide.
4. **Shared code:** put private support used by contributions under `shared/`.
5. **No new pack:** keep one-off work in a run directory.

Reuse capabilities with `astrid.sdk.invoke(...)`. Do not open the Runtime
database or invoke `run.py` directly. See [creating tools](../../../../../docs/guides/creating-tools.md)
for contribution choices and [pack data storage](../../../../../docs/packs/data-storage.md)
for mutable project settings.

## Give the pack useful skills

Give the pack one main skill for its common workflow. Add an extra when a
distinct workflow needs separate instructions or should load only on demand.
Move details that crowd the normal path into extra skills or ordinary
references. Declare the main skill under `documentation`; declare each extra
as a `kind: skill` resource. References are files linked from a skill, not
skill resources. See the [skill-writing guide](references/writing-skills.md).

The [Timeline pack](../../../timeline/pack.yaml) shows this layout: its main
skill is `docs/SKILL.md`, its extras are `timeline-series`,
`timeline-narrated`, `timeline-review`, and `timeline-shots`, and its helper
guides live in `docs/references/`.

Use this decision map when an idea is underspecified:

1. **Existing capability** — discover and inspect the catalog with
   `astrid.sdk.discover()` and `astrid.sdk.get_capability(...)`. If existing
   executors can be wired to satisfy the request, add no new implementation.
2. **Action** — a callable public function. Keep its implementation and
   action-specific helpers under `actions/`, and declare the public entry in
   `pack.yaml`. A helper is private support and does not become a public action
   unless it has its own declaration.
3. **UI** — a contribution received by a host application. Keep host-specific
   code under `ui/`, and declare the public contribution in `pack.yaml`.
4. **Rendering** — renderer or rendering support owned by the pack. Declare it
   under `rendering:` in `pack.yaml` and follow the current authoring steps in
   the [pack guide](../../../../../docs/guides/create-a-pack.md); the rendering
   skill remains the downstream compatibility layer. Declaring a renderer,
   planner, or finalizer describes that contribution; it does not by itself
   register a generation entry.
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
   Add a useful pack description or optional `agent.purpose`. Optional
   `agent.normal_entrypoints` should name only preferred declared contributions
   or supported aliases; do not force action ids onto UI, rendering, documents,
   or support-only packs. Keep `agent.required_context` only for files an agent
   actually needs to read. For authored port lists, document each port's type
   and meaning, and state input requiredness without changing defaults. Keep
   schema-based contracts and their own descriptions, required structure, and
   defaults intact.
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

Use the schema and field reference linked by the pack guide instead of
inventing fields. Declare network, files, subprocesses, environment, GPU,
and external-service needs in the pack `permissions`; declare the specific
secret environment variables in the pack-level `secrets` list in `pack.yaml`.
Permission metadata is disclosure-only in the current contract.

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
required runtime/dependencies are available. Write and review the pack's tests
with the [pack testing skill](../pack-testing/SKILL.md), and regenerate the
capability catalog if a manifest changed. For rendering contributions,
use the rendering-specific validation and smoke path linked from the [current
pack guide](../../../../../docs/guides/create-a-pack.md).
