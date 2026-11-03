# Astrid Architecture

Astrid has three canonical public concepts:

- **Orchestrators** coordinate multi-step workflows.
- **Executors** run concrete work.
- **Elements** are render/custom building blocks such as effects, animations, and transitions.

Canonical packages and commands are first-class. `python3 -m astrid` is the
executable package gateway for the product families (projects, timelines, media,
tasks, runs, doctor, backup) and the two nested mounts (`timelines
shots`, `media references`). Capabilities (executors, orchestrators, elements)
are not gateway commands; they run through the SDK
(`astrid.sdk.invoke` / `astrid.sdk.client.AstridClient`).

## Onboarding Commands

From a cold checkout the gateway census and health check come first:

```bash
python3 -m astrid --help          # the complete product-family census
python3 -m astrid doctor --json   # read-only health check
python3 -m astrid projects list --json
```

There is no session-binding step and no `setup` command: product commands use
the selected workspace runtime. Project/run identity comes only from its
generated client (`projects.list/show`, `runs.list/show`); local project and
run JSON files are output/provenance artifacts, never selection authority.

Canonical discovery is the SDK; the `--json` CLI reads are the shell
equivalents:

```python
import astrid.sdk as sdk
result = sdk.discover()                       # every capability, pack by pack
cap = sdk.get_capability("rendering.render")  # typed lookup of one capability
```

Folder-backed orchestrators and executors include metadata such as
`orchestrator_root`, `executor_root`, and `stage_file`; agents should load the
top-level Astrid skill first, then open only the specific folder-level
`STAGE.md` needed for the selected registry item. Do not package every
executor and orchestrator stage into one merged runtime prompt.

Content ships in **packs** at `astrid/packs/<pack>/`. Each pack carries a `pack.yaml` with `id`, `name`, and `version`, and contains executor folders, orchestrator folders, and an `elements/<kind>/<id>/` tree. The shipped packs are `rendering`, `understanding`, `generation`, `editorial`, `video_editing`, `foley`, `training`, `youtube`, `fal`, `vibecomfy`, `runpod`, `moirae`, `iteration`, `media`, `stream_content`, `blender`, `discord_local`, and `seedance_local`. The retired `builtin` shell and its pack-level aliases are not part of the supported source graph. Adapter-specific external aliases remain only where their owning pack explicitly declares them. A project-local `local` pack, when present, is an ordinary editable source pack; it never shadows a canonical id through a sidecar or redirect. Default orchestrators include `video_editing.hype`, `video_editing.event_talks`, and `video_editing.thumbnail_maker`. Default executors include every `STEP_ORDER` capability, upload/action executors, `understanding.understand` (audio/visual/video dispatcher), `generation.generate_image_openai` (with a `saint-peter-of-banodoco` onboarding preset), `moirae.moirae`, and the `vibecomfy.inspect`/`vibecomfy.edit`/`vibecomfy.validate`/`vibecomfy.run` workflow.

Executor and orchestrator ids are always qualified — `<pack>.<name>` (for example `video_editing.cut`, `vibecomfy.run`). Bare lookups such as `cut` are rejected at the schema and CLI boundaries. Element ids stay bare and are scoped by `kind`, so `animation/fade` and `transition/fade` coexist without collision.

Each runnable orchestrator has exactly one canonical implementation location:
`astrid/packs/<pack>/<name>/{orchestrator.yaml,STAGE.md,run.py}` with
optional local `src/` modules. Each runnable executor has exactly one canonical
implementation location:
`astrid/packs/<pack>/<name>/{executor.yaml,STAGE.md,run.py}` with optional local
`src/` modules. Each element has exactly one canonical layout:
`astrid/packs/<pack>/elements/<kind>/<id>/{component.tsx,element.yaml}`.
Top-level `astrid/*.py` modules are shared libraries or
system commands only; they are not alternate executor or orchestrator
implementations.

