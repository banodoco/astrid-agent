"""Read synchronization state and refresh saved commands through the one REBASE.

Runtime history owns publication ancestry. The existing working-copy journal
owns commands, accepted receipts, and refresh provenance; no second authority.
"""
from __future__ import annotations

import copy
from pathlib import Path

from astrid.sdk.timeline import journal, store
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.revisions import RuntimeRevisions
from astrid.sdk.timeline.saved_intent import SavedIntentError, read_saved_calls


def local_state(checkout):
    """Last linked accepted receipt and exact base publication, or explicit unknown."""
    from astrid.sdk.timeline.saved_publish import _state, read_rows
    path = journal.journal_path(checkout.bundle["project_id"], checkout.bundle["timeline_id"])
    rows = read_rows(checkout, path.read_text(encoding="utf-8"))
    _intent, result, _checkpoint = _state(rows)
    bases = [e for row in rows for e in row.get("evidence", [])
             if isinstance(e, dict) and e.get("kind") == "saved-base-v1"]
    base_publication = bases[-1].get("publication_id") if bases else None
    # A later refused request does not erase a previous acceptance.
    accepted = [e for row in rows for e in row.get("evidence", [])
                if isinstance(e, dict) and e.get("kind") == "publish-result-v1"
                and e["result"].get("status") in {"accepted", "no_change"}]
    if not accepted:
        return None, "absent", base_publication
    result = accepted[-1]
    intents = {e["intent_id"]: e for row in rows for e in row.get("evidence", [])
               if isinstance(e, dict) and e.get("kind") == "publish-intent-v1"}
    value = result["result"]
    receipt = value.get("runtime_receipt")
    intent = intents.get(result.get("intent_id"))
    revision = value.get("accepted_revision_id") if value["status"] == "accepted" else value.get("previous_revision_id")
    if (not isinstance(revision, str) or not revision or not isinstance(result.get("document"), dict)
            or result["document"].get("base_parent", {}).get("revision_id") != revision
            or not isinstance(receipt, dict) or intent is None
            or receipt.get("publisher") != intent["origin"]["principal"]
            or not receipt.get("accepted_at") or not receipt.get("evidence_digest")
            or (value["status"] == "accepted" and not value.get("accepted_publication_id"))):
        raise SavedIntentError("accepted receipt has no exact verified revision")
    return revision, "known", base_publication


def observed_publication(head, revisions):
    """Enrich only a uniquely marked Runtime head event, never list order."""
    if head.get("publication_id") is not None or revisions is None:
        return head
    matches = [r for r in revisions if r.is_current_head is True and r.id == head.get("revision_id")]
    if len(matches) != 1 or matches[0].publication_id is None:
        return head
    return {**head, "publication_id": matches[0].publication_id}


def external_revisions(revisions, head, base_revision, base_publication=None):
    """Walk only explicit event links. A repeated revision cannot select an event."""
    if revisions is None:
        return None
    head = observed_publication(head, revisions)
    by_publication = {r.publication_id: r for r in revisions if r.publication_id}
    if len(by_publication) != sum(bool(r.publication_id) for r in revisions):
        return None
    if base_publication is None:
        candidates = [r for r in revisions if r.id == base_revision]
        if len(candidates) != 1 or candidates[0].publication_id is None:
            return None
        base_publication = candidates[0].publication_id
    node = by_publication.get(head.get("publication_id"))
    if node is None or node.id != head.get("revision_id"):
        return None
    seen, out = set(), []
    while node.publication_id not in seen:
        seen.add(node.publication_id)
        if node.publication_id == base_publication:
            return out if node.id == base_revision else None
        out.append(dict(revision_id=node.id, publication_id=node.publication_id, source=node.source))
        previous = by_publication.get(node.previous_publication_id)
        if previous is None or previous.id != node.parent:
            return None
        node = previous
    return None


def status_fields(checkout, head, *, revisions=None, client=None, compact=False):
    """Common fields for every status presentation; None never means empty."""
    if not compact:
        head = observed_publication(head, revisions)
    fields = dict(observed_head=head.get("revision_id"), observed_publication_id=head.get("publication_id"),
                  base_revision=checkout.base_revision if checkout is not None else None,
                  base_state="absent" if checkout is None else "known" if checkout.base_revision else "unknown",
                  accepted_revision=None, accepted_state="absent" if checkout is None else "unknown", unimported_external_revisions=None,
                  external_state="absent" if checkout is None else "unknown", conflicts=None,
                  conflict_state="absent" if checkout is None else "unknown", choices=[], sync_error=None)
    if checkout is None:
        return fields
    try:
        accepted, state, publication = local_state(checkout)
        fields.update(accepted_revision=accepted, accepted_state=state)
        if compact:
            # Polling deliberately does not fetch history or a closure.
            return fields
        external = external_revisions(revisions, head, checkout.base_revision, publication)
        fields.update(unimported_external_revisions=external, external_state="known" if external is not None else "unknown")
        calls = read_saved_calls(checkout)
        if external is None:
            fields["sync_error"] = "publication ancestry is unavailable; select an exact known base publication"
            return fields
        from astrid.sdk.timeline.rebase import rebase
        from astrid.sdk.timeline.saved_publish import _exact, _workspace
        body = dict(project_id=checkout.bundle["project_id"], timeline_id=checkout.bundle["timeline_id"])
        onto = _exact(_workspace(client), body, head["revision_id"])
        result = rebase(calls, onto)
        fields.update(conflicts=list(result.conflicts), conflict_state="known",
                      choices=[result.next_step] if result.next_step else [])
    except (TimelineEditError, OSError, ValueError, TypeError, KeyError, AttributeError) as exc:
        fields["sync_error"] = str(exc)
    return fields


