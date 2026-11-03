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


def conflict_guidance(conflicts, receipts=(), current_values=()) -> str:
    """Present current conflict values, attributing them to the user only when verified."""
    if not isinstance(conflicts, list) or not conflicts:
        return ""
    touching = [receipt for receipt in receipts or []
                if isinstance(receipt, dict) and receipt.get("touches_conflict")]
    # The packet associates receipts with the conflict set, not each individual
    # row. Attribute a single-field refusal to the user only when every touching
    # receipt is an App publication; otherwise keep the wording neutral.
    is_user_edit = (len(conflicts) == 1 and bool(touching)
                    and all(receipt.get("via") == "app" for receipt in touching))
    statements = []
    for index, conflict in enumerate(conflicts):
        if not isinstance(conflict, dict):
            continue
        theirs = current_values[index] if index < len(current_values) else None
        if not isinstance(theirs, dict) or theirs.get("state") not in {"present", "absent"}:
            theirs = conflict.get("theirs")
        ref = str(conflict.get("ref") or "the changed part").rsplit("/", 1)[-1]
        raw_path = [str(part) for part in conflict.get("path", [])]
        identity = next((part.partition(":")[2] for part in raw_path
                         if part.startswith("id:") and ":" in part), None)
        identity_index = next((index for index, part in enumerate(raw_path)
                               if part.startswith("id:")), None)
        if ref.startswith("clip:"):
            subject = "clip " + ref.partition(":")[2]
        elif ref.startswith("track:"):
            subject = "track " + ref.partition(":")[2]
        elif identity and identity_index:
            collection = raw_path[identity_index - 1]
            subject = ("track " if collection == "tracks" else "clip " if collection == "clips"
                       else f"{collection} ") + identity
        else:
            subject = ref
        path = [part for part in raw_path if not part.startswith("id:")]
        field = path[-1] if path else None
        if field == "at":
            field = "start time"
        address = f"{subject}.{field}" if field else subject
        owner = "user's " if is_user_edit else ""
        if isinstance(theirs, dict) and theirs.get("state") == "present":
            statements.append(f"The {owner}current value for {address} is {theirs.get('value')!r}.")
        elif isinstance(theirs, dict) and theirs.get("state") == "absent":
            statements.append(f"The {owner}current value for {address} is absent.")
        else:
            statements.append(f"Preserve the {owner}current value for {address}.")
    if not statements:
        statements.append("Preserve the current values of the conflicting fields.")
    elif len(statements) == 1:
        statements.append("Preserve it.")
    else:
        statements.append("Preserve them.")
    statements.append("Re-derive only compatible work.")
    return " ".join(statements)


def add_conflict_guidance(presentation: dict, failure: dict, clash_packet=None) -> dict:
    """Attach verified field/value guidance to an initial conflict presentation."""
    details = failure.get("details") if isinstance(failure.get("details"), dict) else {}
    if details.get("status") != "conflict":
        return presentation
    guidance = conflict_guidance(details.get("conflicts"),
                                 (clash_packet or {}).get("receipts", []),
                                 (clash_packet or {}).get("current_values", []))
    existing = presentation.get("fix", "")
    if guidance and guidance not in existing:
        presentation["fix"] = f"{existing.rstrip()} {guidance}".strip()
    return presentation


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
