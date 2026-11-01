"""User-facing wording for structured timeline commit failures."""
from __future__ import annotations

import copy

_STATUS = {
    "invalid": ("This edit is not valid; nothing was committed", "Fix the edit and commit again; the timeline is unchanged."),
    "conflict": ("The timeline changed since this edit was made; nothing was committed", "Read the current timeline, preview the edit again, then commit the reviewed list."),
    "refused": ("The Runtime declined this edit; nothing was committed", "Read the detail, correct the request, then submit a new edit."),
}

_REASONS = {
    "stale_head": ("The edit was made from an older timeline; nothing was committed", "Reopen with Timeline.open(...) and make the edit again."),
    "head_busy": ("The timeline is busy; nothing was committed", "Make the same call again shortly; it sends a new request."),
    "idempotency_mismatch": ("This key was already used for a different edit; nothing was committed", "Use a new key for a new edit."),
    "not_pristine": ("The timeline is not empty; nothing was created", "Open the existing timeline and commit the content as an edit."),
    "capture_mismatch": ("The edit no longer matches the timeline it was made from; nothing was committed", "Reopen with Timeline.open(...) and make the edit again."),
    "previous_value_mismatch": ("The edit expected a different current value; nothing was committed", "Read the timeline and write the edit again from the current value."),
    "selection_changed": ("The timeline changed since this list was made; nothing was committed", "Read the current timeline, preview again, review the new list, then commit it."),
    "dependency_changed": ("The timeline changed since this list was made; nothing was committed", "Read the current timeline, preview again, review the new list, then commit it."),
    "atomic_scope_changed": ("The timeline changed since this list was made; nothing was committed", "Read the current timeline, preview again, review the new list, then commit it."),
    "binding_changed": ("The timeline changed since this list was made; nothing was committed", "Read the current timeline, preview again, review the new list, then commit it."),
}

_DETAILS = {
    "selection_changed": "The clips this edit selected have changed.",
    "dependency_changed": "Something this edit depends on has changed.",
    "atomic_scope_changed": "This edit needs the whole timeline unchanged, and it changed.",
    "binding_changed": "A text or media binding this edit uses has changed.",
    "head_busy": "Another commit was being applied at the same moment.",
    "idempotency_mismatch": "A key names one edit. This request differs from the request first sent with it.",
    "not_pristine": "Creating with content needs a timeline with no edits yet.",
    "capture_mismatch": "The commands were captured from a different copy of the timeline.",
    "previous_value_mismatch": "The current value differs from the value this edit was written against.",
}


def present_failure(failure: dict, *, unknown: bool = False) -> dict:
    """Add readable fields while preserving the original failure envelope."""
    cause = copy.deepcopy(failure)
    details = cause.get("details") if isinstance(cause.get("details"), dict) else {}
    reason = details.get("reason")
    status = details.get("status")
    outcome = "unknown" if unknown else "not_applied"
    if unknown:
        headline = "No reply could be verified; this edit may or may not have been committed"
        detail = "The exact request is saved. No new request will be sent until it is confirmed."
        fix = "Call tl.commit() again with no arguments to confirm the saved request; the edit is not re-run blindly."
    else:
        headline, fix = _REASONS.get(reason, _STATUS.get(status, _STATUS["invalid"]))
        message = cause.get("message")
        detail = _DETAILS.get(reason) or (
            message if isinstance(message, str) and message else f"Runtime reason: {reason or status or 'unknown'}."
        )
    return {"headline": headline, "detail": detail, "fix": fix,
            "outcome": outcome, "cause": cause}
