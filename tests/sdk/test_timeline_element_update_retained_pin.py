"""A public element update compiles retained anchors on other parent clips."""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from astrid.core.receipts.contract import CommandReceipt
from astrid.sdk.contracts import DomainResult
from astrid.sdk.remote import RemoteTimelines
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import _digest, bundle_from_document
from astrid.sdk.timeline.outbox import Outbox
from astrid.sdk.timeline.sync import TimelineSession
from tests.timeline.test_occurrence_media_swap import _retained_anchor_swap


@pytest.mark.parametrize("damage", [None, "mismatch", "revoked"])
def test_public_duration_update_authenticates_other_clips_retained_anchor(tmp_path, monkeypatch, damage):
    anchor_bundle, capture = _retained_anchor_swap()
    document = copy.deepcopy(anchor_bundle["source_mapping"]["_runtime_document"])
    parent = document["parent_revision"]
    payload = parent["payload"]
    existing_anchor = copy.deepcopy(payload["clips"][0])
    element = {"id": "reveal", "kind": "effect", "packId": "already-admitted", "revision": "sha256:" + "1" * 64}
    payload["clips"].append({"id": "effect", "track": existing_anchor["track"], "clipType": element["id"],
                             "elementRef": element, "at": 0, "hold": 1})
    if "clips" in payload["config"]:
        payload["config"]["clips"] = copy.deepcopy(payload["clips"])
    parent["content_digest"] = _digest(payload)
    bundle = bundle_from_document(document)

    project, timeline = bundle["project_id"], bundle["timeline_id"]
    basis = {"revision_id": parent["revision_id"], "publication_id": "accepted"}
    identity = {"realm_id": "realm", "actor_id": "actor"}
    reads, opens, submitted = [], [], []

    class Transport:
        _last_handshake = identity

        def get_project_verified_document(self, project_id, pin):
            reads.append((project_id, copy.deepcopy(pin)))
            if damage == "revoked":
                error = RuntimeError("retained pin revoked")
                error.code = "not_found"
                raise error
            verified = copy.deepcopy(capture)
            if damage == "mismatch":
                verified["document"]["version"] += 1
            return verified

    transport = Transport()
    owner = RemoteTimelines(transport)
    client = SimpleNamespace(_remote=SimpleNamespace(_transport=transport), timelines=owner)
    outbox = Outbox(tmp_path / "update-outbox.json")
    session = TimelineSession(client, outbox, identity, project, timeline)
    with outbox.locked():
        outbox.write({"base": basis, "pending": [], "flight": None,
                      "scope": {**identity, "project_id": project, "timeline_id": timeline}})
    monkeypatch.setattr(TimelineSession, "open", lambda *args, **kwargs: session)
    monkeypatch.setattr(owner, "history", lambda *args, **kwargs: DomainResult.success(
        {"head": copy.deepcopy(basis), "items": [], "next_cursor": None}))

    def open_bundle(project_id, timeline_id, *, revision_id, publication_id=None):
        opens.append((project_id, timeline_id, revision_id, publication_id))
        opened = copy.deepcopy(bundle)
        if publication_id is None:
            opened["source_mapping"].pop("_runtime_document")
        return DomainResult.success({"bundle": opened})

    def submit(commands, **options):
        submitted.append((copy.deepcopy(commands), copy.deepcopy(options)))
        assert options["base"] == basis
        assert [command["op"] for command in commands] == ["clip.set"]
        authoritative = copy.deepcopy(bundle)
        authoritative["source_mapping"]["pinned_documents"] = [copy.deepcopy(capture)]
        plan = execute(authoritative, commands, realm_id=identity["realm_id"])
        assert plan.bundle["parent"]["clips"][0] == existing_anchor
        data = {"accepted_revision_id": "new-revision", "canonical_document": {
            "parent_revision": {"payload": plan.bundle["parent"]}}}
        receipt = CommandReceipt("new-publication", "timeline.commit", options["idempotency_key"],
                                 "hash", project, (1, 1), (), data, "2026-10-20T00:00:00Z")
        return {"data": data, "receipt": receipt.as_dict()}

    monkeypatch.setattr(owner, "open_bundle", open_bundle)
    monkeypatch.setattr(session, "submit", submit)
    saved = outbox.path.read_bytes()
    result = owner.update_parent_element(project, timeline, realm=identity["realm_id"], clip_id="effect",
                                         element=element, expected_head=basis["revision_id"], duration=1.5)
    if damage:
        assert not result.ok
        message = {"mismatch": "scope or version", "revoked": "missing or unavailable"}
        assert message[damage] in result.error.message
        assert submitted == []
        assert outbox.path.read_bytes() == saved
    else:
        assert result.ok, result.error
        assert result.data["duration"] == pytest.approx(1.5)
        assert result.data["before"]["duration"] == 1
        assert "pinned_documents" not in repr(submitted)
        assert capture["document"]["content"] not in submitted[0][0]
    assert reads == [(project, capture["pin"])]
    assert set(opens) == {(project, timeline, basis["revision_id"], basis["publication_id"])}
    assert "pinned_documents" not in bundle["source_mapping"]
