# Pack Taxonomy

Every Astrid pack carries six first-class taxonomy fields that classify what it
is, where it comes from, how it is shipped, and how stable it is. These fields
are defined in the pack manifest (`pack.yaml`) and exposed through every
discovery surface: `python3 -m astrid.core.pack.cli list`, `status`, and
`inspect`.

## Why Taxonomy?

Before M1, packs were discovered as a flat list. Agents had no structured way
to answer "is this pack core or optional?", "is it stable enough to use in
production?", or "what domain does it belong to?" without reading free-text
descriptions. The taxonomy layer makes these answers machine-readable.

## The Six Fields

| Field | Purpose | Default | Valid values |
|---|---|---|---|
| `origin` | Where the pack came from | `"unknown"` | `unknown`, `builtin`, `external` |
| `install_tier` | How the pack is installed | `"default"` | `default`, `core`, `optional` |
| `pack_type` | What kind of package this is | `"capability"` | `capability`, `adapter` |
| `domain` | What problem area the pack serves | `"general"` | `general`, `development`, `editorial`, `generation`, `infrastructure`, `integration`, `media`, `system` |
| `stability` | How stable the pack API is | derived from `status` | `stable`, `experimental`, `deprecated` |
| `support` | Who supports this pack | `"project"` | `project`, `core`, `community` |

### `origin`

Describes the provenance of the pack. Ship with `builtin` for packs that are
part of the Astrid distribution. Use `external` for packs supplied from Git,
a registry, or scaffolded inside a user's project. Use `unknown` when the
origin cannot be determined (the default for manifests that omit the field).

**This is metadata, not identity.** The pack id (e.g., `editorial`, `fal`)
expresses the pack's *purpose* — what it does. The `origin` field tells you
*where it came from*. A pack with id `fal` and origin `external` means
"the fal adapter pack is maintained externally." A pack with id `editorial`
and origin `builtin` means "the editorial pack ships with Astrid."

### `install_tier`

Controls whether the pack is installed by default or must be opted into.

- `core` — Always present. Cannot be uninstalled. (Currently: the shipped
  capability packs.) The `_core` skill directory is not a pack — it is a
  skill-only shell with no `pack.yaml` (see §Shells and Hidden Packs).
- `default` — Installed by default but can be removed. Most scaffolded packs.
- `optional` — Must be explicitly installed.

### `pack_type`

Describes the pack's structural role:

- `capability` — A standard pack containing executors, orchestrators, and/or
  elements. This is the default for scaffolded packs and applies to most packs.
- `adapter` — A pack whose primary purpose is connecting Astrid to an external
  service or tool (e.g., `fal`, `vibecomfy`, `runpod`, `moirae`).

### `domain`

The functional area the pack operates in. This is the primary grouping axis in
CLI output and the most important field for agents to filter on.

Current shipped domains:

| Domain | Packs |
|---|---|
| `media` | `rendering`, `understanding`, `video_editing`, `foley`, `stream_content` — media creation and editing |
| `editorial` | `editorial` — transcript-to-cut editorial pipeline |
| `generation` | `generation` — image/video generation |
| `integration` | `youtube`, `fal`, `vibecomfy`, `moirae` — connections to external services |
| `development` | `iteration`, `training` — author-test-iterate tooling and model training |
| `infrastructure` | `runpod` — GPU provisioning and execution |
| `system` | `_core` is a skill-only shell (no `pack.yaml`, no capabilities) classified under §Shells and Hidden Packs |
| `general` | scaffolded packs, local scratch pack |

**`hype` is not a domain.** The `video_editing.hype` orchestrator lives inside the
`video_editing` pack (domain `media`). An orchestrator id like `video_editing.hype`
identifies a *capability*, not a pack taxonomy category. If you find yourself
wanting a `hype` domain, stop — what you really want is a capability filter
(`astrid.sdk.discover()` can filter capabilities by id or keyword).

### `stability`

Indicates the API stability of the pack. This defaults from the pack's `status`
field using a deterministic mapping:

| `status` | `stability` default |
|---|---|
| `active` | `stable` |
| `stub` | `stable` |
| `experimental` | `experimental` |
| `deprecated` | `deprecated` |

If the manifest explicitly sets `stability`, that value is used regardless of
`status`. This allows a pack to be `status: active` while still declaring
`stability: experimental` during a transitional period.

### `support`

Who is responsible for maintaining the pack:

- `core` — Maintained by the Astrid project. Covered by the same compatibility
  guarantees as the core runtime.
- `project` — Maintained by the project that scaffolded it. No upstream
  guarantee.
- `community` — Community-maintained. Best-effort support.

