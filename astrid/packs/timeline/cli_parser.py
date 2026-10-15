"""The timelines parser: a positional after the flags still fills the verb's open positional."""
from __future__ import annotations

import argparse
from typing import Any


# Verbs whose --project is optional: omitted, it is the workspace runtime's current project.
CURRENT_PROJECT_VERBS = frozenset(
    {"checkout", "apply", "assets", "check", "discard", "find", "lines", "publish", "redo", "status", "undo"}
)


def resolve_current_project(parsed: argparse.Namespace) -> str | None:
    """The workspace's current project id, or None when none is selected (or the runtime cannot say)."""
    projects = getattr(getattr(parsed, "client", None), "projects", None)
    current = getattr(projects, "current", None)
    if current is None:
        return None
    try:
        result = current()
    except Exception:  # noqa: BLE001 - unreachable runtime: the caller reports "no current project"
        return None
    if not getattr(result, "ok", False):
        return None
    data = result.data
    row = data.get("project") if isinstance(data, dict) else None
    if not isinstance(row, dict):
        return None
    ref = row.get("project_id") or row.get("id") or row.get("slug")
    return ref.strip() if isinstance(ref, str) and ref.strip() else None


class TimelinesParser(argparse.ArgumentParser):
    """``show TL --project P c41.mink``: a positional after the flags fills the verb's open positional
    (address, word, query, sheet file, asset key) instead of failing with "unrecognized arguments"."""

    def parse_args(self, args: Any = None, namespace: Any = None) -> argparse.Namespace:
        parsed, extras = self.parse_known_args(args, namespace)
        loose = [e for e in extras if not e.startswith("-")]
        if extras and len(loose) == len(extras):
            for dest in ("address", "word", "query", "sheet", "sheet_file", "key", "steps"):
                if loose and getattr(parsed, dest, "absent") is None:
                    value = loose.pop(0)
                    if dest == "steps":
                        if not value.isdigit() or int(value) < 1:
                            self.error(f"steps must be a positive integer, got {value!r}")
                        value = int(value)
                    setattr(parsed, dest, value)
            if loose and getattr(parsed, "address", None) and getattr(parsed, "command", None) == "show":
                parsed.more_addresses, loose = list(loose), []  # show TL a b c: one record each
        if loose or len(extras) != len([e for e in extras if not e.startswith("-")]):
            self.error(f"unrecognized arguments: {' '.join(extras)}")
        if getattr(parsed, "command", None) in CURRENT_PROJECT_VERBS and not getattr(parsed, "project", None) and not getattr(parsed, "file", None):
            project = resolve_current_project(parsed)  # an explicit --project always wins
            if project is None:
                self.error(
                    f"{parsed.command}: no --project given and no current project is selected "
                    "(pass --project P, or run: astrid projects select <project>)"
                )
            parsed.project = project
        return parsed


class ViewChoices(tuple):
    """Keep built-in help/census while admitting point-qualified declarations.

    Parsing never discovers packs or executes contribution code. Resolution
    belongs to the generic view consumer, which refuses unknown identities.
    """
    def __contains__(self, value):
        return super().__contains__(value) or (
            isinstance(value, str) and value.startswith("timeline.view/")
        )
