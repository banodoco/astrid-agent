"""Native scalar recipes: exact presence, narrow guards and portable capture."""
from __future__ import annotations

import base64
import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from jsonschema import Draft202012Validator

from astrid.core.element import project_packs
from astrid.sdk.timeline._command_state import ABSENT, CommandError, State, equal, present
from astrid.sdk.timeline._scalar_commands import capture_clip_set_params, capture_track_set
from astrid.sdk.timeline.capture import capture_move
from astrid.sdk.timeline.commands import execute, operations
from astrid.sdk.timeline.commit import _digest, _json
from astrid.sdk.timeline.rebase import replay
from tests.core import test_project_packs
from tests.editing_conformance.test_recipe_m1_commands import bundle, clip, inverse, later

FIXTURES = Path(__file__).parent / 'fixtures'
REF = 'occurrence:occ-1/clip:picture-1'
configured_project_pack = pytest.fixture(test_project_packs.configured.__wrapped__)


def call(op, ref, values=None, clear=()):
    return {'schema_version': 1, 'op': op, 'version': '1', 'select': ref,
            'args': {'values': {} if values is None else values, 'clear': list(clear)}}


@pytest.mark.parametrize('aba', [False, True])
def test_inverse_refuses_same_batch_unchanged_or_aba_scalar_write(aba):
    ref = 'parent/track:picture'
    target = execute(bundle(), [call('track.set', ref, {'label': 'target'})])
    undo = inverse(target.bundle, target.records).commands[0]
    staged = ([call('track.set', ref, {'label': 'away'})] if aba else [])
    staged.append(call('track.set', ref, {'label': 'target'}))
    before = copy.deepcopy((target.bundle, target.records))
    with pytest.raises(CommandError) as error:
        execute(target.bundle, [*staged, undo], inverse_records=target.records)
    assert error.value.kind == 'intervening_write'
    assert (target.bundle, target.records) == before


def test_inverse_allows_later_explicit_write_and_disjoint_prior_stages():
    ref, other = 'parent/track:picture', 'occurrence:occ-1/track:picture'
    target = execute(bundle(), [call('track.set', ref, {'label': 'target'})])
    undo = inverse(target.bundle, target.records).commands[0]
    plan = execute(target.bundle, [call('track.set', other, {'label': 'disjoint'}), undo,
                                   call('track.set', ref, {'label': 'later'})],
                   inverse_records=target.records)
    assert State(plan.bundle).entity(ref)['label'] == 'later'
    assert State(plan.bundle).entity(other)['label'] == 'disjoint'
    assert replay(plan, target.bundle, inverse_records=target.records).records == plan.records
    assert inverse(plan.bundle, plan.records).bundle == target.bundle


@pytest.fixture
def admitted_param_revisions(configured_project_pack):
    # Main's real admission/registry path, including its immutable source snapshots.
    _, directory, _ = test_project_packs.seed('project-1')
    old = project_packs.admit_project_pack('realm', 'project-1')['elements'][0]['descriptor']
    path = directory / 'element.yaml'
    definition = yaml.safe_load(path.read_text())
    definition['schema']['properties']['seed']['maximum'] = 10
    path.write_text(yaml.safe_dump(definition))
    new = project_packs.admit_project_pack('realm', 'project-1')['elements'][0]['descriptor']
    assert old['id'] == new['id'] and old['packId'] == new['packId']
    assert old['revision'] != new['revision']
    refs = [{k: descriptor[k] for k in ('packId', 'id', 'kind', 'revision')}
            for descriptor in (old, new)]
    for ref, descriptor in zip(refs, (old, new)):
        assert project_packs.resolve_admitted_element('realm', 'project-1', ref)['descriptor'] == descriptor
    assert not list(Draft202012Validator(old['schema']).iter_errors({'seed': 16}))
    assert list(Draft202012Validator(new['schema']).iter_errors({'seed': 16}))
    return refs