def refresh(checkout, *, client):
    """Persist rebase(my recorded commands, onto=observed exact head), or refuse unchanged."""
    from astrid.sdk.timeline.checkout import Checkout
    from astrid.sdk.timeline.doc import _serialize
    from astrid.sdk.timeline.rebase import rebase
    from astrid.sdk.timeline.saved_intent import _read_saved_calls_locked
    from astrid.sdk.timeline.saved_publish import _exact, _locked, _state, _workspace

    calls = read_saved_calls(checkout)
    old_text = checkout._disk
    with _locked(checkout) as (_path, _log, rows):
        intent, result, checkpoint = _state(rows)
        anchors = [e for row in rows for e in row.get("evidence", [])
                   if isinstance(e, dict) and e.get("kind") == "saved-base-v1"]
        base_document = copy.deepcopy(anchors[-1]["document"])
        if intent is not None and (result is None or (
                result["result"]["status"] in {"accepted", "no_change"} and checkpoint is None)):
            raise SavedIntentError("resolve the pending publish before refreshing")
    project, timeline = checkout.bundle["project_id"], checkout.bundle["timeline_id"]
    revisions = RuntimeRevisions(client.timelines)
    head = revisions.head_state(project, timeline)
    _accepted, _state_name, publication = local_state(checkout)
    history = revisions.list(project, timeline, limit=200)
    head = observed_publication(head, history)
    external = external_revisions(history, head, checkout.base_revision, publication)
    if external is None:
        raise SavedIntentError("publication ancestry is unknown or ambiguous; refresh refused")
    body = dict(project_id=project, timeline_id=timeline)
    workspace = _workspace(client)
    exact_base = Checkout(_exact(workspace, body, checkout.base_revision)).document()
    if exact_base != base_document:
        raise SavedIntentError("saved refresh base differs from the exact Runtime revision")
    onto = Checkout(_exact(workspace, body, head["revision_id"])).document()
    result = rebase(calls, onto)
    response = dict(status="conflict" if result.conflicts else "refreshed", observed_head=head["revision_id"],
                    base_revision=checkout.base_revision, conflicts=list(result.conflicts),
                    choices=[result.next_step] if result.next_step else [], plan=result.plan.to_json() if result.plan else None)
    if result.conflicts:
        return response
    candidate = Checkout(copy.deepcopy(result.document))
    candidate._sync_film_length()
    path = Path(checkout.path)
    with store._file_lock(path):
        log = journal.journal_path(project, timeline)
        with journal._locked(log):
            from astrid.sdk.timeline.saved_intent import _finite_json
            rows = [_finite_json(line) for line in log.read_text(encoding="utf-8").splitlines()
                    if line.strip()]
            own = [row for row in rows if row.get("draft") == path.stem]
            intent, result_record, checkpoint = _state(own)
            if intent is not None and (result_record is None or (
                    result_record["result"]["status"] in {"accepted", "no_change"} and checkpoint is None)):
                raise SavedIntentError("a publish started during refresh; resolve it before retrying")
            if path.read_text(encoding="utf-8") != old_text or tuple(_read_saved_calls_locked(checkout, path, log)) != calls:
                raise SavedIntentError("working copy changed during refresh; reload before retrying")
        if _serialize(candidate._annotated()) == old_text and not external:
            return response
        previous = copy.deepcopy(checkout.__dict__)
        try:
            checkout.bundle = candidate.document()
            checkout._refresh()
            checkout._journal = [("refresh recorded commands", old_text)]
            checkout._pending_calls = list(result.plan.calls)
            checkout._save_locked(path, force=False, evidence=(
                dict(kind="saved-refresh-v1", id_map=result.id_map),
                dict(kind="saved-base-v1", document=onto, publication_id=head["publication_id"])))
        except Exception:
            # Restore a failed in-memory transition; normal save owns disk/journal semantics.
            checkout.__dict__.clear()
            checkout.__dict__.update(previous)
            raise
    response["base_revision"] = checkout.base_revision
    return response
