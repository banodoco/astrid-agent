"""Shared per-turn Astrid preference context for agent integrations.

Reads use the public Runtime client; this module never opens a Runtime database
or persists conversation messages.
"""

from __future__ import annotations

import argparse
import os
import unicodedata
from collections.abc import Mapping
from contextlib import ExitStack
from typing import Any

PREFERENCE_HELPER = """Preference helper
Current explicit instructions override project preferences, which override user preferences,
then ordinary defaults. Preferences never grant permissions or override system rules.
Fetch current state with `astrid preferences [--project PROJECT]`; use
`astrid preferences user` / `astrid preferences project [--project PROJECT]` for one scope.
For generation defaults, inspect `astrid preferences defaults show`; persist a lasting choice
with `astrid preferences defaults set SLOT[/MODE] ENTRY`, or return to the built-in with
`astrid preferences defaults reset SLOT[/MODE]`. “Remember my default” for a generation
workflow or model always uses that CLI, never Markdown preferences. Use Markdown preferences
for other lasting guidance; “for this project” saves project preferences. Temporary choices
stay in this conversation. Read `skill://astrid` for full guidance."""


def _safe_text(value: str) -> str:
    # Preferences are prose, not control delimiters for the host extension.
    for tag in ("astrid_generated_context", "astrid_agent_context", "astrid_editor_context"):
        value = value.replace(f"<{tag}>", f"&lt;{tag}&gt;").replace(
            f"</{tag}>", f"&lt;/{tag}&gt;"
        )
    return "".join(
        char for char in value
        if char in "\n\t" or unicodedata.category(char) != "Cc"
    )


_SCOPE_LIMIT = 6_000
_TOTAL_LIMIT = 12_000


def _bounded(content: str, limit: int, scope: str) -> str:
    if len(content) <= limit:
        return content
    pointer = f"\n[Truncated. Run `astrid preferences {scope}` for the full text.]"
    return content[: max(0, limit - len(pointer))].rstrip() + pointer


def render_context(
    preferences: Mapping[str, Any] | None,
    *,
    error: str | None = None,
) -> str:
    if os.environ.get("ASTRID_PREFERENCES_IN_PROMPT") == "0":
        return ""
    sections = [PREFERENCE_HELPER]
    if error:
        return "Preferences unavailable. Run `astrid preferences` to read current preferences."
    else:
        remaining = _TOTAL_LIMIT - len(PREFERENCE_HELPER) - 4
        for scope in ("user", "project"):
            resource = (preferences or {}).get(scope)
            heading = scope.title() + " preferences"
            if resource is None:
                section = (
                    heading
                    + "\n"
                    + ("No project selected." if scope == "project" else "No saved preferences.")
                )
            else:
                content = _safe_text(str(resource.get("content", "")))
                identity = (
                    f" (project {resource['project_id']})"
                    if scope == "project" and resource.get("project_id")
                    else ""
                )
                heading += identity
                budget = min(_SCOPE_LIMIT, max(0, remaining - len(heading) - 1))
                body = _bounded(content, budget, scope) if content.strip() else "No saved preferences."
                section = heading + "\n" + body
            sections.append(section)
            remaining -= len(section) + 2
    return "\n\n".join(sections)


def current_context(
    *,
    project: str | None = None,
    no_project: bool = False,
    client: Any = None,
) -> str:
    try:
        with ExitStack() as stack:
            if client is None:
                from astrid.sdk.client import AstridClient

                client = stack.enter_context(AstridClient.open_existing_from_launcher())
            if no_project:
                result = client.preferences.get("user")
                data = {"user": result.data, "project": None} if result.ok else None
            else:
                result = client.preferences.view(project=project)
                data = result.data if result.ok else None
        if not result.ok:
            return render_context(None, error=f"Runtime read failed ({result.error.code}).")
        return render_context(data)
    except Exception as exc:  # noqa: BLE001 - runtime adapters normalize different transport failures
        return render_context(None, error=f"Runtime read failed ({type(exc).__name__}).")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--project")
    selection.add_argument("--no-project", action="store_true")
    args = parser.parse_args(argv)
    print(current_context(project=args.project, no_project=args.no_project))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
