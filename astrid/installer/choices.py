"""Saved setup choices and their command-line precedence."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .core import read_json, write_json

CHOICES_KEY = "setup_choices"
BOOLEAN_CHOICES = ("autostart", "terminal_command", "skills")
CHOICE_KEYS = (*BOOLEAN_CHOICES, "skills_mechanism")
DEFAULT_CHOICES: dict[str, bool | str] = {
    "autostart": True,
    "terminal_command": True,
    "skills": True,
    "skills_mechanism": "symlink",
}


def _validated(source: Mapping[str, Any] | None) -> dict[str, bool | str]:
    if source is None:
        return {}
    if not isinstance(source, Mapping):
        raise ValueError("Saved setup choices must be a JSON object.")
    values: dict[str, bool | str] = {}
    for key in BOOLEAN_CHOICES:
        if key in source:
            value = source[key]
            if type(value) is not bool:
                raise ValueError(f"Saved setup choice {key!r} must be true or false.")
            values[key] = value
    if "skills_mechanism" in source:
        value = source["skills_mechanism"]
        if not isinstance(value, str) or value not in {"symlink", "copy"}:
            raise ValueError("Saved skills mechanism must be 'symlink' or 'copy'.")
        values["skills_mechanism"] = value
    return values


def resolve_choices(
    *,
    explicit: Mapping[str, Any] | None = None,
    saved: Mapping[str, Any] | None = None,
    selected: Mapping[str, Any] | None = None,
    existing_install: bool = False,
) -> dict[str, bool | str]:
    """Resolve explicit flags, saved values, prompt/--yes selection, then defaults.

    Existing installs deliberately start with the historical all-on behavior;
    no installed artifact or empty state field is used to guess a preference.
    Fresh installs use the same all-on defaults.
    """
    flag_values = _validated(explicit)
    saved_values = _validated(saved)
    selected_values = _validated(selected)
    defaults = DEFAULT_CHOICES.copy()
    if existing_install:
        defaults.update({key: True for key in BOOLEAN_CHOICES})
    return {
        key: flag_values.get(key, saved_values.get(key, selected_values.get(key, defaults[key])))
        for key in CHOICE_KEYS
    }


def merge_choices(record: Mapping[str, Any], choices: Mapping[str, Any]) -> dict[str, Any]:
    """Return a selection record with resolved choices, preserving unknown data."""
    values = _validated(choices)
    prior = record.get(CHOICES_KEY, {})
    if not isinstance(prior, Mapping):
        raise ValueError("Saved setup choices must be a JSON object.")
    return {**record, CHOICES_KEY: {**prior, **values}}


def save_choices(path: Path, choices: Mapping[str, Any]) -> dict[str, Any]:
    """Merge setup choices into astrid-install.json without dropping other keys."""
    record = read_json(path)
    updated = merge_choices(record, choices)
    if updated != record:
        write_json(path, updated)
    return updated


def is_existing_install(marker: Mapping[str, Any], selection: Mapping[str, Any],
                        program_root: Path) -> bool:
    """Recognize an install only from Astrid's explicit ownership records."""
    from .service import LABEL

    if marker.get("owner") == LABEL:
        return True
    if selection.get("owner") != LABEL or selection.get("uninstalled") is True:
        return False
    selected_root = selection.get("program_root")
    if selected_root is None:
        from .service import default_program_root

        selected_root = str(default_program_root(Path.home(), sys.platform))
    return (isinstance(selected_root, str)
            and Path(os.path.abspath(Path(selected_root).expanduser()))
            == Path(os.path.abspath(program_root.expanduser())))


def explicit_choices_from_args(args: Any) -> dict[str, bool | str]:
    """Extract only choices explicitly supplied on the installer command line."""
    values = {
        "autostart": args.autostart,
        "terminal_command": args.terminal_command,
        "skills": args.skills,
        "skills_mechanism": args.skills_mechanism,
    }
    return {key: value for key, value in values.items() if value is not None}


def print_plan(choices: Mapping[str, Any]) -> None:
    print("Astrid setup plan:")
    print(f"  Start at sign-in: {'yes' if choices['autostart'] else 'no'}")
    print(f"  Install the astrid command: {'yes' if choices['terminal_command'] else 'no'}")
    print(f"  Install coding-agent skills: {'yes' if choices['skills'] else 'no'}")
    if choices["skills"]:
        print(f"  Skills method: {'use copies' if choices['skills_mechanism'] == 'copy' else 'links'}")
