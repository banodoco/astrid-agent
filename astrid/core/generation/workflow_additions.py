"""LE-3: adapt canonical repository resolution to generation readiness.

VibeComfy owns URL parsing, Git lookup and immutable ref normalization. The
Agent owns the next-step envelope and the original command's retry spelling.
A successfully resolved declaration is not evidence of code consent.
"""
from __future__ import annotations

import re
import subprocess
from collections.abc import Sequence
from typing import Any

from .preflight import generation_error


def _repair_add_argv(argv: Sequence[str], count: int) -> tuple[list[str], dict[str, str]]:
    """Keep the invocation, but ask the person to correct its declarations.

    A changed pin invalidates the displayed code token; do not replay one.
    The caller supplies the actual workflow command, with canonical --add
    spellings. All other controls (including --replace and --no-run) survive.
    """
    result: list[str] = []
    placeholders: dict[str, str] = {}
    items = iter(argv)
    index = 0
    for arg in items:
        if arg == "--dry-run" or arg.startswith("--dry-run="):
            continue
        flag, separator, value = arg.partition("=")
        if flag == "--consent":
            scope = value if separator else next(items)
            if scope == "user_env":
                result.extend(["--consent", scope])
            continue
        if flag == "--add":
            if not separator:
                next(items)
            index += 1
            name = f"NODE_REF_{index}"
            result.extend(["--add", f"<{name}>"])
            placeholders[name] = (
                f"Correct HTTPS repository declaration {index}, with a full commit SHA "
                "or an exact refs/heads/ or refs/tags/ name"
            )
            continue
        # The fix schema interprets angle-bracket tokens as placeholders, not
        # literal prompt/argument values. Preserve their position and ask for
        # the original value instead of accidentally emitting an undeclared
        # placeholder or an invalid empty argv element.
        if not arg.strip() or "\0" in arg or re.search(r"<[^<>]+>", arg):
            name = f"ORIGINAL_ARG_{len(result) + 1}"
            placeholders[name] = "The original argument at this position, unchanged"
            result.append(f"<{name}>")
        else:
            result.append(arg)
    if index != count:
        raise ValueError("workflow retry argv must contain each --add declaration once")
    if "--json" not in result:
        result.append("--json")
    return result, placeholders


def resolve_workflow_additions(
    declarations: Sequence[str], *, retry_argv: Sequence[str],
) -> list[dict[str, Any]]:
    """Resolve the whole declared set before entry publication or authorization."""
    if not declarations:
        return []
    from vibecomfy.custom_node_refs import CustomNodeRefConflict, resolve_added_node_refs

    try:
        return resolve_added_node_refs(declarations)
    except ValueError as exc:
        # Classify the explicit resolver boundary, never match English prose.
        # Git ls-remote exits 2 for a missing ref; other failed Git processes
        # and timeouts mean the source could not be queried.
        cause = exc.__cause__
        if isinstance(exc, CustomNodeRefConflict):
            code = "pin_conflict"
            step = "Choose one consistent source and commit per node pack; "
        elif (isinstance(cause, (OSError, subprocess.TimeoutExpired))
              or isinstance(cause, subprocess.CalledProcessError) and cause.returncode != 2):
            code = "source_unreachable"
            step = "Check that the HTTPS repository is reachable; "
        else:
            code = "commit_unavailable"
            step = "Correct the missing, ambiguous or invalid repository reference; "
        argv, placeholders = _repair_add_argv(retry_argv, len(declarations))
        raise generation_error(
            exc, code=code, details={"field": "add"},
            fixes=[{
                "id": "correct-node-ref", "who": "human", "scope": "managed_write",
                "instruction": {
                    "step": step + "use a full commit or an exact branch or tag, then retry. "
                            "Obtain fresh code consent if the resolved declaration changes.",
                    "then_argv": argv,
                },
                "placeholders": placeholders,
            }],
            attempt_count=0,
        ) from exc
