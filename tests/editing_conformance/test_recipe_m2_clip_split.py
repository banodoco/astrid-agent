"""Literal source halves and stable insertion through native recorded inverses."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from astrid.core.timeline.duration import (
    clip_end_frame,
    clip_start_frame,
    clip_timeline_duration,
    timeline_duration_frames,
)
from astrid.sdk.timeline._collections import order, restoration_orders
from astrid.sdk.timeline._command_state import ABSENT, CommandError, State
from astrid.sdk.timeline._recording import Recording
from astrid.sdk.timeline._scalar_commands import capture_clip_set_params
from astrid.sdk.timeline._split_commands import capture_clip_split
from astrid.sdk.timeline.commands import execute, validate_command
from astrid.sdk.timeline.commit import (
    _digest,
    _manifest,
    bundle_from_document,
    document_from_bundle,
)
from astrid.sdk.timeline.rebase import replay
from tests.editing_conformance.fixture_loader import load_fixture
from tests.editing_conformance.test_recipe_m1_commands import accepted_boundaries, inverse, later
from tests.sdk.test_timeline_commit_provider_document import MEDIA, document_fixture

FIXTURE = Path(__file__).parent / 'fixtures' / 'recipe_m2_clip_split.json'
TARGET = 'parent/clip:clip-a'
RIGHT = 'parent/clip:right'


def source_document(*, hold=False, literal_for=False, source_type='video', shared=False):
    document = document_fixture(shared=shared)
    payload = document['parent_revision']['payload']
    row = payload['clips'][0]
    row.update(at=1, **{'from': 2, 'to': 10, 'speed': 2},
               opaque={'nested': [None, False, 0, {}, []], 'literal': 'clip-a'},
               params={'volume': .7, 'unknown': [None, False]})
    if hold:
        row['hold'] = 8
    else:
        row.pop('hold')
    if literal_for:
        row['app']['astrid'] = {'for': 4, 'opaque': {'for': 'literal'}}
    payload['config']['clips'] = copy.deepcopy(payload['clips'])
    payload['registry'] = copy.deepcopy(payload['registry'])
    payload['registry']['assets']['picture'].update(type=source_type, duration=20)
    if not shared:
        payload['occurrences'] = []
        document['shot_revisions'] = []
        document['internal_timeline_revisions'] = []
        document['dependency_manifest'] = _manifest([], [], [{'media_id': MEDIA, 'content_digest': MEDIA}], [])
    document['parent_revision']['content_digest'] = _digest(payload)
    return document


def split(original, *, on=3, right_id='right'):
    return execute(original, [capture_clip_split(original, TARGET, on=on, right_id=right_id)])


def assert_refusal_unchanged(bundle, call, *, kind=None):
    before = copy.deepcopy(bundle)
    with pytest.raises(CommandError) as error:
        call()
    if kind is not None:
        assert error.value.kind == kind
    assert bundle == before


@pytest.mark.parametrize('hold', [False, True])
@pytest.mark.parametrize('literal_for', [False, True])
@pytest.mark.parametrize('source_type', ['video', 'audio', 'image'])
def test_exact_source_halves_speed_literal_end_and_half_open_frames(hold, literal_for, source_type):
    original = bundle_from_document(source_document(hold=hold, literal_for=literal_for, source_type=source_type))
    plan = split(original)
    old = State(original).entity(TARGET)
    left, right, neighbor = plan.bundle['parent']['clips']
    assert [row['id'] for row in (left, right, neighbor)] == ['clip-a', 'right', 'clip-b']
    assert (left['from'], left['to'], right['from'], right['to']) == (2, 6, 6, 10)
    assert left['speed'] == right['speed'] == old['speed'] == 2
    assert left['at'] == old['at'] == 1
    assert right['at'] == 3
    assert right['at'] + clip_timeline_duration(right) == old['at'] + clip_timeline_duration(old) == 5
    assert clip_end_frame(left, 30000 / 1001) == clip_start_frame(right, 30000 / 1001)
    assert clip_end_frame(right, 30000 / 1001) == clip_end_frame(old, 30000 / 1001)
    assert timeline_duration_frames(plan.bundle['parent'], 30000 / 1001) == timeline_duration_frames(original['parent'], 30000 / 1001)
    for half in (left, right):
        assert half['opaque'] == old['opaque']
        assert half['params'] == old['params']
        assert ('hold' in half) is hold
        if hold:
            assert half['hold'] == 4
        if literal_for:
            assert half['app']['astrid'] == {'for': 2, 'opaque': {'for': 'literal'}}
    assert neighbor == State(original).entity('parent/clip:clip-b')
    assert original['source_mapping'] == plan.bundle['source_mapping']
    assert original['parent']['registry'] == plan.bundle['parent']['registry']


@pytest.mark.parametrize('position', [1, 5, -1, 0, 6, 1.00001, 4.99999, True, None, float('nan'), float('inf')])
def test_bounds_and_subframe_halves_refuse_atomically(position):
    original = bundle_from_document(source_document())
    assert_refusal_unchanged(original, lambda: split(original, on=position))


def test_compiled_reload_inverse_exact_row_absence_opaque_order_and_redo_same_ids():
    original = bundle_from_document(source_document(hold=True, literal_for=True, shared=True))
    plan = split(original)
    document = document_from_bundle(plan.bundle)
    reloaded = bundle_from_document(document)
    undone = inverse(reloaded, plan.records)
    restored = document_from_bundle(undone.bundle)
    baseline = document_from_bundle(original)
    # Parent revision name needs its accepted input closure; authored content does not.
    assert restored['parent_revision']['payload'] == baseline['parent_revision']['payload']
    assert restored['parent_revision']['content_digest'] == baseline['parent_revision']['content_digest']
    for key in ('shot_revisions', 'internal_timeline_revisions', 'dependency_manifest'):
        assert document[key] == restored[key] == baseline[key]
    assert order(State(undone.bundle), 'parent', 'clip') == [TARGET, 'parent/clip:clip-b']
    assert State(undone.bundle).get(RIGHT, []) == ABSENT
    redone = inverse(bundle_from_document(restored), undone.records)
    assert document_from_bundle(redone.bundle) == document
    assert State(redone.bundle).entity(RIGHT) == State(plan.bundle).entity(RIGHT)
    accepted = accepted_boundaries(redone.records, undone.bundle, redone.bundle)
    assert document_from_bundle(inverse(redone.bundle, accepted).bundle)['parent_revision']['payload'] == baseline['parent_revision']['payload']
    assert_refusal_unchanged(redone.bundle, lambda: inverse(redone.bundle, redone.records), kind='history_unavailable')
    assert replay(plan, original).records == plan.records


def test_authoritative_target_order_and_created_id_claims():
    original = bundle_from_document(source_document())
    plan = split(original)
    command = plan.commands[0]
    assert command['resolved'] == [TARGET]
    assert command['created'] == [RIGHT]
    selected = next(s for s in command['selections'] if s['key'].endswith('.after'))
    assert selected == {'key': 'collection.clips.after', 'query_version': 1,
                        'owner': 'parent', 'members': [TARGET],
                        'order': [TARGET, 'parent/clip:clip-b'],
                        'boundaries': [TARGET, 'parent/clip:clip-b'], 'policy': 'guard-membership'}
    assert restoration_orders(plan.records) == {('parent', 'clip'): (
        [TARGET, 'parent/clip:clip-b'], [TARGET, RIGHT, 'parent/clip:clip-b'])}
    assert all(not (read['ref'] == 'parent' and read['path'] == []) for read in command['reads'])
    for field, value in [('created', ['parent/clip:other']), ('resolved', ['parent/clip:clip-b']),
                         ('bindings', {'split.target': 'parent/clip:clip-b'})]:
        changed = copy.deepcopy(command)
        changed[field] = value
        assert_refusal_unchanged(original, lambda: execute(original, [changed]))
    changed = copy.deepcopy(command)
    changed['selections'][-1]['boundaries'] = [TARGET]
    assert_refusal_unchanged(original, lambda: execute(original, [changed]), kind='selection_changed')
    head = copy.deepcopy(original)
    State(head).collection('parent', 'clip').reverse()
    head['parent']['config']['clips'].reverse()
    assert_refusal_unchanged(head, lambda: replay(plan, head), kind='selection_changed')


def test_aba_dependent_edits_collision_and_missing_history_refuse_atomically():
    original = bundle_from_document(source_document())
    plan = split(original)
    aba = [later(TARGET, ['to'], 8), later(TARGET, ['to'], 6)]
    assert_refusal_unchanged(plan.bundle, lambda: inverse(plan.bundle, plan.records, aba), kind='intervening_write')
    assert replay(plan, original, intervening_records=aba).records == plan.records
    assert_refusal_unchanged(plan.bundle, lambda: inverse(plan.bundle, plan.records, None), kind='history_unavailable')
    assert_refusal_unchanged(plan.bundle, lambda: inverse(plan.bundle, []), kind='history_unavailable')
    dependent = execute(plan.bundle, [capture_clip_set_params(plan.bundle, RIGHT, values={'volume': .2})])
    assert_refusal_unchanged(dependent.bundle, lambda: inverse(dependent.bundle, plan.records, dependent.records), kind='intervening_write')
    # A preexisting neighbor starts following the new right half after the split.
    head = copy.deepcopy(plan.bundle)
    for rows in (head['parent']['clips'], head['parent']['config']['clips']):
        rows[-1]['app']['astrid'] = {'on': 'right'}
    assert_refusal_unchanged(head, lambda: inverse(head, plan.records))
    assert_refusal_unchanged(head, lambda: inverse(head, plan.records, [later(
        'parent/clip:clip-b', ['app', 'astrid', 'on'], 'right')]), kind='intervening_write')
    assert_refusal_unchanged(original, lambda: split(original, right_id='clip-b'), kind='identity_collision')
    assert_refusal_unchanged(original, lambda: split(original, right_id='clip-a'), kind='identity_collision')
    assert_refusal_unchanged(plan.bundle, lambda: replay(plan, plan.bundle), kind='identity_collision')


def test_disjoint_neighbor_property_preserved_on_rebase_and_inverse():
    original = bundle_from_document(source_document())
    plan = split(original)
    edit = execute(original, [capture_clip_set_params(original, 'parent/clip:clip-b', values={'other': [None, False]})])
    rebased = replay(plan, edit.bundle, intervening_records=edit.records)
    assert inverse(rebased.bundle, plan.records, edit.records).bundle == edit.bundle
    head_edit = execute(plan.bundle, [capture_clip_set_params(plan.bundle, 'parent/clip:clip-b', values={'other': 7})])
    undone = inverse(head_edit.bundle, plan.records, head_edit.records)
    assert State(undone.bundle).entity('parent/clip:clip-b')['params']['other'] == 7


@pytest.mark.parametrize('field,value', [
    ('time_map', {}), ('transition', 'fade'), ('effects', []), ('enter', {'type': 'fade'}),
    ('keyframes', {}), ('animation', {}), ('from', True), ('from', None), ('to', None),
    ('speed', 0), ('speed', False), ('hold', 7), ('asset', 'missing'),
    ('app', {'on': 'word:abc'}), ('app', {'astrid': {'until': 'end'}}),
    ('app', {'astrid': {'for': 2}}), ('app', {'astrid': {'sequence': {}}}),
    ('app', {'astrid': {'words': [[0, 1, 'word']]}}),
    ('params', {'keyframes': []}), ('params', {'volume_curve': {}}),
])
def test_recognized_unsupported_shapes_refuse_without_mutation(field, value):
    original = bundle_from_document(source_document())
    for rows in (original['parent']['clips'], original['parent']['config']['clips']):
        rows[0][field] = copy.deepcopy(value)
    assert_refusal_unchanged(original, lambda: split(original))


def test_image_hold_only_and_legacy_literal_for_presence():
    original = bundle_from_document(source_document(source_type='image'))
    for rows in (original['parent']['clips'], original['parent']['config']['clips']):
        rows[0].pop('from')
        rows[0].pop('to')
        rows[0]['hold'] = 8
        rows[0]['app']['for'] = 4
        rows[0]['app']['astrid'] = {'for': None, 'opaque': []}
    plan = split(original)
    for row in plan.bundle['parent']['clips'][:2]:
        assert row['hold'] == 4
        assert row['app']['for'] == 2
        assert row['app']['astrid'] == {'for': None, 'opaque': []}
        assert 'from' not in row and 'to' not in row
    assert inverse(plan.bundle, plan.records).bundle == original
    for rows in (original['parent']['clips'], original['parent']['config']['clips']):
        rows[0]['clipType'] = 'text'
    assert_refusal_unchanged(original, lambda: split(original))


def test_split_accepts_the_default_main_registry_fixture():
    document = document_fixture(shared=False)
    payload = document['parent_revision']['payload']
    for rows in (payload['clips'], payload['config']['clips']):
        rows[0].update(at=1, **{'from': 2, 'to': 10, 'speed': 2})
        rows[0].pop('hold', None)
    payload['registry'] = copy.deepcopy(payload['registry'])
    payload['registry']['assets']['picture']['duration'] = 20
    document['parent_revision']['content_digest'] = _digest(payload)
    plan = split(bundle_from_document(document))
    assert [row['id'] for row in plan.bundle['parent']['clips'][:2]] == ['clip-a', 'right']


@pytest.mark.parametrize('source_type', ['video/mp4', 'audio/wav', 'image/png'])
def test_split_uses_main_registry_mime_kind_normalizer(source_type):
    original = bundle_from_document(source_document(source_type=source_type))
    plan = split(original)
    assert plan.bundle['parent']['clips'][1]['asset'] == 'picture'


def test_native_move_split_mime_fixture_has_no_renderer_or_legacy_writer_imports():
    import os
    import subprocess
    import sys

    source = '''
import importlib.abc, json, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.startswith((
                'astrid.packs.rendering', 'astrid.core.rendering', 'astrid.sdk.authoring_render_preview',
                'astrid.core.timeline.compatibility', 'composition', 'runtime_protocol',
                'astrid.sdk.timeline.edits', 'astrid.sdk.timeline.model', 'astrid.sdk.timeline.journal')):
            raise AssertionError('native save imported ' + fullname)
sys.meta_path.insert(0, Block())
from astrid.sdk.timeline._split_commands import capture_clip_split
from astrid.sdk.timeline.capture import capture_move
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import bundle_from_document
document = json.load(sys.stdin)
original = bundle_from_document(document)
moved = execute(original, [capture_move(original, 'parent/clip:clip-a', 3.003)]).bundle
plan = execute(moved, [capture_clip_split(moved, 'parent/clip:clip-a', on=4, right_id='right')])
assert [row['id'] for row in plan.bundle['parent']['clips'][:2]] == ['clip-a', 'right']
assert plan.bundle['parent']['registry']['assets']['picture']['type'] == 'video/mp4'
'''
    result = subprocess.run(
        [sys.executable, '-c', source],
        input=json.dumps(source_document(source_type='video/mp4')),
        env={**os.environ, 'PYTHONPATH': str(Path(__file__).parents[2]), 'PYTHONDONTWRITEBYTECODE': '1'},
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr


def test_split_preserves_static_move_intent_and_splits_literal_for():
    from astrid.sdk.timeline.capture import capture_move

    original = bundle_from_document(source_document())
    moved = execute(original, [capture_move(original, TARGET, 3.003)]).bundle
    plan = split(moved, on=4, right_id='right')
    left, right = plan.bundle['parent']['clips'][:2]
    assert left['app']['astrid']['fixed_start'] is True
    assert right['app']['astrid']['fixed_start'] is True
    assert left['app']['astrid']['for'] + right['app']['astrid']['for'] == pytest.approx(4)


@pytest.mark.parametrize('change', [
    {'args': {'on': 3}}, {'args': {'on': 3, 'right_id': 'r', 'index': 1}},
    {'args': {'on': 3, 'right_id': 'bad/id'}}, {'args': {'on': 3, 'right_id': 'bad::id'}},
    {'select': 'occurrence:occ-a/clip:clip-a'}, {'select': 'parent/clip:bad::id'},
    {'version': '2'}, {'intent': {'ripple': 'selected'}},
])
def test_closed_parameters_and_parent_stable_select(change):
    command = {'schema_version': 1, 'op': 'clip.split', 'version': '1',
               'select': TARGET, 'args': {'on': 3, 'right_id': 'right'}, **change}
    with pytest.raises(CommandError):
        validate_command(command)


@pytest.mark.parametrize('kind', ['clip', 'track'])
@pytest.mark.parametrize('predecessor', ['clip-a', 'clip-b'])
def test_create_after_is_generic_native_recording_not_a_split_undo(kind, predecessor):
    original = bundle_from_document(source_document())
    if kind == 'track':
        original['parent']['config']['tracks'] = [{'id': 'clip-a', 'kind': 'visual'}, {'id': 'clip-b', 'kind': 'audio'}]
    ctx = Recording(copy.deepcopy(original), {'op': 'fixture.create'}, 0)
    row = {'id': 'right', 'kind': 'visual', 'at': 9, 'hold': 1, 'opaque': [None, False]}
    ctx.create('parent', kind, row, after='parent/' + kind + ':' + predecessor)
    ctx.compile_mirrors()
    record = ctx.finish()
    expected = ['clip-a', 'right', 'clip-b'] if predecessor == 'clip-a' else ['clip-a', 'clip-b', 'right']
    assert order(ctx.state, 'parent', kind) == ['parent/' + kind + ':' + identity for identity in expected]
    assert inverse(ctx.state.bundle, [record]).bundle == original
    undone = inverse(ctx.state.bundle, [record])
    assert inverse(undone.bundle, undone.records).bundle == ctx.state.bundle


def test_portable_golden_capture_and_accepted_record_output():
    golden = load_fixture(FIXTURE)
    original = bundle_from_document(golden['input'])
    plan = execute(original, golden['commands'])
    assert plan.records == golden['records']
    assert document_from_bundle(plan.bundle) == golden['output']
    assert plan.commands[0] == capture_clip_split(original, TARGET, on=3, right_id='right')


@pytest.mark.parametrize('hold', [False, True])
def test_fractional_speed_and_precise_source_end_are_not_rounded(hold):
    original = bundle_from_document(source_document(hold=hold))
    for rows in (original['parent']['clips'], original['parent']['config']['clips']):
        rows[0].update(speed=1.3, **{'from': 2.123456789, 'to': 10.987654321})
        if hold:
            rows[0]['hold'] = 10.987654321 - 2.123456789
    plan = split(original)
    left, right = plan.bundle['parent']['clips'][:2]
    assert left['to'] == right['from'] == 2.123456789 + 2 * 1.3
    assert right['to'] == 10.987654321
    old = State(original).entity(TARGET)
    assert right['at'] + clip_timeline_duration(right) == pytest.approx(old['at'] + clip_timeline_duration(old), abs=1e-12)
    assert inverse(plan.bundle, plan.records).bundle == original


@pytest.mark.parametrize('damage', ['missing', 'duplicate', 'mirrors', 'transition', 'moving-hold', 'fade'])
def test_bad_identity_mirrors_neighbor_transition_and_unknown_source_window_refuse(damage):
    original = bundle_from_document(source_document())
    if damage == 'mirrors':
        original['parent']['config']['clips'][0]['at'] = 9
    else:
        for rows in (original['parent']['clips'], original['parent']['config']['clips']):
            if damage == 'missing':
                rows.pop(0)
            elif damage == 'duplicate':
                rows.append(copy.deepcopy(rows[0]))
            elif damage == 'transition':
                rows[-1]['transition'] = 'crossfade'
            elif damage == 'fade':
                rows[0]['params']['fadeIn'] = 1
            else:
                rows[0].pop('from')
                rows[0].pop('to')
                rows[0]['hold'] = 8
    assert_refusal_unchanged(original, lambda: split(original))


def test_order_aba_and_missing_or_corrupt_insertion_proof_refuse():
    original = bundle_from_document(source_document())
    plan = split(original)
    aba = [later('parent', ['config', 'clips'], []), later('parent', ['config', 'clips'], original['parent']['clips'])]
    assert_refusal_unchanged(plan.bundle, lambda: inverse(plan.bundle, plan.records, aba), kind='intervening_write')
    for damage in ('missing', 'boundary', 'members'):
        records = copy.deepcopy(plan.records)
        if damage == 'missing':
            records[0]['selections'] = []
        else:
            selected = next(s for s in records[0]['selections'] if s['key'].endswith('.after'))
            selected[damage if damage == 'members' else 'boundaries'] = []
        assert_refusal_unchanged(plan.bundle, lambda: inverse(plan.bundle, records))


def test_create_after_rejects_foreign_missing_anchor_and_mixed_creations():
    original = bundle_from_document(source_document())
    for anchor in ('parent/clip:missing', 'occurrence:occ-a/clip:clip-a', 0):
        ctx = Recording(copy.deepcopy(original), {'op': 'fixture.create'}, 0)
        assert_refusal_unchanged(ctx.state.bundle, lambda: ctx.create(
            'parent', 'clip', {'id': 'right'}, after=anchor))
    ctx = Recording(copy.deepcopy(original), {'op': 'fixture.create'}, 0)
    ctx.create('parent', 'track', {'id': 'first'}, after='parent/track:picture')
    assert_refusal_unchanged(ctx.state.bundle, lambda: ctx.create('parent', 'track', {'id': 'second'}))


def test_null_hold_speed_and_intent_stay_present_through_reload_inverse_redo():
    document = source_document()
    payload = document['parent_revision']['payload']
    for rows in (payload['clips'], payload['config']['clips']):
        rows[0].update(hold=None, speed=None, params=None)
        rows[0]['app']['astrid'] = {'for': None, 'on': None, 'empty': {}, 'list': []}
    document['parent_revision']['content_digest'] = _digest(payload)
    original = bundle_from_document(document)
    plan = split(original, on=5)
    for row in plan.bundle['parent']['clips'][:2]:
        assert row['hold'] is row['speed'] is row['params'] is None
        assert row['app']['astrid'] == State(original).entity(TARGET)['app']['astrid']
    reloaded = bundle_from_document(document_from_bundle(plan.bundle))
    undone = inverse(reloaded, plan.records)
    assert document_from_bundle(undone.bundle)['parent_revision']['payload'] == payload
    assert document_from_bundle(inverse(undone.bundle, undone.records).bundle) == document_from_bundle(plan.bundle)


@pytest.mark.parametrize('kind', ['clip', 'track'])
def test_generic_create_after_proof_is_scoped_to_an_occurrence(kind):
    original = bundle_from_document(source_document(shared=True))
    ctx = Recording(copy.deepcopy(original), {'op': 'fixture.create'}, 0)
    owner = 'occurrence:occ-a'
    predecessor = owner + '/' + kind + ':' + ('clip-a' if kind == 'clip' else 'picture')
    ctx.create(owner, kind, {'id': 'right', 'track': 'picture', 'at': 1, 'hold': 1}, after=predecessor)
    assert order(ctx.state, owner, kind)[1] == owner + '/' + kind + ':right'
    record = ctx.finish()
    undone = inverse(ctx.state.bundle, [record])
    assert undone.bundle == original
    assert inverse(undone.bundle, undone.records).bundle == ctx.state.bundle