The proposed future distinction between a complete **Tool**, reusable UI
**Widget**, and callable **Action** is a product-design direction, not a new
V3 taxonomy. See the [future model](../guides/frontend-pack-vision.md); use the
current declarations in this page when authoring a pack.

## Pack, Capability, Category — Clearing Up the Vocabulary

These terms are easy to confuse. Here is the definitive distinction:

**Pack** — A directory with a `pack.yaml` manifest. Contains capabilities
(executors, orchestrators, elements). The pack is the *container*.

**Capability** — Something a pack can *do*. An executor, orchestrator, or
element. Identified by a qualified id like `video_editing.hype` or
`media.clip_extract`.

**Category** — Free-form text in `metadata.category`. Not a taxonomy axis.
The `--category` CLI flag filters on `metadata.category` only. If you want
structured filtering, use the taxonomy flags (`--domain`, `--origin`, etc.).

**Metadata** — Arbitrary key-value data in `pack.metadata`. Taxonomy fields
live at the top level of the manifest, not inside `metadata`. This keeps the
taxonomy contract explicit and validated.

## How Taxonomy Appears in CLI Output

### JSON Output

Every pack payload includes both top-level taxonomy fields and a nested
`taxonomy` object:

```json
{
  "id": "rendering",
  "domain": "media",
  "origin": "builtin",
  "install_tier": "core",
  "pack_type": "capability",
  "stability": "stable",
  "support": "core",
  "taxonomy": {
    "origin": "builtin",
    "install_tier": "core",
    "pack_type": "capability",
    "domain": "media",
    "stability": "stable",
    "support": "core"
  }
}
```

The flat `packs` array in `python3 -m astrid.core.pack.cli list --json` and
`status --json` is
preserved for backward compatibility. A new top-level `groups` key provides
domain-grouped views:

```json
{
  "packs": [{ "id": "rendering", "domain": "media", ... }, ...],
  "groups": [
    {
      "group_by": "domain",
      "value": "media",
      "taxonomy": { "domain": "media" },
      "packs": [{ "id": "rendering", ... }]
    }
  ]
}
```

### Plain-Text Output

Plain-text `list` and `status` group output by domain:

```text
taxonomy: domain=development
iteration       Astrid Iteration        1.0.0   origin=builtin  tier=core       type=capability  stability=stable       support=core

taxonomy: domain=media
rendering       Astrid Rendering       1.0.0   origin=builtin  tier=core       type=capability  stability=stable       support=core
```

`python3 -m astrid.core.pack.cli inspect` adds a dedicated taxonomy block:

```text
id: rendering
name: Astrid Rendering
...
taxonomy:
  origin: builtin
  install_tier: core
  pack_type: capability
  domain: media
  stability: stable
  support: core
```

## Filtering with Taxonomy

All six taxonomy fields have corresponding CLI flags:

```bash
# Filter by domain (most common)
python3 -m astrid.core.pack.cli list --domain system
python3 -m astrid.core.pack.cli status --domain media

# Filter by origin
python3 -m astrid.core.pack.cli list --origin builtin

# Filter by install tier
python3 -m astrid.core.pack.cli list --install-tier core

# Filter by pack type
python3 -m astrid.core.pack.cli list --pack-type capability

# Filter by stability
python3 -m astrid.core.pack.cli list --stability stable

# Filter by support level
python3 -m astrid.core.pack.cli list --support core
```

Multiple filters can be combined. The `--category` flag remains separate and
filters on `metadata.category` only — it is not a taxonomy filter.

```bash
# These are different filters with different semantics:
python3 -m astrid.core.pack.cli list --domain system      # taxonomy.domain
python3 -m astrid.core.pack.cli list --category system     # metadata.category
```

## Visibility and Hidden Packs

The `visibility` field is not a taxonomy axis — it controls whether a pack
appears in default discovery. Packs with `visibility: hidden` are excluded
from `packs list` and `packs status` unless `--show-hidden` is passed.

```bash
# Default discovery excludes hidden packs
python3 -m astrid.core.pack.cli list

# Include hidden packs
python3 -m astrid.core.pack.cli list --show-hidden

# Status respects visibility too
python3 -m astrid.core.pack.cli status --show-hidden

# Inspect always works for any pack (hidden or visible)
python3 -m astrid.core.pack.cli inspect rendering
```

### Example Packs vs. Runtime Packs

Not every pack directory in the repository is a runtime-discovered pack:

- **`astrid/packs/`** — Runtime packs. Discovered by `python3 -m astrid.core.pack.cli list`,
  `status`, and capability searches. Currently: `rendering`, `understanding`, `generation`,
  `editorial`, `video_editing`, `foley`, `training`, `youtube`, `fal`,
  `vibecomfy`, `runpod`, `moirae`, `iteration`, `media`,
  `stream_content`, plus the dynamically-created `local` scratch
  pack. The `_core` directory is a skill-only shell — it contains only `docs/SKILL.md`
  and is not a pack (no `pack.yaml`).

