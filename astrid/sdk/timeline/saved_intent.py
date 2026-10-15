"""Recover exact saved commands from the existing draft journal, without I/O replay.

The checkout row must carry its original saved base. Older digest-only rows
cannot establish that base and are deliberately not upgraded from a snapshot.
This is local provenance verification, not remote revision authentication.
"""
from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from pathlib import Path

from astrid.sdk.timeline import journal, store
from astrid.sdk.timeline.checkout import Checkout
from astrid.sdk.timeline.commands import CommandError, EditCommand
from astrid.sdk.timeline.doc import _serialize


class SavedIntentError(CommandError):
    """The persisted source does not prove an exact base-to-draft command chain."""

    code = "unverified_saved_intent"


def read_saved_calls(checkout: Checkout) -> tuple[dict, ...]:
    """Return original serialized OpCalls in save order, or fail closed.

    Only one unambiguous checkout lifetime is admitted for this draft name.
    Every saved transaction is replayed with the existing detached semantic
    runner and must reproduce its recorded digest. Unsupported media/file
    adapters, undo/redo, and histories needing reconciliation are refused.
    The supplied handle and saved bytes must both match the verified result.
    Nothing is saved, fetched, reconciled, rebased, or inferred from a diff.
    """
    try:
        return _read_saved_calls(checkout)
    except SavedIntentError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        raise SavedIntentError(f"Cannot verify saved intent: {exc}") from exc


def _refuse(message):
    raise SavedIntentError(message)


def _finite_json(text):
    def nonfinite(value):
        _refuse(f"Saved intent requires finite JSON (got {value})")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                _refuse(f"Saved intent has an ambiguous duplicate JSON field {key!r}")
            result[key] = value
        return result

    value = json.loads(text, parse_constant=nonfinite, object_pairs_hook=unique)
    try:
        json.dumps(value, allow_nan=False)  # also reject exponent overflow (e.g. 1e9999)
    except ValueError:
        _refuse("Saved intent requires finite JSON numbers")
    return value


def _read_saved_calls(checkout):
    if checkout.path is None:
        _refuse("Saved intent requires an exact saved draft path")
    target = Path(checkout.path)
    project = checkout.bundle.get("project_id")
    timeline = checkout.bundle.get("timeline_id")
    if not all(isinstance(value, str) and value for value in (project, timeline)):
        _refuse("Saved intent requires exact project and timeline identities")
    if target.resolve() != store.draft_path(project, timeline, target.stem).resolve():
        _refuse("Saved intent requires the exact canonical draft path")
    # Match save's lock ordering (draft, then journal), and verify/read one
    # journal snapshot so a concurrent normal writer cannot change the proof.
    path = journal.journal_path(project, timeline)
    with store._file_lock(target), journal._locked(path):
        return _read_saved_calls_locked(checkout, target, path)


