"""Fit recipe and integer allocation assertions; no R-13/L13 scenario claim."""
from __future__ import annotations

import copy
import math

import pytest

from astrid.sdk import timeline_intent as intent
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.sequence_fit import allocate_fit_frames, fit_curve_inverse
from astrid.sdk.timeline_checkout import Checkout
from test_timeline_checkout import FPS, bundle
from test_timeline_reflow_golden import _film_with_a_time_lapse


def _generated(**overrides):
    data = bundle()
    clips = data["shots"]["A"]["internal_timeline"]["clips"]
    plate = clips.pop(0)
    for i in range(3):
        step = copy.deepcopy(plate)
        step.update(id=f"seq-{i}", at=round(i * 4 / 3, 6), hold=round(4 / 3, 6), asset="Q")
        step["app"] = {"sequence": "seq", "sequence_index": i}
        clips.append(step)
    recipe = {"land": {"word": "s1:2", "text": "viral"}, "lead": [12], "race": [6, 4, 2],
              "cycle": ["P", "Q"], "then": "R"}
    recipe.update(overrides)
    clips[-3]["app"]["sequence_fit"] = recipe
    return Checkout(data)


def test_linear_fixed_recipe_retains_authored_weights_across_refits():
    tl = Checkout(_film_with_a_time_lapse())
    seq = tl.sequence("c3.step-02")
    weights = [30, 15, 9, 6, 30]
    seq.fit(until="end", shape=weights, curve="linear")
    assert intent.sequence_fit(seq.steps[0].data) == {"weights": weights, "curve": "linear"}
    seq.fit(until="end")
    assert seq.frames == weights
    assert seq.steps[-1].end == pytest.approx(9.0)
    doc = tl.document()
    tl.retime()
    assert tl.document() == doc


def test_fixed_weight_recipe_overrides_rounded_current_lengths():
    tl = Checkout(_film_with_a_time_lapse())
    seq = tl.sequence("c3.step-02")
    seq.fit(until="+3s", shape=[30, 15, 9, 6, 30], curve="linear")
    seq.steps[2].data["hold"] = 3.2
    tl.retime()
    assert seq.frames == [30, 15, 9, 6, 30]


@pytest.mark.parametrize("weights,total,expected", [([1, 3], 8, [2, 6]), ([1, 1, 1], 5, [2, 2, 1]),
                                                        ([100, 1, 1], 3, [1, 1, 1]), ([9, 1], 3, [2, 1])])
def test_linear_allocation_preserves_largest_remainders(weights, total, expected):
    assert allocate_fit_frames(weights, total) == expected
    assert allocate_fit_frames(weights, total) == expected


def test_relay_ends_at_requested_frame_from_nonzero_start():
    tl = Checkout(_film_with_a_time_lapse())
    clips = [{"at": 0, "hold": 1 / FPS} for _ in range(3)]
    tl._relay_steps(clips, 7 / FPS, 5 / FPS, [1, 1, 1])
    assert [round(c["hold"] * FPS) for c in clips] == [2, 2, 1]
    assert round((clips[-1]["at"] + clips[-1]["hold"]) * FPS) == 12


def test_fixed_undersized_span_rejects_without_lengthening():
    tl = Checkout(_film_with_a_time_lapse())
    clips = [{"at": i / FPS, "hold": 1 / FPS} for i in range(3)]
    before = copy.deepcopy(clips)
    with pytest.raises(TimelineEditError, match="2 frames for 3 required steps"):
        tl._relay_steps(clips, 0, 2 / FPS)
    assert clips == before


@pytest.mark.parametrize("weights", [[0, 1], [-1, 1], [math.inf, 1], [math.nan, 1]])
def test_invalid_fixed_weights_reject(weights):
    with pytest.raises(TimelineEditError, match="finite and positive"):
        allocate_fit_frames(weights, 8)


def test_generated_linear_preserves_lead_race_and_hard_land():
    tl = _generated(curve="linear")
    tl.resolve()
    steps = tl.sequence("seq-00").steps
    assert [round(c.duration * FPS) for c in steps][:4] == [12, 6, 4, 2]
    assert steps[-1].asset == "R"
    assert steps[-1].start == pytest.approx(tl.word("viral").start)
    assert steps[-1].end == pytest.approx(4.0)
    assert all(c.duration * FPS >= 1 - 1e-5 for c in steps)
    assert tl.resolve() == []


