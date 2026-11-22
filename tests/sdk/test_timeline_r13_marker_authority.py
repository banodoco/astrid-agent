"""Legacy placement markers stay attached to their exact source scope."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline.commit import bundle_from_document
from astrid.sdk.timeline.validation import FinalCandidateError, validate_final_candidate
from tests.sdk.test_timeline_commit_provider_document import document_fixture


@pytest.mark.parametrize("field", [
    "project_id", "timeline_id", "revision_id", "internal_timeline_revision_id",
    "internal_timeline_content_digest",
])
def test_child_marker_cannot_borrow_the_legacy_exemption_from_another_source(field):
    baseline = bundle_from_document(document_fixture(shared=False))
    baseline["shots"]["chapter-shot"]["internal_timeline"]["tracks"][0]["app"] = {
        "clipPlacementVersion": 2,
    }
    # Check the child even while it is unplaced.
    baseline["placements"] = []
    candidate = copy.deepcopy(baseline)
    validate_final_candidate(candidate, baseline=baseline)
    source = candidate if field in {"project_id", "timeline_id"} else candidate["source_mapping"]["shots"]["chapter-shot"]
    source[field] = "changed"
    with pytest.raises(FinalCandidateError, match=r"app\.clipPlacementVersion"):
        validate_final_candidate(candidate, baseline=baseline)


@pytest.mark.parametrize("field,value", [
    ("fit", "cover"), ("scale", 2), ("scaleAppliesToPositionedClips", True),
])
def test_legacy_marker_cannot_be_retained_when_placement_changes(field, value):
    baseline = bundle_from_document(document_fixture(shared=False))
    baseline["parent"]["config"]["tracks"][0]["app"] = {"clipPlacementVersion": 2}
    candidate = copy.deepcopy(baseline)
    track = candidate["parent"]["config"]["tracks"][0]
    location = track["app"] if field == "scaleAppliesToPositionedClips" else track
    location[field] = value
    with pytest.raises(FinalCandidateError, match=r"app\.clipPlacementVersion"):
        validate_final_candidate(candidate, baseline=baseline)
    del track["app"]["clipPlacementVersion"]
    with pytest.raises(FinalCandidateError, match=r"app\.clipPlacementVersion"):
        validate_final_candidate(candidate, baseline=baseline)
    track["app"]["clipPlacementVersion"] = 1
    validate_final_candidate(candidate, baseline=baseline)


@pytest.mark.parametrize("damage", ["alias", "occurrences", "shot-digest", "child-digest"])
def test_shared_aliases_cannot_inherit_the_marker_from_forged_authority(damage):
    from astrid.sdk.timeline.commit import _digest, _manifest
    from tests.sdk.test_shared_placement_fps import _document

    def marked_bundle(*, shared):
        document = _document(shared=shared)
        parent = document["parent_revision"]
        shot, = document["shot_revisions"]
        child, = document["internal_timeline_revisions"]
        parent["payload"]["config"]["output"].pop("fps")
        child["payload"]["output"].pop("fps")
        child["payload"]["tracks"][0]["app"] = {"clipPlacementVersion": 2}
        for image in (parent, child):
            image["content_digest"] = _digest(image["payload"])
        document["dependency_manifest"] = _manifest(
            [shot], [child], document["dependency_manifest"]["media"], [],
        )
        return bundle_from_document(document)

    baseline = marked_bundle(shared=False)
    candidate = marked_bundle(shared=True)
    if damage in {"shot-digest", "child-digest"}:
        field = "content_digest" if damage == "shot-digest" else "internal_timeline_content_digest"
        for source in candidate["source_mapping"]["shots"].values():
            source[field] = "sha256:" + "0" * 64
    elif damage == "alias":
        local_id = next(iter(candidate["shots"]))
        bogus_id = "forged-alias"
        shot = candidate["shots"].pop(local_id)
        shot["shot_id"] = bogus_id
        candidate["shots"][bogus_id] = shot
        candidate["source_mapping"]["shots"][bogus_id] = candidate["source_mapping"]["shots"].pop(local_id)
        for placement in candidate["placements"]:
            if placement["shot_id"] == local_id:
                placement["shot_id"] = bogus_id
    else:
        document = copy.deepcopy(candidate["source_mapping"]["_runtime_document"])
        parent = document["parent_revision"]
        for occurrence in parent["payload"]["occurrences"]:
            occurrence["occurrence_id"] = "replacement-" + occurrence["occurrence_id"]
        parent["content_digest"] = _digest(parent["payload"])
        candidate = bundle_from_document(document)
    with pytest.raises(FinalCandidateError, match=r"app\.clipPlacementVersion"):
        validate_final_candidate(candidate, baseline=baseline)
