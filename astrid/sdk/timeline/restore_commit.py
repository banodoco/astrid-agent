"""CLI restore request producer; the existing semantic COMMIT owns all image edits.

The timeline journal retains the original envelope before dispatch so a repeated
key replays the same captured head, selection, session and receipt after a race
or lost response. No draft or public history is rewritten here.
"""
from __future__ import annotations

import copy
import uuid

from astrid.sdk.contracts import DomainResult, ErrorObject
from astrid.sdk.timeline import journal
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.commit import _digest
from astrid.sdk.timeline.revisions import RuntimeRevisions, resolve_revision_record
from astrid.sdk.timeline.saved_intent import _finite_json
from astrid.sdk.timeline.saved_publish import _authenticate, _key, _verify_result, _workspace
from astrid.sdk.workspace_client import WorkspaceClientError


def _intent(path, key, origin, selection, message):
    if not path.exists():
        return None
    if journal.verify(path):
        raise TimelineEditError("restore journal is damaged; run timelines log --verify")
    found = []
    for line in path.read_text(encoding="utf-8").splitlines():
        row = _finite_json(line)
        for item in row.get("evidence", []):
            if isinstance(item, dict) and item.get("kind") == "restore-commit-intent-v1" and item["body"]["request_id"] == key:
                if (item.get("request_hash") != _digest(item["body"])
                        or item.get("origin") != origin or item.get("selection") != selection
                        or item.get("message") != message
                        or (row.get("project"), row.get("timeline")) != (
                            item["body"]["project_id"], item["body"]["timeline_id"])):
                    raise TimelineEditError("restore key has different scope, origin or intent")
                found.append(item)
    if len(found) > 1:
        raise TimelineEditError("restore journal has duplicate request envelopes")
    return found[0] if found else None


def restore_revision(client, project, timeline, source, *, idempotency_key=None, message=None):
    """Submit one named whole-image COMMIT, preserving exact-key recovery."""
    key = _key(idempotency_key) or uuid.uuid4().hex
    scope = client.timelines.resolve_scope(project, timeline)
    if not scope.ok:
        return DomainResult.failure(scope.error, idempotency_key=key)
    project, canonical_timeline = scope.data["project_id"], scope.data["timeline_id"]
    if "@" in source:
        name, _, source = source.rpartition("@")
        if name and name not in {timeline, canonical_timeline}:
            raise TimelineEditError("restore source must belong to the same timeline")
    selection = dict(project=project, timeline=canonical_timeline, ref=source)
    workspace = _workspace(client)
    origin, session = _authenticate(workspace)
    path = journal.journal_path(project, canonical_timeline)
    with journal._locked(path):
        intent = _intent(path, key, origin, selection, message)
    if intent is None:
        store = RuntimeRevisions(client.timelines)
        head = store.head_state(project, canonical_timeline)
        if not head["revision_id"]:
            raise TimelineEditError("restore requires a published head")
        selected = resolve_revision_record(project, canonical_timeline, source, store=store)
        if selected is None:
            # Select the captured head event, never a fresh mutable head.
            rows = store.list(project, canonical_timeline, limit=200) or []
            selected = next((row for row in rows if row.id == head["revision_id"]
                             and row.publication_id == head["publication_id"]), None)
            if selected is None:
                raise TimelineEditError("captured restore head publication is unavailable")
        command = store.restore_command(selected)
        body = dict(api_version=1, project_id=project, timeline_id=canonical_timeline,
                    request_id=key, session_id=session, session_sequence=1,
                    base_revision_id=head["revision_id"], base_publication_id=head["publication_id"],
                    scope=dict(target="parent", complete=["clips", "tracks"], projection_version=1),
                    change=dict(commands=[command]), source=dict(via="cli"),
                    label=message if message is not None else f"Restore {selected.id}", dry_run=False)
        prepared = dict(kind="restore-commit-intent-v1", body=body, request_hash=_digest(body),
                        origin=origin, selection=selection, message=message)
        with journal._locked(path):
            intent = _intent(path, key, origin, selection, message)
            if intent is None:
                journal._append_locked(path, project=project, timeline=canonical_timeline,
                                       draft="restore-commit", kind="restore", outcome="pending", label=body["label"],
                                       source=body["source"], evidence=(prepared,))
                intent = prepared  # append fsyncs before any mutation request
    body = intent["body"]
    try:
        result = workspace.commit_timeline(copy.deepcopy(body))
        _verify_result(workspace, intent, result)
    except (WorkspaceClientError, TimelineEditError) as exc:
        return DomainResult.failure(ErrorObject(getattr(exc, "code", "unverified_commit"), str(exc), {
            "details": getattr(exc, "details", None), "retry": "repeat restore with this idempotency key and selection",
        }), idempotency_key=key)
    with journal._locked(path):
        _intent(path, key, origin, selection, message)
        journal._append_locked(path, project=project, timeline=canonical_timeline,
                               draft="restore-commit", kind="restore", outcome=result["status"], label=body["label"],
                               source=body["source"], evidence=(dict(
                                   kind="restore-commit-result-v1", request_id=key,
                                   request_hash=intent["request_hash"], result=copy.deepcopy(result)),))
    if result["status"] != "accepted":
        error = result.get("error") or {}
        return DomainResult.failure(ErrorObject(error.get("code", result["status"]),
            error.get("message", "restore COMMIT requires correction"), {"receipt": result}),
            idempotency_key=key)
    return DomainResult.success(result, idempotency_key=key)
