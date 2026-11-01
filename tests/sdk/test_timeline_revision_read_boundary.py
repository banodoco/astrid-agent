"""Revision inspection and authenticated capture have distinct source custody."""
from __future__ import annotations

import copy
import json

import pytest

from astrid.sdk import timeline
from astrid.sdk.contracts import DomainResult
from astrid.sdk.timeline import store
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.capture import capture_move
from astrid.sdk.timeline.checkout import Checkout
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import _digest, bundle_from_document
from astrid.sdk.timeline.sync import TimelineSession
from astrid.sdk.timeline_sheet import NativeSheet, apply_sheet, render_sheet
from tests.sdk.test_timeline_restructure import restructure_runtime  # noqa: F401
from tests.sdk.test_timeline_restructure_guarded_runtime import (
    restructure_inverse_runtime,  # noqa: F401
)
from tests.sdk.test_timeline_sheet_native import duration_edit, edit_cell


def test_revision_inspection_does_not_replace_authenticated_session_capture(
    restructure_inverse_runtime, tmp_path,  # noqa: F811
):
    env = restructure_inverse_runtime
    session = TimelineSession.open(env.project, "t", client=env.client,
                                   outbox_path=tmp_path / "boundary.json")
    state = session.outbox.read()
    base = state["base"]
    ordinary = store.fetch_bundle(env.project, "t", client=env.client,
                                  revision_id=base["revision_id"])
    original = copy.deepcopy(ordinary)
    assert "_runtime_document" not in ordinary["source_mapping"]
    assert "publication_id" not in ordinary["base_parent"]
    command = capture_move(ordinary, "occurrence:occ-film/clip:cross", 4)
    preview = execute(ordinary, [command]).bundle
    assert preview["source_mapping"] == ordinary["source_mapping"]
    assert ordinary == original

    document = env.document()
    authenticated = {
        **ordinary,
        "base_parent": {**ordinary["base_parent"], "publication_id": base["publication_id"]},
        "source_mapping": {**ordinary["source_mapping"], "_runtime_document": document},
    }
    projected = {**ordinary, "base_parent": authenticated["base_parent"]}
    assert session._bundle(state) == projected
    assert "_runtime_document" not in json.dumps(session._bundle(state))
    assert session._capture_bundle(state) == authenticated
    assert NativeSheet.export(session)._bundle == authenticated
    assert NativeSheet.load(session)._bundle == authenticated
    canonical = bundle_from_document(document)
    assert canonical["source_mapping"] == authenticated["source_mapping"]
    opened = timeline.open(env.project, "t", client=env.client,
                           outbox_path=tmp_path / "public.json")
    assert opened.bundle["source_mapping"] == ordinary["source_mapping"]
    assert opened._execution_bundle(opened.bundle) == authenticated


def test_clean_session_capture_refuses_a_mismatched_pair_without_writes(
    restructure_inverse_runtime, tmp_path,  # noqa: F811
):
    env = restructure_inverse_runtime
    session = TimelineSession.open(env.project, "t", client=env.client,
                                   outbox_path=tmp_path / "wrong-pair.json")
    state = session.outbox.read()
    history = env.client.timelines.history(env.project, "t", limit=200).data
    historical = next(row for row in history["items"]
                      if row["revision_id"] != state["base"]["revision_id"])
    state["base"]["publication_id"] = historical["publication_id"]
    stored = tuple(env.service.store.conn.iterdump())
    outbox = session.outbox.path.read_bytes()
    calls = copy.deepcopy(env.calls)
    with pytest.raises(TimelineEditError, match="original revision/publication pair changed"):
        session._bundle(state)
    assert tuple(env.service.store.conn.iterdump()) == stored
    assert session.outbox.path.read_bytes() == outbox
    assert env.calls == calls


@pytest.mark.parametrize("damage", ["publication", "accepted-document"])
def test_private_capture_reauthenticates_same_revision_without_writes(
    restructure_inverse_runtime, tmp_path, monkeypatch, damage,  # noqa: F811
):
    env = restructure_inverse_runtime
    session = TimelineSession.open(env.project, "t", client=env.client,
                                   outbox_path=tmp_path / "reauthenticate.json")
    state = session.outbox.read()
    genuine = session._capture_bundle(state)
    history = env.client.timelines.history
    stored = tuple(env.service.store.conn.iterdump())
    outbox = session.outbox.path.read_bytes()
    calls = copy.deepcopy(env.calls)

    def damaged(*args, **kwargs):
        data = copy.deepcopy(history(*args, **kwargs).data)
        entry = next(row for row in data["items"]
                     if row["publication_id"] == state["base"]["publication_id"])
        assert entry["revision_id"] == state["base"]["revision_id"]
        if damage == "publication":
            entry["receipt"]["receipt_id"] = "different-publication"
        else:
            document = copy.deepcopy(genuine["source_mapping"]["_runtime_document"])
            parent = document["parent_revision"]
            parent["payload"]["private-cache-forgery"] = True
            parent["content_digest"] = _digest(parent["payload"])
            entry["receipt"]["result"]["canonical_document"] = document
        return DomainResult.success(data)

    monkeypatch.setattr(env.client.timelines, "history", damaged)
    message = ("original revision/publication pair changed" if damage == "publication"
               else "canonical document differs from the original closure read")
    for read in (session._bundle, session._capture_bundle):
        with pytest.raises(TimelineEditError, match=message):
            read(state)
    assert tuple(env.service.store.conn.iterdump()) == stored
    assert session.outbox.path.read_bytes() == outbox
    assert env.calls == calls


