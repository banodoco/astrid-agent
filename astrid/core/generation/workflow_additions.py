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


def repair_workflow_argv(
    argv: Sequence[str], *, correct_add_count: int | None = None, replace_entry: bool = False,
) -> tuple[list[str], dict[str, str]]:
    """Keep the invocation, changing only the controls required by the fix.

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
        if flag == "--add" and correct_add_count is not None:
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
    if correct_add_count is not None and index != correct_add_count:
        raise ValueError("workflow retry argv must contain each --add declaration once")
    if replace_entry and "--replace" not in result:
        result.append("--replace")
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
        elif isinstance(cause, subprocess.CalledProcessError) and cause.returncode == 2:
            code = "commit_unavailable"
            step = "Correct the unavailable repository reference; "
        elif _known_ref_refusal(exc):
            code = "commit_unavailable"
            step = "Correct the missing, ambiguous or invalid repository reference; "
        else:
            # The pinned resolver has plain ValueError at specific boundaries.
            # Do not turn an unrelated programming/contract failure into a Git
            # error, or suppress its original diagnostic.
            raise
        argv, placeholders = repair_workflow_argv(retry_argv, correct_add_count=len(declarations))
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


def _known_ref_refusal(error: ValueError) -> bool:
    """Recognize only explicit validation/ref-response refusals of the pinned resolver.

    The resolver currently exposes these specific ValueErrors, plus typed Git
    causes and CustomNodeRefConflict. Do not classify arbitrary ValueErrors.
    """
    message = str(error)
    if message in {
        "--add requires HTTPS URL[@REF]",
        "--add requires an HTTPS repository URL without credentials, query or fragment",
        "--add repository URL must name a repository",
        "--add REF must be HEAD, a branch, a tag or a full commit SHA",
        "--add REF must name a branch or tag",
        "Invalid IPv6 URL",
        "Port out of range 0-65535",
    }:
        return True
    return any((
        message.startswith("Port could not be cast to integer value as "),
        message.startswith("invalid git ls-remote response for ") and " at " in message,
        message.startswith("conflicting git ls-remote response for ") and " at " in message,
        message.startswith("--add REF ") and message.endswith("; use an exact branch or tag")
        and " is missing or ambiguous at " in message,
    ))
