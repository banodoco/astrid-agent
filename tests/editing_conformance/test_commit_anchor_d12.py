"""D12 retains unresolved legacy words only with unchanged declarations and sources."""
from __future__ import annotations

import copy
import json

import pytest

from astrid.core.timeline.authoring_bundle import AuthoringBundleError
from astrid.sdk.timeline import _commit_timing_validation as timing
from astrid.sdk.timeline.commit import _digest, _manifest, bundle_from_document
from astrid.sdk.timeline.validation import validate_final_candidate
from tests.sdk import test_timeline_commit_provider as provider
from tests.sdk.test_p1_doc_patch_provider import _service_call
from tests.sdk.test_timeline_commit_provider_document import document_fixture

runtime_abi = provider.runtime_abi
DECLARATION = '"missing legacy phrase"'


def encoded(value):
    return json.dumps(value, sort_keys=True, allow_nan=False)


def legacy_document():
    document = document_fixture(shared=False)
    parent = document["parent_revision"]
    parent["payload"]["clips"][0]["app"]["astrid"] = {"on": DECLARATION}
    parent["payload"]["config"]["clips"] = copy.deepcopy(parent["payload"]["clips"])
    parent["content_digest"] = _digest(parent["payload"])
    return document


def test_untouched_legacy_word_reaches_compiler_without_binding_or_authored_changes(monkeypatch):
    baseline = bundle_from_document(legacy_document())
    value = copy.deepcopy(baseline)
    value["parent"]["config"]["tracks"].append({"id": "new", "kind": "audio"})
    before = encoded(value)
    captured = []
    compile_timing = timing.compile_timing

    def capture(inputs):
        captured.append(inputs)
        return compile_timing(inputs)

    monkeypatch.setattr(timing, "compile_timing", capture)
    validate_final_candidate(value, baseline=baseline)
    assert captured[-1].legacy_declarations == ((("parent", "", "clip-a"), "on", DECLARATION),)
    assert captured[-1].bindings == ()
    assert captured[-1].bundle["parent"]["clips"][0]["app"]["astrid"]["on"] == DECLARATION
    assert encoded(value) == before


@pytest.mark.parametrize("damage", ["create", "modify", "target-invalidate"])
def test_new_modified_or_invalidated_legacy_word_is_refused(damage):
    baseline = bundle_from_document(legacy_document())
    value = copy.deepcopy(baseline)
    if damage == "create":
        baseline["parent"]["clips"][0]["app"].pop("astrid")
        baseline["parent"]["config"]["clips"] = copy.deepcopy(baseline["parent"]["clips"])
    elif damage == "modify":
        value["parent"]["clips"][0]["app"]["astrid"]["on"] = '"another missing phrase"'
        value["parent"]["config"]["clips"] = copy.deepcopy(value["parent"]["clips"])
    else:
        value["shots"]["chapter-shot"]["internal_timeline"]["clips"][0]["at"] = 2
    before = encoded(value)
    with pytest.raises(AuthoringBundleError, match="legacy word anchor is missing"):
        validate_final_candidate(value, baseline=baseline)
    assert encoded(value) == before


@pytest.mark.parametrize("owner", ["parent", "shot", "internal", "occurrence"])
def test_equal_bytes_with_replacement_physical_owner_are_not_exempt(owner):
    baseline = bundle_from_document(legacy_document())
    document = legacy_document()
    parent = document["parent_revision"]
    shot = document["shot_revisions"][0]
    internal = document["internal_timeline_revisions"][0]
    if owner == "parent":
        parent["revision_id"] = "replacement-parent"
    elif owner == "shot":
        shot["revision_id"] = "replacement-shot"
        parent["payload"]["occurrences"][0]["shot_revision_id"] = "replacement-shot"
    elif owner == "internal":
        internal["revision_id"] = "replacement-internal"
        shot["internal_timeline_revision_id"] = "replacement-internal"
        shot["payload"]["internal_timeline_revision_id"] = "replacement-internal"
    else:
        parent["payload"]["occurrences"][0]["occurrence_id"] = "replacement-occurrence"
    for image in (parent, shot, internal):
        image["content_digest"] = _digest(image["payload"])
    document["dependency_manifest"] = _manifest(
        document["shot_revisions"], document["internal_timeline_revisions"],
        document["dependency_manifest"]["media"], [],
    )
    value = bundle_from_document(document)
    with pytest.raises(AuthoringBundleError, match="legacy word anchor is missing"):
        validate_final_candidate(value, baseline=baseline)


def test_previously_resolvable_word_cannot_be_invalidated_by_source_trim():
    from tests.editing_conformance.test_native_word_anchor_vectors import fixture

    baseline, _, _ = fixture()
    baseline["parent"]["clips"][0]["app"]["astrid"]["on"] = '"stop"'
    baseline["parent"]["config"]["clips"] = copy.deepcopy(baseline["parent"]["clips"])
    validate_final_candidate(baseline)
    value = copy.deepcopy(baseline)
    value["shots"]["chapter-shot"]["internal_timeline"]["clips"][0]["to"] = 5
    with pytest.raises(AuthoringBundleError, match="legacy word anchor is missing"):
        validate_final_candidate(value, baseline=baseline)


@pytest.mark.parametrize("dry_run", [False, True])
def test_production_commit_preserves_legacy_word_and_full_v2_controls(dry_run):
    realm = provider.DetachedRealm(legacy_document())
    original = copy.deepcopy(realm.documents[realm.head["revision_id"]])
    result = _service_call(realm, [provider.track_create()], controls={
        "label": "Add track", "merge": "fields", "reads": [], "dry_run": dry_run,
        "source": {"via": "python"}, "rationale": "Unrelated edit", "group_id": "anchor-d12",
        "steps": [{"id": "track", "label": "Add track", "intent": "Add audio",
                   "tags": ["test"], "command_ordinals": [0], "patch": [], "reads": []}],
    })
    data = result["data"]
    assert data["status"] == ("preview" if dry_run else "accepted")
    if dry_run:
        assert data["would_status"] == "accepted"
        assert result["receipt"] is None
    else:
        assert result["receipt"]["result"] == data
    document = data["canonical_document"]
    assert encoded(document["parent_revision"]["payload"]["clips"]) == encoded(
        original["parent_revision"]["payload"]["clips"])
    assert document["internal_timeline_revisions"] == original["internal_timeline_revisions"]
    records = data if dry_run else realm.prepared[-1]["records"]
    assert records["label"] == "Add track"
    assert records["steps"][0]["id"] == "track"
    assert records["steps"][0]["patch"]
