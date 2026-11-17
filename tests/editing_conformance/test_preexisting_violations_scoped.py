"""A long-lived timeline that already fails a whole-candidate rule stays editable and duplicable.

Each rule judges only what a change introduces or touches. What the baseline already had, on entities
the change leaves alone, is reported as a warning and does not block; a new or touched violation still does.
Fixtures are small and inline.
"""
from __future__ import annotations

import copy

import pytest

from astrid.core.timeline.authoring_bundle import AuthoringBundleError
from astrid.sdk.timeline._preexisting import PREEXISTING, split_findings
from astrid.sdk.timeline.validation import validate_final_candidate
from astrid.sdk.timeline_checkout import Checkout
from tests.editing_conformance.test_commit_v2_validation import candidate

SHOT = "chapter-shot"
FORMULA = {"params.at": {"as": "clip_frame", "moment": "word:line-1:first"}}


def child_clips(bundle):
    return bundle["shots"][SHOT]["internal_timeline"]["clips"]


def unrelated_change(bundle):
    """An edit that touches none of the entities the violations below sit on."""
    bundle["parent"]["config"]["tracks"].append({"id": "extra", "kind": "visual"})


def with_formulas(bundle):
    clip = child_clips(bundle)[0]
    clip.setdefault("app", {}).setdefault("astrid", {})["formulas"] = copy.deepcopy(FORMULA)
    clip["params"] = {"values": [{"at": 3}]}
    return bundle


def with_unknown_asset(bundle):
    child_clips(bundle)[0]["asset"] = "missing-asset"
    base = bundle["shots"][SHOT].get("base_internal_timeline")
    if base is not None:  # the head as stored already names it, so it is a legacy declaration
        base["clips"][0]["asset"] = "missing-asset"
    return bundle


def warnings_of(result):
    assert result and all(line.startswith(PREEXISTING) for line in result)
    return " ".join(result)


def test_split_findings_keeps_only_unchanged_findings_on_the_baseline():
    found = {"a": 1, "b": 2, "c": 3}
    assert split_findings(found, {"a": 1, "b": 9}) == (["b", "c"], ["a"])
    assert split_findings(found, None) == (["a", "b", "c"], [])


# --- rule: unsupported timing declaration (_timing_evaluators) ------------------------------------------------

@pytest.mark.parametrize("declaration", ["formulas", "sequence", "sequence_fit", "orphan", "gap_after_s"])
def test_existing_unsupported_declaration_does_not_block_an_unrelated_change(declaration):
    baseline = with_formulas(candidate())
    clip = child_clips(baseline)[0]
    if declaration != "formulas":
        clip["app"]["astrid"].pop("formulas")
        clip["app"]["astrid"][declaration] = {"name": "a"} if declaration == "sequence_fit" else "a" if declaration == "sequence" else True
        if declaration == "gap_after_s":
            clip["app"]["astrid"][declaration] = 1.5
    value = copy.deepcopy(baseline)
    unrelated_change(value)
    result = validate_final_candidate(value, baseline=baseline)
    assert declaration in warnings_of(result)
    assert value["shots"][SHOT] == baseline["shots"][SHOT]  # authored data is untouched


def test_formulas_cached_result_may_be_recalculated_without_a_new_refusal():
    baseline = with_formulas(candidate())
    value = copy.deepcopy(baseline)
    child_clips(value)[0]["params"] = {"values": [{"at": 4}]}
    assert "formulas" in warnings_of(validate_final_candidate(value, baseline=baseline))


def test_new_unsupported_declaration_is_refused():
    baseline = candidate()
    value = with_formulas(copy.deepcopy(baseline))
    with pytest.raises(AuthoringBundleError, match="unsupported timing declaration"):
        validate_final_candidate(value, baseline=baseline)
    with pytest.raises(AuthoringBundleError, match="unsupported timing declaration"):
        validate_final_candidate(with_formulas(candidate()))  # no baseline: nothing was pre-existing


@pytest.mark.parametrize("touch", ["declaration", "timing", "track"])
def test_edited_or_touched_unsupported_declaration_is_refused(touch):
    baseline = with_formulas(candidate())
    value = copy.deepcopy(baseline)
    clip = child_clips(value)[0]
    if touch == "declaration":
        clip["app"]["astrid"]["formulas"]["params.at"]["moment"] = "word:line-2:first"
    elif touch == "timing":
        clip["at"] = 2
    else:
        value["shots"][SHOT]["internal_timeline"]["tracks"].append({"id": "other", "kind": "visual"})
        clip["track"] = "other"
    with pytest.raises(AuthoringBundleError, match="unsupported timing declaration"):
        validate_final_candidate(value, baseline=baseline)


def test_other_timing_errors_still_refuse_beside_a_retained_declaration():
    baseline = with_formulas(candidate())
    value = copy.deepcopy(baseline)
    unrelated_change(value)
    child_clips(value)[1].setdefault("app", {}).setdefault("astrid", {})["on"] = "c999"
    with pytest.raises(AuthoringBundleError, match="candidate.timing"):
        validate_final_candidate(value, baseline=baseline)


