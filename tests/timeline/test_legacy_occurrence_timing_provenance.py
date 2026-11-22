"""Unchanged occurrence-owned word declarations survive authored timing edits."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._command_state import State
from astrid.sdk.timeline.commit import bundle_from_document
from astrid.sdk.timeline._commit_timing_validation import (
    _legacy_exemptions,
    validate_candidate_timing,
)
from tests.editing_conformance.test_recipe_occurrence_media import inputs


OWNER = "occurrence:occ-a"
CLIP = OWNER + "/clip:clip-a"


def _legacy_word_bundle():
    document, *_ = inputs("image")
    bundle = bundle_from_document(document)
    row = State(bundle).entity(CLIP)
    row["app"] = {"astrid": {"on": '"unbound legacy word"'}}
    row["hold"] = 1.25
    return bundle


@pytest.mark.parametrize("field", ["at", "hold"])
def test_unchanged_occurrence_word_provenance_survives_timing_only_edit(field):
    baseline = _legacy_word_bundle()
    candidate = copy.deepcopy(baseline)
    row = State(candidate).entity(CLIP)
    row[field] += 0.1 if field == "at" else -0.1

    shot_id = State(baseline).occurrence(OWNER)["shot_id"]
    assert (("shot", shot_id, "clip-a"), "on") in _legacy_exemptions(candidate, baseline)
    assert State(candidate).entity(CLIP)["app"] == State(baseline).entity(CLIP)["app"]
    assert candidate["source_mapping"] == baseline["source_mapping"]
    validate_candidate_timing(candidate, baseline=baseline)


@pytest.mark.parametrize("damage", ["declaration", "word_anchor", "source_provenance", "media_window", "asset_rebind"])
def test_timing_edit_does_not_exempt_changed_word_provenance(damage):
    baseline = _legacy_word_bundle()
    candidate = copy.deepcopy(baseline)
    row = State(candidate).entity(CLIP)
    row["at"] += 0.1
    if damage == "declaration":
        row["app"]["astrid"]["on"] = '"different legacy word"'
    elif damage == "word_anchor":
        row["app"]["astrid"]["word_anchors"] = {"on": {"source": "invented"}}
    elif damage == "source_provenance":
        shot_id = State(candidate).occurrence(OWNER)["shot_id"]
        candidate["source_mapping"]["shots"][shot_id]["internal_timeline_id"] = "different-timeline"
    elif damage == "asset_rebind":
        State(candidate).registry(OWNER)["assets"]["picture"]["media_id"] = "sha256:" + "f" * 64
    else:
        row.update({"from": 1, "to": 2, "speed": 2})

    shot_id = State(baseline).occurrence(OWNER)["shot_id"]
    assert (("shot", shot_id, "clip-a"), "on") not in _legacy_exemptions(candidate, baseline)
    from astrid.sdk.timeline.compiler import TimingCompileError

    with pytest.raises(TimingCompileError):
        validate_candidate_timing(candidate, baseline=baseline)


def test_new_occurrence_word_declaration_is_not_inherited_as_preexisting():
    baseline = _legacy_word_bundle()
    base_row = State(baseline).entity(CLIP)
    base_row["app"]["astrid"].pop("on")
    candidate = copy.deepcopy(baseline)
    State(candidate).entity(CLIP)["app"]["astrid"]["on"] = '"new unbound word"'

    shot_id = State(baseline).occurrence(OWNER)["shot_id"]
    assert (("shot", shot_id, "clip-a"), "on") not in _legacy_exemptions(candidate, baseline)
    from astrid.sdk.timeline.compiler import TimingCompileError

    with pytest.raises(TimingCompileError):
        validate_candidate_timing(candidate, baseline=baseline)
