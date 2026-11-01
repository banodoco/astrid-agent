"""D12 preserves historical text provenance through the production provider."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._command_state import present
from astrid.sdk.timeline.commit import _digest, _manifest, bundle_from_document
from astrid.sdk.timeline.validation import validate_final_candidate
from tests.editing_conformance.test_doc_patch_v2 import change, patch, submit
from tests.sdk.test_timeline_commit_provider import CommitRejected, DetachedRealm
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi
from tests.sdk.test_timeline_commit_provider_document import MEDIA, document_fixture

REFERENCE = {"kind": "text", "binding_id": "historical-binding",
             "anchor": {"document_id": "historical-transcript", "word_id": "word-1"}}


def legacy_document():
    document = document_fixture(shared=False)
    internal = document["internal_timeline_revisions"][0]
    internal["payload"]["clips"][0]["elementRef"] = copy.deepcopy(REFERENCE)
    shot = document["shot_revisions"][0]
    shot["payload"]["text_bindings"] = [{
        "binding_id": REFERENCE["binding_id"], "kind": "transcript", "slot": None,
        "media_id": MEDIA, "content_hash": MEDIA, "byte_size": 0, "mime_type": "text/plain",
    }]
    for image in (internal, shot):
        image["content_digest"] = _digest(image["payload"])
    document["dependency_manifest"] = _manifest(
        document["shot_revisions"], document["internal_timeline_revisions"],
        document["dependency_manifest"]["media"], [],
    )
    return document


def test_provider_preserves_unchanged_legacy_text_reference_and_exact_source_images():
    original = legacy_document()
    realm = DetachedRealm(original)
    result = submit(realm, patch(change(["name"], present("Renamed Film"), "Edited")), reads=[])
    assert result["data"]["status"] == "accepted"
    assert result["receipt"]
    canonical = result["data"]["canonical_document"]
    assert canonical["shot_revisions"] == original["shot_revisions"]
    assert canonical["internal_timeline_revisions"] == original["internal_timeline_revisions"]
    assert canonical["dependency_manifest"] == original["dependency_manifest"]


@pytest.mark.parametrize("damage", [
    "create", "modify", "binding-remove", "binding-rebind", "element", "unresolvable-element",
])
def test_provider_refuses_new_modified_or_target_invalidated_references_atomically(damage):
    original = legacy_document()
    realm = DetachedRealm(original)
    path = ["clips", "id:clip-a", "elementRef"]
    ref = "internal-revision:internal-1"
    before, value = present(REFERENCE), copy.deepcopy(REFERENCE)
    if damage == "create":
        path[1] = "id:clip-b"
        before = {"state": "absent"}
    elif damage == "modify":
        value["anchor"]["word_id"] = "missing-word"
    elif damage == "element":
        value = {"kind": "effect", "binding_id": "forged"}
    elif damage == "unresolvable-element":
        value = {"packId": "missing-pack", "kind": "effect", "id": "missing", "revision": "r1"}
    else:
        ref, path = "shot-revision:shot-1", ["text_bindings"]
        before = present(original["shot_revisions"][0]["payload"]["text_bindings"])
        value = [] if damage == "binding-remove" else copy.deepcopy(before["value"])
        if damage == "binding-rebind":
            value[0]["head"] = 2
    head = copy.deepcopy(realm.head)
    with pytest.raises(CommitRejected) as raised:
        submit(realm, patch(change(path, before, value, ref=ref)), reads=[])
    assert raised.value.failure["http_status"] == 422
    assert raised.value.failure["details"]["status"] == "invalid"
    assert realm.head == head
    assert not realm.accepted and not realm.prepared


@pytest.mark.parametrize("reference", [
    {}, {"kind": "unknown"}, {"kind": "text", "binding_id": "untrusted"},
    {**REFERENCE, "packId": "untrusted"},
    {**REFERENCE, "anchor": {"document_id": "historical-transcript", "word_id": False}},
])
def test_unknown_reference_shapes_are_never_grandfathered(reference):
    baseline = bundle_from_document(legacy_document())
    clip = baseline["shots"]["chapter-shot"]["internal_timeline"]["clips"][0]
    clip["elementRef"] = reference
    with pytest.raises(ValueError, match="elementRef"):
        validate_final_candidate(copy.deepcopy(baseline), baseline=baseline)


def test_legacy_text_reference_still_validates_native_clip_parameters():
    baseline = bundle_from_document(legacy_document())
    clip = baseline["shots"]["chapter-shot"]["internal_timeline"]["clips"][0]
    clip["clipType"] = "am-sprite"
    candidate = copy.deepcopy(baseline)
    candidate["shots"]["chapter-shot"]["internal_timeline"]["clips"][0]["params"] = {"scale": 0}
    with pytest.raises(ValueError, match="minimum"):
        validate_final_candidate(candidate, baseline=baseline)


def test_legacy_exemption_requires_the_original_source_identity():
    baseline = bundle_from_document(legacy_document())
    candidate = copy.deepcopy(baseline)
    candidate["source_mapping"]["shots"]["chapter-shot"]["internal_timeline_revision_id"] = "unrelated"
    with pytest.raises(ValueError, match="unchanged declaration and source"):
        validate_final_candidate(candidate, baseline=baseline)


def test_parent_text_reference_keeps_its_shot_binding_dependency():
    baseline = bundle_from_document(legacy_document())
    baseline["parent"]["clips"][0]["elementRef"] = copy.deepcopy(REFERENCE)
    baseline["parent"]["config"]["clips"] = copy.deepcopy(baseline["parent"]["clips"])
    candidate = copy.deepcopy(baseline)
    validate_final_candidate(candidate, baseline=baseline)
    candidate["shots"]["chapter-shot"]["payload"]["text_bindings"] = []
    with pytest.raises(ValueError, match="unchanged declaration and source"):
        validate_final_candidate(candidate, baseline=baseline)


def test_unchanged_unresolvable_historical_text_binding_is_preserved():
    baseline = bundle_from_document(legacy_document())
    baseline["shots"]["chapter-shot"]["payload"]["text_bindings"] = []
    candidate = copy.deepcopy(baseline)
    candidate["parent"]["name"] = "Unrelated edit"
    before = copy.deepcopy(candidate)
    validate_final_candidate(candidate, baseline=baseline)
    assert candidate == before


def test_historical_text_reference_cannot_hide_behind_a_second_alias():
    baseline = bundle_from_document(legacy_document())
    clip = baseline["shots"]["chapter-shot"]["internal_timeline"]["clips"][0]
    clip["element_ref"] = copy.deepcopy(REFERENCE)
    with pytest.raises(ValueError, match="ambiguous reference aliases"):
        validate_final_candidate(copy.deepcopy(baseline), baseline=baseline)


def test_historical_text_reference_keeps_its_retained_document_identity():
    baseline = bundle_from_document(legacy_document())
    baseline["source_mapping"]["pinned_documents"] = [{
        "pin": {"document_id": REFERENCE["anchor"]["document_id"], "version": 1, "content_digest": MEDIA},
    }]
    candidate = copy.deepcopy(baseline)
    candidate["source_mapping"]["pinned_documents"][0]["pin"]["version"] = 2
    with pytest.raises(ValueError, match="unchanged declaration and source"):
        validate_final_candidate(candidate, baseline=baseline)


@pytest.mark.parametrize("params", [{}, None])
def test_modified_selected_elements_require_retained_authority_even_without_parameters(params):
    baseline = bundle_from_document(document_fixture(shared=False))
    candidate = copy.deepcopy(baseline)
    clip = candidate["shots"]["chapter-shot"]["internal_timeline"]["clips"][0]
    clip.update(elementRef={"packId": "missing-pack", "kind": "effect", "id": "missing", "revision": "r1"},
                params=params)
    with pytest.raises(ValueError, match="retained schema"):
        validate_final_candidate(candidate, baseline=baseline)
