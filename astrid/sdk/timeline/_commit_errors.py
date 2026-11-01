"""Stable semantic error envelopes for timeline commits."""
from __future__ import annotations

import copy

# These are request/base failures.  A provider error raised while replaying an
# already-valid request over a newer head is still a conflict; the caller of
# ``failure`` supplies that distinction through the base/current pins.
_INVALID_REASONS = frozenset({
    "ambiguous_ref",
    "capture_mismatch",
    "idless_list_address",
    "invalid_commands",
    "invalid_document",
    "invalid_edit_command",
    "invalid_path",
    "invalid_steps",
    "managed_closure_write",
    "outside_closure",
    "previous_value_mismatch",
    "unsupported_operation",
    "unsupported_operation_version",
    "unsupported_version",
})
_REFUSED_REASONS = frozenset({
    "head_busy",
    "history_unavailable",
    "idempotency_mismatch",
    "not_pristine",
    "stale_head",
})

_HINTS = {
    "ambiguous_ref": "Use the exact reachable revision ref before retrying",
    "capture_mismatch": "Recreate the edit from the current timeline",
    "idless_list_address": "Replace the whole array with its complete previous value",
    "invalid_document": "Read the timeline and submit a valid canonical document change",
    "invalid_path": "Use a valid document path from the current timeline",
    "previous_value_mismatch": "Read the current value and reapply the intended change",
    "selection_changed": "Read the current collection and choose the intended item again",
    "dependency_changed": "Read the current dependency and reapply the intended change",
    "atomic_scope_changed": "Read the current timeline and reapply the complete edit",
    "binding_changed": "Read the current binding and reauthor the edit",
    "history_unavailable": "Refresh the timeline history before retrying",
    "head_busy": "Retry the exact request after the timeline settles",
    "idempotency_mismatch": "Use a new idempotency key for a changed request",
    "stale_head": "Read the current timeline head before retrying",
}
_COMMANDS = {
    "history_unavailable": "timeline history",
    "idempotency_mismatch": "timeline commit",
    "head_busy": "timeline commit",
    "stale_head": "timeline show",
    "selection_changed": "timeline show",
    "dependency_changed": "timeline show",
    "atomic_scope_changed": "timeline show",
    "binding_changed": "timeline show",
}


def _base_is_current(base, current):
    return base is not None and current is not None and base == current


def is_invalid(reason, base, current):
    """Return whether ``reason`` describes malformed input at its claimed base."""
    if reason in _REFUSED_REASONS:
        return False
    at_base = current is None or _base_is_current(base, current)
    return at_base and (reason in _INVALID_REASONS or reason.startswith("invalid_") or
                        reason.startswith("unsupported_"))


def _details(reason, base, current, *, conflicts, path, hint, suggested_command, status):
    return {
        "status": status,
        "reason": reason,
        "code": reason,
        "path": list(path or []),
        "hint": hint or _HINTS.get(reason, "Read the current timeline and retry the edit"),
        "suggested_command": suggested_command or _COMMANDS.get(
            reason, "timeline edit" if status == "invalid" else "timeline show"
        ),
        "base": copy.deepcopy(base),
        "current": copy.deepcopy(current),
        "conflicts": copy.deepcopy(conflicts or []),
    }


def failure(reason, message, base, current=None, *, conflicts=None, path=(),
            hint=None, suggested_command=None, invalid=None, status=None):
    """Build the existing failure shape with v2 semantic details.

    ``invalid`` is deliberately optional: callers with an exception raised at
    the original base get the contract's 422 classification automatically,
    while legacy refusal reasons retain their established 409 semantics.
    """
    if invalid is None:
        invalid = is_invalid(reason, base, current)
    if invalid:
        status = "invalid"
    elif status is None:
        intervening_head = current is not None and base is not None and current != base
        status = "conflict" if conflicts or (intervening_head and reason not in _REFUSED_REASONS) else "refused"
    return {
        "http_status": 422 if invalid else 409,
        "code": "validation_error" if invalid else "conflict",
        "message": message,
        "details": _details(reason, base, current, conflicts=conflicts, path=path,
                             hint=hint, suggested_command=suggested_command, status=status),
    }