- **`examples/packs/`** — Teaching packs. These are committed reference examples
  that demonstrate pack authoring patterns (multi-step pipelines, agent-attested
  workflows, element components). They are **not** runtime-discovered — you will
  not see them in `list` output even with `--show-hidden`. Validate them
  with `python3 -m astrid.core.pack.cli validate`:

  ```bash
  python3 -m astrid.core.pack.cli validate examples/packs/minimal
  python3 -m astrid.core.pack.cli validate examples/packs/file_summarizer
  ```

  The example packs are: `minimal` (canonical external-pack contract),
  `media` (pack with elements and schemas), `file_summarizer` (multi-step text
  pipeline), `text_digest` (agent-in-the-loop text pipelines), and `text_review`
  (machine summary + agent verdict workflow).

### Shells and Hidden Packs

M2 classifies three directory types under `astrid/packs/` that are not
ordinary capability packs:

#### `_core` — Skill-Only Shell

`astrid/packs/_core/` is **not a pack**. It is an internal skill-only shell
— a directory containing only `docs/SKILL.md` (the root Astrid gateway
skill). It has no `pack.yaml`, no executors, no orchestrators, no elements,
and is not discovered by `packs list` or `packs status`.

`_core` is coupled to several internal subsystems:

- **`astrid/skills/registry.py`** — writes the auto-managed pack registry
  block into `_core/docs/creative-work/references/packs.md` via `CORE_SKILL_MD`.
- **`astrid/skills/discovery.py`** — explicitly skips `_core` during skill
  descriptor discovery since it is manifest-less.
- **`astrid/skills/harnesses/`** (base, claude, codex, hermes) — all
  recognize `pack_id == "_core"` as the gateway skill and handle it
  specially (e.g., symlinking as `astrid` rather than by pack name).
- **`astrid/skills/__init__.py`** — installs `_core` by default as the
  gateway skill, filters it from the pack list, and uses it as the fallback
  when no other packs are installed.
- **`astrid/core/pack/validate.py`** — `INTERNAL_PACK_DIRS` excludes
  `_core` from pack structure validation because it has no manifest.

M2 treats `_core` as a **permanent visible exception** — it is not moved
under `_shells/` because the coupled paths above depend on its location.

#### `stream_content` — Real Shipped Packs

This pack is **real shipped pack data** with stable pack and
capability IDs:

| Pack | Type | Capabilities | Taxonomy |
|---|---|---|---|
| `stream_content` | Mixed executor+orchestrator | `stream_content.clip_candidates`, `stream_content.segment_map`, `stream_content.distill` | origin=`builtin`, domain=`media`, install_tier=`default` |

It is discovered, listed, and inspected through the normal pack CLI
surface. Its pack id and capability ids are **stable** across M2.

### Canonical Clip Extraction

The canonical product clip extraction executor is `media.clip_extract`. It
lives in `astrid/packs/media/executors/clip_extract/` and appears in runtime
discovery as `media.clip_extract`:

```python
import astrid.sdk as sdk

for capability in sdk.discover().capabilities:
    if "clip_extract" in capability.id:
        print(capability)
```

The former scaffold-only packs `clip_tools` and `video_tools` (which contained
duplicate `clip_extract` scaffolding) have been removed. The example packs in
`examples/packs/` no longer carry `clip_extract` executor artifacts — they
demonstrate text-processing workflows, not media extraction.

## Defaults for Unspecified Fields

When a manifest omits taxonomy fields, deterministic defaults apply:

| Field | Default | Notes |
|---|---|---|
| `origin` | `"unknown"` | Explicitly declare for shipped packs |
| `install_tier` | `"default"` | Override to `core` for non-removable packs |
| `pack_type` | `"capability"` | Most packs are capabilities |
| `domain` | `"general"` | Always override for shipped packs |
| `stability` | `"stable"` | Derived from `status` if present |
| `support` | `"project"` | Override to `core` for maintained packs |

These defaults ensure backward compatibility: existing manifests that do not
declare taxonomy fields continue to work, and newly scaffolded packs start
with sensible values that authors can override.

## Scaffolded Pack Template

Running `packs new` creates a manifest with explicit taxonomy fields:

```yaml
schema_version: 1
id: my_pack
name: My Pack
version: 0.1.0
description: A pack for My Pack.
origin: external
install_tier: core
pack_type: capability
domain: system
stability: stable
support: core
```

This gives pack authors immediate visibility into the taxonomy vocabulary and
reminds them to set appropriate values before shipping.
