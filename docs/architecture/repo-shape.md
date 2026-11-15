# Astrid Repository Shape — M5 Canonical Contract

This document is the **M5 architecture map** for the Astrid repository. It
classifies every top-level surface, documents the enforcement model, and
records settled milestone decisions that shape the current checkout. It is
normative for M5 and should be updated when later milestones reclassify a
surface.

**M5 key rule**: New internal code belongs under `astrid/core/*`. New pack
machinery (discovery, resolver, manifest, alias resolution,
validation, CLI, installation, entrypoint, agent indexing, gitignore filtering,
schemas) belongs under `astrid/core/pack/*`. `astrid/packs/` is for pack data
only — executors, orchestrators, elements, `pack.yaml`, `skill/` — with no
top-level Python machinery except `__init__.py`. Gateway subcommand logic lives
in the `astrid/core/gateway/` package. SDK implementation lives in the
`astrid/sdk/` package.

Astrid has no local schema-pack host, schema registry, or migration directory.
The neutral workspace runtime owns product DDL, constraints, and migration
application; `astrid/packs/` manifests describe executable capabilities only.

## 1. Public Facades

### 1.1 `import astrid` — The Public SDK Boundary

| Surface | File | Classification |
| --- | --- | --- |
| `import astrid` | `astrid/__init__.py` | **Public SDK facade** — lazy-loads the v1 SDK names via `__getattr__`. The normative v1 contract is [docs/platform-contract.md](../contracts/platform-contract.md). |
| `astrid.sdk` | `astrid/sdk/` | **Public SDK package** — DTOs, exception taxonomy, serialization helpers, discovery/invoke/generate facades. Direct imports of private SDK implementation modules are out of contract for v1. |

The 28 names in `astrid.__all__` are:

- **Functions**: `discover`, `get_capability`, `invoke`, `generate`, `read_events`, `subscribe_events`
- **DTOs**: `Capability`, `DiscoveryResult`, `EventStreamRecord`, `InvocationResult`, `CapabilityHandle`, `Port`, `Output`, `AliasRecord`, `Provenance`, `SafetyDeclaration`, `ExecError`
- **Exceptions**: `AstridSDKError`, `CapabilityNotFoundError`, `CapabilityAmbiguousError`, `CapabilityValidationError`, `CapabilityMissingInputError`, `CapabilityPreconditionError`, `CapabilityRuntimeError`, `CapabilityLeaseError`, `CapabilityEventLogError`, `UnsupportedCapabilityError`, `CapabilityInvocationError`

### 1.2 CLI Entrypoints

| Surface | File | Classification |
| --- | --- | --- |
| `python3 -m astrid` | `astrid/__main__.py` | **Executable package gateway** — delegates to `astrid.core.gateway.main()`. |
| `astrid.core.gateway` | `astrid/core/gateway/` | **Subcommand router package** — runtime project resolution and command dispatch. The facade lives in `__init__.py`; implementation modules are `dispatch.py`, `help.py`, `project.py`, and `wait.py`. |
| `astrid doctor` | `astrid/core/gateway/dispatch.py` | **Runtime health diagnostic** — delegates to the generated workspace client. |
| `astrid.skills.cli` | `astrid/skills/cli.py` | **Skills CLI** — skill discovery and harness management. |
| `astrid.core.timeline` | `astrid/core/timeline/` | **Timeline value helpers** — non-authoritative validation and rendering support. Product timeline commands are runtime-client adapters. |

### 1.3 Non-Contract Surfaces (v1)

The following are explicitly out of the v1 public contract, even if importable:

- `astrid.sdk` as a direct import target
- Everything under `astrid.core.*`
- Everything under `astrid.packs.*`
- Registry internals, resolver internals, and helper functions
- CLI implementation modules and verb-routing internals
- Internal tests, fixtures, and generated discovery payloads
- Gateway implementation modules under `astrid/core/gateway/` are internal implementation detail

These surfaces may change in any minor or patch release without deprecation.

## 2. `astrid/core` — Internal Kernel

`astrid/core` is the **internal kernel** — these modules implement framework
machinery and are not part of the public SDK contract. All core subdirectories
and their purposes:

