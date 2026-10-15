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
