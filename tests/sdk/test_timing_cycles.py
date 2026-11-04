"""Distinct R4 directed-cycle and forward-reference cases, on main bundle inputs."""
from __future__ import annotations

import copy
from fractions import Fraction

import pytest
from test_timing_compiler import CARD, VOICE, captured, encoded, film

from astrid.sdk.timeline.compiler import TimingCycleError, TimingInputs, compile_timing


def cuts():
    bundle = film()
    internal = bundle["shots"]["shot"]["internal_timeline"]
    internal["tracks"].append({"id": "plate", "kind": "visual"})
    internal["clips"] = [
        {"id": "one", "at": 0, "hold": 4, "track": "plate", "app": {"cut": "c01"}},
        {"id": "two", "at": 4, "hold": 6, "track": "plate", "app": {"cut": "c02"}},
    ]
    return bundle, internal["clips"]


def test_directed_cycle_is_stable_across_repeat_and_storage_order():
    bundle, clips = cuts()
    clips[0]["app"]["on"], clips[1]["app"]["on"] = "c2", "c1"
    before = encoded(bundle)
    paths = []
    for _ in range(2):
        with pytest.raises(TimingCycleError) as error:
            compile_timing(TimingInputs(bundle))
        assert error.value.code == "CYCLE"
        assert error.value.path[0] == error.value.path[-1]
        assert len(set(error.value.path)) == 4
        paths.append(error.value.path)
    assert encoded(bundle) == before
    clips.reverse()
    with pytest.raises(TimingCycleError) as error:
        compile_timing(TimingInputs(bundle))
    assert error.value.path == paths[0] == paths[1]


@pytest.mark.parametrize("text", ["c1", "c001 +2f", "+0.2s"])
def test_self_cycle_uses_main_cut_aliases_and_own_cut(text):
    bundle, clips = cuts()
    clips[0]["app"]["on"] = text
    with pytest.raises(TimingCycleError):
        compile_timing(TimingInputs(bundle))


def test_cut_end_depends_on_next_cut_start():
    bundle, clips = cuts()
    clips[0]["app"]["on"] = "end of c1"
    clips[1]["app"]["on"] = "c1"
    with pytest.raises(TimingCycleError) as error:
        compile_timing(TimingInputs(bundle))
    assert ("cut", "c01", "end") in error.value.path
    assert ("cut", "c02", "start") in error.value.path


def test_word_and_cut_form_a_cycle_without_changing_source_or_authored_state():
    bundle, document, pin = captured()
    clips = bundle["shots"]["shot"]["internal_timeline"]["clips"]
    clips[1].update(track="plate")
    clips[1]["app"]["astrid"]["cut"] = "c1"
    clips[0]["app"] = {"astrid": {"on": "c1"}}
    before = encoded([bundle, document])
    with pytest.raises(TimingCycleError) as error:
        compile_timing(TimingInputs(bundle, sources=[document], bindings=[pin]))
    assert ("clip", *VOICE, "start") in error.value.path
    assert ("clip", *CARD, "start") in error.value.path
    assert encoded([bundle, document]) == before


def test_valid_forward_and_terminal_end_references_are_accepted():
    bundle, clips = cuts()
    clips.append({"id": "layer", "track": "type", "at": 0, "hold": 1,
                  "app": {"on": "c2 -1s", "until": "end of c2"}})
    clips[1]["app"]["until"] = "end"
    result = compile_timing(TimingInputs(bundle))
    timing = result.timings["occ", "shot", "layer"]
    assert (timing["start"], timing["end"]) == (5, 12)
    assert result.bundle == bundle


def test_long_valid_dependency_chain_does_not_recurse():
    bundle, clips = cuts()
    clips.clear()
    for index in range(1100):
        declarations = {"cut": f"c{index + 1}", "for": 1}
        if index:
            declarations["on"] = f"c{index} +0.1s"
        clips.append({"id": str(index), "at": index / 10, "hold": 1,
                      "track": "plate", "app": declarations})
    result = compile_timing(TimingInputs(copy.deepcopy(bundle)))
    assert len(result.timings) == 1100


def test_main_fixed_move_intent_keeps_authored_start_even_with_cut_membership():
    bundle, clips = cuts()
    clips.append({"id": "layer", "track": "type", "at": 0.7, "hold": 1,
                  "app": {"astrid": {"cut": "c01", "fixed_start": True, "for": 0.5}}})
    interval = compile_timing(TimingInputs(bundle)).timings["occ", "shot", "layer"]
    assert (interval["start"], interval["end"]) == (Fraction(27, 10), Fraction(16, 5))


def test_terminal_cut_layer_follows_picture_end_without_changing_authored_hold():
    bundle, clips = cuts()
    clips[-1]["hold"] = 2
    clips.append({"id": "layer", "track": "type", "at": 4.5, "hold": 0.5,
                  "app": {"cut": "c02"}})
    interval = compile_timing(TimingInputs(bundle)).timings["occ", "shot", "layer"]
    assert (interval["start"], interval["end"]) == (6, 8)
    assert clips[-1]["hold"] == 0.5
