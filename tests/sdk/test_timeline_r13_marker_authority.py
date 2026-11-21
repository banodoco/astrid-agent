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
