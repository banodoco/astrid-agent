"""Canonical registry of every ASTRID_* environment variable.

Invariant: for every constant defined here, constant_name == constant_value,
so that the registry is self-documenting and grep-safe.

Consumer modules should import from here rather than defining their own string
literals. Allowlisted operational modules may define their own private process
variables; product/runtime identity variables remain catalogued here.
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# User and workspace configuration
# ---------------------------------------------------------------------------

ASTRID_HOME = "ASTRID_HOME"
"""Root of the per-user Astrid state directory (~/.astrid). Set by the user."""

ASTRID_ENV_FILE = "ASTRID_ENV_FILE"
"""Optional path override for the shared per-user ``astrid.env`` file."""

ASTRID_PROJECTS_ROOT = "ASTRID_PROJECTS_ROOT"
"""Override for the projects root directory. Set by tests and CI environments."""

ASTRID_REMOTION_PROJECT_DIR = "ASTRID_REMOTION_PROJECT_DIR"
"""Absolute server-owned Remotion project/runtime directory for rendering.
The render task API never accepts this value from callers."""

ASTRID_NODE_EXECUTABLE = "ASTRID_NODE_EXECUTABLE"
"""Absolute server-owned Node executable used by the Remotion renderer.
The render task API never accepts this value from callers and the renderer
never resolves Node through the ambient ``PATH``."""

ASTRID_TIMELINE_SCHEMA_PYTHONPATH = "ASTRID_TIMELINE_SCHEMA_PYTHONPATH"
"""Absolute server-owned Python install root containing
``banodoco_timeline_schema`` for Remotion timeline validation."""

ASTRID_CAPABILITY_MATRIX_PATH = "ASTRID_CAPABILITY_MATRIX_PATH"
"""Absolute server-owned capability matrix path for the installed host."""

ASTRID_SOURCE_PROVENANCE_ROOT = "ASTRID_SOURCE_PROVENANCE_ROOT"
"""Absolute pinned source checkout retained for non-wheel build assets and provenance."""


ASTRID_VIBECOMFY_CHECKOUT = "ASTRID_VIBECOMFY_CHECKOUT"
"""Absolute, server-selected VibeComfy checkout at the supported revision."""

ASTRID_WARM_IDLE_SECONDS = "ASTRID_WARM_IDLE_SECONDS"
"""Seconds every warm holder (models, model servers, the Remotion frame owner)
stays resident after its last task. Overrides each holder's default; a request's
``lifecycle.idle_timeout_seconds`` still wins for its own holder."""

# ---------------------------------------------------------------------------
# Project run context
# ---------------------------------------------------------------------------

ASTRID_PROJECT_RUN = "ASTRID_PROJECT_RUN"
"""Set to ``1`` inside a project-run subprocess to signal that the process was
launched by Astrid's run machinery (not a bare CLI invocation)."""

ASTRID_PROJECT_SLUG = "ASTRID_PROJECT_SLUG"
"""Owning project slug for a project-run subprocess. Project-aware pack
entrypoints use this to enforce that managed timeline and experiment inputs
live inside the same project."""

# ---------------------------------------------------------------------------
# Task run context (propagated into subprocess env by build_child_subprocess_env)
# ---------------------------------------------------------------------------

ASTRID_TASK_RUN_ID = "ASTRID_TASK_RUN_ID"
"""UUID of the active task run; set by the task harness before launching a step."""

ASTRID_TASK_PROJECT = "ASTRID_TASK_PROJECT"
"""Project slug of the active task run; set alongside ASTRID_TASK_RUN_ID."""

ASTRID_TASK_STEP_ID = "ASTRID_TASK_STEP_ID"
"""Step identifier within the current task run."""

ASTRID_TASK_ITEM_ID = "ASTRID_TASK_ITEM_ID"
"""Optional item-level identifier within a repeating step."""

ASTRID_TASK_ITERATION = "ASTRID_TASK_ITERATION"
"""Zero-padded iteration index (e.g. ``001``) for repeat steps."""

# ---------------------------------------------------------------------------
# Identity and invocation
# ---------------------------------------------------------------------------

ASTRID_ACTOR = "ASTRID_ACTOR"
"""Identifies the actor driving the current invocation (e.g. agent:<id>).
Cleared from the child subprocess env by build_child_subprocess_env."""

