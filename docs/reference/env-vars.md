# Astrid Environment Variables

Stage1 runtime connection and the remaining Astrid process variables.

## Stage1 runtime connection

The live CLI and SDK connect to the Astrid Runtime. The runtime
owns durable projects, media, receipts, events, and execution state; Astrid
does not select a project root or open a local database/CAS.

| Variable | Who sets | Effect |
|---|---|---|
| `ASTRID_RUNTIME_CREDENTIAL` | Runtime launcher / operator | Path to the credential/token file used by the generated workspace client. |
| `ASTRID_RUNTIME_SOURCE_MANIFEST` | Astrid launcher / operator | Existing Astrid source-profile manifest passed to neutral bootstrap. |
| `ASTRID_RUNTIME_DATA_ROOT` | Astrid launcher / operator | Absolute installation-owned support root. Defaults to `Astrid/.astrid-data` from the checkout config (or `~/.astrid-data` for a wheel install); overrides the macOS `Library/Application Support/Astrid Runtime` location. |
| `ASTRID_COMPUTE_PROFILE` | RunPod executor / operator | Optional user compute-profile id loaded from `~/.astrid/compute-profiles/<id>.json`. |

These are runtime composition variables, not project-store overrides. Astrid's
installer reads the configured manifest when it installs or repairs the local
service. Run the install line again to apply service changes. `AstridClient.open(...)`
takes all connection values explicitly and does not read these variables.

The `ASTRID_*` registry below remains useful for pack subprocesses, tests, and
authoring tools. Variables marked **historical/internal** are not live workspace
authority and must not be used to configure product state.

**Source of truth**: `astrid/core/env_vars.py`.  All constants listed here are
defined in that module following the invariant `constant_name == constant_value`
(the constant identifier equals the env-var string it names).  Use the constant
rather than the bare string in any new code.

There are no legacy-name exceptions. `get_author_test_env()` reads the
canonical `ASTRID_AUTHOR_TEST` key only.

---

## Session and home

Astrid Stage1 has no session-binding or home-directory state authority. Do not
set session identifiers or project-root overrides to configure a product
command; project and actor context come from the runtime request.

## Renderer and subprocess context

| Constant | Env var | Who sets | Who reads | Effect |
|---|---|---|---|---|
| `ASTRID_REMOTION_PROJECT_DIR` | `ASTRID_REMOTION_PROJECT_DIR` | Release operator | Remotion renderer | Absolute server-owned Remotion project with `node_modules`; never accepted from task input. |
| `ASTRID_NODE_EXECUTABLE` | `ASTRID_NODE_EXECUTABLE` | Release operator | Remotion renderer | Absolute server-owned executable Node path. Readiness performs a bounded `--version` probe; never resolved from `PATH` or accepted from task input. |
| `ASTRID_TIMELINE_SCHEMA_PYTHONPATH` | `ASTRID_TIMELINE_SCHEMA_PYTHONPATH` | Release operator | Remotion renderer | Absolute server-owned install root containing `banodoco_timeline_schema`; validated by module origin before Remotion-only admission. |
| `ASTRID_WARM_IDLE_SECONDS` | `ASTRID_WARM_IDLE_SECONDS` | User / operator | Pack host warm holds (`core/execution/warm_hold.py`) | Seconds every warm holder stays resident after its last task before the host releases it. Overrides each holder's default (600 s for model holders; 45 s for the Remotion frame owner, or 300 s with `ASTRID_REMOTION_WARM_MEMORY_BUDGET_BYTES` of at least 512 MiB). A request's `lifecycle.idle_timeout_seconds` still wins for its own holder. Idle release ends the Wan2GP child and shuts down the frame owner. For VibeComfy it unloads models and keeps the engine process, which the Runtime selected and the host cannot restart; whether GPU or unified memory actually came back is not measured. `astrid doctor` lists each hold under `warmHolds` with `on_idle` saying exactly this. |

## Project run context

| Constant | Env var | Who sets | Who reads | Effect |
|---|---|---|---|---|
| `ASTRID_PROJECT_RUN` | `ASTRID_PROJECT_RUN` | `project/runtime.py` (`project_run_env`) | Child processes | Set to `1` inside a runtime-bound subprocess to distinguish a harness-driven invocation from a bare CLI call. |

## Task run context

These are propagated into subprocess env by `build_child_subprocess_env`.

