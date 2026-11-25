"""R15-P87-07: real move capture/execution keeps stored starts integer-valued."""
from __future__ import annotations

import copy
from fractions import Fraction

import pytest

from astrid.sdk.timeline._command_state import CommandError, State
from astrid.sdk.timeline.capture import capture_move
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import bundle_from_document
from tests.sdk.test_timeline_commit_provider_document import document_fixture

OWNER = 'occurrence:occ-a'
MOVE_CASES = [
    pytest.param(30, 4.25, 128, id='30fps-half-frame'),
    pytest.param(30, 4.2666, 128, id='30fps-fractional-second'),
    pytest.param({'numerator': 30000, 'denominator': 1001}, 4.2666, 128,
                 id='ntsc-fractional-second'),
    pytest.param(25, 4.2666, 107, id='25fps-fractional-second'),
    pytest.param(30, 2.5, 75, id='30fps-exact-2500ms'),
    pytest.param(25, 2.4, 60, id='25fps-exact-2400ms'),
    pytest.param({'numerator': 30000, 'denominator': 1001}, 1.001, 30,
                 id='ntsc-exact-1001ms'),
    pytest.param(16, 0.0625, 1, id='ms-tie-rounds-down-to-even'),
    pytest.param(16, 0.1875, 3, id='ms-tie-rounds-up-to-even'),
]


def _frame_ms(frame, fps):
    rate = Fraction(fps['numerator'], fps['denominator']) if isinstance(fps, dict) else Fraction(fps)
    return round(Fraction(frame * 1000, 1) / rate)


def _move_bundle(fps=30, *, origin_ms=0, duration_ms=10000, hold=1, codec='at_ms'):
    # Start with a verified legacy document, then author the timing fixture on
    # the detached bundle, as the existing move conformance tests do.
    bundle = bundle_from_document(document_fixture(shared=False))
    state = State(bundle)
    occurrence = state.occurrence(OWNER)
    occurrence.update(source_offset=0, speed=1, duration_ms=duration_ms)
    occurrence['placement']['start_ms'] = origin_ms
    for owner in ('parent', OWNER):
        state.config(owner)['output']['fps'] = copy.deepcopy(fps)
        target = state.entity(owner + '/clip:clip-a')
        target['hold'] = hold
        if codec != 'at':
            target['at_ms'] = target['at'] * 1000
        if codec == 'at_ms':
            target.pop('at')
    bundle['parent']['config']['clips'] = copy.deepcopy(bundle['parent']['clips'])
    return bundle


@pytest.mark.parametrize('owner', ['parent', OWNER])
@pytest.mark.parametrize('fps,on,frame', MOVE_CASES)
def test_clip_move_writes_integer_at_ms(owner, fps, on, frame):
    original = _move_bundle(fps)
    before = copy.deepcopy(original)
    ref = owner + '/clip:clip-a'
    command = capture_move(original, ref, on)
    plan = execute(original, [command])
    value = State(plan.bundle).entity(ref)['at_ms']
    assert type(value) is int
    assert value == _frame_ms(frame, fps)
    assert value >= 0
    writes = [write for write in plan.records[0]['writes']
              if write['ref'] == ref and write['path'] == ['at_ms']]
    assert len(writes) == 1
    assert type(writes[0]['after']['value']) is int
    assert writes[0]['after']['value'] == value
    pin = next(dependency['pin'] for dependency in plan.records[0]['dependencies']
               if dependency['key'] == 'move.quantization')
    assert pin['rounding'] == 'half-toward-positive-infinity'
    assert plan.commands == [command]
    assert original == before


@pytest.mark.parametrize('origin_ms,expected', [(1000, 3267), (1000.5, 3266),
                                              (1001.5, 3266), (4266.666666666667, 0)])
def test_clip_move_subtracts_stored_occurrence_start_before_rounding_local_ms(origin_ms, expected):
    original = _move_bundle(origin_ms=origin_ms)
    ref = OWNER + '/clip:clip-a'
    plan = execute(original, [capture_move(original, ref, 4.2666)])
    value = State(plan.bundle).entity(ref)['at_ms']
    assert type(value) is int
    assert value == expected
    assert State(plan.bundle).occurrence(OWNER)['placement']['start_ms'] == origin_ms


@pytest.mark.parametrize('owner', ['parent', OWNER])
@pytest.mark.parametrize('fps', [30, {'numerator': 30000, 'denominator': 1001}, 25])
def test_clip_move_keeps_dual_codecs_consistent_across_repeated_moves(owner, fps):
    bundle = _move_bundle(fps, codec='both')
    ref = owner + '/clip:clip-a'
    for on in (4.2666, 4.3333):
        command = capture_move(bundle, ref, on)
        plan = execute(bundle, [command])
        moved = State(plan.bundle).entity(ref)
        assert type(moved['at_ms']) is int
        assert moved['at'] == moved['at_ms'] / 1000
        assert plan.commands == [command]
        bundle = plan.bundle


def test_clip_move_keeps_at_only_frame_seconds():
    original = _move_bundle(codec='at')
    plan = execute(original, [capture_move(original, 'parent/clip:clip-a', 4.2666)])
    moved = State(plan.bundle).entity('parent/clip:clip-a')
    assert moved['at'] == float(Fraction(128, 30))
    assert 'at_ms' not in moved


@pytest.mark.parametrize('on,hold,boundary_ms,accepted', [
    (4.2666, 1.0002, 5267, True),
    (127 / 30, 1.0008, 5234, False),
])
def test_clip_move_next_occurrence_boundary_uses_exact_frame_seconds(on, hold, boundary_ms, accepted):
    original = _move_bundle(hold=hold)
    next_occurrence = copy.deepcopy(State(original).occurrence(OWNER))
    next_occurrence['occurrence_id'] = 'next'
    next_occurrence['placement']['start_ms'] = boundary_ms
    original['placements'].append(next_occurrence)
    before = copy.deepcopy(original)
    ref = OWNER + '/clip:clip-a'
    if accepted:
        plan = execute(original, [capture_move(original, ref, on)])
        moved = State(plan.bundle).entity(ref)
        assert moved['at_ms'] == 4267
        # Exact end is 0.133 ms inside; rounded storage alone crosses by 0.2 ms.
        assert moved['at_ms'] / 1000 + hold > boundary_ms / 1000
    else:
        with pytest.raises(CommandError, match='moved interval crosses the next occurrence') as error:
            capture_move(original, ref, on)
        assert error.value.kind == 'occurrence_boundary'
    assert original == before