# --- rule: authored fps declarations keep their typed locations (validation) ----------------------------------

def test_existing_mismatched_fps_does_not_block_an_unrelated_change_but_a_new_mismatch_does():
    baseline = candidate()
    baseline["shots"][SHOT]["internal_timeline"]["output"] = {"fps": 24}  # the parent says 30000/1001
    value = copy.deepcopy(baseline)
    unrelated_change(value)
    validate_final_candidate(value, baseline=baseline)
    value["parent"]["config"]["output"]["fps"] = 25
    with pytest.raises(AuthoringBundleError, match="same rational rate"):
        validate_final_candidate(value, baseline=baseline)


def test_dropping_a_typed_fps_location_is_refused_but_a_source_copy_keeps_the_source_as_it_is():
    destination = candidate()  # the empty destination's config names an fps the source never stored
    copied = copy.deepcopy(destination)
    copied["parent"]["config"].pop("output")
    with pytest.raises(AuthoringBundleError, match="retain their typed locations"):
        validate_final_candidate(copied, baseline=destination)
    validate_final_candidate(copied, baseline=destination, authenticated_copy=True)


# --- rule: unknown asset (validation) --------------------------------------------------------------------------

def test_existing_unknown_asset_does_not_block_an_unrelated_change():
    baseline = with_unknown_asset(candidate())
    value = copy.deepcopy(baseline)
    unrelated_change(value)
    assert "missing-asset" in warnings_of(validate_final_candidate(value, baseline=baseline))


@pytest.mark.parametrize("change", ["new-clip", "other-missing-asset"])
def test_introduced_unknown_asset_is_refused(change):
    baseline = with_unknown_asset(candidate())
    value = copy.deepcopy(baseline)
    if change == "new-clip":
        clip = copy.deepcopy(child_clips(value)[1])
        clip.update(id="added", asset="also-missing", at=7)
        child_clips(value).append(clip)
    else:
        child_clips(value)[0]["asset"] = "different-missing-asset"
    with pytest.raises(AuthoringBundleError, match="asset"):
        validate_final_candidate(value, baseline=baseline)


def test_source_copy_of_a_clip_with_an_unknown_asset_is_accepted_against_an_empty_destination():
    source = with_unknown_asset(candidate())
    empty = candidate()
    for shot in empty["shots"].values():
        shot["internal_timeline"]["clips"] = []
    empty["placements"], empty["shots"] = [], {}
    with pytest.raises(AuthoringBundleError, match="unknown asset"):
        validate_final_candidate(source, baseline=empty)
    validate_final_candidate(source, baseline=empty, authenticated_copy=True)


def test_source_copy_still_needs_its_placements_to_name_shots():
    value = candidate()
    value["placements"][0]["shot_id"] = "nowhere"
    with pytest.raises(AuthoringBundleError, match="missing shot"):
        validate_final_candidate(value, authenticated_copy=True)


# --- rule: clip longer than its shot (check) -------------------------------------------------------------------

def canonical_media(bundle):
    for shot in bundle["shots"].values():
        for index, entry in enumerate(shot["internal_timeline"]["registry"]["assets"].values()):
            entry["media_id"] = "sha256:" + f"{index + 1:x}" * 64
    return bundle


def overrunning_head():
    from tests.sdk.test_timeline_restructure import flat_film, published

    head = canonical_media(flat_film())
    head["shots"]["film"]["internal_timeline"]["clips"].append(
        {"id": "over", "clipType": "am-sprite", "track": "sprite", "asset": "R", "at": 11.5, "hold": 2.0, "params": {}})
    return published(head)


def check(bundle):
    return Checkout(copy.deepcopy(bundle))._check_core()


def length_blocking(report):
    return [line for line in report.blocking if line.startswith("length")]


def test_existing_overrun_does_not_block_an_unrelated_edit_and_is_reported():
    head = overrunning_head()
    assert length_blocking(check(head)) == []
    edited = copy.deepcopy(head)
    edited["shots"]["film"]["internal_timeline"]["clips"][0]["params"] = {"note": 1}
    report = check(edited)
    assert length_blocking(report) == []
    assert any(line.startswith(PREEXISTING + "length") and "over ends at 13.50" in line for line in report.problems)
    assert "length" not in " ".join(report.blocking)


@pytest.mark.parametrize("touch", ["longer", "new-overrun"])
def test_introduced_or_touched_overrun_still_blocks(touch):
    head = overrunning_head()
    edited = copy.deepcopy(head)
    clips = edited["shots"]["film"]["internal_timeline"]["clips"]
    if touch == "longer":
        next(clip for clip in clips if clip["id"] == "over")["hold"] = 3.0
    else:
        clips.append({"id": "second", "clipType": "am-sprite", "track": "sprite", "asset": "R",
                      "at": 11.9, "hold": 2.0, "params": {}})
    assert length_blocking(check(edited)), touch
