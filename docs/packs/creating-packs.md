# Creating Astrid Packs

A pack is a reusable bundle that Astrid agents can discover and run: callable
actions, host UI contributions, rendering implementations, pack-owned project
documents, and the guidance that tells an agent when to use them. `pack.yaml`
declares the public surface; nothing is exported because a folder exists.

For pack identity, capability identity, default-enabled versus optional
placement, aliases, and in-place edits, use the contract at
[contract.md](contract.md). For the shortest route to a first pack, start at
[When and how to create a pack](../guides/create-a-pack.md). This guide is the
reference for the layout and the manifest fields.

Capability packs are not database schema packs. Do not add `schema-pack.yaml`,
SQL migrations, or a local schema registry; a `database` field fails
validation. The workspace Runtime owns product and project state, including a
generic project Documents store. A pack may define small mutable per-project
settings there while providing executable capabilities; it does not own a
second project database or durable invocation artifacts. See
[Pack data storage](data-storage.md) for the shared convention.

## Quick start

```bash
# 1. Scaffold a pack. Repeat --role for a mixed pack (default: action).
python3 -m astrid.core.pack.cli new my_video_tools --role action --role rendering

# 2. Enter the pack directory
cd my_video_tools

# 3. Edit pack.yaml and the files it declares, then validate
python3 -m astrid.core.pack.cli validate .
# valid: /path/to/my_video_tools (context=standalone)
```

`new` writes a `pack.yaml` that already validates, one `docs/SKILL.md`, and
only the role folders you asked for. `--starter wrapper` and `--starter nested`
cover wrapping an external package and placing a pack inside another
repository; see the pack-builder skill (linked below) for when to choose them.
Use `pack.yaml` as the source of truth: there is no scaffold verb for adding a
single action later, so add the file and its `actions:` entry by hand and
re-run `validate`.

## Layout

A pack is a directory whose name equals its `id` and whose root holds
`pack.yaml`. The implementation lives under four role folders; each public
declaration in `pack.yaml` points into the folder for its role.

```text
my_video_tools/
  pack.yaml              # manifest (required; the only manifest name)
  README.md              # human-facing docs
  docs/
    SKILL.md             # the one authored pack skill
    references/          # guides, templates and assets the skill links to
    templates/
  actions/               # role: callable functions
    transcribe.py
  ui/                    # role: contributions received by a host
    editor/extension.tsx
  rendering/             # role: renderers, planners, finalizers, elements
    highlight/
      renderer.yaml
      run.py
  shared/                # role: support reused by the pack's own contributions
```

| Section in `pack.yaml` | Role folder | What a declaration points at |
|---|---|---|
| `actions` | `actions/` | `invocation.path` of a `python` action must be beneath `actions/` |
| `ui` | `ui/` | `entry` must be beneath `ui/` |
| `rendering` | `rendering/` | `path` (a renderer, planner, finalizer or element descriptor) must be beneath `rendering/` |
| `documents` | none | `schema` is inline JSON Schema, or a relative path to one |

`shared/` has no section of its own: list the files you want projected as
`resources`. Other trees, such as `docs/`, `examples/`, `fixtures/`, `golden/`
and `schemas/`, are not roles; a file in one counts only when `pack.yaml`
declares its path.

Admission (`astrid/core/pack/canonical.py`) enforces these rules; every one
fails `validate` with the path and field:

- The manifest is `pack.yaml`. A `pack.yml`, `pack.json` or `schema-pack.yaml`
  beside it is an error, as is a `database` field.