def schema_options(documents, reference, project='project-1'):
    row = next(copy.deepcopy(row) for (owner, _), row in documents.rows.items()
               if owner == project and row['content']['pack_id'] == reference['packId'])
    row['project_id'] = project
    pin = {'document_id': row['document_id'], 'version': row['version'],
           'content_digest': project_packs._json_digest(row['content'])}
    objects = {}
    for admission in row['content']['admissions']:
        descriptor = admission['snapshot_object']
        objects[descriptor['object_id']] = {
            **descriptor, 'bytes_base64': base64.b64encode(
                documents.media.objects[(project, descriptor['object_id'])]).decode('ascii')}
    return {'schema_source': pin, 'schema_inputs': [({'pin': pin, 'document': row}, objects)],
            'realm_id': 'realm'}


@pytest.mark.parametrize('field', ['elementRef', 'element_ref'])
@pytest.mark.parametrize('change', ['revision', 'attach', 'remove'])
def test_original_param_basis_refuses_new_admitted_schema(
        admitted_param_revisions, configured_project_pack, field, change):
    from astrid.core.timeline.authoring_bundle import compile_authoring_candidate

    original = bundle()
    ref = 'parent/clip:direct'
    old, new = admitted_param_revisions
    target = clip(original, ref)
    target.pop('asset')
    target['clipType'] = 'reveal'
    if change != 'attach':
        target[field] = old
    original['parent']['config']['clips'] = copy.deepcopy(original['parent']['clips'])
    compile_authoring_candidate(original)
    options = schema_options(configured_project_pack, old) if change != 'attach' else {}
    inputs = {key: value for key, value in options.items() if key != 'schema_source'}
    plan = execute(original, [capture_clip_set_params(original, ref, values={'seed': 16}, **options)],
                   **inputs)
    head = copy.deepcopy(original)
    if change == 'remove':
        del clip(head, ref)[field]
    else:
        clip(head, ref)[field] = new
    head['parent']['config']['clips'] = copy.deepcopy(head['parent']['clips'])
    compile_authoring_candidate(head)
    snapshot = copy.deepcopy(head)
    with pytest.raises(CommandError):
        replay(plan, head, **inputs)
    assert head == snapshot
    if change == 'revision':
        for history in ([later(ref, [field], old)],
                        [later(ref, [field], new), later(ref, [field], old)]):
            assert replay(plan, original, intervening_records=history, **inputs).records == plan.records


@pytest.mark.parametrize('container', [ABSENT, present(None)])
def test_clear_only_params_preserves_nonobject_presence_and_leaf_intent(container):
    original = bundle()
    if container != ABSENT:
        clip(original, REF)['params'] = copy.deepcopy(container['value'])
    command = capture_clip_set_params(original, REF, clear=['missing'])
    plan = execute(original, [command])
    assert plan.bundle == original
    assert command['expect'] == {REF + '::params.missing': ABSENT}
    assert plan.records[0]['writes'] == [
        {'ref': REF, 'path': ['params', 'missing'], 'before': ABSENT, 'after': ABSENT}]
    assert {'ref': REF, 'path': ['params'], 'state': container} in command['reads']
    undone = inverse(plan.bundle, plan.records)
    assert undone.bundle == original
    assert inverse(undone.bundle, undone.records).bundle == plan.bundle
    assert replay(plan, original).records == plan.records
    for changed in (ABSENT, present(None), present({}), present(False)):
        if changed == container:
            continue
        head = copy.deepcopy(original)
        State(head).set(REF, ['params'], changed)
        with pytest.raises(CommandError):
            replay(plan, head)
        with pytest.raises(CommandError):
            inverse(head, plan.records)
    assert replay(plan, original, intervening_records=[
        later(REF, ['params', 'missing'], None)]).records == plan.records