| Constant | Env var | Who sets | Who reads | Effect |
|---|---|---|---|---|
| `ASTRID_TASK_RUN_ID` | `ASTRID_TASK_RUN_ID` | Task harness (`task/env.py`) | `task/env.py`, operator view | UUID of the active task run. |
| `ASTRID_TASK_PROJECT` | `ASTRID_TASK_PROJECT` | Task harness | `task/env.py`, `task/hook.py` | Project slug of the active task run. |
| `ASTRID_TASK_STEP_ID` | `ASTRID_TASK_STEP_ID` | Task harness | `task/env.py` | Step identifier within the current task run. |
| `ASTRID_TASK_ITEM_ID` | `ASTRID_TASK_ITEM_ID` | Task harness | `task/env.py` | Optional item-level identifier for repeating steps. |
| `ASTRID_TASK_ITERATION` | `ASTRID_TASK_ITERATION` | Task harness | `task/env.py` | Zero-padded iteration index (e.g. `001`) for repeat steps. |

## Identity and invocation

| Constant | Env var | Who sets | Who reads | Effect |
|---|---|---|---|---|
| `ASTRID_ACTOR` | `ASTRID_ACTOR` | Session/task subsystem | `task/env.py` | Identifies the actor driving the invocation (e.g. `agent:<id>`).  Cleared from the child subprocess env. |
| `ASTRID_AUTHOR_TEST` | `ASTRID_AUTHOR_TEST` | `orchestrate/test_runner.py` | `task/env.py` (`is_author_test_mode`) | Set to `1` in author-test mode: auto-approves attested gates, uses a scratch projects root. |
| `ASTRID_INTERNAL_INVOCATION` | `ASTRID_INTERNAL_INVOCATION` | `executor/runner.py`, `orchestrator/runner.py` | `executor/runner.py`, `task/command_render.py` | Set to `1` by the runner when launching a step subprocess, so the child can skip certain prompts. |
| `ASTRID_STRICT_INSTRUCTION_SUBST` | `ASTRID_STRICT_INSTRUCTION_SUBST` | User / CI | `task/operator/view.py` | Set to `1` to enforce strict substitution of `${ASTRID_…}` placeholders; raises on unknown tokens.  Implied when `ASTRID_AUTHOR_TEST` is set. |

## Pack discovery

| Constant | Env var | Who sets | Who reads | Effect |
|---|---|---|---|---|
| `ASTRID_PACKS_PATH` | `ASTRID_PACKS_PATH` | Authoring/tests | `core/pack/discovery.py` | Optional authoring/test discovery path. Stage1 live capability registration is checkout/runtime-owned; this does not install or update a live pack. |

## Logging

| Constant | Env var | Who sets | Who reads | Effect |
|---|---|---|---|---|
| `ASTRID_LOG_MAX_BYTES` | `ASTRID_LOG_MAX_BYTES` | User / CI | `runtime/log_capture.py` (`RotatingTextLog`) | Soft byte cap for rotating log files.  Defaults to 10 MiB when unset. |

## Feature flags / opt-outs

| Constant | Env var | Who sets | Who reads | Effect |
|---|---|---|---|---|
| `ASTRID_NO_NUDGE` | `ASTRID_NO_NUDGE` | User | `skills/__init__.py` | Set to `1` to suppress the skill-nudge banner. |

## External catalog (Banodoco)

| Constant | Env var | Who sets | Who reads | Effect |
|---|---|---|---|---|
| `ASTRID_RUNTIME_CATALOG_URL` | `ASTRID_RUNTIME_CATALOG_URL` | User / CI | `executor/banodoco_catalog.py` | URL of the Banodoco agent-executor catalog.  Required when `ASTRID_RUNTIME_AGENT_EXECUTORS=1`. |

## Internal/authoring-only variables

The following directories contain references to `ASTRID_*` env vars but are
**not required** to import from `env_vars.py`. Their constants are catalogued
for historical or authoring purposes only; none is a live workspace authority.

| Directory | Reason |
|---|---|
| `astrid/core/audit/` | Separate concern; defines its own context (`ASTRID_AUDIT_*`). |
| `astrid/packs/` | Executor `run.py` files that do not import astrid core. |

### Additional env vars in allowlisted modules

| Constant | Env var | Module | Effect |
|---|---|---|---|
| `ASTRID_STATE_HOME` | `ASTRID_STATE_HOME` | `skills/state.py` | Overrides the skills state directory (default: `~/.local/share/astrid` or `~/Library/Application Support/astrid`). |
| `ASTRID_AUDIT_DISABLED` | `ASTRID_AUDIT_DISABLED` | `audit/context.py` | Set to `1` to disable audit event recording. |
| `ASTRID_AUDIT_RUN_DIR` | `ASTRID_AUDIT_RUN_DIR` | `audit/context.py` | Directory for the current audit run. |
