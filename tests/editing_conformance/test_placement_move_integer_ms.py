"""R15-P87-07: placement moves share integer writes and exact boundary checks."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._command_state import CommandError, State
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.native_placement_commands import capture_placement
from tests.editing_conformance.test_clip_move_integer_ms import (
    MOVE_CASES,
    OWNER,
    _frame_ms,
    _move_bundle,
)


def _capture(bundle, owner, on):
    return capture_placement(bundle, owner, 'clip-a', on,
                             ownership='parent' if owner == 'parent' else 'occurrence')


@pytest.mark.parametrize('owner', ['parent', OWNER])
@pytest.mark.parametrize('fps,on,frame', MOVE_CASES)
def test_placement_move_writes_integer_at_ms(owner, fps, on, frame):
    original = _move_bundle(fps)
    before = copy.deepcopy(original)
    command = _capture(original, owner, on)
    plan = execute(original, [command])
    ref = owner + '/clip:clip-a'
    value = State(plan.bundle).entity(ref)['at_ms']
    assert type(value) is int
    assert value == _frame_ms(frame, fps)
    assert value >= 0
    writes = [write for write in plan.records[0]['writes']
              if write['ref'] == ref and write['path'] == ['at_ms']]
    assert len(writes) == 1
    assert type(writes[0]['after']['value']) is int
    assert writes[0]['after']['value'] == value
    assert plan.commands == [command]
    assert original == before


@pytest.mark.parametrize('origin_ms,expected', [(1000, 3267), (1000.5, 3266),
                                              (1001.5, 3266), (4266.666666666667, 0)])
def test_placement_move_subtracts_stored_occurrence_start(origin_ms, expected):
    original = _move_bundle(origin_ms=origin_ms)
    plan = execute(original, [_capture(original, OWNER, 4.2666)])
    value = State(plan.bundle).entity(OWNER + '/clip:clip-a')['at_ms']
    assert type(value) is int
    assert value == expected
    assert State(plan.bundle).occurrence(OWNER)['placement']['start_ms'] == origin_ms


@pytest.mark.parametrize('owner', ['parent', OWNER])
def test_placement_move_keeps_dual_codecs_consistent_across_repeated_moves(owner):
    bundle = _move_bundle(codec='both')
    for on in (4.2666, 4.3333):
        plan = execute(bundle, [_capture(bundle, owner, on)])
        moved = State(plan.bundle).entity(owner + '/clip:clip-a')
        assert type(moved['at_ms']) is int
        assert moved['at'] == moved['at_ms'] / 1000
        bundle = plan.bundle


@pytest.mark.parametrize('codec', ['at_ms', 'both', 'at'])
@pytest.mark.parametrize('on,hold,duration_ms,accepted', [
    (4.2666, 1.0002, 5267, True),
    (127 / 30, 1.0008, 5234, False),
])
def test_placement_move_duration_boundary_uses_exact_frame_seconds(codec, on, hold, duration_ms, accepted):
    original = _move_bundle(hold=hold, duration_ms=duration_ms, codec=codec)
    before = copy.deepcopy(original)
    if accepted:
        plan = execute(original, [_capture(original, OWNER, on)])
        moved = State(plan.bundle).entity(OWNER + '/clip:clip-a')
        if codec != 'at':
            assert moved['at_ms'] == 4267
            assert moved['at_ms'] / 1000 + hold > duration_ms / 1000
        else:
            assert moved['at'] + hold < duration_ms / 1000
    else:
        with pytest.raises(CommandError, match='placed interval exceeds occurrence duration') as error:
            _capture(original, OWNER, on)
        assert error.value.kind == 'occurrence_boundary'
    assert original == before
