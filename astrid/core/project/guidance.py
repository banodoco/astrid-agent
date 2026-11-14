"""Actionable errors for commands that need a runtime project address.

Project identity is owned by the connected workspace runtime. This module is
intentionally a pure formatting/validation seam: it never reads environment
variables, preference files, the current directory, or a local project tree.
"""

from __future__ import annotations


def selected_project(
    explicit_project: str | None, *, runtime_client: object | None = None
) -> tuple[str | None, str]:
    """Resolve an explicit project or an actor-scoped runtime selection.

    ``runtime_client`` is supplied by the SDK/CLI composition root. Keeping
    the lookup injected prevents this core helper from importing a transport
    or consulting ambient process state.
    """

    if isinstance(explicit_project, str) and explicit_project.strip():
        return explicit_project, "explicit"
    getter = getattr(runtime_client, "selected_project_ref", None)
    if callable(getter):
        selected = getter()
        if isinstance(selected, str) and selected.strip():
            return selected, "runtime"
    return None, "missing"


FIRST_PROJECT_NAME = "My first project"
FIRST_PROJECT_SLUG = "my-first-project"


def format_first_project_notice(*, created: bool) -> str:
    """One-line notice printed when generation creates or selects the starter project."""

    verb = "Created and selected" if created else "Selected"
    return f'{verb} project "{FIRST_PROJECT_NAME}" for this generation.'


def format_project_required_guidance(*, operation: str) -> str:
    """Return stable recovery text for an unaddressed runtime command."""

    return (
        f"project is required for {operation}; pass --project <project> "
        "or select a current project in the workspace runtime"
    )


__all__ = [
    "FIRST_PROJECT_NAME",
    "FIRST_PROJECT_SLUG",
    "format_first_project_notice",
    "format_project_required_guidance",
    "selected_project",
]
