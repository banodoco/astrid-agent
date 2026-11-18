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
PARAM_FORMULA = {"as": "clip_frame", "moment": "word:line-1:first"}
FORMULA = {"at": {"as": "clip_frame", "moment": "word:line-1:first"}}  # a path outside params: a timing input


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


def test_split_findings_keeps_only_unchanged_findings_on_the_baseline():
    found = {"a": 1, "b": 2, "c": 3}
    assert split_findings(found, {"a": 1, "b": 9}) == (["b", "c"], ["a"])
    assert split_findings(found, None) == (["a", "b", "c"], [])


# --- rule: unsupported timing declaration (_timing_evaluators) ------------------------------------------------

def test_params_formulas_written_by_the_sdk_are_not_timing_declarations():
    """``Clip.set`` records a formula through ``set_formula`` on a ``params.*`` path: it derives a cached param."""
    from astrid.sdk import timeline_intent as intent

    value = candidate()
    clip = child_clips(value)[0]
    clip["params"] = {"values": [{"at": 3}]}
    intent.set_formula(clip, "params.values[0].at", PARAM_FORMULA)
    assert clip["app"]["astrid"]["formulas"]
    validate_final_candidate(value)  # even a new one, with no baseline
    validate_final_candidate(value, baseline=candidate())


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
    with pytest.raises(AuthoringBundleError, match="unsupported timing declaration"):
        validate_final_candidate(value)  # new: refused
    validate_final_candidate(value, baseline=baseline)
    assert value["shots"][SHOT] == baseline["shots"][SHOT]  # authored data is untouched


def test_formulas_cached_result_may_be_recalculated_without_a_new_refusal():
    baseline = with_formulas(candidate())
    value = copy.deepcopy(baseline)
    child_clips(value)[0]["params"] = {"values": [{"at": 4}]}
    validate_final_candidate(value, baseline=baseline)


def test_new_unsupported_declaration_is_refused():
    baseline = candidate()
    value = with_formulas(copy.deepcopy(baseline))
    with pytest.raises(AuthoringBundleError, match="unsupported timing declaration"):
        validate_final_candidate(value, baseline=baseline)
    with pytest.raises(AuthoringBundleError, match="unsupported timing declaration"):
        validate_final_candidate(with_formulas(candidate()))  # no baseline: nothing was pre-existing


def test_edited_unsupported_declaration_is_refused():
    baseline = with_formulas(candidate())
    value = copy.deepcopy(baseline)
    child_clips(value)[0]["app"]["astrid"]["formulas"]["at"]["moment"] = "word:line-2:first"
    with pytest.raises(AuthoringBundleError, match="unsupported timing declaration"):
        validate_final_candidate(value, baseline=baseline)


@pytest.mark.parametrize("touch", ["timing", "track"])
def test_moving_a_clip_keeps_its_unchanged_unsupported_declaration_retained(touch):
    """The signature is the declaration's own value: the kernel never evaluates it, so a move or a track
    change on its clip changes nothing it refuses."""
    baseline = with_formulas(candidate())
    value = copy.deepcopy(baseline)
    clip = child_clips(value)[0]
    if touch == "timing":
        clip["at"] = 2
    else:
        value["shots"][SHOT]["internal_timeline"]["tracks"].append({"id": "other", "kind": "visual"})
        clip["track"] = "other"
    validate_final_candidate(value, baseline=baseline)


def _head_gap_after():
    head = candidate()
    child_clips(head)[0].setdefault("app", {}).setdefault("astrid", {})["gap_after_s"] = 1.5
    return head


def test_moving_a_clip_with_an_unchanged_gap_after_s_is_accepted_and_a_changed_one_refused():
    baseline = _head_gap_after()
    moved = copy.deepcopy(baseline)
    child_clips(moved)[0]["at"] = 2
    validate_final_candidate(moved, baseline=baseline)
    changed = copy.deepcopy(baseline)
    child_clips(changed)[0]["app"]["astrid"]["gap_after_s"] = 2.5
    with pytest.raises(AuthoringBundleError, match="unsupported timing declaration"):
        validate_final_candidate(changed, baseline=baseline)
    added = copy.deepcopy(baseline)
    child_clips(added)[1].setdefault("app", {}).setdefault("astrid", {})["gap_after_s"] = 1.5
    with pytest.raises(AuthoringBundleError, match="unsupported timing declaration"):
        validate_final_candidate(added, baseline=baseline)


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
    validate_final_candidate(value, baseline=baseline)


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


def test_existing_overrun_survives_a_ripple_edit():
    """Moving the shot (an earlier shot trimmed, inserted or removed) moves both ends of the overrun in film time."""
    head = overrunning_head()
    edited = copy.deepcopy(head)
    edited["placements"][0]["placement"]["start_ms"] = 1000
    report = check(edited)
    assert length_blocking(report) == []
    assert any(line.startswith(PREEXISTING + "length") for line in report.problems)


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


# --- the invariant ---------------------------------------------------------------------------------------------

def _head_unsupported_declaration():
    return with_formulas(candidate())


def _head_unknown_asset():
    return with_unknown_asset(candidate())


def _head_mismatched_fps():
    head = candidate()
    head["shots"][SHOT]["internal_timeline"]["output"] = {"fps": 24}
    return head


def _head_half_association():
    head = candidate()
    head["parent"]["registry"].setdefault("assets", {})["half"] = {
        "media_id": "sha256:" + "4a" * 32, "generationId": "gen-1", "variantId": None}
    return head


