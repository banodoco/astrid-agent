#!/usr/bin/env python3
"""Runtime-hosted subprocess orchestrator for the hype pipeline, including refine between cut and render in pool flow.

.. note::

    The ``hype.timeline.json`` artifacts produced by the ``cut`` step (and
    consumed by downstream steps like ``refine``, ``render``, ``validate``) are
    Remotion timeline files. Managed TimelineConfig-producing steps emit the
    same validated raw container to the project timeline as
    ``timeline.config_replaced``; arrangement-only editor artifacts remain
    non-container compatibility read models."""

from __future__ import annotations

from astrid.pack_api import AstridError, render_astrid_error
from astrid.pack_api import guard_canonical_entrypoint

guard_canonical_entrypoint('video_editing.hype')
import sys

from astrid.pack_api import ProjectRuntimeError

# Extracted modules
from astrid.packs.video_editing.actions.hype.config import (  # noqa: F401 — re-exported for command compatibility
    STEP_ORDER,
    load_config,
    normalize_config,
    normalize_extra_args,
    normalize_many,
    parse_asset_entry,
    usage_error,
)
from astrid.packs.video_editing.actions.hype.parser import (  # noqa: F401 — build_parser re-exported for command compatibility
    build_parser,
    resolve_args,
)

# Extracted module
from astrid.packs.video_editing.actions.hype.project_adapter import (  # noqa: F401 — re-exported for command compatibility
    _prepare_project_main,
    _project_hype_artifact_roots,
    _project_hype_metadata,
    _project_slug_for_gate,
    _restore_project_env,
    _set_project_env,
    _system_exit_code,
)
from astrid.packs.video_editing.actions.hype.runner import (  # noqa: F401 — re-exported for command compatibility
    _apply_trim_deltas_to_arrangement,
    _asset_kind_for_sentinel,
    _brief_allow_generative_visuals,
    _clear_per_brief_sentinels,
    _coerce_frontmatter_value,
    _invalidate_downstream_sentinels,
    _notes_overlap_ratio,
    _plan_action,
    _redact_command,
    _register_run_inputs,
    _register_step_outputs,
    _rotate_editor_review,
    _run_revise,
    _run_steps_once,
    log_dir_for_step,
    parse_brief_frontmatter,
    pool_main,
    prepare_brief_artifacts,
    print_log_tail,
    run_step,
    sentinel_paths,
    should_rerun,
    step_output_root,
    write_skip_log,
)

# Extracted modules
from astrid.packs.video_editing.actions.hype.steps import (  # noqa: F401 — re-exported for command compatibility
    PER_BRIEF_SENTINELS,
    PER_SOURCE_SENTINELS,
    Step,
    _arrange_target_duration,
    _initial_facts,
    _verdict_build_cmd,
    _write_dry_run_plan,
    add_extra_args,
    asset_args,
    build_pool_cut_cmd,
    build_pool_steps,
    probe_audio_duration,
    select_steps,
    step_argv,
)


# === Main command entry point (kept separate from the callback facade) ===
# Project/gate adapter helpers imported from .project_adapter
def main(argv: list[str] | None = None) -> int:
    project_context = None
    project_env: dict[str, str | None] = {}
    effective_argv = list(sys.argv[1:] if argv is None else argv)
    try:
        try:
            project_context, effective_argv = _prepare_project_main(effective_argv)
        except ProjectRuntimeError as exc:
            raise AstridError(
                str(exc),
                recovery_command="Check project configuration and retry: astrid status",
            ) from exc
        if project_context is not None:
            project_env = _set_project_env(project_context.project_slug)
        try:
            args = resolve_args(effective_argv)
        except AstridError as exc:
            if project_context is not None:
                render_astrid_error(exc)
                return 2
            raise
        except SystemExit as exc:
            if project_context is not None:
                return _system_exit_code(exc)
            raise
        if project_context is not None:
            args.project = project_context.project_slug
        try:
            returncode = pool_main(args)
        except SystemExit as exc:
            if project_context is not None:
                return _system_exit_code(exc)
            raise
        except Exception:
            raise
        return returncode
    finally:
        _restore_project_env(project_env)


if __name__ == "__main__":
    raise SystemExit(main())