@pytest.mark.parametrize('container', [ABSENT, present(None)])
def test_clear_then_set_batch_preserves_exact_absent_or_null_inverse(container):
    original = bundle()
    if container != ABSENT:
        clip(original, REF)['params'] = copy.deepcopy(container['value'])
    commands = [capture_clip_set_params(original, REF, clear=['missing']),
                capture_clip_set_params(original, REF, values={'x': 1})]
    plan = execute(original, commands)
    assert replay(plan, original).records == plan.records
    undone = inverse(plan.bundle, plan.records)
    assert undone.bundle == original
    assert inverse(undone.bundle, undone.records).bundle == plan.bundle


def test_registered_sprite_strip_binding_refuses_selected_registry_retarget():
    from astrid.core.element import load_default_registry
    from astrid.core.timeline.authoring_bundle import compile_authoring_candidate

    definition = load_default_registry().get('effect', 'am-sprite', pack_id='astrid_motion')
    frames = {'frameWidth': 16, 'frameHeight': 16, 'count': 4, 'fps': 8}
    Draft202012Validator(definition.schema).validate({'frames': frames})
    original = bundle()
    clip(original, REF)['clipType'] = definition.id
    plan = execute(original, [capture_clip_set_params(original, REF, values={'frames': frames})])
    head = copy.deepcopy(original)
    registry_ref = 'occurrence:occ-1/registry'
    State(head).entity(registry_ref)['assets']['old-picture']['media_id'] = 'sha256:' + 'f' * 64
    # Main accepts this closure; this is not an unsupported registry shape.
    compiled = compile_authoring_candidate(head).publication
    assert any(row['payload']['registry']['assets']['old-picture']['media_id'] == 'sha256:' + 'f' * 64
               for row in compiled['internal_timeline_revisions'])
    # The same clip.asset now selects different strip bytes; frame rectangles
    # authored for the original strip must not silently target that replacement.
    with pytest.raises(CommandError):
        replay(plan, head)
    unrelated = copy.deepcopy(original)
    State(unrelated).entity(registry_ref)['assets']['voice']['media_id'] = 'sha256:' + 'e' * 64
    assert replay(plan, unrelated).commands == plan.commands
    path = ['assets', 'old-picture', 'media_id']
    assert replay(plan, original, intervening_records=[
        later(registry_ref, path, None)]).records == plan.records


def test_registry_and_label_after_move_preserves_move_inverse_redo():
    assert {'track.set', 'clip.set-params'} <= operations().keys()
    original = bundle()
    moved = execute(original, [capture_move(original, 'parent/clip:direct', 4)])
    # A scalar edit consumes no timing, lane membership or containing revision.
    command = capture_track_set(moved.bundle, 'parent/track:picture', values={'label': 'Picture'})
    assert command['reads'] == [{'ref': 'parent/track:picture', 'path': ['id'],
                                 'state': present('picture')}]
    result = execute(moved.bundle, [command])
    assert State(result.bundle).entity('parent/track:picture')['label'] == 'Picture'
    undone = inverse(result.bundle, result.records)
    assert undone.bundle == moved.bundle
    assert inverse(undone.bundle, undone.records).bundle == result.bundle


@pytest.mark.parametrize('before', [ABSENT, present(None), present(''), present('Old')])
@pytest.mark.parametrize('clear', [False, True])
def test_track_label_exact_clear_and_original_presence(before, clear):
    original = bundle()
    ref = 'occurrence:occ-1/track:picture'
    if before != ABSENT:
        State(original).entity(ref)['label'] = copy.deepcopy(before['value'])
    snapshot = copy.deepcopy(original)
    command = capture_track_set(original, ref, values={} if clear else {'label': ''},
                                clear=['label'] if clear else [])
    assert command['expect'] == {ref + '::label': before}
    plan = execute(original, [command])
    assert State(plan.bundle).get(ref, ['label']) == (ABSENT if clear else present(''))
    assert original == snapshot
    assert inverse(plan.bundle, plan.records).bundle == original
    assert replay(plan, original).records == plan.records
    assert 'label' not in State(plan.bundle).entity('parent/track:picture')
    assert 'label' not in State(plan.bundle).entity('occurrence:occ-2/track:picture')