HEADS = {"unsupported-declaration": _head_unsupported_declaration, "unknown-asset": _head_unknown_asset,
         "mismatched-fps": _head_mismatched_fps, "gap-after-s": _head_gap_after,
         "half-association": _head_half_association}


@pytest.mark.parametrize("name", sorted(HEADS))
def test_invariant_every_long_lived_head_accepts_an_unrelated_commit_and_a_duplicate(name):
    """A new validation rule that breaks this needs a migration or a baseline exemption."""
    head = HEADS[name]()
    edited = copy.deepcopy(head)
    unrelated_change(edited)
    validate_final_candidate(edited, baseline=head)
    empty = copy.deepcopy(head)
    empty["placements"], empty["shots"] = [], {}
    validate_final_candidate(copy.deepcopy(head), baseline=empty, authenticated_copy=True)


# --- scope: a shot-internal revision bump keeps the baseline of the clips it did not touch -----------------------

def _unresolved_params_head():
    value = candidate()
    clip = child_clips(value)[0]
    clip.update(clipType="no-such-element-anywhere", params={"x": 1})
    return value


def test_shot_internal_revision_bump_judges_untouched_clips_against_the_previous_revision():
    baseline = _unresolved_params_head()
    value = copy.deepcopy(baseline)
    value["source_mapping"]["shots"][SHOT]["internal_timeline_revision_id"] = "internal-revision-next"
    unrelated_change(value)
    validate_final_candidate(value, baseline=baseline)


def test_shot_internal_revision_bump_still_refuses_what_the_edit_touches():
    baseline = _unresolved_params_head()
    value = copy.deepcopy(baseline)
    value["source_mapping"]["shots"][SHOT]["internal_timeline_revision_id"] = "internal-revision-next"
    child_clips(value)[0]["params"] = {"x": 2}
    with pytest.raises(AuthoringBundleError, match="clipType"):
        validate_final_candidate(value, baseline=baseline)


def test_another_internal_timeline_carries_no_baseline_over():
    baseline = _unresolved_params_head()
    value = copy.deepcopy(baseline)
    value["source_mapping"]["shots"][SHOT]["internal_timeline_id"] = "another-internal-timeline"
    with pytest.raises(AuthoringBundleError, match="clipType"):
        validate_final_candidate(value, baseline=baseline)


# --- scope: strict candidate checks judge only the clips the candidate added or changed --------------------------

def _overrunning_shot_head():
    head = candidate()
    clip = child_clips(head)[0]
    clip.update(at=9.9, hold=0.103)  # ends 3 ms past the 10 s occurrence
    return head


def _edit_sibling(value):
    child_clips(value)[1]["at"] = 6
    child_clips(value).append({**copy.deepcopy(child_clips(value)[1]), "id": "added", "at": 8})


def test_untouched_clip_with_a_small_overrun_does_not_block_an_unrelated_clip_edit():
    baseline = _overrunning_shot_head()
    value = copy.deepcopy(baseline)
    _edit_sibling(value)
    validate_final_candidate(value, baseline=baseline)


def test_same_overrun_on_the_edited_clip_is_refused():
    baseline = _overrunning_shot_head()
    value = copy.deepcopy(baseline)
    child_clips(value)[0]["hold"] = 0.104
    with pytest.raises(AuthoringBundleError, match="half-open bounds"):
        validate_final_candidate(value, baseline=baseline)


def test_reserialised_numbers_are_not_a_change():
    baseline = _overrunning_shot_head()
    value = copy.deepcopy(baseline)
    child_clips(value)[0]["at"] = 9.9  # same value; and an integral float for another clip
    child_clips(value)[1]["at"] = float(child_clips(value)[1]["at"])
    validate_final_candidate(value, baseline=baseline)


def test_shrinking_the_occurrence_so_an_untouched_clip_overruns_is_refused():
    baseline = candidate()
    value = copy.deepcopy(baseline)
    value["placements"][0]["duration_ms"] = 1500  # clip-a (1..3) and clip-b (5..7) no longer fit
    with pytest.raises(AuthoringBundleError, match="half-open bounds"):
        validate_final_candidate(value, baseline=baseline)


def _unbound_word_head():
    head = candidate()
    child_clips(head)[0].setdefault("app", {}).setdefault("astrid", {})["on"] = "word:hello"
    return head


def test_untouched_unresolved_word_binding_does_not_block_an_edit_to_another_clip_of_the_shot():
    baseline = _unbound_word_head()
    value = copy.deepcopy(baseline)
    _edit_sibling(value)
    validate_final_candidate(value, baseline=baseline)


def test_unresolved_word_binding_on_a_changed_clip_is_refused():
    baseline = _unbound_word_head()
    value = copy.deepcopy(baseline)
    child_clips(value)[0]["app"]["astrid"]["on"] = "word:goodbye"
    with pytest.raises(AuthoringBundleError, match="missing exact word binding"):
        validate_final_candidate(value, baseline=baseline)


def test_unresolved_word_binding_on_an_added_clip_is_refused():
    baseline = candidate()
    value = copy.deepcopy(baseline)
    _edit_sibling(value)
    child_clips(value)[-1].setdefault("app", {}).setdefault("astrid", {})["on"] = "word:hello"
    with pytest.raises(AuthoringBundleError, match="missing exact word binding"):
        validate_final_candidate(value, baseline=baseline)