@pytest.mark.parametrize("recipe", [{"lead": [40]}, {"offset_frames": 81}, {"race": [0]}, {"lead": [0]}])
def test_generated_invalid_or_undersized_span_rejects_atomically(recipe):
    tl = _generated(**recipe)
    before = tl.document()
    with pytest.raises(TimelineEditError, match="at least one frame|too short"):
        tl.resolve()
    assert tl.document() == before


@pytest.mark.parametrize("mechanism", ["fixed", "generated"])
def test_step_curve_rejects_noninvertible_progress(mechanism):
    tl = _generated(curve="step") if mechanism == "generated" else Checkout(_film_with_a_time_lapse())
    before = tl.document()
    with pytest.raises(TimelineEditError, match="has no inverse progress"):
        tl.resolve() if mechanism == "generated" else tl.sequence("c3.step-02").fit(curve="step")
    assert tl.document() == before


@pytest.mark.parametrize("curve,canonical", [("in", "in"), ("out", "out"), ("in-out", "in-out"),
    ("inOut", "in-out"), ("inout", "in-out"), ("in_out", "in-out"),
    ("ease-in", "in"), ("ease-out", "out"), ("ease-in-out", "in-out")])
def test_named_curve_fixed_membership_uses_declared_analytic_inverse(curve, canonical):
    tl = Checkout(_film_with_a_time_lapse())
    seq = tl.sequence("c3.step-02")
    weights = [30, 15, 9, 6, 30]
    seq.fit(until="end", shape=weights, curve=curve)
    expected = allocate_fit_frames(weights, 90, fit_curve_inverse(canonical))
    assert seq.frames == expected
    assert intent.sequence_fit(seq.steps[0].data) == {"weights": weights, "curve": canonical}
    assert seq.steps[-1].end == pytest.approx(9.0)
    assert min(seq.frames) >= 1
    before = tl.document()
    tl.retime()
    assert tl.document() == before


@pytest.mark.parametrize("curve,weights,total,expected", [
    ("in", [1, 3], 8, [4, 4]), ("out", [3, 1], 8, [4, 4]),
    ("in-out", [1, 6, 1], 8, [2, 4, 2]),
    ("out", [1, 1, 100], 3, [1, 1, 1]),
])
def test_named_inverse_boundaries_round_deterministically_and_keep_minima(curve, weights, total, expected):
    assert allocate_fit_frames(weights, total, fit_curve_inverse(curve)) == expected
    assert sum(expected) == total and min(expected) >= 1


@pytest.mark.parametrize("curve", ["in", "out", "in-out"])
def test_generated_named_inverse_curves_race_only_through_retime_and_refit(curve):
    tl = _generated(curve=curve)
    for clip in tl.clips():
        if intent.sequence(clip.data):
            intent.set_cut(clip.data, "c1")  # exercise retime's cut-sequence relay
    tl.resolve()
    seq = tl.sequence("seq-00")
    before = seq.frames
    assert before[0] == 12  # authored lead is outside the race transform
    # The declared race has 27 frames and weights [6,4,2,...,2,1].
    race_weights = [6, 4] + [2] * 8 + [1]
    assert before[1:-1] == allocate_fit_frames(race_weights, 27, fit_curve_inverse(curve))
    assert sum(before[1:-1]) == 27
    assert seq.steps[-1].start == pytest.approx(tl.word("viral").start)
    assert seq.steps[-1].end == pytest.approx(4.0)
    assert min(before) >= 1 and sum(before) == 120

    # retime's full-span relay must not curve the lead/landing or compound the
    # already rounded race. Only _fit_sequences owns a generated race curve.
    tl.retime()
    assert seq.frames == before

    # End refitting grows the landing, keeping the original lead and race span.
    intent.set_until(seq.steps[0].data, "+5s")
    tl.resolve()
    assert seq.frames[:-1] == before[:-1]
    assert seq.steps[-1].start == pytest.approx(tl.word("viral").start)
    assert seq.steps[-1].end == pytest.approx(5.0)
    assert sum(seq.frames) == 150
    assert tl.resolve() == []


@pytest.mark.parametrize("curve", ["in", "out", "in-out"])
def test_named_curve_undersized_fixed_span_refuses_atomically(curve):
    tl = Checkout(_film_with_a_time_lapse())
    before = tl.document()
    with pytest.raises(TimelineEditError, match="too short for 5 required steps"):
        tl.sequence("c3.step-02").fit(until="+0.1s", curve=curve)
    assert tl.document() == before