@pytest.mark.parametrize('container', [ABSENT, present(None), present({}),
                                      present({'old': None, 'empty': '', 'opaque': [None, {}]})])
def test_param_set_clear_null_and_opaque_literal_keys_roundtrip(container):
    original = bundle()
    if container != ABSENT:
        clip(original, REF)['params'] = copy.deepcopy(container['value'])
    values = {'null': None, 'a.b[0]::c': {'unknown': [None, False, {}, 'ƒ(keep literal)']}, '': ''}
    command = capture_clip_set_params(original, REF, values=values, clear=['old'])
    plan = execute(original, [command])
    params = clip(plan.bundle, REF)['params']
    assert all(params[key] == value for key, value in values.items())
    assert 'old' not in params
    if container.get('value'):
        assert params['opaque'] == [None, {}] and params['empty'] == ''
    assert inverse(plan.bundle, plan.records).bundle == original
    undone = inverse(plan.bundle, plan.records)
    assert inverse(undone.bundle, undone.records).bundle == plan.bundle
    assert replay(plan, original).records == plan.records
    assert execute(original, [plan.commands[0]]).records == plan.records
    assert 'params' not in clip(plan.bundle, 'occurrence:occ-2/clip:picture-1')
    assert 'params' not in clip(plan.bundle)


def test_authored_json_own_keys_survive_state_canonical_reload_digest_and_inverse():
    original = bundle()
    value = {
        '__proto__': {'kind': 'data', 'enabled': False},
        'constructor': {'kind': 'data', 'enabled': True},
        'prototype': {'kind': 'data', 'count': 0},
        'boolean': False,
        'number': 0,
    }
    command = capture_clip_set_params(original, REF, values=value)
    plan = execute(original, [command])
    params = State(plan.bundle).entity(REF)['params']
    assert params == value and set(params) == set(value)
    for key, expected in value.items():
        assert State(plan.bundle).get(REF, ['params', key]) == present(expected)
    assert not equal(State(plan.bundle).get(REF, ['params', 'boolean']),
                     State(plan.bundle).get(REF, ['params', 'number']))
    for key in ('__proto__', 'constructor', 'prototype'):
        assert State(plan.bundle).entity('parent/clip:' + key, optional=True) is None

    encoded = _json(plan.bundle)
    assert json.loads(encoded) == plan.bundle
    assert _digest(json.loads(encoded)) == _digest(plan.bundle)
    reloaded = json.loads(encoded)
    assert State(reloaded).entity(REF)['params'] == value
    undone = inverse(reloaded, plan.records)
    assert undone.bundle == json.loads(_json(original))
    redone = inverse(undone.bundle, undone.records)
    assert redone.bundle == reloaded


def test_null_value_and_clear_have_distinct_leaf_claims():
    original = bundle()
    clip(original, REF)['params'] = {'keep': {}, 'null': None}
    set_null = capture_clip_set_params(original, REF, values={'new': None})
    clear_null = capture_clip_set_params(original, REF, clear=['null'])
    assert set_null['expect'] == {REF + '::params.new': ABSENT}
    assert clear_null['expect'] == {REF + '::params.null': present(None)}
    for command in (set_null, clear_null):
        assert all(read['path'] != ['params'] for read in command['reads'])


def test_disjoint_same_owner_leaf_rebase_inverse_and_no_containing_revision_guard():
    original = bundle()
    clip(original, REF)['params'] = {'x': 1, 'y': None, 'opaque': {'keep': []}}
    a = execute(original, [capture_clip_set_params(original, REF, values={'x': 2})])
    b = execute(original, [capture_clip_set_params(original, REF, values={'y': False})])
    result = replay(b, a.bundle, intervening_records=a.records)
    assert clip(result.bundle, REF)['params'] == {'x': 2, 'y': False, 'opaque': {'keep': []}}
    assert result.commands == b.commands
    assert inverse(result.bundle, result.records).bundle == a.bundle
    changed = copy.deepcopy(original)
    for source in changed['source_mapping']['shots'].values():
        source['revision_id'] = 'new-containing-revision'
        source['content_digest'] = 'sha256:' + 'f' * 64
    assert replay(b, changed).commands == b.commands