| Subdirectory | Purpose |
| --- | --- |
| `core/adapter/` | Render adapters (local, manual, remote artifact fetch) |
| `core/element/` | Element schema, registry, catalog, CLI, install |
| `core/executor/` | Executor schema, registry, runner, folder loader, CLI, banodoco catalog |
| `core/generation/` | Generation backends (base, codex, fal, vibecomfy), feature registry, verb dispatch |
| `core/integrations/` | External-service integrations (RunPod and other approved neutral adapters) |
| `core/model_catalog/` | Model registry, schema, CLI |
| `core/orchestrator/` | Orchestrator schema, registry, runner, folder loader, CLI, plan template |
| `core/pack/` | **Canonical pack machinery** (M2): discovery, resolver, manifest, alias_resolver, validate, CLI, install, entrypoint, agent_index, gitignore, schemas/v1/ |
| `core/project/` | Project schema, paths, run management, sidecar, JSON I/O, CLI |
| `core/runtime/` | In-process runtime invocation and subprocess log capture |
| `core/task_handler_registry.py` | Task-handler registration boundary for pack-owned adapters |
| `core/timeline/` | Timeline value models, validation, integrity, projection, and rendering support. Durable timeline state and schema belong to the neutral workspace runtime. |
| `core/util/` | Generic utilities (log-and-swallow, etc.) |

Loose `.py` files at `astrid/core/` are kernel helpers such as `scaffold.py`,
`search.py`, `cli_choices.py`, and `dirty.py`.
Pack machinery does not live in loose root-level core modules; it belongs under
`astrid/core/pack/`.

**Anti-coupling rules enforced by `validate_import_layering()`**:

- `astrid/core` must not import from `astrid.packs` or `astrid.packs.*`
- Core subsystems such as `astrid.core.audit`, `astrid.core.orchestrate`,
  `astrid.core.threads`, and `astrid.core.verify` may import one another as
  internal implementation modules.

### 2.1 Named Import-Layering Exemptions

These exemptions are recorded in `astrid/core/structure.py` under `_IMPORT_LAYERING_EXEMPT_REL`:

| File | Exemption Reason | Milestone |
| --- | --- | --- |

### 2.2 Contributor Placement Guidance

When adding new code to the repository, follow these placement rules:

- **New pack machinery**: `astrid/core/pack/*` (discovery, resolver, manifest, alias_resolver, validate, CLI, install, entrypoint, agent_index, gitignore)
- **New internal framework code**: `astrid/core/<domain>/*`
- **New pack data** (executors, orchestrators, elements, skills): `astrid/packs/<pack>/`
- **New public SDK surface**: add to the `astrid/sdk/` package and expose it through the package facade only when it is part of the v1 contract
- **New gateway subcommands**: add to the appropriate `astrid/core/gateway/*.py` module or create a new one; register dispatch in `astrid/core/gateway/dispatch.py`
- **New pack-domain libraries**: under the owning pack, for example
  `astrid/packs/editorial/hype/`
- **New shared utilities**: `astrid/core/util/`
- **New CLI entrypoints for subsystems**: follow the pattern of `astrid/<subsystem>/cli.py`

**Do not** add new `.py` files directly under `astrid/` — they must be listed in `TOP_LEVEL_ASTRID_FILES` in `astrid/core/structure.py` and approved as canonical top-level modules. Adding a new top-level module requires updating `TOP_LEVEL_ASTRID_FILES` in `astrid/core/structure.py` and this document.

## 3. Retired Compatibility Surfaces

The current checkout has no live Python compatibility shim modules. The old
top-level aliases for `_media`, `_paths`, `pipeline`, timeline re-exports,
pack machinery, and SDK split modules were retired in favor of canonical
imports. Historical milestone briefs may still mention those transitions, but
live architecture docs and tests should point at the canonical modules.

Canonical homes:

| Old surface | Canonical surface |
| --- | --- |
| `astrid._media` | `astrid.core.media` |
| `astrid._paths` | `astrid.core.paths` |
| `astrid.pipeline` | `astrid.core.gateway` |
| `astrid.timeline.*` | `astrid.core.timeline.*` |
| `astrid.core.pack_*`, `astrid.core.manifest`, `astrid.core.alias_resolver` | `astrid.core.pack.*` |
| `astrid.packs.{cli,validate,agent_index,gitignore,install,_canonical_entrypoint}` | `astrid.core.pack.*` |
| `astrid.sdk_*`, `astrid.sdk_results` | `astrid.sdk.*` |

## 4. Domain Subsystems