- `schema_version` is `3` for new packs. Version `2` is still read for
  existing packs (it declares `content` roots and `extensions` instead of the
  four sections below; the renderer scaffold in
  [Rendering](#rendering) emits it). Any other value is rejected.
- `id` matches `^[a-z][a-z0-9_]*$` and equals the directory name. `version` is
  a three-part release version such as `0.1.0`.
- Every declared path is a safe POSIX-relative path: no leading `/`, no `..`,
  no `\`, and segments made of letters, digits, `.`, `_` and `-`. It must
  exist, stay inside the pack, and contain no symlink. Nothing may declare
  `pack.yaml` itself as a resource.
- Declaration keys are local ids matching `^[a-z][a-z0-9_-]*$`. The public id
  is `<pack id>.<key>` (`music.align`), and public ids are unique across all
  sections and aliases. An element is the exception: its key is
  `<kind>/<id>` and is its own identity.
- The manifest is read as data. Validation never imports or runs pack code, and
  does not install dependencies.

## Declarations

### Actions

An action is a callable unit of work. The key is the local id; the public id is
`<pack>.<key>`.

```yaml
actions:
  transcribe:
    description: Transcribe source audio to a transcript file.
    invocation:
      kind: python
      path: actions/transcribe.py
      function: run
    inputs:
      - name: source
        type: file
        required: true
    outputs:
      - name: transcript
        type: file
        mode: create
```

| Field | Required | Meaning |
|---|---|---|
| `description` | yes | What the action does |
| `invocation` | yes | `kind: python` with `path` (beneath `actions/`) and `function`, or `kind: command` with `command.argv` and optional `cwd`, `env`, `input_args` |
| `inputs` | yes | A list of ports (`name`, `type`, `required`, `default`, `description`) or a JSON Schema (object, boolean, or a relative path) |
| `outputs` | yes | A list of ports (adds `mode`: `create`, `create_or_replace` or `mutate`, plus `path_template`, `extension`) or a JSON Schema |
| `name`, `version`, `short_description`, `keywords` | no | Discovery metadata; `short_description` feeds the generated catalog |
| `resources` | no | Files projected with this action (`path`, `kind`) |
| `graph` | no | A composed action lists `child_actions` and `child_orchestrators` it calls |
| `cache`, `isolation`, `external_runtime`, `metadata`, `conditions`, `clip_kinds_supported`, `pipeline_requirements`, `scoped_configs` | no | Optional runtime hints |

Port `type` is one of `boolean`, `directory`, `file`, `html`, `integer`,
`json`, `number`, `path`, `string`. Port names are unique within a list.
Composition goes through the public action API (`astrid.sdk.invoke`), not a
direct import of another action's module; the working example is
`examples/packs/minimal/`.

### UI

A `ui` declaration binds a contribution to a host application.

```yaml
ui:
  editor:
    type: editor
    entry: ui/editor/extension.tsx
    target: video-editor
```

`type: editor` takes `entry` (beneath `ui/`) and optional `target`,
`compatibility` and `resources`. `type: tool` is a whole-Tool contribution: it
requires `target: astrid` and `compatibility: {host: "1"}`. The Runtime and host
decide how a contribution mounts; the pack only declares it.

### Rendering

A `rendering` declaration names a renderer, planner, finalizer or element
descriptor.

```yaml
rendering:
  highlight:
    type: renderer
    path: rendering/highlight/renderer.yaml
  effects/project-title-card:
    type: element
    path: rendering/elements/effects/project-title-card/element.yaml
    resources:
      - kind: implementation
        path: rendering/elements/effects/project-title-card/component.tsx
```

`type` is `renderer`, `planner`, `finalizer` or `element`. Validation reads each
descriptor as data and checks its identity against the declaration:

- A renderer, planner or finalizer descriptor's `id` must equal `<pack>.<key>`
  (here `my_video_tools.highlight`).
- An element descriptor's normalized `kind` and `id` must equal the declaration
  key (`effects/project-title-card`), and a `pack_id`, if present, must be the
  pack's own id.

A renderer manifest must declare `render`, a planner `plan`, and a finalizer
`finalize`; any may also declare `support`. The command is an argv prefix,
never a shell string, and runs with the pack root as its working directory. The
public facade stays `rendering.render`: a backend pack contributes an
implementation behind it and must not register a renderer under the facade id
or ask callers to import its module. The wire, artifact, profile, audio,
finalization and provenance contract is
[render-backend-v1.md](../contracts/render-backend-v1.md).

To author a renderer without hand-writing the layout, scaffold the four-file
renderer pack (`pack.yaml`, `renderer.yaml`, `render.py`, `test_renderer.py`):

```bash
python3 -m astrid.core.rendering.cli create wave acme_wave
python3 -m astrid.core.rendering.cli validate acme_wave
```

That scaffold is a flat schema-2 pack (its renderer is declared under
`extensions.rendering`, and its id is `<dest>.<name>`, here `acme_wave.wave`),
discoverable from its source checkout as it stands. Follow the golden path in
[render-backend-v1.md](../contracts/render-backend-v1.md#renderer-author-golden-path)
for the test, discovery and replay steps. A pack that mixes actions with a
renderer uses the schema-3 `rendering:` section above instead.

### Documents

A `documents` declaration names a Runtime project document the pack owns and
the schema of its content.

```yaml
documents:
  settings:
    format_version: 1
    schema: docs/references/settings.schema.json
```

`format_version` is an integer of at least 1 or a non-blank string. Keep code,
schemas, assets and static defaults as ordinary declared resources, and put a
pack's mutable per-project configuration in the Runtime document. The Runtime
document version is a write-conflict version; version the JSON content schema
separately and let the owning pack validate and migrate it. User and project
preferences remain Markdown instructions. For the ID convention, SDK and CLI
examples, CAS recovery, source-path authority and scaling guidance, follow
[Pack data storage](data-storage.md).

## Other `pack.yaml` fields

- **Identity**: `schema_version`, `id`, `name`, `version`, `description`.
- **Taxonomy**: `status`, `visibility`, `domain`, `stability`, `support`,
  `keywords`. See [pack-taxonomy.md](pack-taxonomy.md) for the vocabulary.
- **`capabilities`**: a list of local ids (`^[a-z][a-z0-9_]*$`) summarizing the
  pack's public capabilities.
- **`documentation`**: `{kind: skill, path: docs/SKILL.md}`,
  `{kind: agents, path: AGENTS.md}`, or `{kind: none, reason: ...}`. Author
  exactly one ordinary `docs/SKILL.md` with `name` and `description`
  frontmatter; link adjacent guides, references, templates and assets normally.
- **`agent`**: `purpose`, `normal_entrypoints` (public ids),
  `do_not_use_for`, `required_context` (declared paths an agent should read
  first).
- **`resources`**: `{path, kind}` files projected with the pack (`reference`,
  `template`, `schema`, `shared`, `implementation`, `documentation`, ...).
- **`authoring_only`**: `{path, kind, reason}` files that live in the tree for
  authors and are excluded from the Runtime projection.
- **`dependencies`**: `python`, `npm` and `system` lists. Declaring them does
  not install them.
- **`secrets`**: pack-level `{name, required, description}` entries naming the
  environment variables the pack reads. Values never go in the manifest.
- **`astrid_version`**: a compatibility string.
- **`permissions`** and **`aliases`**, below.

### Permissions

The `permissions` array lists the capability domains the pack needs. Permissions
are **disclosure metadata only** in v1: they are not enforced at runtime, do not
configure sandboxing, and do not restrict what the pack can do. Unknown keys
are rejected.

| Field | Required | Description |
|---|---|---|
| `id` | yes | One of the six ids below |
| `reason` | yes | Non-blank explanation of why the pack needs it |
| `access` | no | What is accessed (for example "reads input videos") |
| `services` | no | External services contacted (for example `api.openai.com`) |

| ID | Meaning |
|---|---|
| `project_files` | Reads or writes files in the project directory |
| `network` | Makes outbound network connections |
| `subprocess` | Spawns child processes |
| `environment` | Reads environment variables |
| `accelerator` | Uses GPU or specialized hardware |
| `external_services` | Calls paid or third-party cloud services |

```yaml
permissions:
  - id: network
    reason: Calls OpenAI and fal.ai APIs for AI generation
    services: [api.openai.com, api.fal.ai]
  - id: environment
    reason: Reads OPENAI_API_KEY from environment for API authentication
  - id: subprocess
    reason: Runs ffmpeg for video encoding
```

An empty array (`permissions: []`) is valid. Permissions say what kind of thing
the pack does; `secrets` say which specific variables it reads. A pack that
calls an external API typically declares both. Neither is enforced: Astrid does
not intercept, filter or audit environment reads, and source-discovered pack
code runs with your user's process permissions. Inspecting a pack shows its
permissions in a trust summary with that disclosure.

### Aliases

A pack can declare alternate ids that resolve to a canonical capability, which
keeps references working when a capability is renamed.

```yaml
aliases:
  - kind: renderer
    alias: video_tool.legacy
    canonical_id: video_tool.highlight
```

- `kind` is one of `executor`, `orchestrator`, `renderer`, `planner` or
  `finalizer`.
- `alias` and `canonical_id` are both `<pack>.<id>`, distinct, and both owned by
  this pack. Declare an alias on the pack that owns the canonical capability.
- An alias cannot reuse the public id of a declared action, UI, rendering or
  document entry.

## Discovery and inspection

```bash
# List discovered packs (grouped by taxonomy domain in plain text)
python3 -m astrid.core.pack.cli list
python3 -m astrid.core.pack.cli list --json

# Filter by taxonomy fields
python3 -m astrid.core.pack.cli list --domain system
python3 -m astrid.core.pack.cli list --origin builtin
python3 -m astrid.core.pack.cli list --stability stable
python3 -m astrid.core.pack.cli list --install-tier core

# Include packs marked visibility: hidden (excluded by default)
python3 -m astrid.core.pack.cli list --show-hidden

# Inspect one pack and its declared sections (always includes hidden packs)
python3 -m astrid.core.pack.cli inspect my_video_tools
python3 -m astrid.core.pack.cli inspect my_video_tools --json

# Validate every discovered pack and show its effective status
python3 -m astrid.core.pack.cli status
python3 -m astrid.core.pack.cli status --json
```

The pack CLI is an internal module surface; `packs` is intentionally not a
ninth top-level gateway family. `list`, `status` and `inspect` include
additional pack collections from `ASTRID_PACKS_PATH` (colon-separated) or an
explicit repeatable `--pack-root PATH` flag; these scopes match SDK discovery
and render invocation scope and do not modify the repository's local pack.
`status` annotates a pack whose `agent.purpose` is
`"TODO: describe what this pack is for"` with an effective status of `stub`;
that annotation is runtime-only.

## Validation

`python3 -m astrid.core.pack.cli validate <path>` is static. It checks that:

- `pack.yaml` is valid YAML with no duplicate keys and validates against the
  schema for its `schema_version`
  (`astrid/core/pack/schemas/v3/pack.json` for new packs);
- the id matches the directory, declaration keys and public ids are valid and
  unique, and every declared path obeys the layout rules above;
- every declared file exists inside the pack with no symlink, including the
  documentation pointer, `required_context`, `resources`, and each
  declaration's own `resources`;
- each rendering descriptor's identity matches its declaration.

Errors name the pack and the offending field, for example:

```text
[layout] my_video_tools: actions.transcribe.invocation.path must be beneath actions/
[layout] my_video_tools: resource 'docs/references/settings.schema.json' is missing or escapes owner root
[layout] my_video_tools: rendering.highlight.path: descriptor identity 'my_video_tools.wave' must match 'my_video_tools.highlight'
```

Validation does not import or execute pack code, require a bound Astrid
session, or install dependencies, and it does not prove runtime behavior. Write
the pack's tests as described in the
[pack testing skill](../../astrid/packs/_core/docs/pack-testing/SKILL.md), and
regenerate the capability catalog after a manifest change with
`python scripts/gen_capability_index.py`.

## Reference examples

`examples/packs/` holds teaching packs. They are not runtime-discovered (they
live outside `astrid/packs/`); see [`examples/README.md`](../../examples/README.md).

- `minimal`: one action and one composed action, the smallest useful pack.
- `file_summarizer`: a multi-step text pipeline.
- `text_digest`: agent-in-the-loop text pipelines.
- `text_review`: machine summary plus agent verdict.
- `media`: actions, a JSON Schema, and a rendering element resource.

```bash
python3 -m astrid.core.pack.cli validate examples/packs/minimal
python3 -m astrid.core.pack.cli validate examples/packs/media
```

## Related guides

- [pack-builder skill](../../astrid/packs/_core/docs/pack-builder/SKILL.md):
  deciding what to build and the authoring steps an agent follows.
- [pack-testing skill](../../astrid/packs/_core/docs/pack-testing/SKILL.md):
  where a pack's tests go, which lane they run in, and how to test an action.
- [pack-taxonomy.md](pack-taxonomy.md): the taxonomy fields and how to filter
  by them.
- [contract.md](contract.md): pack identity, capability identity, aliases and
  placement.
- [data-storage.md](data-storage.md): mutable per-project pack settings.
- [discovery-for-agents.md](../guides/discovery-for-agents.md): how a cold agent
  discovers capabilities with `astrid.sdk.discover()` and
  `astrid.sdk.get_capability()`.

Remote registries and per-pack dependency isolation are not supported.
Pack-hosted app tools and interface extensions beyond the `ui` section are a
separate architecture proposal; see
[Pack-Hosted Tools and Interface Contributions](../architecture/pack-hosted-interfaces.md).
