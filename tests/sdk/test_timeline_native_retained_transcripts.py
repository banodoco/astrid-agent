"""Public native capture uses the transcript pin retained by its exact publication."""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

from astrid.sdk.contracts import DomainResult
from astrid.sdk.timeline import Checkout, TimelineEditError
from astrid.sdk.timeline.outbox import Outbox
from astrid.sdk.timeline.sync import TimelineSession
from tests.timeline.test_occurrence_media_swap import (
    NEW_ID,
    CatalogTransport,
    _retained_anchor_swap,
)


@pytest.mark.parametrize("route", ["queue", "commit", "attach", "media_add"])
@pytest.mark.parametrize("revoked", [False, True])
def test_native_capture_uses_the_exact_retained_parent_anchor(tmp_path, monkeypatch, route, revoked):
    bundle, capture = _retained_anchor_swap()
    project, timeline = bundle["project_id"], bundle["timeline_id"]
    basis = {"revision_id": bundle["base_parent"]["revision_id"], "publication_id": "accepted"}
    identity = {"realm_id": "realm", "actor_id": "actor"}
    reads, opens, flushes = [], [], []

    class Transport(CatalogTransport):
        _last_handshake = identity

        def get_project_verified_document(self, project_id, pin):
            reads.append((project_id, copy.deepcopy(pin)))
            if revoked:
                error = RuntimeError("retained pin revoked")
                error.code = "not_found"
                raise error
            return copy.deepcopy(capture)

    def open_bundle(project_id, timeline_id, *, revision_id, publication_id=None):
        opens.append((project_id, timeline_id, revision_id, publication_id))
        return DomainResult.success({"bundle": copy.deepcopy(bundle)})

    transport = Transport()
    client = SimpleNamespace(
        _remote=SimpleNamespace(_transport=transport),
        timelines=SimpleNamespace(_client=transport, open_bundle=open_bundle),
    )
    outbox = Outbox(tmp_path / "outbox.json")
    session = TimelineSession(client, outbox, identity, project, timeline)
    with outbox.locked():
        outbox.write({"base": basis, "pending": [], "flight": None,
                      "scope": {**identity, "project_id": project, "timeline_id": timeline}})
    command = {"schema_version": 1, "op": "track.set", "version": "1",
               "select": "parent/track:picture", "args": {"values": {"label": "New label"}, "clear": []}}
    monkeypatch.setattr(session, "flush", lambda **kwargs: flushes.append(kwargs))
    attached = Checkout.attach(session) if route == "attach" else None
    before = outbox.path.read_bytes()
    preview = copy.deepcopy(attached.bundle) if attached is not None else None

    def edit():
        if route == "queue":
            session.queue(command)
        elif route == "commit":
            session.commit(command)
        elif route == "media_add":
            selection = {
                "asset": "new-footage", "object_id": NEW_ID, "generation_id": "new-generation",
                "variant_id": "new-variant", "catalog_digest": "sha256:" + "a" * 64,
            }
            session.add_media(selection, on=0, duration=1, track="added-track", clip_id="added")
        else:
            attached.clip("visual").set_duration(1.5)

    if revoked:
        with pytest.raises(TimelineEditError, match="exact transcript pin is missing or unavailable"):
            edit()
        assert outbox.path.read_bytes() == before
        assert flushes == []
        if attached is not None:
            assert attached.bundle == preview
    else:
        edit()
        pending = outbox.read()["pending"]
        assert len(pending) == (2 if route == "media_add" else 1)
        assert len(flushes) == (route in {"commit", "media_add"})
        if route == "media_add":
            assert [item["op"] for item in pending] == ["track.create", "clip.add-media"]
            assert pending[-1]["args"]["row"]["id"] == "added"
        assert b"pinned_documents" not in outbox.path.read_bytes()
        if attached is not None:
            expected_duration = attached.quantize(1.5)
            assert attached.clip("visual").duration == pytest.approx(expected_duration)
            assert not attached.bundle["source_mapping"].get("pinned_documents")
            reopened = Checkout.attach(session)
            assert reopened.clip("visual").duration == pytest.approx(expected_duration)
            assert not reopened.bundle["source_mapping"].get("pinned_documents")

    assert reads == [(project, capture["pin"])] * (2 if route == "attach" and not revoked else 1)
    assert set(opens) == {(project, timeline, basis["revision_id"], basis["publication_id"])}