def _read_saved_calls_locked(checkout, target, path):
    """Internal entry point for the producer already holding draft then journal."""
    project, timeline = checkout.bundle.get("project_id"), checkout.bundle.get("timeline_id")
    if not path.is_file() or not path.stat().st_size:
        _refuse("Saved intent journal is missing or empty")
    problems = journal.verify(path)
    if problems:
        _refuse("Saved intent journal is damaged: " + "; ".join(problems))
    # Do not use journal.read's intentionally forgiving log parser.
    rows = [_finite_json(line) for line in path.read_text(encoding="utf-8").splitlines()]
    text = target.read_bytes().decode("utf-8")
    _finite_json(text)
    identities = set()
    selected = []
    for row in rows:
        identity = row.get("id")
        if (row.get("v") != journal.VERSION or not isinstance(identity, str) or not identity
                or identity in identities):
            _refuse("Saved intent journal has ambiguous record identity or version")
        identities.add(identity)
        if row.get("project") != project or row.get("timeline") != timeline:
            _refuse("Saved intent journal has a wrong project/timeline scope")
        if not isinstance(row.get("draft"), str) or not row["draft"]:
            _refuse("Saved intent journal has ambiguous draft scope")
        if row["draft"] == target.stem:
            selected.append(row)
    if not selected or selected[0].get("kind") != "checkout":
        _refuse("Saved intent has no initial checkout anchor")
    checkpoints = [i for i, row in enumerate(selected) if any(
        isinstance(item, Mapping) and item.get("kind") == "publish-checkpoint-v1"
        for item in row.get("evidence", []))]
    if checkpoints:
        from astrid.sdk.timeline.saved_publish import _state
        _state(selected)  # checkpoints require a linked durable intent/result
    anchor_index = checkpoints[-1] if checkpoints else 0
    anchor = selected[anchor_index]
    if sum(row.get("kind") == "checkout" for row in selected) != 1:
        _refuse("Saved intent has ambiguous checkout lifetimes")
    bases = [item for item in anchor.get("evidence", [])
             if isinstance(item, Mapping) and item.get("kind") == "saved-base-v1"]
    if len(bases) != 1 or not isinstance(bases[0].get("document"), dict):
        _refuse("Saved intent checkout lacks exact saved base provenance")
    base = copy.deepcopy(bases[0]["document"])
    if (base.get("project_id") != project or base.get("timeline_id") != timeline
            or not isinstance(base.get("base_parent"), dict)
            or not base["base_parent"].get("revision_id")
            or not isinstance(base.get("base_parent_payload"), dict)):
        _refuse("Saved intent cannot anchor the exact saved base identity/payload")
    if (anchor.get("outcome") != "changed" or (anchor_index == 0 and anchor.get("before") is not None)
            or anchor.get("calls") != [] or not anchor.get("after")
            or journal.document_digest(base) != anchor["after"]):
        _refuse("Saved intent checkout digest does not anchor its base")
    replay = Checkout(base)
    if journal.digest(_serialize(replay._annotated())) != anchor["after"]:
        _refuse("Saved intent base cannot round-trip as the recorded checkout")
    after = anchor["after"]
    calls = []
    from astrid.sdk.timeline.commit import RuntimeCommitService

    for row in selected[anchor_index + 1:]:
        if row.get("kind") == "publish" and row.get("calls") == [] and row.get("evidence") and all(
                isinstance(item, Mapping) and item.get("kind") in {
                    "publish-intent-v1", "publish-result-v1"}
                for item in row["evidence"]):
            continue
        refreshes = [e for e in row.get("evidence", []) if isinstance(e, Mapping) and e.get("kind") == "saved-refresh-v1"]
        if refreshes:
            from astrid.sdk.timeline.rebase import rebase
            bases = [e for e in row["evidence"] if isinstance(e, Mapping) and e.get("kind") == "saved-base-v1"]
            if (len(refreshes) != 1 or len(bases) != 1 or row.get("kind") != "save"
                    or row.get("outcome") != "changed" or row.get("before") != after):
                _refuse("Saved refresh has ambiguous base or discontinuous provenance")
            refreshed = rebase(calls, bases[0]["document"], id_map=refreshes[0].get("id_map"))
            if refreshed.conflicts or [c.to_json() for c in refreshed.plan.calls] != row.get("calls"):
                _refuse("Saved refresh does not replay the original recorded commands")
            replay = Checkout(refreshed.document)
            replay._sync_film_length()
            if journal.digest(_serialize(replay._annotated())) != row.get("after"):
                _refuse("Saved refresh commands do not reproduce the recorded result")
            after, calls = row["after"], copy.deepcopy(row["calls"])
            continue
        # Some surfaces retain both an edit audit and its durable save.
        # Only an exact duplicate of a proved save may be ignored; it cannot
        # substitute for a missing save or add a second command application.
        if row.get("kind") == "edit" and row.get("outcome") == "changed" and any(
                saved.get("kind") == "save" and saved.get("outcome") == "changed" and
                all(saved.get(key) == row.get(key) for key in ("before", "after", "calls"))
                for saved in selected[anchor_index + 1:]):
            continue
        # These are audit-only transactions; none persisted a document.
        if row.get("kind") == "edit" and row.get("outcome") in {
                "no-change", "dry-run", "rolled-back", "refused"}:
            continue
        if row.get("kind") != "save" or row.get("outcome") != "changed":
            _refuse(f"Saved intent cannot replay {row.get('kind')!r} history")
        if not row.get("before") or not row.get("after") or row["before"] != after:
            _refuse("Saved intent has missing or discontinuous document digests")
        saved = row.get("calls")
        if not isinstance(saved, list) or (row["before"] != row["after"] and not saved):
            _refuse("Saved intent material save has no commands")
        for call in saved:
            parsed = EditCommand.coerce(call)
            if any(getattr(parsed, key) is None for key in ("expect", "bindings", "created", "authorship")):
                _refuse("Saved intent requires original runner-recorded command guards")
        # Reuse the existing I/O-free replay policy and transaction runner.
        # Its strict media/adapter refusals remain implementation boundaries.
        document, _plan, _direct = RuntimeCommitService._apply(replay.document(), saved)
        replay = Checkout(document)
        replay._sync_film_length()  # the existing final save normalization
        if journal.digest(_serialize(replay._annotated())) != row["after"]:
            _refuse("Saved intent commands do not reproduce the recorded save digest")
        after = row["after"]
        calls.extend(copy.deepcopy(saved))
    if journal.digest(text) != after or _serialize(replay._annotated()) != text:
        _refuse("Saved intent current draft bytes do not match the recorded command result")
    if checkout._disk != text or _serialize(checkout._annotated()) != text:
        _refuse("Saved intent handle is stale, unsaved, or contains manual helper edits")
    return tuple(calls)