@pytest.mark.parametrize('op,ref,values,path', [
    ('track.set', 'parent/track:picture', {'label': ''}, ['label']),
    ('clip.set-params', REF, {'x': None}, ['params', 'x']),
])
def test_unchanged_intent_and_aba_allow_rebase_but_guard_inverse(op, ref, values, path):
    original = bundle()
    if op == 'track.set':
        State(original).entity(ref)['label'] = ''
    else:
        clip(original, ref)['params'] = {'x': None}
    plan = execute(original, [call(op, ref, values)])
    assert plan.bundle == original
    assert any(w['path'] == path and w['before'] == w['after'] for w in plan.records[0]['writes'])
    for history in ([later(ref, path, values[next(iter(values))])],
                    [later(ref, path, 'away'), later(ref, path, values[next(iter(values))])]):
        with pytest.raises(CommandError, match='intervening'):
            inverse(plan.bundle, plan.records, history)
        assert replay(plan, original, intervening_records=history).records == plan.records


@pytest.mark.parametrize('original,value,conflict', [(2, 2.0, False), (True, 1, True)])
def test_ordinary_rebase_keeps_numeric_equivalence_and_boolean_distinction(original, value, conflict):
    before = bundle()
    clip(before, REF)['params'] = {'x': original}
    plan = execute(before, [capture_clip_set_params(before, REF, values={'x': 'B'})])
    head = copy.deepcopy(before)
    clip(head, REF)['params']['x'] = value
    history = [later(REF, ['params', 'x'], value)]
    if conflict:
        with pytest.raises(CommandError):
            replay(plan, head, intervening_records=history)
    else:
        assert replay(plan, head, intervening_records=history).commands == plan.commands


@pytest.mark.parametrize('change', ['delete', 'owner', 'source', 'value', 'type', 'asset'])
def test_original_identity_and_value_guards_refuse_without_partial_writes(change):
    original = bundle()
    clip(original, REF)['params'] = {'x': 1}
    command = capture_clip_set_params(original, REF, values={'x': 2})
    head = copy.deepcopy(original)
    if change == 'delete':
        State(head).config('occurrence:occ-1')['clips'].clear()
    elif change == 'owner':
        head['placements'][0]['shot_id'] = head['placements'][1]['shot_id']
    elif change == 'source':
        shot = head['placements'][0]['shot_id']
        head['source_mapping']['shots'][shot]['shot_id'] = 'other-source'
    elif change == 'value':
        clip(head, REF)['params']['x'] = 3
    elif change == 'type':
        clip(head, REF)['clipType'] = 'audio'
    else:
        clip(head, REF)['asset'] = 'new-asset'
    snapshot = copy.deepcopy(head)
    with pytest.raises(CommandError):
        execute(head, [command])
    assert head == snapshot


def test_declared_param_validation_uses_raw_stored_units_and_literal_values():
    original = bundle()
    clip(original, REF)['clipType'] = 'am-sprite'
    plan = execute(original, [capture_clip_set_params(original, REF, values={'x': 17, 'scale': 6})])
    assert clip(plan.bundle, REF)['params'] == {'x': 17, 'scale': 6}
    for values in ({'scale': True}, {'scale': 25}, {'flipX': 0}, {'stepFrames': 4},
                   {'keyframes': [{'frame': -1, 'x': 0, 'y': 0}]}, {'x': 'ƒ(keep literal)'}, {'x': None}):
        with pytest.raises(CommandError):
            capture_clip_set_params(original, REF, values=values)
    # Clearing bypasses value validation and removes presence explicitly.
    clear = capture_clip_set_params(plan.bundle, REF, clear=['x'])
    assert 'x' not in clip(execute(plan.bundle, [clear]).bundle, REF)['params']