| Package | Classification | Notes |
| --- | --- | --- |
| `astrid/core/contracts/` | **Shared library** | Common schema dataclasses (ports, outputs, cache, commands, isolation, errors, run status). |
| `astrid/packs/editorial/hype/` | **Pack-owned domain library** | Hype/editing logic such as arrangement rules, enriched arrangements, and text matching. |
| `astrid/core/util/` | **Utility library** | Generic helpers (LLM client construction, environment handling). |
| `astrid/core/audit/` | **Ephemeral pack helper** | In-process provenance descriptions; durable events, receipts, and evidence are owned by the workspace runtime. It never creates or reads an audit ledger. |
| `astrid/core/verify/` | **Verification helpers** | Soft boundary — currently a convention, not a hard gate. |
| `astrid/core/modalities/` | **Modality helpers** | Modality-specific support code. |
| `astrid/skills/` | **Skill harnesses** | Agent skill discovery, registry, and harness runtimes (base, codex, claude, hermes). |
| `astrid/core/theme/` | **Shared library** | Theme resolution, CLI, and schema validation helpers. Soft boundary — convention. |
| `astrid/core/paths.py` | **Shared library** | Repository path resolution for source-only capability work. Runtime project paths come from the workspace client. |
| `astrid/core/gateway/setup.py` | **Shared library** | Setup CLI support. |

### 4.1 CLI/Domain/Import-Layering Convention

The repository enforces a strict import layering convention:

- **CLI modules** live as `<subsystem>/cli.py` for each surviving subsystem (core/pack and runtime-backed product adapters). CLI modules may import from their subsystem's internals and from shared libraries, but must not be imported by non-CLI code.
- **Pack-owned domain libraries** such as `astrid/packs/editorial/hype/` may import from core shared libraries but must not import CLI modules.
- **Core kernel** (`astrid/core/`) must not import concrete pack implementation modules except through the generic pack host.
- **Pack data** (`astrid/packs/<pack>/`) must not import from `astrid/core/` except through sanctioned entrypoints.

These rules are enforced by `validate_import_layering()` in `astrid/core/structure.py`.

## 5. `astrid/packs` — Capability Surface

`astrid/packs` is the **capability surface** — every executor, orchestrator, and
element ships in a pack under `astrid/packs/<pack>/`. The pack machinery kernel
(`astrid/core/pack/`) provides the framework; packs provide the capabilities.

**M5 rule**: `astrid/packs/` is for pack data only. Top-level Python files are
not allowed under `astrid/packs/` except `__init__.py`. New pack machinery must
go under `astrid/core/pack/*`, not under `astrid/packs/`.

### 5.1 Pack Layout

Each pack carries a `pack.yaml` with `id`, `name`, and `version`, and may
contain:

| Layout | Contents |
| --- | --- |
| `executors/<name>/` | `executor.yaml`, `run.py`, `STAGE.md`, optional local `src/` |
| `orchestrators/<name>/` | `orchestrator.yaml`, `run.py`, `STAGE.md`, optional local `src/` |
| `elements/<kind>/<id>/` | `component.tsx`, `element.yaml` |
| `skill/` | `SKILL.md` for agent-facing skill documentation |

### 5.2 Current Shipped Packs

The shipped packs are: `rendering`, `understanding`, `generation`, `editorial`,
`video_editing`, `foley`, `training`, `youtube`, `fal`, `vibecomfy`,
`runpod`, `moirae`, `iteration`, `media`, and `stream_content`.

A project-local `local` pack at `astrid/packs/local/` is an optional ordinary
source root for user-authored capabilities. Its `pack.yaml` and content
manifests are discovered like every other source pack; there is no fork state,
sidecar redirect, or local-priority rule.

The retired `builtin` pack is not shipped. Historical fixtures that referenced
it are retired; new capability work uses the canonical shipped packs.

The `_core/` directory is a **skill-only shell** — it contains only
`skill/SKILL.md` (the root Astrid gateway skill), has no `pack.yaml`, and is
not a runtime pack. It is coupled to `astrid/skills/` and treated as a
permanent visible exception.

### 5.3 Top-Level Pack Files

The only Python file directly under `astrid/packs/` is the package marker:

| File | Classification |
| --- | --- | --- |
| `__init__.py` | Package init |

`_validate_packs_top_level_modules()` in `astrid/core/structure.py` rejects any other
top-level module under `astrid/packs/`.

### 5.4 ID Qualification Rules

