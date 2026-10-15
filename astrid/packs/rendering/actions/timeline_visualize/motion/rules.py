"""Project rules: thresholds and severities for the registered checks, without Python.

``astrid-lint.toml`` lives with the work (``timelines lint`` and ``visualize``
look in the working directory and its parents; ``--rules FILE`` names one)::

    # thresholds: any key a registered check reads (timelines lint --list-checks)
    max_cut_s = 4                 # off by default; deliberate_hold cuts are exempt
    stamp_to_word_max_s = 0.1
    min_text_px = 32
    max_presenter_share = 0.3

    [severity]                    # per finding code: off | info | warn | error
    EDGE = "off"
    LONG = "error"

    [checks]
    disable = ["vo-level"]        # skip whole checks by name

Unknown keys are errors that list the known ones, so a typo never silently
turns a rule off.
"""
from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from ..layers.base import SEVERITIES, check_defaults, checks

RULES_FILENAME = "astrid-lint.toml"


def find_rules(start: Path | None = None) -> Path | None:
    """Find rules from an explicitly caller-bound root; never use worker cwd."""
    if start is None:
        return None
    here = Path(start).resolve()
    home = Path.home().resolve()
    for folder in (here, *here.parents):
        candidate = folder / RULES_FILENAME
        if candidate.is_file():
            return candidate
        if folder == home or folder.parent == folder:
            break
    return None


def parse_rules(document: dict[str, Any], *, origin: str = RULES_FILENAME) -> dict[str, Any]:
    """Validate a parsed rules document into ``{params, severity, disable, path}``."""
    known = check_defaults()
    registry = checks(include_v3=True)
    codes = {code for check in registry.values() for code in check.codes}
    params: dict[str, Any] = {}
    severity: dict[str, str] = {}
    disable: list[str] = []
    problems: list[str] = []
    for key, value in document.items():
        if key == "severity":
            if not isinstance(value, dict):
                problems.append("[severity] must be a table of CODE = \"off|info|warn|error\"")
                continue
            for code, level in value.items():
                if level not in ("off", *SEVERITIES):
                    problems.append(f"[severity] {code} = {level!r}: use off, info, warn or error")
                elif codes and code not in codes:
                    problems.append(f"[severity] unknown finding code {code!r}; known: {', '.join(sorted(codes))}")
                else:
                    severity[str(code)] = str(level)
        elif key == "checks":
            names = value.get("disable", []) if isinstance(value, dict) else None
            if not isinstance(names, list):
                problems.append("[checks] disable must be a list of check names")
                continue
            for name in names:
                if name not in registry:
                    problems.append(f"[checks] disable: unknown check {name!r}; known: {', '.join(sorted(registry))}")
                else:
                    disable.append(str(name))
        elif key in known:
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                problems.append(f"{key} must be a number (got {value!r})")
            else:
                params[key] = float(value)
        else:
            problems.append(f"unknown key {key!r}; thresholds are: {', '.join(sorted(known))} "
                            "(plus [severity] and [checks] tables)")
    if problems:
        raise ValueError(f"{origin}: " + "; ".join(problems))
    return {"params": params, "severity": severity, "disable": disable, "path": origin}


def load_rules(path: Path) -> dict[str, Any]:
    with Path(path).open("rb") as stream:
        try:
            document = tomllib.load(stream)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"{path}: not valid TOML ({exc})") from None
    return parse_rules(document, origin=str(path))


def resolve_rules(explicit: str | None = None, *, search: bool = False,
                  caller_root: Path | None = None) -> dict[str, Any] | None:
    """Resolve a caller-bound rules file; never inspect the worker's cwd by default.

    Public admission should pass the exact path (or already parsed JSON via
    ``filmstrip_options.rules_value``).  ``search=True`` is retained only for
    an explicit, caller-owned root and is not an ambient worker fallback.
    """
    if explicit:
        return load_rules(Path(explicit).expanduser())
    found = find_rules(caller_root) if search and caller_root is not None else None
    return load_rules(found) if found else None