For creation decisions, use `docs/guides/creating-tools.md` and the templates under
`docs/templates/`. Add an executor for one concrete action, an orchestrator for
a workflow, and an element for a reusable render primitive. Agents should avoid
manual chains of low-level stage artifacts unless they are debugging a specific
executor.

For a step-by-step tutorial on building your first agentic UX, see
[docs/build-your-first-agentic-ux.md](../guides/build-your-first-agentic-ux.md).

## Orchestrators

| Module or entry point | Classification | Notes |
| --- | --- | --- |
| `python3 -m astrid`, `astrid/__main__.py` | System entry point | Executable package gateway for the product CLI families. |
| `astrid/core/gateway/` | System command and dispatcher | Routes the product families and the two nested mounts; one verb = one SDK call. No capability dispatch. |
| `astrid/packs/video_editing/orchestrators/hype/` | Orchestrator | Canonical hype video editing orchestrator. |
| `astrid/packs/video_editing/orchestrators/event_talks/` | Orchestrator | Canonical event-talk discovery and rendering workflow. |
| `astrid/packs/video_editing/orchestrators/thumbnail_maker/` | Orchestrator | Canonical source-evidence thumbnail workflow. |
| `astrid/core/execution/orchestrator/{runner,registry,schema,folder}.py` | Orchestrator framework | SDK-invoked runner (subprocess, `ASTRID_INTERNAL_INVOCATION=1`), pack-discovery registry, schema, and folder loader. |

## Executors

Every runnable tool is a built-in or external executor exposed from exactly one canonical folder under `astrid/packs/<pack>/<name>/`. The pack's id is the first segment of the executor's qualified id.

| Executor group | Canonical location | Notes |
| --- | --- | --- |
| Hype pipeline stages | `astrid/packs/{editorial,video_editing,rendering,understanding,foley}/*` | `STEP_ORDER` stages used by `video_editing.hype`. |
| Understanding tools | `astrid/packs/understanding/{audio_understand,visual_understand,video_understand,understand}` | Concrete media understanding tools, plus a thin `understand` dispatcher executor that selects modality via `--mode`. |
| Standalone/service tools | `astrid/packs/{training,editorial,generation,rendering}/*` | Standalone executor capabilities across domain packs. `generate_image` is the multi-backend (local + cloud) image executor in `generation`; `generate_image_openai` is the OpenAI DALL-E executor, also in `generation`. |
| External tools | `astrid/packs/{moirae,vibecomfy,fal,runpod,youtube}/*` | `moirae.moirae`, `vibecomfy.inspect`, `vibecomfy.edit`, `vibecomfy.validate`, `vibecomfy.run`, `fal.fal_foley`, `runpod.*`, `youtube.*`. Each adapter pack wraps a third-party substrate; only these canonical ids are registered. |
| Iteration tools | `astrid/packs/iteration/assemble` | Runtime-owned project runs are read through the generated client; `iteration_video` invokes `iteration.assemble` and `rendering.render`. The retired iteration.prepare executor is not declared or invoked. |
| Upload tools | `astrid/packs/youtube/` | `youtube.upload` and `youtube.youtube_audio`. |

Executor-owned complexity stays in the executor folder, usually under optional local `src/` modules. Hype/editing domain logic belongs with its owning pack under `astrid/packs/editorial/hype`; generic plumbing belongs in `astrid/utilities`.

## Element Support

