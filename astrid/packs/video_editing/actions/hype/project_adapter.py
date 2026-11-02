"""Project-run and gate environment adapter helpers for the hype orchestrator.

Extracted from ``run.py``.
Kept separate from the main ``run.py`` facade so that project binding,
gate command interception, and environment variable management are
isolated from the manifest-facing ``main()`` entrypoint.
"""

import argparse
import os
from pathlib import Path
from typing import Any

from astrid.pack_api import admit_orchestrator_project_run
from astrid.pack_api import project_run_env, reject_project_with_out


def _project_slug_for_gate(argv: list[str]) -> str | None:
    """Extract the ``--project`` value from *argv* for gate interception."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--project")
    parsed, _unknown = parser.parse_known_args(argv)
    return parsed.project


def _prepare_project_main(argv: list[str]) -> tuple[Any | None, list[str]]:
    """Prepare a project run context when ``--project`` is present.

    Runtime admission creates durable task/run identity; the returned path is
    an ephemeral workspace for derived pack artifacts.
    Returns ``(context, effective_argv)`` where *context* is the kernel
    admission context or ``None`` when no ``--project`` was requested.
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--project")
    parser.add_argument("--out")
    parser.add_argument("--brief")
    parser.add_argument("--brief-slug", dest="brief_slug")
    parsed, _unknown = parser.parse_known_args(argv)
    if not parsed.project:
        return None, argv
    reject_project_with_out(parsed.project, parsed.out)
    from astrid.sdk.client import AstridClient

    with AstridClient.open_from_launcher() as client:
        context = admit_orchestrator_project_run(
            project=parsed.project,
            tool_id="video_editing.hype",
            argv=["hype", *argv],
            client=client,
        )
    return context, [*argv, "--out", str(context.run_root)]


def _set_project_env(project_slug: str | None = None) -> dict[str, str | None]:
    """Capture and set project environment variables.

    Returns a prior-state dict suitable for ``_restore_project_env``.
    """
    prior = {key: os.environ.get(key) for key in project_run_env(project_slug)}
    os.environ.update(project_run_env(project_slug))
    return prior


def _restore_project_env(prior: dict[str, str | None]) -> None:
    """Restore environment variables to their *prior* state."""
    for key, value in prior.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def _system_exit_code(exc: SystemExit) -> int:
    """Extract an integer exit code from a :exc:`SystemExit` exception."""
    if isinstance(exc.code, int):
        return exc.code
    return 1


def _project_hype_metadata(args: argparse.Namespace) -> dict[str, Any]:
    """Build hype-specific project metadata for run finalization."""
    return {
        "brief_out": str(getattr(args, "brief_out", "")),
        "brief_slug": str(getattr(args, "brief_slug", "")),
        "dry_run": bool(getattr(args, "dry_run", False)),
    }


def _project_hype_artifact_roots(args: argparse.Namespace) -> list[Path]:
    """Collect artifact root paths from *args* for run finalization."""
    roots: list[Path] = []
    brief_out = getattr(args, "brief_out", None)
    if brief_out is not None:
        roots.append(Path(brief_out))
    out = getattr(args, "out", None)
    if out is not None:
        roots.append(Path(out))
    return roots
