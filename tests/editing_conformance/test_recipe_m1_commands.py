"""Executable M1 recipe contract on real main authoring bundles."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from astrid.core.timeline.authoring_bundle import (
    compile_authoring_candidate,
    open_authoring_bundle,
)
from astrid.sdk.timeline import capture as capture_module
from astrid.sdk.timeline._command_state import (
    ABSENT,
    CommandError,
    State,
    address,
    equal,
    overlaps,
    present,
)
from astrid.sdk.timeline.capture import capture_move, capture_track_create
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import bundle_from_document, document_from_bundle
from astrid.sdk.timeline.emitter import emit_document_commands
from astrid.sdk.timeline.rebase import replay
from tests.editing_conformance.staging_recipes import (
    install_staging_operations,
    staged_move,
)
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi
from tests.timeline.test_authoring_bundle import _closure, _digest, _runtime_document

FIXTURES = Path(__file__).parent / 'fixtures'
_REMOVALS: dict[int, tuple[Any, Any, dict, dict]] = {}


@pytest.fixture(autouse=True)
def removal_history():
    yield
    _REMOVALS.clear()


def bundle(*, shared=True, app='missing', fps=30):
    parent, shots, internals = _closure(shared=shared)
    parent['payload']['config']['output'] = {'fps': fps}
    for index, row in enumerate(parent['payload']['occurrences']):
        row['placement']['start_ms'] = 10000 + index * 10000
        row['duration_ms'] = 10000
        row['source_offset'], row['speed'] = 0, 1
    if app != 'missing':
        internals[0]['payload']['clips'][0]['app'] = copy.deepcopy(app)
        parent['payload']['clips'][0]['app'] = copy.deepcopy(app)
        parent['payload']['config']['clips'][0]['app'] = copy.deepcopy(app)
    for image in [parent, *shots, *internals]:
        image['content_digest'] = _digest(image['payload'])
    return bundle_from_document(_runtime_document(parent, shots, internals))


def clip(bundle, ref='parent/clip:direct'):
    return State(bundle).entity(ref)


def inverse(bundle, records, intervening=()):
    context = _REMOVALS.get(id(records))
    if context is not None and context[0] is records:
        _, realm, entry, previous = context
        later_commands = emit_document_commands(previous, bundle)
        if later_commands:
            removal_commit(realm, later_commands, 'Edit surviving row')
        publications = [entry['publication_id']]
        command = {'schema_version': 1, 'op': 'timeline.inverse', 'version': '1',
                   'select': None, 'args': {'publications': publications}}
        selected = removal_records(realm, entry, bundle)
        later_records = [record for item in realm.entries[
            realm.entries.index(entry) + 1:] for record in item['records']['commands']]
        # Acceptance exercises the production history guard; local execution
        # retains the fixture's authoring projection for its exact comparisons.
        removal_commit(realm, [command], 'Invert collection edit')
        plan = execute(bundle, [command], inverse_records=selected,
                       intervening_records=[*later_records, *intervening])
        accepted_before = bundle_from_document(
            realm.documents[entry['previous']['revision_id']])
        operation = entry['records']['commands'][0]['command']['op']
        restore_projection = (
            operation == 'clip.delete' and not State(bundle).collection('parent', 'clip')
        ) or (
            operation == 'timeline.inverse'
            and not State(accepted_before).collection('parent', 'clip')
        )
        if restore_projection:
            for key in ('base_parent', 'base_parent_payload', 'source_mapping'):
                plan.bundle[key] = copy.deepcopy(accepted_before[key])
        _REMOVALS[id(plan.records)] = (plan.records, realm, realm.entries[-1],
                                      copy.deepcopy(plan.bundle))
        return plan
    return execute(bundle, [{'schema_version': 1, 'op': 'timeline.inverse', 'version': '1',
                             'select': None, 'args': {'publications': ['accepted-publication']}}],
                   inverse_records=records, intervening_records=intervening)


def later(ref, path, value):
    return {'writes': [{'ref': ref, 'path': path, 'before': present(value), 'after': present(value)}]}


@pytest.mark.parametrize('app', ['missing', None, {}, {'astrid': None},
                                 {'on': None, 'until': 'end', 'until_film_end': True, 'opaque': [True],
                                  'astrid': {'on': 'word:old:0', 'until': None, 'cut': 'cut-old',
                                             'until_film_end': None, 'opaque': {'a': 1}}}])
def test_exact_authored_move_inverse_redo(app):
    original = bundle(app=app)
    before = copy.deepcopy(original)
    command = capture_move(original, 'parent/clip:direct', 4)
    plan = execute(original, [command])
    assert original == before
    moved = clip(plan.bundle)
    assert moved['at'] == 4
    assert moved['app']['astrid']['fixed_start'] is True
    assert moved['app']['astrid']['for'] == 1
    for container in (moved['app'], moved['app']['astrid']):
        assert all(key not in container for key in ('on', 'until', 'until_film_end'))
    undone = inverse(plan.bundle, plan.records)
    assert undone.bundle == before
    redone = inverse(undone.bundle, undone.records)
    assert redone.bundle == plan.bundle
    assert replay(plan, before).bundle == plan.bundle
    assert replay(plan, before).records == plan.records


def test_same_value_writes_and_presence_are_authoritative():
    original = bundle(app={'astrid': {'fixed_start': True, 'for': 1}})
    command = capture_move(original, 'parent/clip:direct', 3)
    plan = execute(original, [command])
    assert plan.bundle == original
    writes = plan.records[0]['writes']
    assert any(w['path'] == ['at'] and equal(w['before'], w['after']) for w in writes)
    assert any(w['path'] == ['app', 'on'] and w['before'] == ABSENT == w['after'] for w in writes)
    assert all(command['authorship'][w['ref'] + '::' + ('.'.join(w['path']))] == 'direct' for w in writes)
    with pytest.raises(CommandError, match='intervening'):
        inverse(plan.bundle, plan.records, [later('parent/clip:direct', ['at'], 3)])


def test_trimmed_positive_speed_nonzero_occurrence_and_sibling_isolation():
    original = bundle()
    ref = 'occurrence:occ-1/clip:picture-1'
    target = clip(original, ref)
    target.pop('hold')
    target.update({'from': 2, 'to': 6, 'speed': 2, 'at': .5})
    sibling = copy.deepcopy(clip(original, 'occurrence:occ-2/clip:picture-1'))
    plan = execute(original, [capture_move(original, ref, 12.5)])
    moved = clip(plan.bundle, ref)
    assert moved['at'] == 2.5
    assert moved['app']['astrid']['for'] == 2
    assert (moved['from'], moved['to'], moved['speed']) == (2, 6, 2)
    assert clip(plan.bundle, 'occurrence:occ-2/clip:picture-1') == sibling
    assert inverse(plan.bundle, plan.records).bundle == original
    compilation = compile_authoring_candidate(plan.bundle)
    assert compilation.publication['parent_composition']['occurrences'][0]['placement']['start_ms'] == 10000


def test_quantization_once_with_rational_fps_and_ms_codec():
    original = bundle(fps={'numerator': 30000, 'denominator': 1001})
    target = clip(original)
    target['at_ms'] = target.pop('at') * 1000
    original['parent']['config']['clips'] = copy.deepcopy(original['parent']['clips'])
    plan = execute(original, [capture_move(original, 'parent/clip:direct', 1001 / 60000)])
    assert clip(plan.bundle)['at_ms'] == pytest.approx(1001 / 30)
    pin = next(d['pin'] for d in plan.records[0]['dependencies'] if d['key'] == 'move.quantization')
    assert pin['numerator'] == 30000 and pin['denominator'] == 1001
    assert inverse(plan.bundle, plan.records).bundle == original


def test_obsolete_direct_rational_capture_refuses_and_current_capture_guards_rate_change(monkeypatch):
    original = bundle()
    original['parent']['config']['output'] = {'numerator': 30000, 'denominator': 1001}
    original_before = copy.deepcopy(original)

    def pre_b96_output_rate(ctx):
        return capture_module.rate(ctx.read('parent', ['config', 'output', 'fps']))

    with monkeypatch.context() as old_reader:
        old_reader.setattr(capture_module, '_output_rate', pre_b96_output_rate)
        old_command = capture_move(original, 'parent/clip:direct', 1001 / 60000)

    old_command_before = copy.deepcopy(old_command)
    old_rate_reads = [read for read in old_command['reads']
                      if read['ref'] == 'parent' and read['path'][:2] == ['config', 'output']]
    assert old_rate_reads == [{
        'ref': 'parent', 'path': ['config', 'output', 'fps'], 'state': {'state': 'absent'}}]

    with pytest.raises(CommandError) as unchanged_error:
        execute(original, [old_command])
    assert unchanged_error.value.kind == 'capture_mismatch'
    assert original == original_before
    assert old_command == old_command_before

    changed = copy.deepcopy(original)
    changed['parent']['config']['output'] = {'numerator': 60, 'denominator': 1}
    changed_before = copy.deepcopy(changed)
    with pytest.raises(CommandError) as stale_old_error:
        execute(changed, [old_command])
    assert stale_old_error.value.kind == 'capture_mismatch'
    assert changed == changed_before
    assert old_command == old_command_before

    current_command = capture_move(original, 'parent/clip:direct', 1001 / 60000)
    current_plan = execute(original, [current_command])
    assert current_plan.commands == [current_command]
    with pytest.raises(CommandError) as stale_current_error:
        execute(changed, [current_command])
    assert stale_current_error.value.kind == 'dependency_changed'
    assert original == original_before


@pytest.mark.parametrize('field,value,kind', [('speed', 0, 'unsupported_timing'),
                                            ('speed', -1, 'unsupported_timing'),
                                            ('speed', {'numerator': 1, 'denominator': 1}, 'unsupported_timing'),
                                            ('time_map', {'kind': 'piecewise'}, 'unsupported_map'),
                                            ('children', [], 'unsupported_map')])
def test_unsupported_child_maps_fail_precisely(field, value, kind):
    original = bundle()
    clip(original)[field] = value
    with pytest.raises(CommandError) as error:
        capture_move(original, 'parent/clip:direct', 4)
    assert error.value.kind == kind


@pytest.mark.parametrize('field,value', [('speed', 2), ('speed', {'numerator': 1, 'denominator': 1}),
                                       ('speed', True), ('source_offset', 100),
                                       ('source_offset', {'start': 2}), ('time_map', {})])
def test_unsupported_occurrence_maps_fail_closed(field, value):
    original = bundle()
    original['placements'][0][field] = value
    with pytest.raises(CommandError) as error:
        capture_move(original, 'occurrence:occ-1/clip:picture-1', 12)
    assert error.value.kind == 'unsupported_map'


@pytest.mark.parametrize('on', [9, 19.1, 20])
def test_complete_interval_must_fit_occurrence(on):
    original = bundle()
    with pytest.raises(CommandError) as error:
        capture_move(original, 'occurrence:occ-1/clip:picture-1', on)
    assert error.value.kind == 'occurrence_boundary'
    assert capture_move(original, 'occurrence:occ-1/clip:picture-1', 19)['args']['on'] == 19


def test_disjoint_same_child_rebase_and_original_guards_after_materialization():
    original = bundle()
    ref = 'occurrence:occ-1/clip:picture-1'
    a = execute(original, [capture_move(original, ref, 12)])
    b_command = capture_move(a.bundle, ref, 14)
    # A's first materialization gives each shared occurrence its real owner.
    publication = compile_authoring_candidate(a.bundle).publication
    head = open_authoring_bundle(
        {'project_id': original['project_id'], 'timeline_id': original['timeline_id'],
         'revision_id': publication['parent_revision_id'], 'content_digest': publication['content_digest'],
         'payload': publication['parent_composition']},
        shot_revisions=[dict(row, project_id=original['project_id']) for row in publication['shot_revisions']],
        internal_timeline_revisions=[dict(row, project_id=original['project_id']) for row in publication['internal_timeline_revisions']])
    clip(head, ref)['opaque_clip']['concurrent'] = 'other author'
    next(iter(head['source_mapping']['shots'].values()))['content_digest'] = 'sha256:' + 'f' * 64
    b = execute(head, [b_command])
    assert clip(b.bundle, ref)['at'] == 4
    assert clip(b.bundle, ref)['opaque_clip']['concurrent'] == 'other author'
    assert b.commands[0] == b_command
    head2 = copy.deepcopy(head)
    clip(head2, ref)['opaque_clip']['more'] = 'preserve'
    rebased = replay(b, head2)
    assert clip(rebased.bundle, ref)['opaque_clip']['more'] == 'preserve'
    changed = copy.deepcopy(head)
    clip(changed, ref)['at'] = 3
    with pytest.raises(CommandError) as error:
        execute(changed, [b_command])
    assert error.value.kind == 'dependency_changed'
    # Ordinary handoff and rebase allow equal final state; inverse does not.
    execute(head, [b_command])
    assert replay(b, head, intervening_records=[later(ref, ['at'], 2)]).commands == b.commands


@pytest.mark.parametrize('change', ['owner', 'source', 'rate', 'blocker', 'order'])
def test_changed_map_source_owner_and_query_order_refuse(change):
    original = bundle()
    ref = 'occurrence:occ-1/clip:picture-1'
    plan = execute(original, [capture_move(original, ref, 12)])
    head = copy.deepcopy(original)
    if change == 'owner':
        head['placements'][0]['shot_id'] = head['placements'][1]['shot_id']
    elif change == 'source':
        State(head).entity('occurrence:occ-1/registry')['assets']['old-picture']['media_id'] = 'sha256:' + 'f' * 64
    elif change == 'rate':
        head['parent']['config']['output']['fps'] = 25
    elif change == 'blocker':
        added = copy.deepcopy(head['placements'][1])
        added['occurrence_id'] = 'new'
        head['placements'].append(added)
    else:
        head['placements'].reverse()
    with pytest.raises(CommandError):
        replay(plan, head)


def test_replay_permits_disjoint_and_equal_state_while_inverse_guards_aba():
    original = bundle(app={'opaque': {'x': 1}, 'astrid': {'opaque': 2}})
    plan = execute(original, [capture_move(original, 'parent/clip:direct', 4)])
    head = copy.deepcopy(original)
    clip(head)['app']['opaque']['x'] = 2
    assert clip(replay(plan, head).bundle)['app']['opaque']['x'] == 2
    for path in (['at'], ['app'], ['app', 'astrid', 'for'], ['track']):
        history = [later('parent/clip:direct', path, 3)]
        assert replay(plan, original, intervening_records=history).records == plan.records
        with pytest.raises(CommandError) as error:
            inverse(plan.bundle, plan.records, history)
        assert error.value.kind == 'intervening_write'
    inverse(plan.bundle, plan.records, [later('parent/clip:direct', ['opaque'], 1)])
    with pytest.raises(CommandError) as error:
        inverse(plan.bundle, plan.records, [later('parent/clip:direct', ['at'], 4)])
    assert error.value.kind == 'intervening_write'


def test_group_coalesces_intermediate_writes_without_capturing_other_work():
    original = bundle(app={'opaque': 'original'})
    a = execute(original, [capture_move(original, 'parent/clip:direct', 4)])
    b = capture_move(a.bundle, 'parent/clip:direct', 5)
    group = execute(original, [a.commands[0], b])
    head = copy.deepcopy(group.bundle)
    clip(head)['opaque_clip'] = {'later': True}
    head['parent']['config']['clips'] = copy.deepcopy(head['parent']['clips'])
    undone = inverse(head, group.records)
    expected = copy.deepcopy(original)
    clip(expected)['opaque_clip'] = {'later': True}
    expected['parent']['config']['clips'] = copy.deepcopy(expected['parent']['clips'])
    assert undone.bundle == expected
    assert inverse(undone.bundle, undone.records).bundle == head


def test_track_create_inverse_redo_exact_opaque_row_and_identity_collision():
    original = bundle()
    row = {'id': 'new', 'kind': 'visual', 'label': 'New track', 'opaque': {'keep': [None, True]}}
    command = capture_track_create(original, 'occurrence:occ-1', row)
    plan = execute(original, [command])
    assert plan.records[0]['created'] == [{'ref': 'occurrence:occ-1/track:new', 'kind': 'track', 'owner': 'occurrence:occ-1'}]
    assert inverse(plan.bundle, plan.records).bundle == original
    undone = inverse(plan.bundle, plan.records)
    assert inverse(undone.bundle, undone.records).bundle == plan.bundle
    assert replay(plan, original).bundle == plan.bundle
    occupied = copy.deepcopy(original)
    State(occupied).config('occurrence:occ-1')['tracks'].append(row)
    with pytest.raises(CommandError):
        replay(plan, occupied)
    with pytest.raises(CommandError, match='identity pairs'):
        replay(plan, original, id_map={'new': 'replacement'})
    assert replay(plan, original, id_map={'new': 'new'}).bundle == plan.bundle
    populated = copy.deepcopy(plan.bundle)
    State(populated).config('occurrence:occ-1')['clips'].append({'id': 'later', 'track': 'new', 'at': 0, 'hold': 1})
    with pytest.raises(CommandError) as error:
        inverse(populated, plan.records)
    assert error.value.kind == 'surviving_dependents'
    reordered = copy.deepcopy(plan.bundle)
    State(reordered).config('occurrence:occ-1')['tracks'].reverse()
    with pytest.raises(CommandError):
        inverse(reordered, plan.records)


def test_paths_json_equality_claims_and_frozen_schema():
    assert address('parent/clip:direct::app["a::b"].x', []) == ('parent/clip:direct', ('app', 'a::b', 'x'))
    assert address('app["astrid"].for', ['parent/clip:direct']) == ('parent/clip:direct', ('app', 'astrid', 'for'))
    assert overlaps(('x', ('params',)), ('x', ('params', 'x')))
    assert not overlaps(('x', ('params',)), ('x', ('paramsExtra',)))
    assert not equal(present(True), present(1))
    assert not equal(present(None), ABSENT)
    original = bundle()
    command = capture_move(original, 'parent/clip:direct', 4)
    alternate = copy.deepcopy(command)
    alternate['expect']['at'] = alternate['expect'].pop('parent/clip:direct::at')
    execute(original, [alternate])
    forged = copy.deepcopy(command)
    forged['reads'] = []
    with pytest.raises(CommandError) as error:
        execute(original, [forged])
    assert error.value.kind == 'capture_mismatch'
    schema = json.loads((Path(__file__).parents[2] / 'astrid/core/timeline/schema/edit-command.schema.json').read_text())
    Draft202012Validator(schema).validate(command)
    for unknown in ({'x': 1}, {'on': 4, 'relative': True}):
        bad = dict(command, args=unknown)
        with pytest.raises(CommandError):
            execute(original, [bad])
    with pytest.raises(CommandError):
        inverse(original, [])
    with pytest.raises(CommandError):
        inverse(original, [{'writes': []}], [None])


@pytest.mark.parametrize('name', ['recipe_m1_move.json', 'recipe_m1_occurrence.json', 'recipe_m1_shared.json'])
def test_published_capture_golden_fixture(name):
    fixture = json.loads((FIXTURES / name).read_text())
    command = capture_move(fixture['bundle'], fixture['select'], fixture['on'])
    assert command == fixture['command']
    assert execute(fixture['bundle'], [command]).bundle == fixture['after_bundle']


@pytest.mark.parametrize('codec', ['duration', 'duration_ms'])
def test_duration_codec_uses_main_positive_speed_semantics(codec):
    original = bundle()
    target = clip(original)
    target.pop('hold')
    target[codec] = 4 if codec == 'duration' else 4000
    target['speed'] = 2
    original['parent']['config']['clips'] = copy.deepcopy(original['parent']['clips'])
    plan = execute(original, [capture_move(original, 'parent/clip:direct', 4)])
    assert clip(plan.bundle)['app']['astrid']['for'] == 2
    assert plan.semantic_events == []
    assert inverse(plan.bundle, plan.records).bundle == original


def test_inverse_preserves_disjoint_app_fields_and_guards_real_owner_aba():
    original = bundle(app={'astrid': {'opaque': 'base'}, 'opaque': 'base'})
    ref = 'occurrence:occ-1/clip:picture-1'
    plan = execute(original, [capture_move(original, ref, 12)])
    head = copy.deepcopy(plan.bundle)
    clip(head, ref)['app']['astrid']['opaque'] = 'later author'
    clip(head, ref)['app']['opaque'] = 'later author'
    undone = inverse(head, plan.records, [later(ref, ['app', 'opaque'], 'later author')])
    assert clip(undone.bundle, ref)['at'] == 0
    assert clip(undone.bundle, ref)['app']['astrid']['opaque'] == 'later author'
    assert clip(undone.bundle, ref)['app']['opaque'] == 'later author'
    with pytest.raises(CommandError) as error:
        inverse(head, plan.records, [later('occurrence:occ-1', ['shot_id'], 'same-owner')])
    assert error.value.kind == 'intervening_write'
    with pytest.raises(CommandError):
        replay(plan, original, intervening_records=[later('parent', [], {})])


@pytest.mark.parametrize('dependency', ['audio', 'effects', 'audio_bindings', 'external'])
def test_track_inverse_checks_surviving_declared_reference_edges(dependency):
    original = bundle()
    plan = execute(original, [capture_track_create(original, 'occurrence:occ-1', {'id': 'new', 'kind': 'audio'})])
    head = copy.deepcopy(plan.bundle)
    if dependency in ('audio', 'effects'):
        State(head).config('occurrence:occ-1').setdefault(dependency, []).append({'track_id': 'new'})
    elif dependency == 'audio_bindings':
        owner = State(head).occurrence('occurrence:occ-1')['shot_id']
        head['shots'][owner]['payload']['audio_bindings'].append({'track_id': 'new'})
    else:
        clip(head, 'occurrence:occ-2/clip:picture-1')['track_ref'] = 'occurrence:occ-1/track:new'
    with pytest.raises(CommandError) as error:
        inverse(head, plan.records)
    assert error.value.kind == 'surviving_dependents'


def test_structural_group_uses_generic_creation_and_restores_order():
    original = bundle()
    a = capture_track_create(original, 'occurrence:occ-1', {'id': 'a', 'kind': 'visual'})
    first = execute(original, [a])
    b = capture_track_create(first.bundle, 'occurrence:occ-1', {'id': 'b', 'kind': 'audio'})
    group = execute(original, [a, b])
    undone = inverse(group.bundle, group.records)
    assert undone.bundle == original
    assert inverse(undone.bundle, undone.records).bundle == group.bundle
    head = copy.deepcopy(original)
    head['placements'][0]['shot_id'] = head['placements'][1]['shot_id']
    with pytest.raises(CommandError):
        replay(first, head)


def test_boolean_is_not_numeric_guard_and_atomic_failure_does_not_mutate():
    original = bundle()
    clip(original)['enabled'] = False
    command = capture_move(original, 'parent/clip:direct', 4)
    head = copy.deepcopy(original)
    clip(head)['enabled'] = 0
    with pytest.raises(CommandError):
        execute(head, [command])
    before = copy.deepcopy(original)
    with pytest.raises(CommandError):
        execute(original, [command, dict(command, args={'on': -1})])
    assert original == before
    assert execute(original, [dict(command, rationale='Keep this', group_id='gesture')]).commands[0]['rationale'] == 'Keep this'


def test_fixed_move_executes_without_runtime_or_renderer_imports():
    import os
    import subprocess
    import sys

    source = '''
import importlib.abc, json, sys
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.startswith(('astrid.packs.rendering', 'banodoco_workspace_client', 'runtime_protocol')):
            raise AssertionError('save imported ' + fullname)
sys.meta_path.insert(0, Block())
from astrid.sdk.timeline.commands import execute
f=json.load(open(sys.argv[1]))
assert execute(f['bundle'], [f['command']]).bundle == f['after_bundle']
'''
    result = subprocess.run([sys.executable, '-c', source, str(FIXTURES / 'recipe_m1_move.json')],
                            env={**os.environ, 'PYTHONPATH': str(Path(__file__).parents[2])},
                            capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr


def test_output_blockers_capture_activity_role_and_negative_membership():
    original = bundle()
    ref = 'occurrence:occ-1/clip:picture-1'
    original['placements'][1]['hidden'] = True
    plan = execute(original, [capture_move(original, ref, 22)])
    head = copy.deepcopy(original)
    head['placements'][1]['hidden'] = False
    with pytest.raises(CommandError) as error:
        replay(plan, head)
    assert error.value.kind == 'selection_changed'
    # Same-lane audio occurrences do not impose a picture boundary.
    original['parent']['config']['tracks'][0]['kind'] = 'audio'
    original['placements'][1]['hidden'] = False
    plan = execute(original, [capture_move(original, ref, 22)])
    head = copy.deepcopy(original)
    head['parent']['config']['tracks'][0]['kind'] = 'visual'
    with pytest.raises(CommandError):
        replay(plan, head)


@pytest.mark.parametrize('layout', ['both', 'payload', 'config', 'empty-payload', 'empty-config'])
def test_parent_clip_layout_and_recorded_mirror_survive_readback_inverse(layout):
    from astrid.core.timeline.shot_composition_projection import (
        project_runtime_parent_composition,
    )

    original = bundle()
    parent = original['parent']
    if layout == 'payload':
        parent['config'].pop('clips')
    elif layout == 'config':
        parent.pop('clips')
    elif layout == 'empty-payload':
        parent['clips'] = []
    elif layout == 'empty-config':
        parent['config']['clips'] = []
    command = capture_move(original, 'parent/clip:direct', 4)
    assert command['intent'] == {'ripple': 'none', 'anchor': 'fixed', 'position': 'absolute'}
    plan = execute(original, [command])
    derived = plan.records[0]['derived_writes']
    if layout == 'both':
        assert derived == [{'ref': 'parent', 'path': ['config', 'clips'],
                            'before': present(original['parent']['config']['clips']),
                            'after': present(plan.bundle['parent']['clips']), 'cause': 0}]
        assert plan.bundle['parent']['config']['clips'] == plan.bundle['parent']['clips']
    else:
        assert derived == []
    assert plan.semantic_events == []
    publication = compile_authoring_candidate(plan.bundle).publication
    project_runtime_parent_composition(
        {'project_id': original['project_id'], 'timeline_id': original['timeline_id'],
         'revision_id': publication['parent_revision_id'], 'payload': publication['parent_composition']},
        shot_revisions=publication['shot_revisions'],
        internal_timeline_revisions=publication['internal_timeline_revisions'])
    assert inverse(plan.bundle, plan.records).bundle == original
    assert replay(plan, original).records == plan.records


def test_semantic_owner_refuses_actual_source_change_and_inverse_source_aba():
    original = bundle()
    ref = 'occurrence:occ-1/clip:picture-1'
    plan = execute(original, [capture_move(original, ref, 12)])
    changed = copy.deepcopy(original)
    shot_id = changed['placements'][0]['shot_id']
    changed['source_mapping']['shots'][shot_id]['shot_id'] = 'different-source'
    with pytest.raises(CommandError):
        replay(plan, changed)
    with pytest.raises(CommandError) as error:
        inverse(plan.bundle, plan.records, [later('occurrence:occ-1', ['shot_id'], shot_id)])
    assert error.value.kind == 'intervening_write'


@pytest.mark.parametrize('kind', ['transcript', 'config'])
def test_compiler_records_exact_retained_document_pin_without_evaluating_anchor(kind):
    original = bundle(app={'astrid': {'on': 'word:old-source:word-1'}})
    content = {'retained': True}
    pin = {'document_id': 'retained:' + kind, 'version': 1, 'content_digest': _digest(content)}
    unused = dict(pin, document_id='unused')
    document_kind = 'media.transcript' if kind == 'transcript' else 'timeline.config'
    original['source_mapping'].update(
        _runtime_document={'dependency_manifest': {'documents': [pin]}},
        pinned_documents=[{'pin': p, 'document': {'kind': document_kind, 'content': content}}
                          for p in (pin, unused)])
    command = capture_move(original, 'parent/clip:direct', 4)
    plan = execute(original, [command])
    dependencies = plan.records[0]['dependencies']
    assert [d for d in dependencies if d['kind'] in ('transcript', 'source')] == [
        {'key': 'compile.document:' + _digest(pin),
         'kind': 'transcript' if kind == 'transcript' else 'source', 'pin': pin, 'policy': 'exact'}]
    assert not any(d['pin'] == unused for d in dependencies)
    assert next(d for d in dependencies if d['key'] == 'move.images')['kind'] == 'entity'
    assert clip(plan.bundle)['app']['astrid'].get('on') is None
    assert inverse(plan.bundle, plan.records).bundle == original


def test_shared_first_materialization_golden_preserves_original_sibling_capture():
    fixture = json.loads((FIXTURES / 'recipe_m1_shared.json').read_text())
    original, head = fixture['bundle'], fixture['head_bundle']
    ref = fixture['select']
    owner = ref.rsplit('/', 1)[0]
    assert State(original).occurrence(owner)['shot_id'] != State(head).occurrence(owner)['shot_id']
    assert State(original).entity(owner + '/shot') == State(head).entity(owner + '/shot')
    plan = execute(original, [fixture['command']])
    rebased = replay(plan, head)
    assert rebased.commands == [fixture['command']]
    assert rebased.bundle == fixture['rebased_bundle']
    undone = inverse(rebased.bundle, rebased.records)
    assert undone.bundle == head
    assert inverse(undone.bundle, undone.records).bundle == rebased.bundle



def test_materialization_alias_is_exact_for_arbitrary_source_ids():
    from astrid.core.timeline.authoring_bundle import _authoring_shot_id

    parent, shots, internals = _closure()
    source = 'shot-placement-' + 'a' * 64
    shots[0]['shot_id'] = source
    for index, row in enumerate(parent['payload']['occurrences']):
        row.update(shot_id=source, source_offset=0, speed=1)
        row['placement']['start_ms'] = index * 10000
    parent['content_digest'] = _digest(parent['payload'])
    original = open_authoring_bundle(parent, shot_revisions=shots,
                                     internal_timeline_revisions=internals)
    ref = 'occurrence:occ-1/clip:picture-1'
    command = capture_move(original, ref, 1)
    plan = execute(original, [command])
    read = next(r for r in command['reads'] if r['ref'] == 'occurrence:occ-1/shot')
    assert read['state'] == present(source)
    head = copy.deepcopy(original)
    old = head['placements'][0]['shot_id']
    materialized = _authoring_shot_id(original['project_id'], original['timeline_id'],
                                     'occ-1', source, shared=True)
    assert old == materialized
    head['source_mapping']['shots'][old]['shot_id'] = materialized
    assert replay(plan, head).commands == [command]
    head['source_mapping']['shots'][old]['shot_id'] = 'shot-placement-' + 'b' * 64
    with pytest.raises(CommandError):
        replay(plan, head)


def recorded_removal(original, owner, kind, identities):
    from tests.editing_conformance.test_recipe_occurrence_media import MediaRealm

    if owner == 'parent' and kind == 'clip':
        document = document_from_bundle(original)
        canonical = bundle_from_document(document)
        original['base_parent'] = copy.deepcopy(canonical['base_parent'])
        original['base_parent_payload'] = copy.deepcopy(canonical['base_parent_payload'])
        original['source_mapping']['_runtime_document'] = copy.deepcopy(
            canonical['source_mapping']['_runtime_document'])
    realm = MediaRealm(document_from_bundle(original), [])
    candidate = copy.deepcopy(original)
    rows = State(candidate).collection(owner, kind)
    rows[:] = [row for row in rows if row['id'] not in identities]
    if owner == 'parent' and kind == 'clip':
        commands = [{'schema_version': 1, 'op': 'clip.delete', 'version': '1',
                     'select': owner + '/clip:' + identity, 'args': {}}
                    for identity in identities]
    else:
        commands = emit_document_commands(original, candidate)
    result = removal_commit(realm, commands, 'Remove collection rows')
    removed = bundle_from_document(result['data']['canonical_document'])
    records = removal_records(realm, realm.entries[-1], removed)
    _REMOVALS[id(records)] = (records, realm, realm.entries[-1], copy.deepcopy(removed))
    return removed, records


def removal_commit(realm, commands, label):
    from tests.editing_conformance.test_doc_patch_v2 import submit

    document = realm.documents[realm.head['revision_id']]
    return submit(
        realm, *commands, project_id=document['project_id'], timeline_id=document['timeline_id'],
        label=label, merge='fields', reads=[], dry_run=False, source={'via': 'python'},
        rationale='M1 exact collection inverse and redo', group_id='m1-collection',
        steps=[{'id': 'edit', 'label': label, 'intent': 'Preserve full rows and authored order',
                'tags': [], 'command_ordinals': list(range(len(commands))), 'reads': [],
                'patch': [change for command in commands if command['op'] == 'doc.patch'
                          for change in command['args']['changes']]}])


def removal_records(realm, entry, head):
    from astrid.sdk.timeline._collections import AcceptedRecords
    from astrid.sdk.timeline._commit_patch import inverse_address_resolver
    from astrid.sdk.timeline._step_undo import select_inverse_records

    def load_boundary(item):
        previous = next(edge for edge in realm.entries
                        if edge['publication_id'] == item['previous']['publication_id'])
        return tuple(bundle_from_document(edge['receipt']['result'].get(
            'canonical_document', realm.documents[edge['revision_id']]))
                     for edge in (previous, item))

    history = {item['publication_id']: item for item in realm.entries}
    accepted = AcceptedRecords([entry], entry['records']['commands'], load_boundary)
    resolver = inverse_address_resolver(
        realm.entries[1:], head, load_boundary=load_boundary, load_history=history.__getitem__)
    return select_inverse_records(accepted, address_resolver=resolver)


def accepted_boundaries(records, before, after):
    # The accepted entry supplies the boundary, never the caller's projections.
    context = _REMOVALS.get(id(records))
    if context is None or context[0] is not records:
        from astrid.sdk.timeline._collections import AcceptedRecords

        entry = {'records': {'commands': records,
                             'undo_of': records[0]['command']['args']['publications']}}
        return AcceptedRecords([entry], records, lambda _: (before, after))
    captured, realm, entry, previous = context
    assert captured is records
    accepted = removal_records(realm, entry, after)
    _REMOVALS[id(accepted)] = (accepted, realm, entry, previous)
    return accepted


@pytest.mark.parametrize('owner', ['parent', 'occurrence:occ-1'])
@pytest.mark.parametrize('kind', ['track', 'clip'])
@pytest.mark.parametrize('removed', [('a',), ('b',), ('c',), ('a', 'b'), ('a', 'b', 'c')])
def test_structural_removal_inverse_redo_preserves_full_rows_and_authored_order(owner, kind, removed):
    original = bundle()
    rows = [{'id': identity, 'kind': 'visual', 'track': 'picture', 'at': index, 'hold': 1,
             'opaque': {'order': [None, False, 0], 'unknown': {'id': identity}},
             **({'nullable': None} if identity == 'b' else {})}
            for index, identity in enumerate(('a', 'b', 'c'))]
    if owner == 'parent' and kind == 'clip':
        original['parent']['clips'] = copy.deepcopy(rows)
    State(original).config(owner)[kind + 's'] = copy.deepcopy(rows)
    removed_bundle, records = recorded_removal(original, owner, kind, removed)
    immutable_records = copy.deepcopy(records)
    head = copy.deepcopy(removed_bundle)
    remaining = State(head).collection(owner, kind)
    if remaining:
        remaining[-1]['opaque']['later'] = 'untouched author'
    if owner == 'parent' and kind == 'clip':
        head['parent']['config']['clips'] = copy.deepcopy(head['parent']['clips'])
    undone = inverse(head, records)
    restored = State(undone.bundle).collection(owner, kind)
    expected = copy.deepcopy(rows)
    if remaining:
        next(row for row in expected if row['id'] == remaining[-1]['id'])['opaque']['later'] = 'untouched author'
    assert json.dumps(restored) == json.dumps(expected)
    assert records == immutable_records
    assert undone.records[0]['selections'][0]['order'] == [
        owner + '/' + kind + ':' + row['id'] for row in remaining]
    redone = inverse(undone.bundle, accepted_boundaries(undone.records, head, undone.bundle))
    assert redone.bundle == head


@pytest.mark.parametrize('layout', ['payload', 'config', 'both', 'null-payload', 'null-config'])
def test_remove_last_clip_keeps_exact_container_shape(layout):
    original = bundle()
    parent = original['parent']
    if layout == 'payload':
        parent['config'].pop('clips')
    elif layout == 'config':
        parent.pop('clips')
    elif layout == 'null-payload':
        parent['clips'] = None
    elif layout == 'null-config':
        parent['config']['clips'] = None
    removed, records = recorded_removal(original, 'parent', 'clip', ['direct'])
    assert State(removed).collection('parent', 'clip') == []
    undone = inverse(removed, records)
    assert undone.bundle == original
    assert inverse(undone.bundle, accepted_boundaries(undone.records, removed, undone.bundle)).bundle == removed


@pytest.mark.parametrize('shape', ['absent', None, []])
def test_collection_shape_is_guarded_without_reading_untouched_member_bytes(shape):
    from astrid.sdk.timeline._recording import Recording, guard

    original = bundle()
    ctx = Recording(original, {}, 0)
    ctx.select('collection.tracks', 'parent')
    head = copy.deepcopy(original)
    head['parent']['config']['tracks'][0]['opaque'] = 'later author'
    for read in ctx.record['reads']:
        guard(State(head), read['ref'], read['path'], read['state'])
    if shape == 'absent':
        head['parent']['config'].pop('tracks')
    else:
        head['parent']['config']['tracks'] = shape
    if shape == []:
        # A list remains a list; the selection supplies the membership guard.
        assert State(head).get('parent/collection:track', []) == ctx.record['reads'][0]['state']
    else:
        with pytest.raises(CommandError):
            guard(State(head), ctx.record['reads'][0]['ref'], [], ctx.record['reads'][0]['state'])
        with pytest.raises(CommandError):
            execute(head, [capture_track_create(original, 'parent', {'id': 'new', 'kind': 'visual'})])


@pytest.mark.parametrize('damage', ['duplicate', 'missing', 'foreign', 'divergent-mirror'])
def test_structural_collections_refuse_ambiguous_scoped_identities(damage):
    from astrid.sdk.timeline._recording import selection

    original = bundle()
    if damage == 'divergent-mirror':
        original['parent']['config']['clips'] = []
    else:
        rows = State(original).collection('parent', 'clip')
        if damage == 'duplicate':
            rows.append(copy.deepcopy(rows[0]))
        elif damage == 'missing':
            rows[0].pop('id')
        else:
            rows[0]['id'] = 'occurrence:foreign/clip:direct'
        original['parent']['config']['clips'] = copy.deepcopy(rows)
    with pytest.raises(CommandError):
        selection(State(original), 'collection.clips', 'parent')


def test_foreign_historical_order_and_missing_order_proof_refuse():
    original = bundle()
    removed, records = recorded_removal(original, 'parent', 'clip', ['direct'])
    for damage in ('missing', 'foreign', 'duplicate'):
        broken = copy.deepcopy(records)
        if damage == 'missing':
            broken[0]['selections'] = []
        else:
            refs = ['occurrence:occ-1/clip:direct'] if damage == 'foreign' else ['parent/clip:direct'] * 2
            broken[0]['selections'][0].update(members=refs, order=refs, boundaries=refs)
        with pytest.raises(CommandError) as error:
            inverse(removed, broken)
        assert error.value.kind == 'history_unavailable'


@pytest.mark.parametrize('address', [('parent', ['config', 'tracks']),
                                     ('parent/track:other', []),
                                     ('parent/track:picture', ['id'])])
def test_collection_selection_detects_structural_publications_even_when_json_returns(address):
    original = bundle()
    plan = execute(original, [capture_track_create(original, 'parent', {'id': 'new', 'kind': 'visual'})])
    with pytest.raises(CommandError) as error:
        inverse(plan.bundle, plan.records, [later(*address, [])])
    assert error.value.kind == 'intervening_write'
    head = copy.deepcopy(plan.bundle)
    head['parent']['config']['tracks'][0]['opaque'] = 'other author'
    undone = inverse(head, plan.records, [later('parent/track:picture', ['opaque'], 'other author')])
    assert undone.bundle['parent']['config']['tracks'][0]['opaque'] == 'other author'


@pytest.mark.parametrize('ref,path', [('parent', ['clips']), ('parent', ['config', 'clips']),
                                     ('parent', ['config']),
                                     ('occurrence:occ-1/source', ['clips']),
                                     ('occurrence:occ-1', [])])
def test_member_and_container_aliases_share_overlap_for_replay_and_inverse(ref, path):
    original = bundle()
    target = 'parent/clip:direct' if ref == 'parent' else 'occurrence:occ-1/clip:picture-1'
    on = 4 if ref == 'parent' else 12
    plan = execute(original, [capture_move(original, target, on)])
    for operation, state in ((replay, original), (inverse, plan.bundle)):
        with pytest.raises(CommandError) as error:
            if operation is replay:
                replay(plan, state, intervening_records=[later(ref, path, [])])
            else:
                inverse(state, plan.records, [later(ref, path, [])])
        assert error.value.kind == 'intervening_write'


def test_explicit_shared_source_overlap_does_not_bind_other_occurrence_owners():
    original = bundle()
    ref = 'occurrence:occ-1/clip:picture-1'
    plan = execute(original, [capture_move(original, ref, 12)])
    source = State(original).entity('occurrence:occ-1/shot')['shot_id']
    with pytest.raises(CommandError):
        inverse(plan.bundle, plan.records, [later('shot:' + source, ['unknown'], None)])
    inverse(plan.bundle, plan.records, [later('occurrence:occ-2/source', ['clips'], [])])


@pytest.mark.parametrize('container_ref,container_path,member_ref', [
    ('parent', ['clips'], 'parent/clip:direct'),
    ('parent', ['config', 'clips'], 'parent/clip:direct'),
    ('parent', ['config', 'tracks'], 'parent/track:direct'),
    ('occurrence:occ-1/source', ['clips'], 'occurrence:occ-1/clip:direct'),
])
def test_coalescing_aliases_folds_actual_execution_order(container_ref, container_path, member_ref):
    from astrid.sdk.timeline.inverse import coalesce

    old = [{'id': 'direct', 'opaque': {'values': [None, 0]}}]
    replaced = [{'id': 'direct', 'opaque': {'values': [None, 1]}}]
    final = [{'id': 'direct', 'opaque': {'values': [None, 2]}}]
    root = {'ref': container_ref, 'path': container_path, 'before': present(old), 'after': present(replaced)}
    leaf = {'ref': member_ref, 'path': ['opaque', 'values', '1'],
            'before': present(1), 'after': present(2)}
    folded = coalesce([{'writes': [root, leaf]}])
    assert folded == [dict(root, after=present(final))]
    first_leaf = dict(leaf, before=present(0), after=present(1))
    later_root = dict(root, before=present(replaced), after=present(final))
    assert coalesce([{'writes': [first_leaf, later_root]}]) == [dict(later_root, before=present(old))]


@pytest.mark.parametrize('key', ['collection.tracks', 'collection.clips',
                                 'collection.occurrences', 'collection.shots'])
def test_shared_collection_queries_include_empty_and_exact_owner_scopes(key):
    from astrid.sdk.timeline._recording import selection

    original = bundle()
    selected = selection(State(original), key, 'parent')
    assert selected['members'] == selected['order'] == selected['boundaries']
    if key == 'collection.tracks':
        original['parent']['config']['tracks'] = []
    elif key == 'collection.clips':
        original['parent']['clips'] = original['parent']['config']['clips'] = []
    elif key == 'collection.occurrences':
        original['placements'] = []
    else:
        original['shots'] = {}
    assert selection(State(original), key, 'parent')['order'] == []
    if key in ('collection.tracks', 'collection.clips'):
        a = selection(State(original), key, 'occurrence:occ-1')
        b = selection(State(original), key, 'occurrence:occ-2')
        assert a['order'] and b['order'] and set(a['order']).isdisjoint(b['order'])


def test_owner_overlap_does_not_coalesce_child_rows_into_placement_json():
    from astrid.sdk.timeline.inverse import coalesce

    placement = {'ref': 'occurrence:occ-1', 'path': [],
                 'before': present({'occurrence_id': 'occ-1', 'shot_id': 's'}), 'after': ABSENT}
    child = {'ref': 'occurrence:occ-1/clip:c', 'path': ['opaque'],
             'before': present(None), 'after': present(True)}
    assert overlaps((placement['ref'], ()), (child['ref'], tuple(child['path'])))
    assert coalesce([{'writes': [child, placement]}]) == [child, placement]
    assert coalesce([{'writes': [placement, child]}]) == [placement, child]
    for ref in ('parent/collection:track', 'occurrence:occ-1/collection:clip', 'shot:s'):
        assert overlaps(('timeline', ()), (ref, ()))
        assert overlaps((ref, ()), ('timeline', ()))


def test_inverse_collection_capture_uses_existing_schema():
    original = bundle()
    removed, records = recorded_removal(original, 'parent', 'clip', ['direct'])
    command = inverse(removed, records).commands[0]
    schema = json.loads((Path(__file__).parents[2] / 'astrid/core/timeline/schema/edit-command.schema.json').read_text())
    Draft202012Validator(schema).validate(command)


@pytest.fixture
def native_staging_operations(monkeypatch):
    """Small registered recipes exercise staging without installing M2 move-set."""
    install_staging_operations(monkeypatch)


@pytest.mark.parametrize('owner', ['parent', 'occurrence:occ-1'])
@pytest.mark.parametrize('second', [False, True])
def test_create_and_own_consumers_inverse_is_staged_and_claims_replay(native_staging_operations, owner, second):
    original = bundle()
    ref = owner + '/clip:' + ('direct' if owner == 'parent' else 'picture-1')
    calls = [capture_track_create(original, owner, {'id': 'c', 'kind': 'visual'}), staged_move(ref, 'c')]
    if second:
        calls += [{'schema_version': 1, 'op': 'track.create', 'version': '1', 'select': owner,
                   'args': {'row': {'id': 'd', 'kind': 'visual'}}}, staged_move(ref, 'd', 5)]
    plan = execute(original, calls)
    immutable = copy.deepcopy(plan.records)
    undone = inverse(plan.bundle, plan.records)
    assert json.dumps(undone.bundle) == json.dumps(original)
    assert plan.records == immutable
    for read in undone.commands[0]['reads']:
        assert State(plan.bundle).matches(read['ref'], read['path'], read['state'])
    assert replay(undone, plan.bundle, inverse_records=plan.records).records == undone.records
    redone = inverse(undone.bundle, undone.records)
    assert json.dumps(redone.bundle) == json.dumps(plan.bundle)
    assert [item['ref'] for item in redone.records[0]['created']] == [
        owner + '/track:c', *([owner + '/track:d'] if second else [])]


def test_ordinary_clip_create_with_own_move_uses_same_reverse_stages(native_staging_operations):
    original = bundle()
    plan = execute(original, [
        capture_track_create(original, 'parent', {'id': 'c', 'kind': 'visual'}),
        {'schema_version': 1, 'op': 'fixture.clip-create', 'version': '1', 'select': 'parent',
         'args': {'row': {'id': 'owned', 'track': 'c', 'at': 1, 'hold': 1, 'unknown': [None, 0]}}},
        staged_move('parent/clip:owned', 'c')])
    undone = inverse(plan.bundle, plan.records)
    assert undone.bundle == original
    assert inverse(undone.bundle, undone.records).bundle == plan.bundle


@pytest.mark.parametrize('dependent', ['clip', 'occurrence', 'audio', 'effects', 'binding',
                                       'external', 'unsupported'])
def test_staged_removal_refuses_surviving_reference_without_partial_changes(native_staging_operations, dependent):
    original = bundle()
    plan = execute(original, [capture_track_create(original, 'parent', {'id': 'c', 'kind': 'visual'}),
                              staged_move('parent/clip:direct', 'c')])
    head = copy.deepcopy(plan.bundle)
    if dependent == 'clip':
        head['parent']['clips'].append({'id': 'newer', 'track': 'c', 'at': 8, 'hold': 1})
        head['parent']['config']['clips'] = copy.deepcopy(head['parent']['clips'])
    elif dependent == 'occurrence':
        head['placements'][0]['placement']['track'] = 'c'
    elif dependent in ('audio', 'effects'):
        head['parent']['config'][dependent] = [{'track': 'c'}]
    elif dependent == 'binding':
        next(iter(head['shots'].values()))['payload']['audio_bindings'].append({'track_ref': 'parent/track:c'})
    else:
        clip(head, 'occurrence:occ-2/clip:picture-1')['owner_ref'] = (
            'parent/track:c' if dependent == 'external' else {'resolver': 'unsupported', 'id': 'c'})
    before = copy.deepcopy(head)
    with pytest.raises(CommandError) as error:
        inverse(head, plan.records)
    assert error.value.kind == 'surviving_dependents'
    assert head == before


def test_staged_inverse_keeps_opaque_strings_and_refuses_all_unchanged_overlap(native_staging_operations):
    original = bundle()
    plan = execute(original, [capture_track_create(original, 'parent', {'id': 'c', 'kind': 'visual'}),
                              staged_move('parent/clip:direct', 'c')])
    head = copy.deepcopy(plan.bundle)
    clip(head)['opaque'] = {'note': 'parent/track:c'}
    head['parent']['config']['clips'] = copy.deepcopy(head['parent']['clips'])
    undone = inverse(head, plan.records, [later('parent/clip:direct', ['opaque'], clip(head)['opaque'])])
    assert clip(undone.bundle)['opaque'] == {'note': 'parent/track:c'}
    for path, value in [(['track'], 'c'), (['at'], 4)]:
        with pytest.raises(CommandError) as error:
            inverse(head, plan.records, [later('parent/clip:direct', path, value)])
        assert error.value.kind == 'intervening_write'


def test_same_target_null_absent_clears_and_unchanged_writes_retain_each_stage(native_staging_operations):
    original = bundle()
    clip(original)['nullable'] = None
    original['parent']['config']['clips'] = copy.deepcopy(original['parent']['clips'])
    def call(state):
        return {'schema_version': 1, 'op': 'fixture.properties', 'version': '1',
                'select': 'parent/clip:direct', 'args': {'changes': [
                    {'path': ['nullable'], 'state': state}, {'path': ['missing'], 'state': ABSENT}]}}
    plan = execute(original, [call(ABSENT), call(present(None)), call(present(None)), call(ABSENT)])
    assert len(plan.records) == 4 and all(len(record['writes']) == 2 for record in plan.records)
    undone = inverse(plan.bundle, plan.records)
    assert undone.bundle == original
    assert inverse(undone.bundle, undone.records).bundle == plan.bundle


def test_overlapping_read_is_verified_at_input_and_bad_late_stage_is_atomic(native_staging_operations):
    original = bundle()
    plan = execute(original, [capture_track_create(original, 'parent', {'id': 'c', 'kind': 'visual'}),
                              staged_move('parent/clip:direct', 'c')])
    broken = copy.deepcopy(plan.records)
    next(read for read in broken[-1]['reads'] if read['path'] == ['track'])['state'] = present('wrong-stage')
    head = copy.deepcopy(plan.bundle)
    with pytest.raises(CommandError):
        inverse(head, broken)
    assert head == plan.bundle


@pytest.mark.parametrize('source', ['source-shot', 'shot-placement-' + 'a' * 64])
def test_reverse_materialization_alias_requires_exact_source_and_occurrence(source):
    from astrid.core.timeline.authoring_bundle import _authoring_shot_id

    parent, shots, internals = _closure()
    shots[0]['shot_id'] = source
    for row in parent['payload']['occurrences']:
        row['shot_id'] = source
    parent['content_digest'] = _digest(parent['payload'])
    original = open_authoring_bundle(parent, shot_revisions=shots,
                                     internal_timeline_revisions=internals)
    state = State(original)
    ref = 'occurrence:occ-1/shot'
    expected = present(_authoring_shot_id(original['project_id'], original['timeline_id'],
                                          'occ-1', source, shared=True))
    assert state.matches(ref, ['shot_id'], expected)
    assert not state.matches('occurrence:occ-2/shot', ['shot_id'], expected)
    for project, timeline, identity in [(original['project_id'], original['timeline_id'], 'other-source'),
                                       ('other-project', original['timeline_id'], source),
                                       (original['project_id'], 'other-timeline', source)]:
        unrelated = present(_authoring_shot_id(project, timeline, 'occ-1', identity, shared=True))
        assert not state.matches(ref, ['shot_id'], unrelated)
    owner = state.occurrence('occurrence:occ-1')['shot_id']
    original['source_mapping']['shots'][owner]['shot_id'] = 'other-source'
    with pytest.raises(CommandError) as error:
        state.matches(ref, ['shot_id'], expected)
    assert error.value.kind == 'identity_conflict'
