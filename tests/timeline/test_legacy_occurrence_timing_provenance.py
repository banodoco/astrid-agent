"""A legacy child overrun may only be retained by a non-worsening start shift."""
from __future__ import annotations

import copy

import pytest

from astrid.core.timeline.authoring_bundle import AuthoringBundleError
from astrid.sdk.timeline.commit import bundle_from_document
from astrid.sdk.timeline.validation import validate_final_candidate
from tests.editing_conformance.test_commit_boundaries_d12 import legacy_document


def _baseline():
    value = bundle_from_document(legacy_document("occurrence"))
    internal = value["shots"]["chapter-shot"]["internal_timeline"]
    internal["clips"] = [clip for clip in internal["clips"] if clip["id"] == "clip-a"]
    return value


def test_start_shift_with_grown_occurrence_bound_reduces_legacy_overrun():
    baseline = _baseline()
    candidate = copy.deepcopy(baseline)
    candidate["placements"][0]["duration_ms"] = 200
    candidate["shots"]["chapter-shot"]["internal_timeline"]["clips"][0]["at"] = 0.9

    validate_final_candidate(candidate, baseline=baseline)


def test_unchanged_clip_retains_overrun_when_occurrence_bound_grows():
    baseline = _baseline()
    candidate = copy.deepcopy(baseline)
    candidate["placements"][0]["duration_ms"] = 200

    validate_final_candidate(candidate, baseline=baseline)


@pytest.mark.parametrize("change", ["label", "asset"])
def test_grown_bound_does_not_exempt_changed_clip_provenance(change):
    baseline = _baseline()
    candidate = copy.deepcopy(baseline)
    candidate["placements"][0]["duration_ms"] = 200
    internal = candidate["shots"]["chapter-shot"]["internal_timeline"]
    if change == "label":
        internal["clips"][0]["label"] = "changed"
    else:
        internal["registry"]["assets"]["picture"]["opaque"] = {"changed": True}

    with pytest.raises(AuthoringBundleError, match="half-open bounds"):
        validate_final_candidate(candidate, baseline=baseline)


def test_start_shift_with_shrunk_occurrence_bound_retains_same_legacy_overrun():
    baseline = _baseline()
    candidate = copy.deepcopy(baseline)
    candidate["placements"][0]["duration_ms"] = 50
    candidate["shots"]["chapter-shot"]["internal_timeline"]["clips"][0]["at"] = 0.95

    validate_final_candidate(candidate, baseline=baseline)


def test_start_shift_with_changed_source_window_is_rejected():
    baseline = _baseline()
    candidate = copy.deepcopy(baseline)
    candidate["placements"][0]["duration_ms"] = 50
    clip = candidate["shots"]["chapter-shot"]["internal_timeline"]["clips"][0]
    clip["at"] = 0.95
    clip["from"] = 0.1

    with pytest.raises(AuthoringBundleError, match="half-open bounds"):
        validate_final_candidate(candidate, baseline=baseline)