def test_private_capture_serves_real_sheet_and_session_acceptance(
    restructure_inverse_runtime, tmp_path,  # noqa: F811
):
    env = restructure_inverse_runtime
    session = TimelineSession.open(env.project, "t", client=env.client,
                                   outbox_path=tmp_path / "sheet-session.json")
    exported = NativeSheet.export(session)
    loaded = NativeSheet.load(session)
    assert loaded._bundle == exported._bundle == session._capture_bundle(session._state())
    text = edit_cell(loaded.text, "occurrence:occ-film/clip:cross", "4")
    landed = loaded.apply(text, session=session, dry_run=False, idempotency_key="private-sheet")
    assert landed["data"]["status"] == "accepted"
    assert "_runtime_document" not in session._bundle(session._state())["source_mapping"]

    opened = timeline.open(env.project, "t", client=env.client,
                           outbox_path=tmp_path / "python-session.json")
    opened.bundle["parent"]["opaque"]["private-cache-session"] = True
    accepted = opened.commit("Private capture", idempotency_key="private-python")
    assert accepted["data"]["status"] == "accepted"
    assert accepted["data"]["canonical_document"]["parent_revision"]["payload"]["opaque"]["private-cache-session"] is True
    assert "_runtime_document" not in opened.bundle["source_mapping"]
    assert opened._runtime_document == accepted["data"]["canonical_document"]


def test_restructure_uses_private_capture_without_changing_detached_source(
    restructure_inverse_runtime, tmp_path, monkeypatch,  # noqa: F811
):
    env = restructure_inverse_runtime
    source = timeline.open(env.project, "t", client=env.client,
                           outbox_path=tmp_path / "name.json")
    source.bundle["shots"][source._shot_ids()[0]]["payload"]["name"] = "02 TWO"
    named = source.commit("Name source chapter", idempotency_key="private-name")
    checkout = Checkout.open(env.project, "t", client=env.client)
    original = copy.deepcopy(checkout.bundle)
    captured = []
    capture = TimelineSession._capture_bundle

    def observe(session, state, extra_commands=()):
        bundle = capture(session, state, extra_commands)
        captured.append(copy.deepcopy(bundle))
        return bundle

    monkeypatch.setattr(TimelineSession, "_capture_bundle", observe)
    result = checkout._commit_restructure("Private restructure", client=env.client, force=False,
                                        idempotency_key="private-restructure")
    assert result["receipt"]["result"]["status"] == "accepted"
    assert result["new_head"] == env.document()["parent_revision"]["revision_id"]
    assert captured[0]["source_mapping"]["_runtime_document"] == named["data"]["canonical_document"]
    assert captured[0]["base_parent"]["publication_id"] == named["receipt"]["receipt_id"]
    assert checkout.bundle == original
    assert "_runtime_document" not in json.dumps(checkout.document())


def test_detached_sheet_retains_private_capture_through_stage_reload_and_apply(
    restructure_inverse_runtime, tmp_path,  # noqa: F811
):
    from astrid.sdk.timeline.outbox import Outbox

    env = restructure_inverse_runtime
    source = timeline.open(env.project, "t", client=env.client,
                           outbox_path=tmp_path / "card.json")
    card = next(clip for clip in source.bundle["shots"][source._shot_ids()[0]]["internal_timeline"]["clips"]
                if clip["id"] == "late")
    card["app"] = {"astrid": {"cut": "c3", "layer": "card", "for": 1}}
    accepted = source.commit("Bind sheet card", idempotency_key="private-card")
    checkout = Checkout(copy.deepcopy(source.bundle))
    checkout._scene_client = env.client
    checkout.save(tmp_path / "detached.json", quiet=True)
    text = render_sheet(checkout, cuts=["c3"])
    outbox = Outbox(tmp_path / "detached.json.outbox.json")
    retained = outbox.read()
    assert retained["sheet"]["bundle"]["source_mapping"]["_runtime_document"] == accepted["data"]["canonical_document"]
    assert retained["sheet"]["basis"] == source.head
    assert "_runtime_document" not in checkout.bundle["source_mapping"]
    assert "for 1s" in text
    checkout = Checkout.load(checkout.path)
    assert apply_sheet(checkout, duration_edit(text, 2), client=env.client)
    assert "_runtime_document" not in checkout.bundle["source_mapping"]
    checkout.save(quiet=True)
    reloaded = Checkout.load(checkout.path)
    assert "_runtime_document" not in reloaded.bundle["source_mapping"]
    result = env.replies[-1]
    assert result["data"]["status"] == "accepted"
    assert result["data"]["commands"][0]["op"] == "clip.set"
    assert outbox.read()["pending"] == [] and outbox.read()["flight"] is None
    document = env.document()
    assert next(clip for image in document["internal_timeline_revisions"]
                for clip in image["payload"]["clips"] if clip["id"] == "late")["hold"] == 2