- Executor and orchestrator IDs are always qualified: `<pack>.<name>` (e.g., `video_editing.cut`). Bare lookups are rejected.
- Element IDs stay bare and are scoped by `kind`, so `animation/fade` and `transition/fade` coexist without collision.

### 5.5 Structure Enforcement

`validate_repo_structure()` enforces:
- Executor folders must contain `executor.yaml`, `run.py`, `STAGE.md`
- Orchestrator folders must contain `orchestrator.yaml`, `run.py`, `STAGE.md`
- Element folders must contain `component.tsx`, `element.yaml`
- Executor folders must not contain orchestrator metadata and vice versa
- The qualified ID's first segment must equal the pack ID
- Custom element kinds declared in `pack.yaml` extensions are accepted
- Top-level `.py` files under `astrid/packs/` are rejected except `__init__.py`

## 6. No Planned-Absent Package Roots

`TOP_LEVEL_ASTRID_DIRS` in `astrid/core/structure.py` is strict: every listed
directory must exist on disk, and no unlisted directory may appear under
`astrid/`. Repository documentation belongs under root `docs/`, not inside the
Python package.

## 7. Tests

Test directory conventions live in the test tree and its plan:
`tests/lanes.py` (lanes and markers), `tests/known_failures.txt` and
`tests/removed.yaml`, documented in
[docs/development/testing-plan.md](../development/testing-plan.md).

## 8. Docs

Documentation lives under `docs/` at the repository root. Key architecture and
contract documents:

| Document | Purpose |
| --- | --- |
| `docs/reference/architecture.md` | User-facing architecture overview (orchestrators, executors, elements, shared libraries, structure enforcement) |
| `docs/architecture/repo-shape.md` | **This document** — M5 canonical repo-shape contract |
| `docs/architecture/top-level-inventory.json` | Machine-readable top-level entry inventory (M5) |
| `docs/architecture/pack-layout-variants.json` | Machine-readable pack variant catalog |
| `docs/architecture/giant-file-split-candidates.json` | Giant-file split candidates with line counts (consumed by M4, now completed) |
| `docs/architecture/shim-legacy-audit.md` | (removed — audit is an ephemeral runtime hand-off described in §4 above) |
| `docs/contracts/platform-contract.md` | Normative v1 platform contract (SDK exports, SemVer, deprecation window). |
| `docs/contracts/cli-contract.md` | Agent CLI contract (stream discipline, output modes, error signaling) |
| `docs/reference/sdk.md` | User-facing SDK walkthrough |
| `docs/contracts/integration_contracts.md` | Integration contracts |

## 9. Soft Boundary Conventions (Not Hard-Gated)

The following boundaries are conventions documented here for awareness. They are
not enforced with hard gates:

| Boundary | Notes |
| --- | --- |
| `threads.ids` | Should not be imported outside the threads package; currently a convention |
| `verify` | Verification helpers should not depend on packs or orchestrate |
| `theme_schema` | Theme schema validation should remain a leaf utility |

Later milestones may choose to enforce these as hard gates.

## 10. Structure Enforcement Model

### 10.1 Validator

`astrid/core/structure.py` is the **single source of truth** for repository
structure enforcement. It exposes:

- `validate_repo_structure()` — top-level entry check, pack layout, import layering, and retired-authority checks (returns `StructureReport`)
- `validate_import_layering()` — core→packs import prohibition
- `validate_migration_completion()` — retired-authority markers, sys.modules injections, dangling `__all__` aliases, compatibility shim detection

### 10.2 Exemption Lists

All exemptions are colocated in `astrid/core/structure.py`:

| Constant | Purpose |
| --- | --- |
| `TOP_LEVEL_ASTRID_FILES` | Allowed top-level `.py` files under `astrid/` |
| `TOP_LEVEL_ASTRID_DIRS` | Allowed top-level directories under `astrid/` |
| `_IMPORT_LAYERING_EXEMPT_REL` | Files exempt from core import-layering rules |

### 10.3 Consumption

`astrid/core/doctor.py` consumes `validate_repo_structure()` and fails when
canonical repository structure drifts.

- **M4** consumed `docs/architecture/giant-file-split-candidates.json` to
  split `gateway.py` and `sdk.py`. Later cleanup folded both split surfaces into
  packages.

- **M5** updates this document to reflect the post-split layout, adds
  contributor placement guidance, formalizes the CLI/domain/import-layering
  convention, and replaces stale references with current-state documentation.