@pytest.mark.parametrize('op,ref,values', [
    ('track.set', 'parent/track:picture', {'label': 'New'}),
    ('clip.set-params', REF, {'literal': None}),
])
@pytest.mark.parametrize('damage', ['unknown', 'missing', 'overlap', 'empty', 'duplicate',
                                   'shared', 'unscoped', 'version', 'intent', 'nan'])
def test_closed_schema_and_semantic_args_refuse(op, ref, values, damage):
    command = call(op, ref, values)
    if damage == 'unknown':
        command['args']['config'] = {}
    elif damage == 'missing':
        del command['args']['clear']
    elif damage == 'overlap':
        command['args']['clear'] = list(values)
    elif damage == 'empty':
        command['args']['values'] = {}
    elif damage == 'duplicate':
        command['args']['clear'] = list(values) * 2
    elif damage == 'shared':
        command['select'] = 'shot:source/' + ref.rsplit('/', 1)[1]
    elif damage == 'unscoped':
        command['select'] = ref.rsplit('/', 1)[1]
    elif damage == 'version':
        command['version'] = '2'
    elif damage == 'intent':
        command['intent'] = {'ownership': 'shared'}
    else:
        command['args']['values'][next(iter(values))] = float('nan')
    with pytest.raises(CommandError):
        execute(bundle(), [command])


@pytest.mark.parametrize('values', [{'name': 'wrong field'}, {'label': None}, {'id': 'new'},
                                    {'kind': 'audio'}, {'params': {}}, {'label': True}])
def test_track_set_cannot_mutate_nonlabel_fields(values):
    with pytest.raises(CommandError):
        capture_track_set(bundle(), 'parent/track:picture', values=values)


def test_params_cannot_replace_nonobject_container_or_nonstring_json_key():
    original = bundle()
    clip(original, REF)['params'] = []
    with pytest.raises(CommandError, match='container'):
        capture_clip_set_params(original, REF, values={'x': 1})
    with pytest.raises(CommandError):
        execute(bundle(), [call('clip.set-params', REF, {1: None})])


@pytest.mark.parametrize('name', ['track', 'params_object', 'params_null', 'params_absent', 'disjoint'])
def test_portable_scalar_capture_goldens(name):
    fixture = json.loads((FIXTURES / f'recipe_m2_scalar_{name}.json').read_text())
    plan = execute(fixture['bundle'], [fixture['command']])
    assert plan.commands == [fixture['command']]
    assert execute(fixture['bundle'], [fixture['request']]).commands == plan.commands
    assert plan.bundle == fixture['after_bundle']
    schema = json.loads((Path(__file__).parents[2] / 'astrid/core/timeline/schema/edit-command.schema.json').read_text())
    Draft202012Validator(schema).validate(fixture['command'])
    assert inverse(plan.bundle, plan.records).bundle == fixture['bundle']
    if 'head_bundle' in fixture:
        assert replay(plan, fixture['head_bundle']).bundle == fixture['rebased_bundle']


def test_scalar_capture_and_save_import_no_renderer_or_runtime():
    script = '''
import json, sys
class Block:
    def find_spec(self, fullname, *args):
        if fullname.startswith(('runtime_protocol', 'composition', 'remotion', 'astrid.packs.rendering',
                                'astrid.sdk.authoring_render_preview', 'astrid.core.timeline.compatibility')) or '.scene' in fullname:
            raise AssertionError(fullname)
sys.meta_path.insert(0, Block())
from astrid.sdk.timeline.commands import execute
for path in sys.argv[1:]:
    f = json.load(open(path))
    assert execute(f['bundle'], [f['request']]).commands == [f['command']]
'''
    result = subprocess.run([sys.executable, '-c', script,
                             *map(str, sorted(FIXTURES.glob('recipe_m2_scalar_*.json')))],
                            env={**os.environ, 'PYTHONDONTWRITEBYTECODE': '1'},
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
