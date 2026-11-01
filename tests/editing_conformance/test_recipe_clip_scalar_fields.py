"""Bundle 2: exact raw fields from the original App three-products journeys."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from astrid.core.timeline.authoring_bundle import compile_authoring_candidate, open_authoring_bundle
from astrid.sdk.timeline._clip_scalar_commands import capture_clip_set
from astrid.sdk.timeline._command_state import ABSENT, CommandError, State, present
from astrid.sdk.timeline._scalar_commands import capture_clip_set_params
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.rebase import replay
from tests.editing_conformance.fixture_loader import load_fixture
from tests.editing_conformance.test_recipe_m1_commands import bundle, clip, inverse, later
from tests.timeline.test_authoring_bundle import OLD, _closure, _digest

FIXTURES = Path(__file__).parent / 'fixtures'
CHILD = 'occurrence:f2-occurrence/clip:f2-agent-clip'
CLEAR = 'occurrence:f2-occurrence/clip:f2-agent-clear-clip'
PARENT = 'parent/clip:f2-transition-clip'
REF = 'occurrence:occ-1/clip:picture-1'


def app_closure():
    # Exact raw rows from App tests/e2e/local/support/harness.ts seedThreeProducts.
    parent, shots, internals = _closure(shared=True)
    parent_rows = [
        {'id': 'f2-app-clip', 'at': 0, 'track': 'V1', 'clipType': 'media',
         'asset': 'f2-app', 'from': 0, 'to': 3, 'speed': 1},
        {'id': 'f2-transition-clip', 'at': 15, 'track': 'V1', 'clipType': 'media',
         'asset': 'f2-video', 'from': 0, 'to': 3, 'speed': 1,
         'transition': {'type': 'crossfade', 'duration': 0.5}},
    ]
    child_rows = [
        {'id': 'f2-plate', 'at': 0, 'track': 'plate', 'clipType': 'media', 'asset': 'f2-plate', 'hold': 12},
        {'id': 'f2-agent-clip', 'at': 3, 'track': 'V2', 'clipType': 'media',
         'asset': 'f2-placeholder', 'hold': 3,
         'app': {'f2Opaque': {'owner': 'f2', 'nested': [1, {'k': 'v'}]}}},
        {'id': 'f2-agent-clear-clip', 'at': 6, 'track': 'V2', 'clipType': 'media',
         'asset': 'f2-clear', 'hold': 3},
    ]
    parent['payload']['clips'] = copy.deepcopy(parent_rows)
    parent['payload']['config'].update(clips=copy.deepcopy(parent_rows), output={'fps': 30},
        tracks=[{'id': 'V1', 'kind': 'visual', 'label': 'V1'}, {'id': 'S1', 'kind': 'visual', 'label': 'S1'}],
        x_f2_opaque={'owner': 'f2', 'nested': [1, {'k': 'v'}], 'flag': True})
    parent['payload']['registry']['assets'] = {
        key: {'media_id': OLD, 'content_sha256': OLD,
              'file': key + ('.mp4' if key == 'f2-video' else '.png'),
              'type': 'video/mp4' if key == 'f2-video' else 'image/png',
              **({'duration': 12} if key == 'f2-video' else {})}
        for key in ('f2-app', 'f2-video')}
    for index, row in enumerate(parent['payload']['occurrences']):
        row.update(occurrence_id='f2-occurrence' if index == 0 else 'shared-sibling',
                   placement={'start_ms': index * 20000}, source_offset=0,
                   duration_ms=12000, speed=1, track='S1')
    internals[0]['payload'].update(clips=child_rows, tracks=[
        {'id': 'plate', 'kind': 'visual', 'label': 'plate'}, {'id': 'V2', 'kind': 'visual', 'label': 'V2'}])
    internals[0]['payload']['registry']['assets'] = {
        key: {'file': key + '.png', 'type': 'image/png', 'media_id': OLD, 'content_sha256': OLD}
        for key in ('f2-plate', 'f2-placeholder', 'f2-clear')}
    for image in [parent, *shots, *internals]:
        image['content_digest'] = _digest(image['payload'])
    return parent, shots, internals


def app_bundle():
    parent, shots, internals = app_closure()
    return open_authoring_bundle(parent, shot_revisions=shots, internal_timeline_revisions=internals)


def app_calls(original):
    return [capture_clip_set(original, CHILD, values={'hold': 4}),
            capture_clip_set(original, CLEAR, clear=['asset']),
            capture_clip_set(original, PARENT, clear=['transition'])]


def test_original_app_journey_fields_exact_capture_roundtrip_and_portable_golden():
    original = app_bundle()
    plan = execute(original, app_calls(original))
    assert clip(plan.bundle, CHILD)['hold'] == 4
    assert clip(plan.bundle, CLEAR) == {k: v for k, v in clip(original, CLEAR).items() if k != 'asset'}
    assert clip(plan.bundle, PARENT) == {k: v for k, v in clip(original, PARENT).items() if k != 'transition'}
    assert State(plan.bundle).config('occurrence:shared-sibling') == State(original).config('occurrence:shared-sibling')
    assert plan.bundle['source_mapping'] == original['source_mapping']
    assert inverse(plan.bundle, plan.records).bundle == original
    assert inverse(inverse(plan.bundle, plan.records).bundle,
                   inverse(plan.bundle, plan.records).records).bundle == plan.bundle
    assert replay(plan, original).records == plan.records
    compile_authoring_candidate(plan.bundle)
    fixture = load_fixture(FIXTURES / 'recipe_m2_app_clip_scalar_fields.json')
    assert fixture == {'bundle': original, 'commands': plan.commands, 'records': plan.records,
                       'after_bundle': plan.bundle}
    assert execute(fixture['bundle'], fixture['commands']).bundle == fixture['after_bundle']


@pytest.mark.parametrize('owner', ['parent', 'occurrence:occ-1'])
@pytest.mark.parametrize('field', ['asset', 'transition'])
@pytest.mark.parametrize('presence', [ABSENT, present(''), present(None), present({'opaque': [None, 0]})])
def test_explicit_clear_retains_tagged_previous_presence_and_full_row(owner, field, presence):
    original = bundle()
    ref = owner + '/clip:' + ('direct' if owner == 'parent' else 'picture-1')
    State(original).set(ref, [field], presence)
    if owner == 'parent':
        original['parent']['config']['clips'] = copy.deepcopy(original['parent']['clips'])
    before = copy.deepcopy(original)
    plan = execute(original, [capture_clip_set(original, ref, clear=[field])])
    assert original == before
    assert field not in clip(plan.bundle, ref)
    assert plan.records[0]['writes'] == [{'ref': ref, 'path': [field], 'before': presence, 'after': ABSENT}]
    assert inverse(plan.bundle, plan.records).bundle == original
    undone = inverse(plan.bundle, plan.records)
    assert inverse(undone.bundle, undone.records).bundle == plan.bundle


@pytest.mark.parametrize('value', [None, '', False, 0, -1, float('inf'), float('nan')])
def test_hold_set_requires_positive_finite_source_seconds(value):
    with pytest.raises(CommandError):
        capture_clip_set(bundle(), REF, values={'hold': value})


@pytest.mark.parametrize('values,clear', [({'hold': 2}, ['hold']), ({'asset': 'anything'}, []),
    ({'asset': ''}, []), ({'asset': None}, []), ({'transition': None}, []),
    ({'transition': {'type': 'crossfade', 'duration': 0.5}}, []), ({'params': {}}, []),
    ({}, ['speed']), ({}, ['hold', 'hold']), ({}, [])])
def test_closed_bounded_fields_and_disjoint_set_clear(values, clear):
    with pytest.raises(CommandError):
        capture_clip_set(bundle(), REF, values=values, clear=clear)


@pytest.mark.parametrize('fps', [30, {'numerator': 30000, 'denominator': 1001}])
def test_source_hold_speed_two_rational_fps_boundary_and_no_implicit_rewrites(fps):
    original = bundle(fps=fps)
    row = clip(original, REF)
    row.update(at=1, hold=2, speed=2, opaque={'null': None, 'order': [False, {}, 0]})
    plan = execute(original, [capture_clip_set(original, REF, values={'hold': 18})])
    assert clip(plan.bundle, REF) == dict(row, hold=18)
    assert inverse(plan.bundle, plan.records).bundle == original
    assert plan.records[0]['writes'] == [{'ref': REF, 'path': ['hold'], 'before': present(2), 'after': present(18)}]
    for hold in (18.01, 20):
        with pytest.raises(CommandError) as error:
            capture_clip_set(original, REF, values={'hold': hold})
        assert error.value.kind == 'occurrence_boundary'
    head = copy.deepcopy(original)
    head['placements'][1]['placement']['start_ms'] = 19000
    with pytest.raises(CommandError):
        execute(head, plan.commands)


def test_clear_hold_uses_surviving_source_window_and_clear_set_batch_inverse():
    original = bundle()
    clip(original, REF).update({'from': 1, 'to': 5, 'speed': 2})
    cleared = execute(original, [capture_clip_set(original, REF, clear=['hold'])])
    assert 'hold' not in clip(cleared.bundle, REF)
    assert inverse(cleared.bundle, cleared.records).bundle == original
    set_again = capture_clip_set(cleared.bundle, REF, values={'hold': 3})
    plan = execute(original, [cleared.commands[0], set_again])
    undone = inverse(plan.bundle, plan.records)
    assert undone.bundle == original
    assert inverse(undone.bundle, undone.records).bundle == plan.bundle
    with pytest.raises(CommandError):
        capture_clip_set(bundle(), REF, clear=['hold'])


@pytest.mark.parametrize('change', ['type', 'element', 'media', 'owner', 'source', 'speed', 'extent', 'map', 'intent'])
def test_original_basis_and_consumed_timing_refuse_changes_atomically(change):
    original = bundle()
    command = capture_clip_set(original, REF, values={'hold': 2})
    head = copy.deepcopy(original)
    if change in ('type', 'element', 'speed'):
        clip(head, REF)[{'type': 'clipType', 'element': 'elementRef', 'speed': 'speed'}[change]] = 2
    elif change == 'media':
        State(head).entity('occurrence:occ-1/registry')['assets']['old-picture']['media_id'] = 'different'
    elif change == 'owner':
        head['placements'][0]['shot_id'] = head['placements'][1]['shot_id']
    elif change == 'source':
        source = head['placements'][0]['shot_id']
        head['source_mapping']['shots'][source]['shot_id'] = 'different'
    elif change in ('extent', 'map'):
        head['placements'][0]['duration_ms' if change == 'extent' else 'source_offset'] = 3
    else:
        clip(head, REF)['app'] = {'astrid': {'for': 1}}
    snapshot = copy.deepcopy(head)
    with pytest.raises(CommandError):
        execute(head, [command])
    assert head == snapshot


def test_disjoint_rebase_unchanged_write_history_aba_and_inverse():
    original = bundle()
    plan = execute(original, [capture_clip_set(original, REF, values={'hold': 1})])
    assert plan.bundle == original
    other = execute(original, [capture_clip_set_params(original, REF, values={'opaque': [None, 0]})])
    rebased = replay(plan, other.bundle, intervening_records=other.records)
    assert inverse(rebased.bundle, rebased.records).bundle == other.bundle
    for history in ([later(REF, ['hold'], 1)], [later(REF, ['hold'], 2), later(REF, ['hold'], 1)]):
        assert replay(plan, original, intervening_records=history).records == plan.records
        with pytest.raises(CommandError, match='intervening'):
            inverse(plan.bundle, plan.records, history)


@pytest.mark.parametrize('key,value', [('asset_id', 'other'), ('media_id', None), ('object_id', 'other')])
def test_asset_clear_refuses_exposing_an_alternate_selector(key, value):
    original = bundle()
    clip(original, REF)[key] = value
    with pytest.raises(CommandError, match='another media selector'):
        capture_clip_set(original, REF, clear=['asset'])


def test_selected_content_sha_and_opaque_registry_siblings_are_independent():
    original = bundle()
    registry = 'occurrence:occ-1/registry'
    State(original).entity(registry)['assets']['old-picture']['content_sha256'] = 'a' * 64
    plan = execute(original, [capture_clip_set(original, REF, clear=['asset'])])
    head = copy.deepcopy(original)
    State(head).entity(registry)['assets']['old-picture']['content_sha256'] = 'b' * 64
    with pytest.raises(CommandError):
        replay(plan, head)
    head = copy.deepcopy(original)
    State(head).entity(registry)['assets']['voice']['content_sha256'] = 'b' * 64
    assert replay(plan, head).commands == plan.commands


def test_parent_hold_and_raw_schema_null_empty_absent_are_distinct():
    from jsonschema import Draft7Validator

    original = bundle()
    ref = 'parent/clip:direct'
    clip(original, ref)['speed'] = 2
    original['parent']['config']['clips'] = copy.deepcopy(original['parent']['clips'])
    plan = execute(original, [capture_clip_set(original, ref, values={'hold': 2.5})])
    assert clip(plan.bundle, ref) == dict(clip(original, ref), hold=2.5)
    assert inverse(plan.bundle, plan.records).bundle == original
    schema = json.loads((Path(__file__).parents[2] / 'astrid/core/timeline/schema/timeline-config.schema.json').read_text())
    validator = Draft7Validator({'$ref': '#/definitions/TimelineClip', 'definitions': schema['definitions']})
    row = {'id': 'raw', 'at': 0, 'track': 'V1', 'clipType': 'media', 'hold': 1}
    assert not list(validator.iter_errors(row))
    # Raw asset permits the empty string, but never null. Neither is a SET
    # accepted by clip.set; media assignment requires the selected-media op.
    assert not list(validator.iter_errors(dict(row, asset='')))
    assert list(validator.iter_errors(dict(row, asset=None)))
    assert not list(validator.iter_errors(dict(row, transition='')))
    assert list(validator.iter_errors(dict(row, transition=None)))
    for value in ('', None):
        assert list(validator.iter_errors(dict(row, hold=value)))


def test_hold_clear_does_not_substitute_a_legacy_duration_codec_for_source_window():
    original = bundle()
    clip(original, REF)['duration_ms'] = 1000
    with pytest.raises(CommandError, match='surviving source window'):
        capture_clip_set(original, REF, clear=['hold'])


@pytest.mark.parametrize('ref,on', [('parent/clip:direct', 4.004), (REF, 12.012)])
@pytest.mark.parametrize('fps', [30, {'numerator': 30000, 'denominator': 1001}])
@pytest.mark.parametrize('clear', [False, True])
def test_move_then_hold_compiles_source_units_and_roundtrips_own_write_stages(ref, on, fps, clear):
    from astrid.core.timeline.duration import clip_end_frame, clip_timeline_duration
    from astrid.core.timeline.shot_composition_projection import project_runtime_parent_composition
    from astrid.sdk.timeline.capture import capture_move, rate

    original = bundle(fps=fps)
    row = clip(original, ref)
    row.update({'hold': 2, 'speed': 2, 'from': 1, 'to': 7,
                'app': {'opaque': [None, False], 'astrid': {'opaque': {'order': [2, 1]}}}})
    if ref.startswith('parent'):
        original['parent']['config']['clips'] = copy.deepcopy(original['parent']['clips'])
    move = capture_move(original, ref, on)
    moved = execute(original, [move])
    edit = capture_clip_set(moved.bundle, ref, **({'clear': ['hold']} if clear else {'values': {'hold': 8}}))
    plan = execute(original, [move, edit])
    assert plan.commands == [move, edit]
    assert execute(moved.bundle, [edit]).bundle == plan.bundle
    expected = copy.deepcopy(clip(moved.bundle, ref))
    if clear:
        expected.pop('hold')
    else:
        expected['hold'] = 8
    expected['app']['astrid']['for'] = 3 if clear else 4
    assert clip(plan.bundle, ref) == expected
    assert [w['path'] for w in plan.records[1]['writes']] == [['app', 'astrid', 'for'], ['hold']]
    assert State(plan.bundle).config('occurrence:occ-2') == State(original).config('occurrence:occ-2')
    undone = inverse(plan.bundle, plan.records)
    assert undone.bundle == original
    assert inverse(undone.bundle, undone.records).bundle == plan.bundle
    assert replay(plan, original).records == plan.records

    publication = compile_authoring_candidate(plan.bundle).publication
    parent = {'project_id': original['project_id'], 'timeline_id': original['timeline_id'],
              'revision_id': publication['parent_revision_id'], 'content_digest': publication['content_digest'],
              'payload': publication['parent_composition']}
    shots = [dict(r, project_id=original['project_id']) for r in publication['shot_revisions']]
    internals = [dict(r, project_id=original['project_id']) for r in publication['internal_timeline_revisions']]
    readback = open_authoring_bundle(parent, shot_revisions=shots, internal_timeline_revisions=internals)
    assert clip(readback, ref) == expected
    projection = project_runtime_parent_composition(parent, shot_revisions=shots, internal_timeline_revisions=internals)
    identity = 'direct' if ref.startswith('parent') else 'occ-1:picture-1'
    compiled = next(r for r in projection.config['clips'] if r['id'] == identity)
    assert clip_timeline_duration(compiled) == expected['app']['astrid']['for']
    assert compiled['at'] == pytest.approx(expected['at'] + (0 if ref.startswith('parent') else 10))
    assert clip_end_frame(compiled, float(rate(fps))) == clip_end_frame(
        dict(expected, at=compiled['at']), float(rate(fps)))


@pytest.mark.parametrize('prefix', [[], ['app'], ['app', 'astrid']])
def test_matching_static_duration_leaves_only_and_unchanged_aba_guards(prefix):
    original = bundle(app={'astrid': {}})
    for path in (['for'], ['app', 'for'], ['app', 'astrid', 'for']):
        State(original).set(REF, path, present(1))
    plan = execute(original, [capture_clip_set(original, REF, values={'hold': 1})])
    assert plan.bundle == original
    assert [w['path'] for w in plan.records[0]['writes']] == [
        ['for'], ['app', 'for'], ['app', 'astrid', 'for'], ['hold']]
    path = prefix + ['for']
    for history in ([later(REF, path, 1)], [later(REF, path, 2), later(REF, path, 1)]):
        with pytest.raises(CommandError, match='intervening'):
            inverse(plan.bundle, plan.records, history)
        assert replay(plan, original, intervening_records=history).records == plan.records
    changed = execute(original, [capture_clip_set(original, REF, values={'hold': 2})])
    assert all(w['after'] == present(2) for w in changed.records[0]['writes'])
    head = copy.deepcopy(changed.bundle)
    clip(head, REF)['app']['astrid']['sibling'] = {'keep': [None, 0]}
    restored = inverse(head, changed.records)
    assert clip(restored.bundle, REF)['app']['astrid']['sibling'] == {'keep': [None, 0]}
    assert clip(restored.bundle, REF)['hold'] == 1


@pytest.mark.parametrize('prefix', [[], ['app'], ['app', 'astrid']])
@pytest.mark.parametrize('key,value', [('for', 2), ('for', True), ('for', '2s'), ('for', {'anchor': 'end'}),
    ('on', 'clip:other'), ('until', 'end'), ('fixed_start', 'dynamic'), ('formulas', {'hold': 'x'})])
def test_hold_after_move_still_refuses_independent_or_dynamic_intent(prefix, key, value):
    from astrid.sdk.timeline.capture import capture_move

    original = bundle()
    moved = execute(original, [capture_move(original, REF, 12)]).bundle
    State(moved).set(REF, prefix + [key], present(value))
    before = copy.deepcopy(moved)
    with pytest.raises(CommandError) as error:
        capture_clip_set(moved, REF, values={'hold': 2})
    assert error.value.kind == 'unsupported_timing'
    assert moved == before


@pytest.mark.parametrize('field,value', [('time_map', {}), ('timeMap', []), ('from_ms', 0),
    ('to_ms', 1000), ('duration', 1), ('duration_ms', 1000)])
def test_hold_after_move_still_refuses_maps_and_incompatible_duration_codecs(field, value):
    from astrid.sdk.timeline.capture import capture_move

    moved = execute(bundle(), [capture_move(bundle(), REF, 12)]).bundle
    clip(moved, REF)[field] = value
    with pytest.raises(CommandError):
        capture_clip_set(moved, REF, values={'hold': 2})


def test_hold_after_move_retains_half_open_occurrence_and_frame_guards():
    from astrid.sdk.timeline.capture import capture_move

    original = bundle(fps={'numerator': 30000, 'denominator': 1001})
    clip(original, REF).update(hold=2, speed=2)
    moved = execute(original, [capture_move(original, REF, 11.011)]).bundle
    # Local start 1.011 leaves exactly 8.989 timeline seconds at speed two.
    plan = execute(moved, [capture_clip_set(moved, REF, values={'hold': 17.978})])
    assert clip(plan.bundle, REF)['app']['astrid']['for'] == pytest.approx(8.989)
    with pytest.raises(CommandError) as error:
        capture_clip_set(moved, REF, values={'hold': 17.98})
    assert error.value.kind == 'occurrence_boundary'
