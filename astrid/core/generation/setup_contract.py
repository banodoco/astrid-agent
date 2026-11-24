"""P56 setup next-step contract, additive to unmapped legacy readiness.

New codes or any new contract field opt an envelope into strict validation.
Mapping existing failures is LE-1b; legacy attached instructions keep their
shape until that mapping is supplied. Fix list order is execution order, and
each error's fix_ids is an ordered subset. Warnings have no repair links.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

SETUP_ISSUE_CODES = frozenset({
    "needs_consent", "entry_exists", "pin_conflict", "pack_version_differs", "unknown_input",
    "source_unreachable", "commit_unavailable", "pip_failed", "protected_change", "node_pack_conflict",
    "partial_install", "classes_not_loaded", "fetch_404", "version_drift",
})
_SCOPES = frozenset({"read_only", "managed_write", "user_env", "paid", "attached_target"})
_FIX_FIELDS = frozenset({"id", "who", "scope", "details", "argv", "instruction", "placeholders"})
_PLACEHOLDER = re.compile(r"<([^<>]+)>")
_NAME = re.compile(r"[A-Z][A-Z0-9_]*")


def uses_setup_contract(envelope: Mapping[str, Any]) -> bool:
    return ("attempt_count" in envelope
            or any(issue.get("code") in SETUP_ISSUE_CODES or "fix_ids" in issue for issue in envelope.get("issues", ()))
            or any(set(fix) & {"id", "who", "instruction", "placeholders"} for fix in envelope.get("fixes", ())))


def _words(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip()) and "\0" not in value


def _argv(value: Any) -> list[str]:
    if not isinstance(value, list) or not value or not all(_words(item) for item in value):
        raise ValueError("setup fix argv must be a nonempty array of nonempty strings")
    if any(item == "--dry-run" or item.startswith("--dry-run=") for item in value):
        raise ValueError("a setup fix must not contain --dry-run")
    return value


def validate_setup_envelope(envelope: Mapping[str, Any]) -> None:
    """Reject incomplete/ambiguous setup guidance without inventing a repair."""
    attempt = envelope.get("attempt_count")
    if type(attempt) is not int or attempt < 0:
        raise ValueError("setup attempt_count must be a nonnegative integer")
    issues, fixes = envelope.get("issues"), envelope.get("fixes")
    if not isinstance(issues, list) or not isinstance(fixes, list):
        raise ValueError("setup issues and fixes must be arrays")
    order = {}
    for index, fix in enumerate(fixes):
        if not isinstance(fix, dict) or set(fix) - _FIX_FIELDS:
            raise ValueError("setup fix has unrecognized fields")
        fix_id = fix.get("id")
        if not _words(fix_id) or fix_id in order:
            raise ValueError("setup fixes need unique nonempty ids")
        order[fix_id] = index
        if (fix.get("who") not in ("agent", "human") or not isinstance(fix.get("scope"), str)
                or fix["scope"] not in _SCOPES):
            raise ValueError("setup fix must name who acts and its scope")
        if not isinstance(fix.get("details", {}), dict):
            raise ValueError("setup fix details must be an object")
        if ("argv" in fix) == ("instruction" in fix):
            raise ValueError("setup fix needs exactly one of argv or instruction")
        texts = []
        if "argv" in fix:
            texts.extend(_argv(fix["argv"]))
        else:
            instruction = fix["instruction"]
            if (not isinstance(instruction, dict) or set(instruction) != {"step", "then_argv"}
                    or not _words(instruction["step"])):
                raise ValueError("setup instruction needs step and then_argv")
            texts.append(instruction["step"])
            texts.extend(_argv(instruction["then_argv"]))
        placeholders = fix.get("placeholders", {})
        if (not isinstance(placeholders, dict)
                or not all(isinstance(key, str) and _NAME.fullmatch(key) and _words(value)
                           for key, value in placeholders.items())):
            raise ValueError("setup placeholders must map uppercase names to explanations")
        used = {name for text in texts for name in _PLACEHOLDER.findall(text)}
        if used != set(placeholders):
            raise ValueError("setup fix must declare every used placeholder and no unused placeholders")
    linked = set()
    for issue in issues:
        if (not isinstance(issue, dict) or not isinstance(issue.get("code"), str)
                or issue["code"] not in SETUP_ISSUE_CODES):
            raise ValueError("undeclared setup issue code")
        if issue.get("severity") not in ("error", "warning") or not _words(issue.get("message")):
            raise ValueError("setup issue needs severity and message")
        if not isinstance(issue.get("details", {}), dict):
            raise ValueError("setup issue details must be an object")
        ids = issue.get("fix_ids", [])
        if (not isinstance(ids, list) or not all(isinstance(item, str) and item in order for item in ids)
                or len(ids) != len(set(ids)) or ids != sorted(ids, key=order.__getitem__)):
            raise ValueError("setup issue fix_ids must reference fixes once in their declared order")
        if issue["severity"] == "error" and not ids:
            raise ValueError("every setup error needs a linked next step")
        if issue["severity"] == "warning" and ids:
            raise ValueError("a setup warning must not invent a repair")
        linked.update(ids)
    if linked != set(order):
        raise ValueError("setup fixes must be linked to an error")
    expected = "blocked" if any(issue["severity"] == "error" for issue in issues) else "ready_with_warnings" if issues else "ready"
    if envelope.get("status") != expected:
        raise ValueError("setup status must match its issues")
