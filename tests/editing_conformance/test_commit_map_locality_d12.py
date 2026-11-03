"""Unsupported legacy maps do not veto independent timing components."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline.commit import _digest, _manifest
from tests.editing_conformance.test_commit_boundaries_d12 import encoded, legacy_document
from tests.editing_conformance.test_doc_patch_v2 import change, patch
from tests.sdk import test_timeline_commit_provider as provider
from tests.sdk.test_p1_doc_patch_provider import _service_call

runtime_abi = provider.runtime_abi
REF = "internal-revision:internal-1"


def submit(realm, changes, *, dry_run=False, created=None):
    return _service_call(realm, [patch(*changes, created=created)], controls={
        "label": "Independent timing edit", "merge": "fields", "reads": [], "dry_run": dry_run,
        "source": {"via": "python"}, "rationale": "D12a locality", "group_id": "map-locality",
        "steps": [{"id": "timing", "label": "Independent timing edit", "intent": "Edit literal",
                   "tags": ["timing"], "command_ordinals": [0], "patch": changes, "reads": []}],
    })


@pytest.mark.parametrize("operation", ["move", "create"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_production_independent_literal_edit_retains_same_image_map_and_targets(operation, dry_run):
    original = legacy_document("map")
    original_bytes = encoded(original)
    realm = provider.DetachedRealm(original)
    expected = copy.deepcopy(original["internal_timeline_revisions"][0]["payload"])
    if operation == "move":
        changes = [change(["clips", "id:clip-b", "at"], {"state": "present", "value": 5}, 6, ref=REF)]
        expected["clips"][1]["at"] = 6
    else:
        literal = {"id": "independent", "track": "picture", "clipType": "text", "at": 7,
                   "hold": 1, "params": {"text": "Independent"}, "speed": None}
        changes = [change(["clips"], {"state": "absent"}, literal, op="list.insert", ref=REF,
                          id="independent", after_id="clip-b", order_before=["clip-a", "clip-b"])]
        expected["clips"].append(literal)
    result = submit(realm, changes, dry_run=dry_run,
                    created=["occurrence:occ-a/clip:independent"] if operation == "create" else None)
    data = result["data"]
    assert data["status"] == ("preview" if dry_run else "accepted")
    if dry_run:
        assert data["would_status"] == "accepted"
        assert result["receipt"] is None
    else:
        assert result["receipt"]["result"] == data
        assert realm.prepared[-1]["records"]["steps"][0]["patch"]
    document = data["canonical_document"]
    assert encoded(document["internal_timeline_revisions"][0]["payload"]) == encoded(expected)
    expected_parent = copy.deepcopy(original["parent_revision"]["payload"])
    expected_parent["occurrences"][0]["shot_revision_id"] = document["shot_revisions"][0]["revision_id"]
    assert encoded(document["parent_revision"]["payload"]) == encoded(expected_parent)
    assert encoded(original) == original_bytes
    assert len(realm.accepted) == (0 if dry_run else 1)
    # Both old rows retain their exact physical image; only the intended payload
    # change and the authenticated successor identities/digests may differ.
    assert len(document["internal_timeline_revisions"]) == 1
    assert document["shot_revisions"][0]["shot_id"] == original["shot_revisions"][0]["shot_id"]


@pytest.mark.parametrize("damage", ["map", "source", "new-map", "new-invalid", "invalidate"])
def test_production_map_or_dependency_damage_refuses_without_writes(damage):
    original = legacy_document("map")
    internal = original["internal_timeline_revisions"][0]
    if damage == "invalidate":
        # A valid cut declaration in another component must still be compiled;
        # deleting its target cannot hide behind the unrelated unsupported map.
        internal["payload"]["clips"][1].setdefault("app", {})["astrid"] = {"cut": "c1"}
        parent = original["parent_revision"]
        for clips in (parent["payload"]["clips"], parent["payload"]["config"]["clips"]):
            clips[0].setdefault("app", {})["astrid"] = {"on": "c1", "for": 1}
        for image in (parent, internal):
            image["content_digest"] = _digest(image["payload"])
        original["dependency_manifest"] = _manifest(
            original["shot_revisions"], original["internal_timeline_revisions"],
            original["dependency_manifest"]["media"], [],
        )
    realm = provider.DetachedRealm(original)
    head = copy.deepcopy(realm.head)
    if damage == "map":
        changes = [change(["clips", "id:clip-a", "source_window", "to"],
                          {"state": "present", "value": 30}, 31, ref=REF)]
    elif damage == "source":
        changes = [change(["registry", "assets", "picture", "duration"],
                          {"state": "absent"}, 10, ref=REF)]
    elif damage == "new-map":
        changes = [change(["clips", "id:clip-b", "time_map"], {"state": "absent"}, {}, ref=REF)]
    elif damage == "new-invalid":
        changes = [change(["clips", "id:clip-b", "app", "astrid"],
                          {"state": "absent"}, {"on": "c999"}, ref=REF)]
    else:
        changes = [change(["clips", "id:clip-b", "app", "astrid", "cut"],
                          {"state": "present", "value": "c1"}, op="remove", ref=REF)]
    with pytest.raises(provider.CommitRejected) as error:
        submit(realm, changes)
    assert error.value.failure["http_status"] == 422
    assert error.value.failure["details"]["code"] == "invalid_document"
    assert realm.head == head
    assert realm.accepted == []
    assert realm.prepared == []
    assert realm.documents[head["revision_id"]] == original


@pytest.mark.parametrize("operation", ["independent", "target-move", "target-remove"])
def test_production_mapped_declaration_keeps_its_actual_cut_dependency(operation):
    original = legacy_document("map")
    internal = original["internal_timeline_revisions"][0]
    clips = internal["payload"]["clips"]
    clips[0].setdefault("app", {})["astrid"] = {"on": "c1"}
    clips[1].setdefault("app", {})["astrid"] = {"cut": "c1"}
    clips.append({"id": "independent", "track": "picture", "clipType": "text",
                  "at": 8, "hold": 1, "params": {"text": "Independent"}})
    internal["content_digest"] = _digest(internal["payload"])
    original["dependency_manifest"] = _manifest(
        original["shot_revisions"], original["internal_timeline_revisions"],
        original["dependency_manifest"]["media"], [],
    )
    realm = provider.DetachedRealm(original)
    if operation == "independent":
        result = submit(realm, [change(["clips", "id:independent", "at"],
                                      {"state": "present", "value": 8}, 8.5, ref=REF)])
        assert result["data"]["status"] == "accepted"
        actual = result["data"]["canonical_document"]["internal_timeline_revisions"][0]["payload"]
        expected = copy.deepcopy(internal["payload"])
        expected["clips"][2]["at"] = 8.5
        assert encoded(actual) == encoded(expected)
        return
    if operation == "target-move":
        changes = [change(["clips", "id:clip-b", "at"], {"state": "present", "value": 5}, 6, ref=REF)]
    else:
        changes = [change(["clips"], {"state": "present", "value": clips[1]}, op="list.remove", ref=REF,
                          id="clip-b", order_before=["clip-a", "clip-b", "independent"])]
    with pytest.raises(provider.CommitRejected) as error:
        submit(realm, changes)
    assert error.value.failure["http_status"] == 422
    assert "unsupported explicit map" in error.value.failure["message"]
    assert realm.accepted == []
    assert realm.prepared == []
    assert realm.head == {"revision_id": "parent-1", "publication_id": "bootstrap"}
