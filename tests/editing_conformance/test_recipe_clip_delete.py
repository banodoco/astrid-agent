"""Native bounded removal uses the same capture, replay and inverse recipe."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from astrid.sdk.timeline._command_state import ABSENT, CommandError, State, present
from astrid.sdk.timeline.capture import capture_clip_delete
from astrid.sdk.timeline.commands import execute, operations
from astrid.sdk.timeline.commit import bundle_from_document, document_from_bundle
from astrid.sdk.timeline.rebase import replay
from tests.editing_conformance.test_recipe_m1_commands import (
    accepted_boundaries,
    inverse,
    later,
)
from tests.sdk.test_timeline_commit_provider_document import document_fixture

FIXTURE = Path(__file__).parent / 'fixtures/recipe_clip_delete.json'


def delete_document():
    document = document_fixture()
    parent = document['parent_revision']['payload']
    rows = parent['clips']
    rows.append(dict(copy.deepcopy(rows[0]), id='clip-c', at=8))
    rows[0]['opaque'] = {'literal': 'parent/clip:clip-b', 'values': [None, False, 0]}
    rows[1]['nullable'] = None
    rows[1]['app'] = None
    rows[2].pop('app')
    parent['config']['clips'] = copy.deepcopy(rows)
    from astrid.sdk.timeline.commit import _digest
    document['parent_revision']['content_digest'] = _digest(parent)
    return document


def reload(bundle):
    return bundle_from_document(json.loads(json.dumps(document_from_bundle(bundle))))


def portable_golden():
    document = delete_document()
    document['parent_revision']['payload']['occurrences'] = []
    document['shot_revisions'] = document['internal_timeline_revisions'] = []
    document['dependency_manifest']['shots'] = document['dependency_manifest']['internal_timelines'] = []
    from astrid.sdk.timeline.commit import _digest
    document['parent_revision']['content_digest'] = _digest(document['parent_revision']['payload'])
    bundle = bundle_from_document(document)
    select = 'parent/clip:clip-b'
    command = capture_clip_delete(bundle, select)
    plan = execute(bundle, [command])
    return {'bundle': bundle, 'select': select, 'command': command,
            'after_bundle': plan.bundle, 'records': plan.records}


@pytest.mark.parametrize('identity', ['clip-a', 'clip-b', 'clip-c'])
def test_first_middle_last_delete_reload_inverse_redo_exact_authored_rows(identity):
    original = bundle_from_document(delete_document())
    immutable = copy.deepcopy(original)
    ref = 'parent/clip:' + identity
    command = capture_clip_delete(original, ref)
    plan = execute(original, [command])
    assert original == immutable
    record = plan.records[0]
    assert record['writes'] == [{'ref': ref, 'path': [],
                                 'before': present(State(original).entity(ref)), 'after': ABSENT}]
    assert {'ref': ref, 'path': [], 'state': record['writes'][0]['before']} in record['reads']
    assert command['resolved'] == [ref] and command['created'] == []
    assert command['expect'] == {ref + '::': record['writes'][0]['before']}
    assert command['authorship'] == {ref + '::': 'direct'}
    assert record['created'] == []
    assert all(binding['ref'].startswith('occurrence:') for binding in record['bindings'])
    assert [row['id'] for row in plan.bundle['parent']['clips']] == [
        row['id'] for row in original['parent']['clips'] if row['id'] != identity]
    removed = reload(plan.bundle)
    undone = inverse(removed, plan.records)
    restored = reload(undone.bundle)
    assert json.dumps(restored['parent']) == json.dumps(original['parent'])
    restored_document = document_from_bundle(restored)
    original_document = document_from_bundle(original)
    assert restored_document['parent_revision']['content_digest'] == original_document['parent_revision']['content_digest']
    for key in ('shot_revisions', 'internal_timeline_revisions', 'dependency_manifest'):
        assert restored_document[key] == original_document[key]
    records = accepted_boundaries(undone.records, removed, restored)
    assert document_from_bundle(inverse(restored, records).bundle) == document_from_bundle(removed)
    assert replay(plan, original).records == plan.records


@pytest.mark.parametrize('layout', ['payload', 'config', 'both', 'null-payload', 'null-config'])
def test_last_member_preserves_exact_collection_container_presence(layout):
    original = bundle_from_document(delete_document())
    parent = original['parent']
    parent['clips'] = parent['clips'][:1]
    parent['config']['clips'] = copy.deepcopy(parent['clips'])
    if layout == 'payload':
        parent['config'].pop('clips')
    elif layout == 'config':
        parent.pop('clips')
    elif layout == 'null-payload':
        parent['clips'] = None
    elif layout == 'null-config':
        parent['config']['clips'] = None
    plan = execute(original, [capture_clip_delete(original, 'parent/clip:clip-a')])
    assert State(plan.bundle).collection('parent', 'clip') == []
    restored = inverse(reload(plan.bundle), plan.records)
    assert restored.bundle['parent'] == parent


def test_unrelated_concurrent_property_is_preserved_on_replay_and_inverse():
    original = bundle_from_document(delete_document())
    plan = execute(original, [capture_clip_delete(original, 'parent/clip:clip-b')])
    head = copy.deepcopy(original)
    State(head).entity('parent/clip:clip-a')['opaque']['newer'] = [None, 5]
    head['parent']['config']['clips'] = copy.deepcopy(head['parent']['clips'])
    intervening = [later('parent/clip:clip-a', ['opaque', 'newer'], [None, 5])]
    rebased = replay(plan, head, intervening_records=intervening)
    undone = inverse(reload(rebased.bundle), plan.records, intervening)
    assert undone.bundle['parent'] == head['parent']


@pytest.mark.parametrize('change', ['order', 'membership', 'target-source', 'target-opaque', 'dependency'])
def test_original_capture_rejects_relevant_changes_atomically(change):
    original = bundle_from_document(delete_document())
    command = capture_clip_delete(original, 'parent/clip:clip-b')
    head = copy.deepcopy(original)
    rows = head['parent']['clips']
    if change == 'order':
        rows.reverse()
    elif change == 'membership':
        rows.append(dict(copy.deepcopy(rows[0]), id='new'))
    elif change == 'target-source':
        rows[1]['asset'] = 'new-source'
    elif change == 'target-opaque':
        rows[1]['opaque'] = {'newer': True}
    else:
        rows[0]['app']['astrid'] = {'on': 'c01'}
    head['parent']['config']['clips'] = copy.deepcopy(rows)
    before = copy.deepcopy(head)
    with pytest.raises(CommandError):
        execute(head, [command])
    assert head == before


@pytest.mark.parametrize('ref,path,value', [
    ('parent/clip:clip-b', ['asset'], 'picture'),
    ('parent/clip:clip-a', ['id'], 'clip-a'),
    ('parent', ['clips'], []),
    ('parent/clip:clip-a', ['app', 'astrid', 'on'], None),
])
def test_equal_state_rebase_keeps_identity_replacement_and_inverse_guards(ref, path, value):
    original = bundle_from_document(delete_document())
    plan = execute(original, [capture_clip_delete(original, 'parent/clip:clip-b')])
    intervening = [later(ref, path, value)]
    if path == ['id'] or ref == 'parent':
        with pytest.raises(CommandError) as error:
            replay(plan, original, intervening_records=intervening)
        assert error.value.kind == 'intervening_write'
    else:
        assert replay(plan, original, intervening_records=intervening).records == plan.records
    with pytest.raises(CommandError) as error:
        inverse(plan.bundle, plan.records, intervening)
    assert error.value.kind == 'intervening_write'


@pytest.mark.parametrize('owner', ['parent', 'occurrence:occ-a'])
@pytest.mark.parametrize('relation', [
    {'on': 'c01'}, {'until': 'end of c01'}, {'on': '"A chapter"'},
    {'cut': 'c01'}, {'sequence': {'id': 'steps', 'index': 0}},
    {'formulas': {'params.when': {'word': 'line:0', 'as': 'clip_seconds'}}},
    {'replaces': ['clip-b']},
])
def test_main_declared_relations_refuse_without_guessing_dependency_proof(owner, relation):
    original = bundle_from_document(delete_document())
    ref = owner + '/clip:clip-a'
    State(original).entity(ref)['app'] = {'astrid': relation}
    if owner == 'parent':
        original['parent']['config']['clips'] = copy.deepcopy(original['parent']['clips'])
    before = copy.deepcopy(original)
    with pytest.raises(CommandError) as error:
        capture_clip_delete(original, 'parent/clip:clip-b')
    assert error.value.kind == 'unsupported_dependency'
    assert original == before


@pytest.mark.parametrize('surface', ['groups', 'film-formula', 'transition', 'legacy-shot', 'nested'])
def test_unsupported_semantic_or_ownership_surfaces_refuse(surface):
    original = bundle_from_document(delete_document())
    parent = original['parent']
    if surface == 'groups':
        parent['config']['pinnedShotGroups'] = [{'id': 'g', 'clipIds': ['clip-b']}]
    elif surface == 'film-formula':
        parent['config']['app'] = {'astrid': {'formulas': {'x': {'word': 'line:0'}}}}
    elif surface == 'transition':
        parent['clips'][0]['transition'] = {'id': 'crossfade', 'duration': .2}
    elif surface == 'legacy-shot':
        parent['clips'][1]['clipType'] = 'shot'
        parent['clips'][1]['params'] = {'shot_id': 's', 'timeline_document_id': 'child'}
    else:
        parent['clips'][1]['children'] = []
    parent['config']['clips'] = copy.deepcopy(parent['clips'])
    with pytest.raises(CommandError) as error:
        capture_clip_delete(original, 'parent/clip:clip-b')
    assert error.value.kind == ('unsupported_ownership' if surface in ('legacy-shot', 'nested') else 'unsupported_dependency')


@pytest.mark.parametrize('transition', ['crossfade', {'type': 'crossfade', 'duration': .2},
    {'id': 'crossfade', 'durationFrames': 6, 'params': {'label': 'parent/clip:clip-b'}}])
def test_unrelated_child_transition_preserved_through_parent_delete_reload_inverse_redo(transition):
    original = bundle_from_document(delete_document())
    child = State(original).entity('occurrence:occ-a/clip:clip-b')
    child['transition'] = copy.deepcopy(transition)
    plan = execute(original, [capture_clip_delete(original, 'parent/clip:clip-b')])
    assert plan.bundle['shots'] == original['shots']
    reloaded = reload(plan.bundle)
    undone = inverse(reloaded, plan.records)
    assert document_from_bundle(undone.bundle) == document_from_bundle(original)
    redone = inverse(undone.bundle, accepted_boundaries(undone.records, reloaded, undone.bundle))
    assert document_from_bundle(redone.bundle) == document_from_bundle(plan.bundle)


@pytest.mark.parametrize('transition', [True, ['crossfade'], {'owner_ref': 'parent/clip:clip-b'},
    {'id': 'crossfade', 'clip_ref': 'parent/clip:clip-b'}, {'id': 'crossfade', 'params': []}])
def test_unrecognized_child_transition_shapes_still_refuse_untouched(transition):
    original = bundle_from_document(delete_document())
    State(original).entity('occurrence:occ-a/clip:clip-b')['transition'] = transition
    before = copy.deepcopy(original)
    with pytest.raises(CommandError) as error:
        capture_clip_delete(original, 'parent/clip:clip-b')
    assert error.value.kind == 'unsupported_dependency'
    assert original == before


@pytest.mark.parametrize('select', [None, 'parent', 'clip-b', 'parent/clip:', 'parent/clip:clip-b/extra',
                                    'parent/clip:a::b', 'shot:s/clip:clip-b',
                                    'occurrence:occ-a/clip:clip-b'])
def test_only_one_parent_owned_identity_is_supported(select):
    with pytest.raises(CommandError) as error:
        capture_clip_delete(bundle_from_document(delete_document()), select)
    assert error.value.kind == 'unsupported_ownership'


@pytest.mark.parametrize('damage', ['args', 'intent', 'missing', 'duplicate', 'mirror', 'resolved', 'reads'])
def test_closed_parameters_singleton_identity_and_capture_claims(damage):
    original = bundle_from_document(delete_document())
    call = capture_clip_delete(original, 'parent/clip:clip-b')
    if damage == 'args':
        call['args'] = {'cleanup_media': True}
    elif damage == 'intent':
        call['intent'] = {'ripple': 'selected'}
    elif damage == 'resolved':
        call['resolved'].append('parent/clip:clip-a')
    elif damage == 'reads':
        call['reads'] = []
    elif damage == 'missing':
        original['parent']['clips'].pop(1)
    elif damage == 'duplicate':
        original['parent']['clips'].append(copy.deepcopy(original['parent']['clips'][1]))
    else:
        original['parent']['config']['clips'].pop()
    before = copy.deepcopy(original)
    with pytest.raises(CommandError):
        execute(original, [call])
    assert original == before


def test_published_app_golden_matches_exact_native_capture_and_recording():
    fixture = json.loads(FIXTURE.read_text())
    assert portable_golden() == fixture
    command = capture_clip_delete(fixture['bundle'], fixture['select'])
    assert command == fixture['command']
    plan = execute(fixture['bundle'], [command])
    assert plan.bundle == fixture['after_bundle'] and plan.records == fixture['records']
    Draft202012Validator(operations()['clip.delete'].parameters).validate({})


def test_canonical_delete_schema_closes_args_intent_and_version():
    schema = json.loads((Path(__file__).parents[2] / 'astrid/core/timeline/schema/edit-command.schema.json').read_text())
    validator = Draft202012Validator(schema)
    valid = capture_clip_delete(bundle_from_document(delete_document()), 'parent/clip:clip-b')
    validator.validate(valid)
    for field, value in [('args', {'cleanup': True}), ('intent', {'ripple': 'selected'}), ('version', '2')]:
        invalid = dict(valid, **{field: value})
        assert list(validator.iter_errors(invalid))
