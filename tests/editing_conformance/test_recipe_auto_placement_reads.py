"""Automatic placement records only the timing/lane inputs it consumes."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._command_state import CommandError, State
from astrid.sdk.timeline._scalar_commands import capture_clip_set_params
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import bundle_from_document
from astrid.sdk.timeline.native_auto_placement import auto_place_completion, auto_place_shared
from astrid.sdk.timeline.native_placement_commands import capture_placement
from astrid.sdk.timeline.rebase import replay
from tests.editing_conformance.test_recipe_m1_commands import inverse
from tests.editing_conformance.test_recipe_occurrence_media import inputs

OWNER = 'occurrence:occ-a'
CHILD = OWNER + '/clip:placed'
MODES = ['anchor-home', 'anchor-overlay', 'fallback-home', 'fallback-overlay',
         'lane-end', 'append-home', 'append-overlay', 'explicit']


def basis(kind, mode):
    document, _, pin, verified = inputs(kind)
    bundle = bundle_from_document(document)
    state = State(bundle)
    state.occurrence(OWNER)['duration_ms'] = 30000
    state.occurrence('occurrence:occ-b')['placement']['start_ms'] = 100000
    state.config(OWNER)['tracks'].append({'id': 'overlay', 'kind': 'visual'})
    state.config(OWNER)['clips'].append({'id': 'clip-c', 'track': 'overlay', 'asset': 'picture',
                                       'clipType': 'media', 'at': 1, 'hold': 8})
    return bundle, pin, verified, request(mode)


def request(mode):
    if mode.startswith('anchor'):
        return auto_place_completion, {'anchor': 'clip-a', **(
            {'preferred_track': 'overlay'} if mode.endswith('overlay') else {})}
    if mode.startswith('fallback'):
        return auto_place_completion, {'anchor': 'gone', 'fallback_at': 22.25,
                                       'fallback_track': 'overlay' if mode.endswith('overlay') else 'picture'}
    if mode == 'lane-end':
        return auto_place_completion, {}
    return auto_place_shared, ({'anchor': 'clip-c'} if mode == 'append-overlay' else
                              {'frame': 390} if mode == 'explicit' else {})


def build(bundle, pin, verified, req):
    function, kwargs = req
    return function(bundle, OWNER, 'placed', pin, ownership='occurrence', verified_media=verified, **kwargs)


@pytest.mark.parametrize('kind', ['image', 'video'])
@pytest.mark.parametrize('mode,clip_id,on,old,fresh', [
    ('explicit', 'clip-a', 391 * 1001 / 30000, 391, 392),
    ('lane-end', 'clip-b', 14, 450, 480),
    ('append-home', 'clip-c', 10, 510, 540),
])
def test_stale_native_move(kind, mode, clip_id, on, old, fresh):
    bundle, pin, verified, req = basis(kind, mode)
    result = build(bundle, pin, verified, req)
    assert result['capture']['start_frame'] == old
    plan = execute(bundle, result['commands'], verified_media=verified)
    moved = execute(bundle, [capture_placement(bundle, OWNER, clip_id, on, ownership='occurrence')])
    with pytest.raises(CommandError) as error:
        replay(plan, moved.bundle, intervening_records=moved.records, verified_media=verified)
    assert error.value.kind == 'dependency_changed'
    assert build(moved.bundle, pin, verified, req)['capture']['start_frame'] == fresh


@pytest.mark.parametrize('kind', ['image', 'video'])
@pytest.mark.parametrize('mode', MODES)
def test_retarget(kind, mode):
    bundle, pin, verified, req = basis(kind, mode)
    result = build(bundle, pin, verified, req)
    plan = execute(bundle, result['commands'], verified_media=verified)
    head = copy.deepcopy(bundle)
    head['placements'][1]['shot_id'] = head['placements'][0]['shot_id']
    with pytest.raises(CommandError) as error:
        replay(plan, head, verified_media=verified)
    assert error.value.kind == 'selection_changed'
    with pytest.raises(CommandError) as error:
        build(head, pin, verified, req)
    assert error.value.kind == 'unsupported_ownership'
    command = result['commands'][0]
    raw = {key: command[key] for key in ('schema_version', 'op', 'version', 'select', 'args', 'intent')}
    with pytest.raises(CommandError) as error:
        execute(head, [raw], verified_media=verified)
    assert error.value.kind == 'unsupported_ownership'


@pytest.mark.parametrize('kind', ['image', 'video'])
@pytest.mark.parametrize('mode', MODES)
@pytest.mark.parametrize('change', ['parent-fps', 'child-fps', 'origin', 'home', 'duration',
                                   'track-kind', 'track-role', 'track-disabled', 'track-delete',
                                   'anchor-membership', 'order'])
def test_guard_matrix(kind, mode, change):
    bundle, pin, verified, req = basis(kind, mode)
    result = build(bundle, pin, verified, req)
    plan = execute(bundle, result['commands'], verified_media=verified)
    assert replay(plan, bundle, verified_media=verified).records == plan.records
    head = copy.deepcopy(bundle)
    state = State(head)
    lane = result['capture']['lane']
    if change == 'parent-fps':
        state.config('parent')['output']['fps'] = 24
    elif change == 'child-fps':
        state.config(OWNER)['output']['fps'] = 24
    elif change == 'origin':
        state.occurrence(OWNER)['placement']['start_ms'] += 1000
    elif change == 'home':
        state.occurrence(OWNER)['placement']['track'] = 'overlay'
    elif change == 'duration':
        state.occurrence(OWNER)['duration_ms'] += 1000
    elif change.startswith('track-'):
        track = state.entity(OWNER + '/track:' + lane)
        if change == 'track-delete':
            state.config(OWNER)['tracks'].remove(track)
        else:
            key, value = {'track-kind': ('kind', 'audio'), 'track-role': ('role', 'input'),
                          'track-disabled': ('disabled', True)}[change]
            track[key] = value
    elif change == 'anchor-membership':
        rows = state.config(OWNER)['clips']
        if mode.startswith('fallback'):
            rows.append({'id': 'gone', 'track': 'picture', 'at': 3, 'hold': 1})
        else:
            rows.pop(0)
    elif change == 'order':
        state.config(OWNER)['clips'].reverse()
    with pytest.raises(CommandError):
        replay(plan, head, verified_media=verified)


@pytest.mark.parametrize('kind', ['image', 'video'])
@pytest.mark.parametrize('mode', ['anchor-home', 'append-home'])
@pytest.mark.parametrize('tamper', [[], ['clip-b'], ['clip-a', 'clip-b', 'clip-c'], ['foreign']])
def test_consumed_cannot_refresh_original_claims(kind, mode, tamper):
    bundle, pin, verified, req = basis(kind, mode)
    command = build(bundle, pin, verified, req)['commands'][0]
    if command['args']['consumed'] == tamper:
        tamper = ['clip-b']
    forged = copy.deepcopy(command)
    forged['args']['consumed'] = tamper
    with pytest.raises(CommandError):
        execute(bundle, [forged], verified_media=verified)
    head = copy.deepcopy(bundle)
    State(head).entity(OWNER + '/clip:clip-a')['at'] = 2
    with pytest.raises(CommandError) as error:
        execute(head, [forged], verified_media=verified)
    assert error.value.kind == 'dependency_changed'
    raw = {key: command[key] for key in ('schema_version', 'op', 'version', 'select', 'args', 'intent')}
    recaptured = execute(bundle, [raw], verified_media=verified)
    assert recaptured.commands == [command]
    with pytest.raises(CommandError):
        replay(recaptured, head, verified_media=verified)


@pytest.mark.parametrize('kind', ['image', 'video'])
@pytest.mark.parametrize('mode', MODES)
@pytest.mark.parametrize('field', ['params', 'volume', 'opaque'])
def test_unrelated_clip_edits_preserve_the_complete_capture_and_original_replay(kind, mode, field):
    bundle, pin, verified, req = basis(kind, mode)
    original = build(bundle, pin, verified, req)
    saved = copy.deepcopy(original)
    plan = execute(bundle, original['commands'], verified_media=verified)
    target = OWNER + '/clip:clip-a'
    if field == 'params':
        changed = execute(bundle, [capture_clip_set_params(bundle, target, values={'unrelated': 1})])
        head, intervening = changed.bundle, changed.records
    else:
        # These opaque top-level fields have no native scalar writer.
        head, intervening = copy.deepcopy(bundle), ()
        State(head).entity(target)[field] = .5 if field == 'volume' else [None, False, {'custom': 1}]
    before = copy.deepcopy(head)
    assert build(head, pin, verified, req) == original
    rebased = replay(plan, head, intervening_records=intervening, verified_media=verified)
    assert rebased.commands == original['commands']
    assert rebased.records == plan.records
    assert State(rebased.bundle).entity(CHILD) == State(plan.bundle).entity(CHILD)
    assert State(rebased.bundle).entity(target) == State(head).entity(target)
    assert inverse(rebased.bundle, rebased.records).bundle == head
    assert head == before
    assert original == saved


@pytest.mark.parametrize('kind', ['image', 'video'])
@pytest.mark.parametrize('timing,end', [
    ({'at': 1, 'hold': 2}, 330),
    ({'at_ms': 1000, 'hold': 2}, 330),
    ({'at': 1, 'hold': 2, 'from': False, 'to': None}, 330),
    ({'at': 1, 'hold': 2, 'from': 2, 'to': 6, 'speed': 2}, 300),
    ({'at': 1, 'from': 2, 'to': 6, 'speed': 2}, 330),
    ({'at': 1, 'hold': None, 'from': 2, 'to': 6, 'speed': 2}, 330),
    ({'at': 1, 'hold': False, 'from': 2, 'to': 6, 'speed': 2}, 330),
    ({'at': 1, 'to': 2, 'speed': None}, 330),
    ({'at': 1, 'from': None, 'to': 2}, 330),
    ({'at': 1}, 271),
])
def test_duration_precedence_and_start_fallback_are_recorded_as_literal_field_states(kind, timing, end):
    bundle, pin, verified, req = basis(kind, 'anchor-home')
    ref = OWNER + '/clip:clip-a'
    row = State(bundle).entity(ref)
    for field in ('at', 'at_ms', 'hold', 'from', 'to', 'speed'):
        row.pop(field, None)
    row.update(timing)
    result = build(bundle, pin, verified, req)
    assert result['capture']['start_frame'] == end
    plan = execute(bundle, result['commands'], verified_media=verified)
    reads = [read for read in plan.records[0]['reads'] if read['ref'] == ref]
    fields = ('at', 'at_ms', 'track', 'hold', 'from', 'to', 'speed')
    assert reads == [{'ref': ref, 'path': [field], 'state': State(bundle).get(ref, [field])}
                     for field in fields]
    assert replay(plan, bundle, verified_media=verified).records == plan.records
    # Even an equivalent default or a previously absent branch input stays guarded.
    for field in fields:
        head = copy.deepcopy(bundle)
        changed = State(head).entity(ref)
        if field in changed:
            del changed[field]
        else:
            changed[field] = None
        with pytest.raises(CommandError) as error:
            replay(plan, head, verified_media=verified)
        assert error.value.kind == 'dependency_changed'
