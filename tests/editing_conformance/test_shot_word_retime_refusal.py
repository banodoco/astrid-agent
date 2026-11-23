"""Commit validation of retimes for legacy word-anchored shot clips."""
from __future__ import annotations

import copy
import re

import pytest

from astrid.core.timeline.authoring_bundle import AuthoringBundleError
from astrid.sdk.timeline.validation import validate_final_candidate
from tests.editing_conformance.test_native_word_anchor_vectors import fixture


GUIDANCE = "Use a cut-relative timing declaration such as +0.8s."


def _with_legacy_anchor(word="stop"):
    bundle, _, _ = fixture()
    internal = bundle["shots"]["chapter-shot"]["internal_timeline"]
    voice = internal["clips"][0]
    voice["app"] = {"astrid": {"cut": "c1"}}
    internal["tracks"].append({"id": "overlay", "kind": "visual"})
    internal["registry"]["assets"]["still"] = {
        **internal["registry"]["assets"]["picture"],
        "type": "image",
    }
    internal["clips"].append({
        "id": "card", "track": "overlay", "asset": "still", "clipType": "media",
        "at": 2.75 if word == "stop" else 1.5, "hold": 0.5,
        "app": {"astrid": {"cut": "c1", "on": f'"{word}"', "for": 0.5}},
    })
    return bundle


def _shot_clips(bundle):
    return bundle["shots"]["chapter-shot"]["internal_timeline"]["clips"]


@pytest.mark.parametrize(("word", "move_narration"), [
    pytest.param("stop", False, id="unique-clip-only"),
    pytest.param("stop", True, id="unique-ripple"),
    pytest.param("again", True, id="ambiguous-ripple"),
])
def test_retiming_a_legacy_word_anchored_shot_clip_is_refused(word, move_narration):
    baseline = _with_legacy_anchor(word)
    candidate = copy.deepcopy(baseline)
    voice, card = _shot_clips(candidate)
    card["at"] += 0.5
    if move_narration:
        voice["at"] += 0.5

    before = copy.deepcopy(candidate)
    with pytest.raises(AuthoringBundleError, match=re.escape(GUIDANCE)):
        validate_final_candidate(candidate, baseline=baseline)
    assert candidate == before


def test_cut_relative_ripple_passes_commit_validation():
    baseline = _with_legacy_anchor()
    candidate = copy.deepcopy(baseline)
    voice, card = _shot_clips(candidate)
    voice["at"] += 0.5
    card["at"] += 0.5
    card["app"]["astrid"]["on"] = "+1.75s"

    validate_final_candidate(candidate, baseline=baseline)


def test_changing_declared_duration_alone_is_refused():
    baseline = _with_legacy_anchor()
    candidate = copy.deepcopy(baseline)
    _shot_clips(candidate)[1]["app"]["astrid"]["for"] = 0.75

    with pytest.raises(AuthoringBundleError, match=re.escape(GUIDANCE)):
        validate_final_candidate(candidate, baseline=baseline)


def test_explicit_shot_word_choice_has_the_same_cut_relative_alternative():
    baseline = _with_legacy_anchor()
    candidate = copy.deepcopy(baseline)
    capture, = baseline["source_mapping"]["pinned_documents"]
    stop = capture["document"]["content"]["segments"][0]["words"][-1]
    _shot_clips(candidate)[1]["app"]["astrid"]["word_anchors"] = {"on": {
        "document": capture["pin"], "source": "occurrence:occ-a/clip:voice", "word_ids": [stop["id"]],
    }}

    with pytest.raises(AuthoringBundleError, match=re.escape(GUIDANCE)):
        validate_final_candidate(candidate, baseline=baseline)


def test_unrelated_parent_edit_preserves_legacy_shot_anchor():
    baseline = _with_legacy_anchor()
    candidate = copy.deepcopy(baseline)
    candidate["parent"]["config"]["tracks"].append({"id": "new", "kind": "audio"})

    validate_final_candidate(candidate, baseline=baseline)
    assert _shot_clips(candidate) == _shot_clips(baseline)
