"""Joined public facade/provider controls up to Runtime's acceptance boundary."""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from astrid.core.receipts.canonical import canonical_json
from astrid.sdk import timeline
from astrid.sdk.timeline.commit import bundle_from_document, create_runtime_commit_service
from astrid.sdk.workspace_client import WorkspaceClientError
from tests.sdk import test_timeline_commit_provider, test_timeline_sync
from tests.sdk.test_timeline_commit_provider import DetachedPort, DetachedRealm
from tests.sdk.test_timeline_commit_provider_document import document_fixture

connected = test_timeline_sync.connected
runtime_abi = test_timeline_commit_provider.runtime_abi


@pytest.mark.parametrize("function", [False, True], ids=["label", "function"])
@pytest.mark.parametrize("reads", [None, []], ids=["omitted-reads", "declared-reads"])
@pytest.mark.parametrize("dry_run", [False, True], ids=["commit", "preview"])
@pytest.mark.parametrize("patch", [True, False], ids=["doc.patch", "native"])
def test_command_reaches_current_provider_with_full_v2_request_and_frozen_retry(
    connected, monkeypatch, runtime_abi, function, reads, dry_run, patch,
):
    client, generated, path = connected
    document = document_fixture()
    document.update(project_id="p", timeline_id="t")
    document["parent_revision"]["revision_id"] = "r0"
    realm = DetachedRealm(document)
    generated.head = copy.deepcopy(realm.head)
    generated.bundles = {"r0": bundle_from_document(document)}
    # This source is the authenticated provider's bootstrap pair, not the
    # ordinary public-session fixture's synthetic pub0 pair.
    generated.publications = {"r0": "bootstrap"}
    change = {"op": "set", "ref": "parent", "path": ["name"],
              "before": {"state": "present", "value": "Renamed Film"}, "value": "Joined film"}
    command = {"schema_version": 1, "op": "doc.patch", "version": "1", "select": None,
               "args": {"changes": [change]}}
    if not patch:
        command = {"schema_version": 1, "op": "clip.move", "version": "1",
                   "select": "parent/clip:clip-a", "args": {"on": 3.003}}
    sent, prepared, evaluations = [], [], []

    class AcceptancePending(ConnectionError):
        pass

    class Port(DetachedPort):
        def accept(self, candidate):
            prepared.append(copy.deepcopy(candidate))
            # Keep the complete provider DTO; no fabricated Runtime acceptance.
            raise AcceptancePending("Runtime acceptance boundary")

        preview = accept

    def send(project, film, body, *, idempotency_key):
        assert (project, film) == ("p", "t")
        sent.append((copy.deepcopy(body), idempotency_key))
        invocation = {"operation": "commitTimeline", "path": "/v1/projects/p/timelines/t/commit",
                      "publisher": "actor", "request": body}
        context = {"request": copy.deepcopy(body), "realm_id": "realm", "publisher": "actor",
                   "idempotency_key": idempotency_key,
                   "request_hash": hashlib.sha256(canonical_json(invocation).encode()).hexdigest()}
        return create_runtime_commit_service().commit(context, Port(realm, context))

    def edit(working):
        evaluations.append(working.head)
        working.command(command, label="Rename", intent="Clarify title", tags=["title"])

    monkeypatch.setattr(generated, "commit_timeline", send)
    current = timeline.open("p", "t", client=client, outbox_path=path)
    if not function:
        edit(current)
    for attempt in range(2):
        with pytest.raises(WorkspaceClientError, match="No reply could be verified") as unknown:
            current.commit(edit if function else "Rename film", reads=reads,
                           dry_run=dry_run, idempotency_key="joined-patch")
        assert unknown.value.cause["message"] == "Runtime acceptance boundary"
        frozen = json.loads(path.read_text())
        if dry_run:  # 5199e67fc: a preview drops the caller's key; the flight gets a generated one
            assert frozen["flight"]["key"].startswith("timeline-edit-")
            assert frozen["flight"]["key"] != "joined-patch"
        else:
            assert frozen["flight"]["key"] == "joined-patch"
        assert json.loads(frozen["flight"]["body"]) == sent[-1][0]
        if attempt == 0:
            current = timeline.open("p", "t", client=client, outbox_path=path)

    assert sent[0] == sent[1] and len(evaluations) == 1
    body = sent[0][0]
    assert body["commands"] == [command]
    assert body["label"] == ("edit" if function else "Rename film")
    assert body["dry_run"] is dry_run
    assert body["base_revision_id"] == "r0" and body["base_publication_id"] == "bootstrap"
    assert body["steps"][0]["patch"] == ([change] if patch else [])
    assert body["steps"][0]["intent"] == "Clarify title"
    assert body["steps"][0]["tags"] == ["title"]
    if reads is None and (function or patch):
        assert "reads" not in body and body["merge"] == "atomic"
        assert body["steps"][0]["reads"] == [{"kind": "scope", "ref": "timeline", "path": []}]
    else:
        assert body.get("reads") == reads and body["merge"] == "fields"
    assert prepared[0] == prepared[1]
    candidate = prepared[0]
    payload = candidate["canonical_document"]["parent_revision"]["payload"]
    if patch:
        assert payload["name"] == "Joined film"
        assert candidate["records"]["steps"][0]["patch"] == [change]
    else:
        assert payload["clips"][0]["at"] == 3.003
        assert any(write["path"] == ["at"] and write["before"] == {"state": "present", "value": 1}
                   and write["after"] == {"state": "present", "value": 3.003}
                   for write in candidate["records"]["commands"][0]["writes"])
    assert candidate["expected_head"] == realm.head
    assert candidate["records"]["label"] == body["label"]
    assert candidate["records"]["merge_mode"] == body["merge"]
    assert realm.accepted == [] and generated.accepted == {}