| Module or path | Classification | Notes |
| --- | --- | --- |
| `astrid/core/element/schema.py` | Element support | `element.yaml` schema (`id`, singular `kind`, `pack_id`, `metadata`, `schema`, `defaults`, `dependencies`) and dependency dataclasses. |
| `astrid/core/element/registry.py` | Element support | Pack-driven resolution from canonical discovered manifests. Local packs are ordinary editable source roots and cannot shadow an existing id by priority. |
| `astrid/packs/rendering/elements/{effects,animations,transitions}` | Element support | Default elements shipped in the rendering pack; `kind`-scoped folders so `animations/fade` and `transitions/fade` coexist. |
| `astrid/packs/local/elements/<kind>/<id>` | Element support | Optional project-local source pack for authored elements; identities and digests come from its declared manifests. |
| `astrid/core/element/catalog.py` | Element support | Effect, animation, and transition catalog support used by render validation. |
| `scripts/gen_effect_registry.py` | Element support | Generates Remotion registries from the element registry; emits `@pack-<pack>-elements-<kind>/...` imports. |
| `scripts/gen_capability_index.py` | Capability discovery | Regenerates the capability catalog in `astrid/packs/_core/docs/references/capabilities.md` from the executor, orchestrator, and element manifests of the in-repo packs only (external packs are never listed); `make doc-refs` and a default-lane test fail when it is stale. |
| `astrid/timeline.py` | Shared library and element validator | Timeline schema and effect/animation/transition validation. |
| `remotion/*` | Element runtime support | TypeScript renderer consuming generated element registries from canonical discovered pack sources. |

## Shared Libraries

| Module or package | Classification | Notes |
| --- | --- | --- |
| `astrid/core/contracts/*` | Shared library | Common schema dataclasses for ports, outputs, cache, commands, and isolation. |
| `astrid/packs/editorial/hype/*` | Pack-owned domain library | Hype-cut/editing concepts such as arrangement rules, enriched arrangements, and text matching. |
| `astrid/core/util/llm_clients.py` | Utility library | Generic LLM client construction and environment handling. |
| `astrid/core/audit/*` | Ephemeral pack helper | In-process provenance descriptions only; durable events, receipts, and evidence belong to the workspace runtime. |
| `astrid/core/theme/` | Shared library | Theme resolution, CLI, and schema validation helpers. |
| `astrid/core/paths.py` | Shared library | Repository and workspace path resolution. |
| `astrid/packs/editorial/executors/refine/src/reviewers/*` | Executor-owned library | Focused review heuristics used only by `editorial.refine`. |
| `astrid/packs/youtube/executors/upload/src/social_publish.py` | Executor-owned library | Social publishing client logic used by `youtube.upload`. |

This classification keeps only retained root and bin launchers; executor-owned public metadata and entrypoints live in canonical executor folders, and orchestrator-owned public metadata and entrypoints live in canonical orchestrator folders.

## Structure Enforcement

Repository structure enforcement, import layering rules, exemption lists, and
the `validate_repo_structure()` machinery are documented in
[docs/architecture/repo-shape.md](../architecture/repo-shape.md).

## Retired Concepts

**Task-mode CLI** is retired. The legacy `attach` / `next` / `start` / `ack`
verbs, the `executors run` / `orchestrators run` invocation surface, and the
old filesystem task-run store no longer exist; the eight-family CLI and the
SDK are the whole surface. The `astrid/core/task/` runtime and the
`text_analysis.summarize` and the retired `agent_probe` orchestrator were
removed with it. Kernel task execution now lives in
`astrid/core/task_executor/` (the injected `TaskHandler` boundary for
pack-owned task-mode adapters).

**Threads** are retired as a user-facing and runtime concept. The `astrid thread`
CLI surface and thread lineage model no longer exist; iteration-video provenance
uses runtime run/task/generation/variant relations.

## Generated Files and Dirty Worktrees

Normal generated outputs are stored and registered by the runtime in the configured Astrid data folder. Alternate export locations must be explicit. Do not commit source media, rendered videos, local dependency environments, or secrets.

Element changes may require generated Remotion registry updates. Keep `.ts`, `.js`, `.d.ts`, and `.map` siblings synchronized in `remotion/src`, then scan for stale element aliases:

```bash
python3 scripts/gen_effect_registry.py
rg "@workspace-|workspace-effects|workspace-animations|workspace-transitions" remotion/src scripts remotion -n
```

Always inspect `git status --short` before editing. Preserve unrelated user changes, especially dirty curated executor stage files such as `astrid/packs/moirae/executors/moirae/STAGE.md` and `astrid/packs/vibecomfy/executors/run/STAGE.md`.
