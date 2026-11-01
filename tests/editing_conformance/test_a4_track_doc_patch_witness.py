"""A4 witness for the mounted track-order save as a generic ``doc.patch``.

The App's dormant ``track.reorder`` capture is deliberately not an Agent
operation.  These cases use the current mounted save shape: an id-addressed
``list.move`` in a ``doc.patch`` command, first through the detached command
stage and then through the existing provider harness.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from astrid.sdk.timeline._command_state import CommandError, present
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import _digest, bundle_from_document
from astrid.sdk.timeline.rebase import replay
from tests.editing_conformance.test_doc_patch_v2 import change, patch, step, submit
from tests.sdk.test_timeline_commit_provider import (
    CommitRejected,
    DetachedRealm,
)
from tests.sdk.test_timeline_commit_provider import (
    runtime_abi as runtime_abi,
)
from tests.sdk.test_timeline_commit_provider_document import document_fixture

APP_TRACK_FIXTURE = Path(__file__).parent / "fixtures" / "recipe_m2_app_track_label.json"


def track_document():
    """Make a real document containing the rows used by the App capture case."""
    app_fixture = json.loads(APP_TRACK_FIXTURE.read_text(encoding="utf-8"))
    rows = copy.deepcopy(app_fixture["bundle"]["parent"]["config"]["tracks"])
    rows.extend([
        {"id": "audio", "kind": "audio", "label": None,
         "opaque": {"null": None, "false": False, "zero": 0}},
        {"id": "overlay", "kind": "visual", "label": "Overlay",
         "opaque": {"keep": [None, False, 0]}},
    ])

    document = document_fixture()
    payload = document["parent_revision"]["payload"]
    payload["config"]["tracks"] = rows
    payload["config"]["app"] = {
        "timelineDataLanePlacement": {"data-1": "between"},
        "opaque": {"keep": None},
    }
    payload["opaque_parent"] = {"keep": [None, False, 0]}
    document["parent_revision"]["content_digest"] = _digest(payload)
    return document


def track_bundle():
    return bundle_from_document(track_document())


def track_rows(bundle):
    return bundle["parent"]["config"]["tracks"]


def track_order(bundle):
    return [row["id"] for row in track_rows(bundle)]


def track_read(order):
    return {
        "kind": "collection",
        "ref": "parent",
        "path": ["config", "tracks"],
        "members": sorted(order),
        "order": list(order),
    }


def move_overlay(bundle, *, order=None, before=None, after_id=None, created=None):
    rows = track_rows(bundle)
    order = track_order(bundle) if order is None else list(order)
    row = next(row for row in rows if row["id"] == "overlay")
    return patch(change(
        ["config", "tracks"],
        present(row) if before is None else before,
        op="list.move",
        id="overlay",
        order_before=order,
        after_id=after_id,
    ), created=[] if created is None else created)


def current_document(realm):
    return realm.documents[realm.head["revision_id"]]


def assert_track_rows_and_neighbors(payload, expected_order, *, audio_opaque=None):
    rows = payload["config"]["tracks"]
    assert [row["id"] for row in rows] == expected_order
    audio = next(row for row in rows if row["id"] == "audio")
    overlay = next(row for row in rows if row["id"] == "overlay")
    assert audio["label"] is None
    assert audio["opaque"] == (audio_opaque or {"null": None, "false": False, "zero": 0})
    assert overlay["opaque"] == {"keep": [None, False, 0]}
    assert payload["config"]["app"] == {
        "timelineDataLanePlacement": {"data-1": "between"},
        "opaque": {"keep": None},
    }
    assert payload["opaque_parent"] == {"keep": [None, False, 0]}


def test_detached_track_doc_patch_preserves_rows_and_json_replays_without_source_write():
    original = track_bundle()
    before = copy.deepcopy(original)
    call = move_overlay(original)

    plan = execute(original, [call], commit_request={"reads": [track_read(track_order(original))]})
    replayed_call = json.loads(json.dumps(call))
    replayed = execute(original, [replayed_call], commit_request={"reads": []})

    assert original == before
    assert track_order(plan.bundle) == ["overlay", "picture", "audio"]
    assert_track_rows_and_neighbors(plan.bundle["parent"], ["overlay", "picture", "audio"])
    assert replayed.bundle == plan.bundle
    assert replay(plan, original).bundle == plan.bundle
    assert plan.records[0]["command"]["op"] == "doc.patch"
    assert plan.records[0]["command"]["args"]["changes"][0]["op"] == "list.move"
    assert plan.records[0]["command"]["args"]["changes"][0]["before"] == present(
        before["parent"]["config"]["tracks"][2])


def test_public_provider_accepts_current_doc_patch_and_persists_c0_step_and_read_records():
    realm = DetachedRealm(track_document())
    base = copy.deepcopy(realm.head)
    bundle = bundle_from_document(current_document(realm))
    order = track_order(bundle)
    call = move_overlay(bundle)
    read = track_read(order)
    controls = {
        "label": "Move tracks",
        "merge": "fields",
        "reads": [read],
        "steps": [step("reorder", 0, call, reads=[read])],
    }

    preview = submit(realm, call, dry_run=True, **controls)
    assert preview["data"]["status"] == "preview"
    assert preview["receipt"] is None
    assert realm.head == base

    accepted = submit(realm, call, **controls)
    assert accepted["data"]["status"] == "accepted"
    assert accepted["data"]["base_revision_id"] == base["revision_id"]
    assert accepted["data"]["base_publication_id"] == base["publication_id"]
    assert_track_rows_and_neighbors(
        accepted["data"]["canonical_document"]["parent_revision"]["payload"],
        ["overlay", "picture", "audio"],
    )

    records = realm.entries[-1]["records"]
    assert records["label"] == "Move tracks"
    assert records["merge_mode"] == "fields"
    assert records["steps"][0]["patch"] == call["args"]["changes"]
    assert read in records["steps"][0]["dependencies"]
    command = records["commands"][0]
    assert command["command"]["op"] == "doc.patch"
    assert command["command"]["args"]["changes"][0]["op"] == "list.move"
    assert command["command"]["args"]["changes"][0]["before"] == present(
        bundle["parent"]["config"]["tracks"][2])
    assert command["created"] == []


@pytest.mark.parametrize("damage", ["previous", "order", "anchor"])
def test_public_track_doc_patch_refuses_bad_previous_order_or_anchor_without_write(damage):
    realm = DetachedRealm(track_document())
    bundle = bundle_from_document(current_document(realm))
    order = track_order(bundle)
    call = move_overlay(bundle)
    change_item = call["args"]["changes"][0]
    if damage == "previous":
        change_item["before"]["value"]["label"] = "not Overlay"
    elif damage == "order":
        change_item["order_before"] = ["audio", "picture", "overlay"]
    else:
        change_item["after_id"] = "missing"
    before_head = copy.deepcopy(realm.head)
    before_entries = copy.deepcopy(realm.entries)
    before_documents = copy.deepcopy(realm.documents)

    with pytest.raises(CommitRejected) as error:
        submit(realm, call, reads=[track_read(order)])

    assert error.value.failure["details"]["status"] == "invalid"
    assert realm.head == before_head
    assert realm.entries == before_entries
    assert realm.documents == before_documents


def test_detached_track_doc_patch_refuses_duplicate_identity_before_mutation():
    bundle = track_bundle()
    bundle["parent"]["config"]["tracks"].append(copy.deepcopy(
        bundle["parent"]["config"]["tracks"][0]))
    before = copy.deepcopy(bundle)

    with pytest.raises(CommandError, match="missing or duplicate collection identity"):
        execute(bundle, [move_overlay(track_bundle())])

    assert bundle == before


def test_public_track_doc_patch_rejects_membership_change_and_forged_created_claim():
    realm = DetachedRealm(track_document())
    base = copy.deepcopy(realm.head)
    bundle = bundle_from_document(current_document(realm))
    order = track_order(bundle)
    inserted = {"id": "new-track", "kind": "visual", "opaque": {"new": None}}
    insert = patch(change(
        ["config", "tracks"],
        {"state": "absent"},
        inserted,
        op="list.insert",
        id="new-track",
        order_before=order,
        after_id="overlay",
    ), created=["parent/track:new-track"])
    submit(realm, insert, reads=[])
    before = copy.deepcopy(realm.documents)
    target = move_overlay(bundle)

    with pytest.raises(CommitRejected) as error:
        submit(realm, target, base=base, reads=[track_read(order)])

    assert error.value.failure["details"]["reason"] == "selection_changed"
    assert realm.documents == before
    assert [row["id"] for row in current_document(realm)["parent_revision"]["payload"]["config"]["tracks"]] == [
        "picture", "audio", "overlay", "new-track"
    ]

    forged = move_overlay(bundle, created=["parent/track:overlay"])
    with pytest.raises(CommandError, match="created identity is occupied"):
        execute(bundle, [forged])


def test_public_track_doc_patch_detects_order_aba_even_when_final_order_returns_to_base():
    realm = DetachedRealm(track_document())
    base = copy.deepcopy(realm.head)
    bundle = bundle_from_document(current_document(realm))
    original_order = track_order(bundle)
    first = move_overlay(bundle)
    submit(realm, first, reads=[])

    moved = bundle_from_document(current_document(realm))
    second = move_overlay(moved, order=["overlay", "picture", "audio"], after_id="audio")
    submit(realm, second, reads=[])
    assert track_order(bundle_from_document(current_document(realm))) == original_order
    before = copy.deepcopy(realm.documents)

    with pytest.raises(CommitRejected) as error:
        submit(realm, first, base=base, reads=[track_read(original_order)])

    assert error.value.failure["details"]["reason"] == "selection_changed"
    assert realm.documents == before


def test_public_track_doc_patch_merges_disjoint_neighbor_field_change():
    realm = DetachedRealm(track_document())
    base = copy.deepcopy(realm.head)
    bundle = bundle_from_document(current_document(realm))
    order = track_order(bundle)
    audio = next(row for row in track_rows(bundle) if row["id"] == "audio")
    neighbor = patch(change(
        ["config", "tracks", "id:audio", "opaque"],
        present(audio["opaque"]),
        {"changed": [None, False, 1]},
    ))
    submit(realm, neighbor, reads=[])
    accepted = submit(realm, move_overlay(bundle), base=base, reads=[track_read(order)])

    assert accepted["data"]["status"] == "rebased"
    payload = accepted["data"]["canonical_document"]["parent_revision"]["payload"]
    assert_track_rows_and_neighbors(
        payload,
        ["overlay", "picture", "audio"],
        audio_opaque={"changed": [None, False, 1]},
    )
    assert next(row for row in payload["config"]["tracks"] if row["id"] == "audio")["opaque"] == {
        "changed": [None, False, 1]
    }