ASTRID_AUTHOR_TEST = "ASTRID_AUTHOR_TEST"
"""Set to ``1`` in author-test mode: auto-approves attested gates, uses a
scratch projects root, and disables nudges. Set by test_runner.py."""

ASTRID_INTERNAL_INVOCATION = "ASTRID_INTERNAL_INVOCATION"
"""Set to ``1`` by the executor/orchestrator runner when it launches a step
subprocess, so the child can distinguish a harness-driven run from a bare CLI."""

ASTRID_STRICT_INSTRUCTION_SUBST = "ASTRID_STRICT_INSTRUCTION_SUBST"
"""Set to ``1`` to force strict substitution of ``${ASTRID_…}`` placeholders in
task operator views; raises AssertionError for unknown tokens instead of
leaving them as-is. Implied when :data:`ASTRID_AUTHOR_TEST` is set."""

# ---------------------------------------------------------------------------
# Pack discovery
# ---------------------------------------------------------------------------

ASTRID_PACKS_PATH = "ASTRID_PACKS_PATH"
"""Colon-separated list of additional pack search directories. Read by
pack_discovery to extend the built-in pack search path."""

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

ASTRID_LOG_MAX_BYTES = "ASTRID_LOG_MAX_BYTES"
"""Soft byte cap for rotating log files. Read by RotatingTextLog in
runtime/log_capture.py. Defaults to 10 MiB when unset."""

# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# External catalog (Banodoco)
# ---------------------------------------------------------------------------

ASTRID_RUNTIME_CATALOG_URL = "ASTRID_RUNTIME_CATALOG_URL"
"""URL of the Banodoco agent-executor catalog. Required when
ASTRID_RUNTIME_AGENT_EXECUTORS=1. Read by executor/banodoco_catalog.py."""

# ---------------------------------------------------------------------------
# Allowlisted modules — catalog only, consumers are contract-locked
# ---------------------------------------------------------------------------

ASTRID_STATE_HOME = "ASTRID_STATE_HOME"
"""Override for the skills state directory (~/.local/share/astrid or
~/Library/Application Support/astrid). Read by skills/state.py."""

ASTRID_AUDIT_DISABLED = "ASTRID_AUDIT_DISABLED"
"""Set to ``1`` to disable audit event recording. Read by audit/context.py."""

ASTRID_AUDIT_RUN_DIR = "ASTRID_AUDIT_RUN_DIR"
"""Directory for the current audit run. Set and read by audit/context.py."""

def get_author_test_env() -> str | None:
    """Return the canonical author-test flag value, or ``None`` when unset."""
    return os.environ.get(ASTRID_AUTHOR_TEST)


__all__ = [
    "ASTRID_ACTOR",
    "ASTRID_AUDIT_DISABLED",
    "ASTRID_AUDIT_RUN_DIR",
    "ASTRID_AUTHOR_TEST",
    "ASTRID_RUNTIME_CATALOG_URL",
    "ASTRID_HOME",
    "ASTRID_ENV_FILE",
    "ASTRID_INTERNAL_INVOCATION",
    "ASTRID_LOG_MAX_BYTES",
    "ASTRID_PACKS_PATH",
    "ASTRID_PROJECT_RUN",
    "ASTRID_PROJECT_SLUG",
    "ASTRID_PROJECTS_ROOT",
    "ASTRID_NODE_EXECUTABLE",

    "ASTRID_REMOTION_PROJECT_DIR",
    "ASTRID_CAPABILITY_MATRIX_PATH",
    "ASTRID_SOURCE_PROVENANCE_ROOT",
    "ASTRID_TIMELINE_SCHEMA_PYTHONPATH",
    "ASTRID_VIBECOMFY_CHECKOUT",
    "ASTRID_WARM_IDLE_SECONDS",
    "ASTRID_STATE_HOME",
    "ASTRID_STRICT_INSTRUCTION_SUBST",
    "ASTRID_TASK_ITEM_ID",
    "ASTRID_TASK_ITERATION",
    "ASTRID_TASK_PROJECT",
    "ASTRID_TASK_RUN_ID",
    "ASTRID_TASK_STEP_ID",
    "get_author_test_env",
]
