"""Native parent media trim: main semantics, closed capture, exact inverse."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from astrid.core.timeline.duration import (
    clip_end_frame,
    clip_start_frame,
    clip_timeline_duration,
    timeline_duration_seconds,
)
from astrid.sdk.timeline._command_state import ABSENT, CommandError, State
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline._recording import Recording
from astrid.sdk.timeline._trim_commands import capture_clip_trim
from astrid.sdk.timeline.capture import capture_move
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import _digest, bundle_from_document, document_from_bundle
from astrid.sdk.timeline.rebase import replay
from astrid.sdk.timeline_checkout import Clip
from tests.editing_conformance.fixture_loader import load_fixture
from tests.editing_conformance.test_recipe_m1_commands import inverse, later
from tests.sdk.test_timeline_commit_provider_document import document_fixture

FIXTURE = Path(__file__).parent / 'fixtures' / 'recipe_m2_clip_trim.json'
BOUNDARIES = json.loads(FIXTURE.with_name('recipe_m2_clip_trim_boundaries.json').read_text())
REF = 'parent/clip:clip-a'


def media_document(**changes):
    document = document_fixture()
    payload = document['parent_revision']['payload']
    for rows in (payload['clips'], payload['config']['clips']):
        rows[0].update({'from': 2, 'to': 6, 'speed': 2, 'hold': 17,
                        'app': {'astrid': {'for': 19, 'until': None, 'opaque': [None, False]},
                                'org.example': {'literal': 'clip-a', 'clear': None}}, **changes})
    payload['registry'] = copy.deepcopy(payload['registry'])
    payload['registry']['assets']['picture']['type'] = 'video'
    payload['registry']['assets']['picture']['duration'] = 10
    document['parent_revision']['content_digest'] = _digest(payload)
    return document


def bundle(**changes):
    value = bundle_from_document(media_document())
    for rows in (value['parent']['clips'], value['parent']['config']['clips']):
        rows[0].update(copy.deepcopy(changes))
    return value


def clip(value):
    return State(value).entity(REF)


def test_image_refuses_source_trim_even_with_authored_window():
    original = bundle()
    State(original).registry('parent')['assets']['picture']['type'] = 'image'
    before = copy.deepcopy(original)
    with pytest.raises(CommandError, match='images have no source window'):
        capture_clip_trim(original, REF, in_s=1, out_s=5)
    assert original == before


@pytest.mark.parametrize('changes,args', [({}, {'in_s': 1, 'out_s': 5}),
    ({'speed': None}, {'in_s': 0, 'out_s': 10}), ({'speed': .5}, {'out_s': 10}),
    ({}, {'in_s': .000001, 'out_s': 1.000002}),
    ({'from': None, 'to': None, 'hold': None}, {}),
    ({'from': None, 'to': 8}, {'in_s': None, 'out_s': None})])
def test_trim_matches_main_model_precedence_and_source_defaults(monkeypatch, changes, args):
    original = bundle(**changes)
    row = copy.deepcopy(clip(original))
    # Use main's actual trim function, without a checkout, save, or media IO.
    monkeypatch.setattr(Clip, '_source_duration', lambda self: 10)
    main = Clip(SimpleNamespace(_is_audio=lambda *args: False), None, row)
    main.trim(**args)
    command = capture_clip_trim(original, REF, **args)
    plan = execute(original, [command])
    assert clip(plan.bundle) == row
    assert clip(plan.bundle)['at'] == clip(original)['at']
    assert clip(plan.bundle)['asset'] == 'picture'
    assert replay(plan, original).records == plan.records
    assert inverse(plan.bundle, plan.records).bundle == original
    assert inverse(inverse(plan.bundle, plan.records).bundle,
                   inverse(plan.bundle, plan.records).records).bundle == plan.bundle


@pytest.mark.parametrize('speed', [1, 2, .5, 3 / 2])
def test_trim_hold_is_source_time_and_playback_duration_applies_speed(monkeypatch, speed):
    original = bundle(speed=speed, hold=17)
    row = copy.deepcopy(clip(original))
    monkeypatch.setattr(Clip, '_source_duration', lambda self: 10)
    Clip(SimpleNamespace(_is_audio=lambda *args: False), None, row).trim(1, 5)
    plan = execute(original, [capture_clip_trim(original, REF, in_s=1, out_s=5)])
    assert clip(plan.bundle)['hold'] == 4
    assert clip(plan.bundle)['hold'] == row['hold']
    assert clip_timeline_duration(clip(plan.bundle)) == pytest.approx(4 / speed)
    assert clip(plan.bundle)['from'] == 1
    assert clip(plan.bundle)['to'] == 5


def test_native_group_replaces_track_ripple_and_compiles_parent_extension():
    original = bundle()
    original['parent']['config']['output'] = {'fps': 30}
    for rows in (original['parent']['clips'], original['parent']['config']['clips']):
        rows[0].update(at=0, **{'from': 3, 'to': 4, 'hold': 1, 'speed': 1})
        rows[1].update(at=1, **{'from': 8, 'to': 9, 'hold': 1, 'speed': 1})
        rows.append({'id': 'music', 'track': 'music', 'asset': 'music', 'clipType': 'media',
                     'at': 0, 'from': 0, 'to': 2, 'hold': 2})
    original['parent']['config']['tracks'].append({'id': 'music', 'kind': 'audio'})
    original['parent']['registry']['assets']['music'] = {
        'media_id': 'sha256:' + '2' * 64, 'type': 'audio', 'duration': 2,
    }
    audio_before = next(row for row in original['parent']['clips'] if row['id'] == 'music').copy()

    group = {'group_id': 'visual-ripple',
             'rationale': 'ripple=track; parent_duration=extend'}
    trim = capture_clip_trim(original, REF, in_s=3, out_s=4.5)
    intermediate = execute(original, [trim]).bundle
    move = capture_move(intermediate, 'parent/clip:clip-b', 1.5)
    trim.update(group)
    move.update(group)
    plan = execute(original, [trim, move])

    rows = {row['id']: row for row in plan.bundle['parent']['clips']}
    assert (rows['clip-a']['at'], rows['clip-a']['from'], rows['clip-a']['to']) == (0, 3.0, 4.5)
    assert (rows['clip-b']['at'], rows['clip-b']['from'], rows['clip-b']['to']) == (1.5, 8, 9)
    assert {command['group_id'] for command in plan.commands} == {'visual-ripple'}
    assert {command['rationale'] for command in plan.commands} == {group['rationale']}
    assert {key: rows['music'][key] for key in ('at', 'from', 'to')} == {
        key: audio_before[key] for key in ('at', 'from', 'to')
    }

    compiled = document_from_bundle(plan.bundle)['parent_revision']['payload']
    assert timeline_duration_seconds(compiled, 30) == pytest.approx(2.5)


def test_native_group_preserves_exact_track_scoped_ripple_timing_and_audio():
    original = bundle()
    original['parent']['config']['output'] = {'fps': 30}
    for rows in (original['parent']['clips'], original['parent']['config']['clips']):
        rows[0].update(at=0, **{'from': 0, 'to': 1, 'hold': 1, 'speed': 1})
        rows[1].update(at=1, **{'from': 0, 'to': 1, 'hold': 1, 'speed': 1})
        rows.append({'id': 'music', 'track': 'music', 'asset': 'music', 'clipType': 'media',
                     'at': 0, 'from': 0, 'to': 2, 'hold': 2})
    original['parent']['config']['tracks'].append({'id': 'music', 'kind': 'audio'})
    original['parent']['registry']['assets']['music'] = {
        'media_id': 'sha256:' + '2' * 64, 'type': 'audio', 'duration': 2,
    }
    assert (clip(original)['at'], clip(original)['from'], clip(original)['to']) == (0, 0, 1)

    trim = capture_clip_trim(original, REF, in_s=0, out_s=1.5)
    intermediate = execute(original, [trim]).bundle
    move = capture_move(intermediate, 'parent/clip:clip-b', 1.5)
    plan = execute(original, [trim, move])

    rows = {row['id']: row for row in plan.bundle['parent']['clips']}
    assert (rows['clip-a']['at'], rows['clip-a']['from'], rows['clip-a']['to']) == (0, 0, 1.5)
    assert (rows['clip-b']['at'], rows['clip-b']['from'], rows['clip-b']['to']) == (1.5, 0, 1)
    assert (rows['music']['at'], rows['music']['from'], rows['music']['to']) == (0, 0, 2)

    compiled = document_from_bundle(plan.bundle)['parent_revision']['payload']
    assert timeline_duration_seconds(compiled, 30) == pytest.approx(2.5)


def test_native_speed_two_preserves_source_window_move_and_endpoint_length():
    original = bundle()
    for rows in (original['parent']['clips'], original['parent']['config']['clips']):
        rows[0].update(at=4, **{'from': 2, 'to': 4, 'hold': 2, 'speed': 2})

    row = clip(original)
    assert (row['at'], row['from'], row['to'], row['speed']) == (4, 2, 4, 2)
    assert 'source_start' not in row and 'source_end' not in row
    assert (clip_start_frame(row, 30), clip_end_frame(row, 30)) == (120, 150)

    inconsistent = copy.deepcopy(original)
    for rows in (inconsistent['parent']['clips'], inconsistent['parent']['config']['clips']):
        rows[0]['at'] = 0
        rows[0]['duration'] = 3
    before_inconsistent = copy.deepcopy(inconsistent)
    with pytest.raises(CommandError, match='unambiguous positive authored duration/window'):
        capture_move(inconsistent, REF, 10)
    assert inconsistent == before_inconsistent

    moved = execute(original, [capture_move(original, REF, 10)])
    assert (clip(moved.bundle)['at'], clip(moved.bundle)['from'],
            clip(moved.bundle)['to'], clip(moved.bundle)['speed']) == (10, 2, 4, 2)
    moved_again = execute(moved.bundle, [capture_move(moved.bundle, REF, 20)])
    assert (clip(moved_again.bundle)['at'], clip(moved_again.bundle)['from'],
            clip(moved_again.bundle)['to'], clip(moved_again.bundle)['speed']) == (20, 2, 4, 2)

    endpoint = execute(moved_again.bundle,
                       [capture_clip_trim(moved_again.bundle, REF, out_s=5)])
    final = clip(endpoint.bundle)
    assert (final['at'], final['from'], final['to'], final['speed']) == (20, 2, 5, 2)
    assert final['at'] + clip_timeline_duration(final) == pytest.approx(21.5)


def test_native_group_preserves_shifted_source_window_and_audio_bytes():
    original = bundle()
    original['parent']['config']['output'] = {'fps': 30}
    for rows in (original['parent']['clips'], original['parent']['config']['clips']):
        rows[0].update(at=0, **{'from': 3, 'to': 4, 'hold': 1, 'speed': 1})
        rows[1].update(at=1, **{'from': 8, 'to': 9, 'hold': 1, 'speed': 1})
        rows.append({'id': 'music', 'track': 'music', 'asset': 'music', 'clipType': 'media',
                     'at': 0, 'from': 0, 'to': 2, 'hold': 2})
    original['parent']['config']['tracks'].append({'id': 'music', 'kind': 'audio'})
    original['parent']['registry']['assets']['music'] = {
        'media_id': 'sha256:' + '2' * 64, 'type': 'audio', 'duration': 2,
    }
    audio_before = copy.deepcopy(next(row for row in original['parent']['clips']
                                     if row['id'] == 'music'))
    assert (clip(original)['at'], clip(original)['from'], clip(original)['to']) == (0, 3, 4)
    assert (next(row for row in original['parent']['clips'] if row['id'] == 'clip-b')['at'],
            next(row for row in original['parent']['clips'] if row['id'] == 'clip-b')['from'],
            next(row for row in original['parent']['clips'] if row['id'] == 'clip-b')['to']) == (1, 8, 9)

    trim = capture_clip_trim(original, REF, in_s=3, out_s=4.5)
    intermediate = execute(original, [trim]).bundle
    move = capture_move(intermediate, 'parent/clip:clip-b', 1.5)
    plan = execute(original, [trim, move])

    rows = {row['id']: row for row in plan.bundle['parent']['clips']}
    assert (rows['clip-a']['at'], rows['clip-a']['from'], rows['clip-a']['to']) == (0, 3, 4.5)
    assert (rows['clip-b']['at'], rows['clip-b']['from'], rows['clip-b']['to']) == (1.5, 8, 9)
    assert rows['music'] == audio_before

    compiled = document_from_bundle(plan.bundle)['parent_revision']['payload']
    assert timeline_duration_seconds(compiled, 30) == pytest.approx(2.5)


def test_windowless_trim_uses_file_not_hold_and_length_helper_stays_windowless():
    from astrid.sdk.timeline.doc import _set_length, _trim_front

    original = bundle(speed=1, hold=3)
    for rows in (original['parent']['clips'], original['parent']['config']['clips']):
        for key in ('from', 'to'):
            rows[0].pop(key)
    for helper in (_set_length, _trim_front):
        row = copy.deepcopy(clip(original))
        helper(row, 1)
        assert 'from' not in row and 'to' not in row
    plan = execute(original, [capture_clip_trim(original, REF, in_s=2)])
    assert (clip(plan.bundle)['from'], clip(plan.bundle)['to'], clip(plan.bundle)['hold']) == (2, 10, 8)
    assert inverse(plan.bundle, plan.records).bundle == original


@pytest.mark.parametrize('app', ['absent', None, {}, {'astrid': None}, {'astrid': {}},
    {'for': 3, 'until': None}, {'astrid': {'for': None, 'until': None}},
    {'for': 8, 'until': None, 'astrid': {'for': 9, 'until': None, 'opaque': False}, 'opaque': 0}])
def test_absent_null_legacy_intent_and_empty_clear_restore_exactly(monkeypatch, app):
    original = bundle(app=app)
    if app == 'absent':
        for rows in (original['parent']['clips'], original['parent']['config']['clips']):
            rows[0].pop('app')
    row = copy.deepcopy(clip(original))
    monkeypatch.setattr(Clip, '_source_duration', lambda self: 10)
    Clip(SimpleNamespace(_is_audio=lambda *args: False), None, row).trim(1, 5)
    plan = execute(original, [capture_clip_trim(original, REF, in_s=1, out_s=5)])
    assert clip(plan.bundle) == row
    assert inverse(plan.bundle, plan.records).bundle == original


def test_rational_fps_source_contact_and_half_open_intervals_are_not_requantized():
    original = bundle(at=.1001, speed=1)
    plan = execute(original, [capture_clip_trim(original, REF, in_s=9.8999, out_s=10)])
    row = clip(plan.bundle)
    assert (row['at'], row['from'], row['to'], row['hold']) == (.1001, 9.8999, 10, .1001)
    assert clip_start_frame(row, 30000 / 1001) == 3
    assert clip_end_frame(row, 30000 / 1001) == 6
    # At the half-open endpoint, the adjacent clip starts with no shared frame.
    adjacent = dict(row, at=.2002)
    assert clip_start_frame(adjacent, 30000 / 1001) == clip_end_frame(row, 30000 / 1001)
    assert clip_timeline_duration(row) == .1001
    assert document_from_bundle(plan.bundle)['parent_revision']['payload']['config']['output'] == {
        'fps': {'numerator': 30000, 'denominator': 1001}}
    assert not any(read['path'] == ['config', 'output', 'fps'] for read in plan.commands[0]['reads'])


def test_retrim_changes_a_stored_hold_but_reading_and_undo_do_not_normalize_it():
    original = bundle(speed=2, hold=17)
    assert clip_timeline_duration(clip(original)) == pytest.approx(8.5)
    plan = execute(original, [capture_clip_trim(original, REF, in_s=1, out_s=5)])
    assert clip(plan.bundle)['hold'] == 4
    assert inverse(plan.bundle, plan.records).bundle == original
    assert clip(inverse(plan.bundle, plan.records).bundle)['hold'] == 17


@pytest.mark.parametrize('args', [{'in_s': -1}, {'out_s': 11}, {'in_s': 6, 'out_s': 6},
    {'in_s': 7, 'out_s': 6}, {'in_s': 0, 'out_s': .0000001}, {'out_s': float('inf')},
    {'in_s': float('nan')}, {'in_s': True}, {'out_s': '4'}, {'out_s': 0},
    {'out_s': 5, 'ripple': True}, {'from': 1}, {'source_duration': 100}])
def test_bad_closed_bounds_are_atomic(args):
    original = bundle()
    before = copy.deepcopy(original)
    command = {'schema_version': 1, 'op': 'clip.trim', 'version': '1', 'select': REF, 'args': args}
    with pytest.raises(CommandError):
        execute(original, [command])
    assert original == before


@pytest.mark.parametrize('changes', [{'time_map': {}}, {'timeMap': []}, {'from_ms': 0},
    {'at_ms': 1000}, {'duration': 3}, {'source_window': {'start': 0, 'end': 10}},
    {'source_offset': 1},
    {'children': []}, {'clipType': 'text'}, {'speed': 0}, {'speed': -1}, {'speed': True},
    {'speed': {'numerator': 2, 'denominator': 1}}, {'at': -1}, {'at': float('inf')},
    {'asset_id': 'picture'}, {'asset': None}, {'hold': float('nan')},
    {'from': float('inf')}, {'to': '10'}, {'anchor': 'beat:1'}, {'animation': []},
    {'params': {'keyframes': []}}, {'app': {'on': 'end'}}, {'app': {'astrid': {'cut': 'c'}}},
    {'app': {'astrid': {'until': 'end'}}}, {'app': {'astrid': {'formulas': {}}}},
    {'app': {'astrid': {'sequence': 's'}}}, {'app': {'astrid': {'until_film_end': True}}},
    {'app': []}, {'app': {'astrid': []}}])
def test_unsupported_shapes_refuse_without_mutation(changes):
    original = bundle(**changes)
    before = copy.deepcopy(original)
    with pytest.raises(CommandError):
        capture_clip_trim(original, REF, in_s=1, out_s=5)
    assert original == before


@pytest.mark.parametrize('ref', ['occurrence:occ-a/clip:clip-a', 'shot:chapter-shot/clip:clip-a',
                                'parent/clip:missing', 'clip-a', 'parent/clip:clip-a::at'])
def test_only_existing_stable_parent_identity_is_supported(ref):
    with pytest.raises(CommandError):
        capture_clip_trim(bundle(), ref, in_s=1, out_s=5)


@pytest.mark.parametrize('key,value', [('duration', None), ('duration', 0), ('duration', float('inf')),
    ('source_window', {'start': 2, 'end': 10}), ('time_map', {}), ('source', {'kind': 'mapped'})])
def test_source_metadata_is_required_and_maps_refused(key, value):
    original = bundle()
    original['parent']['registry']['assets']['picture'][key] = value
    with pytest.raises(CommandError):
        capture_clip_trim(original, REF, out_s=5)


def test_replay_consumed_source_identity_bounds_and_selection_but_preserves_disjoint_data():
    original = bundle()
    plan = execute(original, [capture_clip_trim(original, REF, in_s=1, out_s=5)])
    for path, value in [(['assets', 'picture', 'duration'], 11),
                        (['assets', 'picture', 'media_id'], 'sha256:' + '2' * 64)]:
        head = copy.deepcopy(original)
        State(head).set('parent/registry', path, {'state': 'present', 'value': value})
        with pytest.raises(CommandError):
            replay(plan, head)
    head = copy.deepcopy(original)
    clip(head)['params'] = {'other': [None, False]}
    State(head).entity('parent/clip:clip-b')['opaque'] = {'keep': True}
    head['parent']['config']['clips'] = copy.deepcopy(head['parent']['clips'])
    result = replay(plan, head)
    assert clip(result.bundle)['params'] == {'other': [None, False]}
    assert inverse(result.bundle, result.records).bundle == head
    baseline, output = document_from_bundle(head), document_from_bundle(result.bundle)
    for key in ('shot_revisions', 'internal_timeline_revisions', 'dependency_manifest'):
        assert output[key] == baseline[key]
    assert output['parent_revision']['payload']['registry'] == baseline['parent_revision']['payload']['registry']
    assert output['parent_revision']['payload']['occurrences'] == baseline['parent_revision']['payload']['occurrences']
    assert next(r for r in plan.commands[0]['reads'] if r['path'] == ['id'])['state']['value'] == 'clip-a'
    for key in ('asset', 'track', 'speed', 'hold', 'from', 'to', 'time_map'):
        head = copy.deepcopy(original)
        clip(head)[key] = 'changed'
        with pytest.raises(CommandError):
            replay(plan, head)
    head = copy.deepcopy(original)
    for rows in (head['parent']['clips'], head['parent']['config']['clips']):
        rows.pop(0)
    with pytest.raises(CommandError):
        replay(plan, head)


def test_unchanged_writes_and_original_capture_claims_are_authoritative():
    original = bundle(speed=1, hold=4, app={'org.example': None})
    command = capture_clip_trim(original, REF)
    plan = execute(original, [command])
    assert plan.bundle == original
    assert all(w['before'] == w['after'] for w in plan.records[0]['writes'])
    with pytest.raises(CommandError, match='intervening'):
        inverse(plan.bundle, plan.records, [later(REF, ['hold'], 4)])
    for field, replacement in [('reads', []), ('expect', {}), ('resolved', []), ('bindings', {})]:
        forged = copy.deepcopy(command)
        forged[field] = replacement
        with pytest.raises(CommandError, match=field):
            execute(original, [forged])


def test_portable_golden_and_closed_schema():
    golden = load_fixture(FIXTURE)
    original = bundle_from_document(golden['input'])
    plan = execute(original, golden['commands'])
    assert plan.records == golden['records']
    assert document_from_bundle(plan.bundle) == golden['output']
    schema = json.loads((Path(__file__).parents[2] / 'astrid/core/timeline/schema/edit-command.schema.json').read_text())
    validator = Draft202012Validator(schema)
    validator.validate(plan.commands[0])
    for changes in ({'args': {'out_s': 5, 'extra': None}}, {'select': 'occurrence:occ-a/clip:clip-a'},
                    {'intent': {'anchor': 'follow'}}):
        assert list(validator.iter_errors({**plan.commands[0], **changes}))
    absent_app = execute(bundle(app=None), [capture_clip_trim(bundle(app=None), REF)])
    assert absent_app.records[0]['writes'][-1]['after'] == ABSENT


def test_trim_accepts_static_move_intent_but_refuses_dynamic_follow():
    original = bundle(app={'astrid': {'fixed_start': True, 'for': 2}})
    plan = execute(original, [capture_clip_trim(original, REF, in_s=1, out_s=5)])
    assert clip(plan.bundle)['app']['astrid']['fixed_start'] is True
    assert 'for' not in clip(plan.bundle)['app']['astrid']
    dynamic = bundle(app={'astrid': {'on': 'word:one'}})
    with pytest.raises(CommandError, match='does not support'):
        capture_clip_trim(dynamic, REF, in_s=1, out_s=5)


@pytest.mark.parametrize('case', BOUNDARIES['cases'], ids=lambda case: case['id'])
def test_portable_boundary_vectors_match_model_and_preserve_native_guards(monkeypatch, case):
    # Reproduce App's counterexample from its unchanged original raw fixture;
    # only authoritative duration and its required digest change in the input.
    document = json.loads(FIXTURE.read_text())['input']
    payload = document['parent_revision']['payload']
    payload['registry']['assets']['picture']['duration'] = case['duration']
    document['parent_revision']['content_digest'] = _digest(payload)
    before = copy.deepcopy(document)
    original = bundle_from_document(document)
    row = copy.deepcopy(clip(original))
    model_before = copy.deepcopy(row)
    tl = SimpleNamespace(_is_audio=lambda *args: False, _mcache='unchanged',
                         _registry=lambda *args: payload['registry']['assets'])
    model = Clip(tl, None, row)
    if 'error' in case:
        def unexpected_write(*args):
            pytest.fail('invalid trim must refuse before recording any write')

        monkeypatch.setattr(Recording, 'write', unexpected_write)
        with pytest.raises(CommandError, match=case['error']):
            capture_clip_trim(original, REF, **case['args'])
        with pytest.raises(TimelineEditError, match=case['error']):
            model.trim(**case['args'])
        assert row == model_before and tl._mcache == 'unchanged'
    else:
        model.trim(**case['args'])
        command = capture_clip_trim(original, REF, **case['args'])
        plan = execute(original, [command])
        assert clip(plan.bundle) == row
        assert {key: row[key] for key in case['applied']} == case['applied']
        assert 0 <= row['from'] < row['to'] <= case['duration']
        serialized = json.loads(json.dumps(row, allow_nan=False))
        assert 0 <= serialized['from'] < serialized['to'] <= case['duration']
        assert clip_timeline_duration(row) == pytest.approx(row['hold'] / row['speed'])
        # Exact retry/replay and guarded undo/redo still use the raw source pin.
        assert execute(original, [command]).records == plan.records
        assert replay(plan, original).records == plan.records
        undone = inverse(plan.bundle, plan.records)
        assert undone.bundle == original
        assert inverse(undone.bundle, undone.records).bundle == plan.bundle
        with pytest.raises(CommandError, match='intervening'):
            inverse(plan.bundle, plan.records, [later(REF, ['hold'], row['hold'])])
        forged = copy.deepcopy(command)
        forged['reads'] = []
        with pytest.raises(CommandError, match='reads'):
            execute(original, [forged])
        published = document_from_bundle(plan.bundle)
        output = published['parent_revision']['payload']
        for rows in (output['clips'], output['config']['clips']):
            assert next(item for item in rows if item['id'] == 'clip-a') == row
        assert output['registry'] == payload['registry']
        assert output['occurrences'] == payload['occurrences']
        assert output['config']['output'] == payload['config']['output']
        for key in ('shot_revisions', 'internal_timeline_revisions', 'dependency_manifest'):
            assert published[key] == document[key]
    assert document == before
    assert original == bundle_from_document(before)
