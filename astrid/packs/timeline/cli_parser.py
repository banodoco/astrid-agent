"""The timelines parser: a positional after the flags still fills the verb's open positional."""
from __future__ import annotations

import argparse
import difflib
import re
import sys
from typing import Any


# Verbs whose --project is optional: omitted, it is the workspace runtime's current project.
CURRENT_PROJECT_VERBS = frozenset(
    {
        "adopt", "archive", "apply", "assets", "check", "checkout", "create", "diff", "discard",
        "duplicate", "edit", "find", "history", "lift", "lines", "list", "log", "publish", "recover",
        "redo", "render", "replace-parent-media", "restore", "revert", "script", "shots", "show", "status",
        "undo", "visualize", "words", "lint",
    }
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


def _project_choices(parsed: argparse.Namespace) -> list[str]:
    """Return names for a useful no-selection error without assuming a default."""
    projects = getattr(getattr(parsed, "client", None), "projects", None)
    list_projects = getattr(projects, "list", None)
    if not callable(list_projects):
        return []
    try:
        result = list_projects()
    except Exception:  # noqa: BLE001 - a choice list is optional error context
        return []
    if not getattr(result, "ok", False):
        return []
    rows = getattr(result, "data", None)
    if isinstance(rows, tuple) and rows and isinstance(rows[0], list):
        rows = rows[0]
    if not isinstance(rows, list):
        return []
    choices: list[str] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = row.get("slug") or row.get("project_id") or row.get("id")
        if not isinstance(name, str) or not name.strip():
            continue
        display = row.get("name")
        choices.append(f"{name} ({display})" if isinstance(display, str) and display else name)
    return choices


class TimelinesParser(argparse.ArgumentParser):
    """``show TL --project P c41.mink``: a positional after the flags fills the verb's open positional
    (address, word, query, sheet file, asset key) instead of failing with "unrecognized arguments"."""

    def error(self, message: str) -> None:
        # Keep unknown timeline flags short and useful. argparse's generated
        # usage for edit is long enough to bury the typo and often wraps past
        # three lines in a terminal.
        match = re.search(r"unrecognized arguments:\s+(.+)$", message)
        if match:
            tokens = match.group(1).split()
            bad = next((token for token in tokens if token.startswith("-") and token not in self._option_string_actions), None)
            if bad:
                options = set(self._option_string_actions)
                pending = [self]
                while pending:
                    current = pending.pop()
                    for action in current._actions:
                        if isinstance(action, argparse._SubParsersAction):
                            pending.extend(action.choices.values())
                        else:
                            options.update(action.option_strings)
                nearest = difflib.get_close_matches(bad, options, n=1, cutoff=0.45)
                command = getattr(getattr(self, "_namespace_for_error", None), "command", None)
                if not command:
                    argv = getattr(self, "_args_for_error", [])
                    top_level = next((
                        action.choices for action in self._actions
                        if isinstance(action, argparse._SubParsersAction)
                    ), {})
                    command = next((token for token in argv if token in top_level), None)
                verb = str(command or self.prog.rsplit(" ", 1)[-1])
                suggestion = f"; did you mean {nearest[0]}?" if nearest else ""
                print(f"{verb}: unknown flag {bad}{suggestion}", file=sys.stderr)
                raise SystemExit(2)
        super().error(message)

    def parse_args(self, args: Any = None, namespace: Any = None) -> argparse.Namespace:
        raw_args = list(sys.argv[1:] if args is None else args)
        self._args_for_error = raw_args
        parsed, extras = self.parse_known_args(raw_args, namespace)
        if extras and all(not e.startswith("-") for e in extras):
            loose = list(extras)
            for dest in ("address", "word", "query", "sheet", "sheet_file", "key", "steps"):
                if loose and getattr(parsed, dest, "absent") is None:
                    value = loose.pop(0)
                    if dest == "steps":
                        if getattr(parsed, "command", None) in {"undo", "revert"} and not value.isdigit():
                            setattr(parsed, dest, value)
                            continue
                        if not value.isdigit() or int(value) < 1:
                            self.error(f"steps must be a positive integer, got {value!r}")
                        value = int(value)
                    setattr(parsed, dest, value)
            if loose and getattr(parsed, "address", None) and getattr(parsed, "command", None) == "show":
                parsed.more_addresses, loose = list(loose), []  # show TL a b c: one record each
            if loose:
                self._namespace_for_error = parsed
                self.error(f"unrecognized arguments: {' '.join(loose)}")
        elif extras:
            self._namespace_for_error = parsed
            self.error(f"unrecognized arguments: {' '.join(extras)}")
        if getattr(parsed, "command", None) in CURRENT_PROJECT_VERBS and not getattr(parsed, "project", None) and not getattr(parsed, "file", None):
            project = resolve_current_project(parsed)  # an explicit --project always wins
            if project is None:
                choices = _project_choices(parsed)
                available = f" Choices: {', '.join(choices)}." if choices else ""
                self.error(
                    f"{parsed.command}: no --project given and no current project is selected."
                    f"{available} Pass --project P or run: astrid projects select <project>."
                )
            parsed.project = project
        return parsed
