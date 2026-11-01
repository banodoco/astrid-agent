"""Independent canonical occurrence copy through the native recipe and history inverse."""
from __future__ import annotations

import copy
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from astrid.core.timeline.authoring_bundle import _identity, compile_authoring_candidate
from astrid.core.timeline.shot_composition import (
    stable_occurrence_deep_link,
    stable_output_identity,
)
from astrid.sdk.timeline._collections import AcceptedRecords
from astrid.sdk.timeline._command_state import CommandError, State
from astrid.sdk.timeline.commands import _SCHEMA, execute
from astrid.sdk.timeline.commit import _digest, _json, bundle_from_document, document_from_bundle
from astrid.sdk.timeline.native_occurrence_duplicate import capture_occurrence_duplicate
from astrid.sdk.timeline.rebase import replay
from tests.editing_conformance.fixture_loader import load_fixture
from tests.editing_conformance.test_recipe_m1_commands import inverse, later
from tests.sdk.test_timeline_commit_provider_document import document_fixture

FIXTURE = Path(__file__).parent / 'fixtures/recipe_m2_occurrence_duplicate.json'


def duplicate_document(count=2, *, shared_shot=False):
    document = document_fixture(shared=False)
    parent = document['parent_revision']['payload']
    first = parent['occurrences'][0]
    first.update(source_offset=0, speed=1, metadata=None)
    parent['occurrences'] = [dict(copy.deepcopy(first), occurrence_id='occ-' + str(i)) for i in range(count)]
    if shared_shot:
        for row in parent['occurrences']:
            row['shot_revision_id'] = 'shot-1'
    document['parent_revision']['content_digest'] = _digest(parent)
    return document


def accepted(records, before, after):
    entry = {'records': {'commands': records}}
    if records[0]['command']['op'] == 'timeline.inverse':
        entry['records']['undo_of'] = records[0]['command']['args']['publications']
    return AcceptedRecords([entry], records, lambda _: (copy.deepcopy(before), copy.deepcopy(after)))


def reload(bundle, sources=()):
    return bundle_from_document(document_from_bundle(bundle, source_bundles=sources))


def duplicate(bundle, source, occurrence_id='occ-copy', shot_id='shot-copy'):
    return capture_occurrence_duplicate(bundle, 'occurrence:' + source, occurrence_id, shot_id)


def portable_golden():
    original = bundle_from_document(duplicate_document(2, shared_shot=True))
    command = duplicate(original, 'occ-0')
    plan = execute(original, [command])
    return {'bundle': original, 'select': command['select'], 'command': command,
            'after_bundle': plan.bundle, 'records': plan.records,
            'after_document': document_from_bundle(plan.bundle)}


@pytest.mark.parametrize('count,index', [(2, 0), (2, 1), (1, 0)])
def test_duplicate_reload_exact_guarded_inverse_redo(count, index):
    document = duplicate_document(count)
    original = bundle_from_document(document)
    before = copy.deepcopy(original)
    command = duplicate(original, 'occ-' + str(index))
    plan = execute(original, [command])
    assert original == before
    assert command['created'] == ['shot:shot-copy', 'occurrence:occ-copy']
    copied_document = document_from_bundle(plan.bundle)
    copied = bundle_from_document(copied_document)
    assert len(copied['placements']) == count + 1
    assert copied_document['dependency_manifest']['media'] == document['dependency_manifest']['media']
    undone = inverse(copied, accepted(plan.records, original, copied))
    assert document_from_bundle(undone.bundle, source_bundles=(original, copied)) == document
    restored = reload(undone.bundle, (original, copied))
    redone = inverse(restored, accepted(undone.records, copied, restored))
    assert document_from_bundle(redone.bundle, source_bundles=(original, copied)) == copied_document
    assert replay(plan, original).records == plan.records


def identity_document(count, fields, document_id):
    document = duplicate_document(count)
    source = document['parent_revision']['payload']['occurrences'][0]
    derived = {
        'stable_deep_link': stable_occurrence_deep_link(
            'project-1', document_id or 'film', source['shot_id'], source['shot_revision_id'], 'occ-0'),
        'output_identity': stable_output_identity('project-1', document_id or 'film', 'occ-0'),
    }
    source.update({field: derived[field] for field in fields})
    if document_id is not None:
        source['parent_document_id'] = document_id
    opaque = {'stable_deep_link': 'deep:occ-0', 'output_identity': 'out:occ-0',
              'shot_id': 'chapter-shot', 'occurrence_id': 'occ-0',
              'ordered': [None, False, 0, 1.0, {}, []]}
    source.update(output_records=[copy.deepcopy(opaque)], task=copy.deepcopy(opaque),
                  associations=[copy.deepcopy(opaque)], unknown=copy.deepcopy(opaque),
                  app={'org.example': copy.deepcopy(opaque)}, metadata=None)
    document['parent_revision']['content_digest'] = _digest(document['parent_revision']['payload'])
    return document


@pytest.mark.parametrize('count', [1, 2], ids=['single-source', 'shared-source'])
@pytest.mark.parametrize('fields', [(), ('stable_deep_link',), ('output_identity',),
                                   ('stable_deep_link', 'output_identity')])
@pytest.mark.parametrize('document_id', [None, 'doc /?%'])
def test_root_identities_raw_compile_reload_guarded_undo_redo(count, fields, document_id):
    document = identity_document(count, fields, document_id)
    original = bundle_from_document(document)
    before = copy.deepcopy(original)
    plan = execute(original, [duplicate(original, 'occ-0')])
    preview = copy.deepcopy(State(plan.bundle).entity('occurrence:occ-copy'))
    compiled = document_from_bundle(plan.bundle)
    rows = compiled['parent_revision']['payload']['occurrences']
    copied = rows[-1]
    revision = copied['shot_revision_id']
    derived = {
        'stable_deep_link': stable_occurrence_deep_link(
            'project-1', document_id or 'film', 'shot-copy', revision, 'occ-copy'),
        'output_identity': stable_output_identity('project-1', document_id or 'film', 'occ-copy'),
    }
    assert _json(copied) == _json({**document['parent_revision']['payload']['occurrences'][0],
                                 'occurrence_id': 'occ-copy', 'shot_id': 'shot-copy',
                                 'shot_revision_id': revision, **{key: derived[key] for key in fields}})
    raw = compile_authoring_candidate(plan.bundle).publication
    assert _json(raw['parent_composition']['occurrences'][-1]) == _json(copied)
    assert _json(rows[:-1]) == _json(document['parent_revision']['payload']['occurrences'])
    assert _json(original) == _json(before)
    assert original['source_mapping']['_runtime_document'] == document
    shot = next(row for row in compiled['shot_revisions'] if row['shot_id'] == 'shot-copy')
    assert shot['revision_id'] == revision
    assert shot['payload']['internal_timeline_revision_id'] == shot['internal_timeline_revision_id']
    internal = next(row for row in compiled['internal_timeline_revisions']
                    if row['revision_id'] == shot['internal_timeline_revision_id'])
    assert internal['payload'] == document['internal_timeline_revisions'][0]['payload']
    assert compiled['dependency_manifest']['media'] == document['dependency_manifest']['media']
    reloaded = bundle_from_document(compiled)
    undone = inverse(reloaded, accepted(plan.records, original, reloaded))
    assert document_from_bundle(undone.bundle, source_bundles=(original, reloaded)) == document
    restored = reload(undone.bundle, (original, reloaded))
    redone = inverse(restored, accepted(undone.records, reloaded, restored))
    assert document_from_bundle(redone.bundle, source_bundles=(original, reloaded)) == compiled
    recorded = next(write['after']['value'] for write in plan.records[0]['writes']
                    if write['ref'] == 'occurrence:occ-copy' and not write['path'])
    assert _json(recorded) == _json(preview) == _json(State(reloaded).entity('occurrence:occ-copy'))
    assert replay(plan, original).records == plan.records


@pytest.mark.parametrize('change', [
    'stable_deep_link', 'output_identity', 'unknown', 'output_records', 'task', 'associations', 'app',
    'identity-aba', 'opaque-aba', 'source-aba', 'source-order', 'dependent',
])
def test_reloaded_identity_duplicate_keeps_strict_inverse_guards(change):
    original = bundle_from_document(identity_document(2, ('stable_deep_link', 'output_identity'), None))
    plan = execute(original, [duplicate(original, 'occ-0')])
    copied = reload(plan.bundle)
    head = copy.deepcopy(copied)
    state = State(head)
    intervening = []
    if change.endswith('-aba'):
        ref, path = {
            'identity-aba': ('occurrence:occ-copy', ['output_identity']),
            'opaque-aba': ('occurrence:occ-copy', ['output_records']),
            'source-aba': ('occurrence:occ-0', []),
        }[change]
        intervening = [later(ref, path, state.get(ref, path)['value'])]
    elif change == 'source-order':
        state.config('occurrence:occ-0')['clips'].reverse()
    elif change == 'dependent':
        head['placements'].append({**copy.deepcopy(state.entity('occurrence:occ-copy')),
                                   'occurrence_id': 'dependent'})
    elif change in ('stable_deep_link', 'output_identity'):
        state.entity('occurrence:occ-copy')[change] = 'changed'
    else:
        nested = state.entity('occurrence:occ-copy')[change]
        nested = nested[0] if isinstance(nested, list) else nested
        nested = nested['org.example'] if change == 'app' else nested
        nested['output_identity'] = 'changed'
    before = copy.deepcopy(head)
    with pytest.raises(CommandError) as error:
        inverse(head, accepted(plan.records, original, copied), intervening)
    assert error.value.kind == ('intervening_write' if change.endswith('-aba') else
                                'inverse_dependency_changed' if change == 'source-order' else
                                'surviving_dependents' if change == 'dependent' else 'inverse_conflict')
    assert head == before


def test_copy_preserves_exact_source_content_and_only_identity_changes():
    original = bundle_from_document(duplicate_document())
    State(original).entity('occurrence:occ-0')['metadata'] = {'explicit': None, 'ordered': [False, 0, []]}
    source = copy.deepcopy(State(original).entity('occurrence:occ-0'))
    plan = execute(original, [duplicate(original, 'occ-0')])
    assert State(plan.bundle).entity('occurrence:occ-copy') == {**source, 'occurrence_id': 'occ-copy',
                                                                 'shot_id': 'shot-copy'}
    assert State(plan.bundle).entity('occurrence:occ-0') == source
    assert State(plan.bundle).config('occurrence:occ-copy') == State(original).config('occurrence:occ-0')


def test_shared_source_copy_and_later_sibling_edit_are_isolated():
    original = bundle_from_document(duplicate_document(shared_shot=True))
    plan = execute(original, [duplicate(original, 'occ-0')])
    head = copy.deepcopy(original)
    State(head).entity('occurrence:occ-1/clip:clip-a')['hold'] = 3
    intervening = [later('occurrence:occ-1/clip:clip-a', ['hold'], 3)]
    rebased = replay(plan, head, intervening_records=intervening)
    assert State(rebased.bundle).entity('occurrence:occ-1/clip:clip-a')['hold'] == 3
    assert State(rebased.bundle).entity('occurrence:occ-copy/clip:clip-a')['hold'] == 2


SOURCE_GUARDS = [
    ('occurrence:occ-0', []), ('occurrence:occ-0', ['shot_id']),
    ('occurrence:occ-0/source', ['clips']),
    ('occurrence:occ-0/shot', ['payload']),
]


@pytest.mark.parametrize('ref,path', SOURCE_GUARDS)
def test_equal_state_and_aba_publication_history_refuses_on_inverse(ref, path):
    original = bundle_from_document(duplicate_document())
    plan = execute(original, [duplicate(original, 'occ-0')])
    value = State(original).get(ref, path)['value']
    with pytest.raises(CommandError, match='intervening'):
        inverse(plan.bundle, accepted(plan.records, original, plan.bundle), [later(ref, path, value)])


@pytest.mark.parametrize('ref,path', SOURCE_GUARDS[:1] + SOURCE_GUARDS[2:3])
def test_equal_state_and_aba_publication_refuses_on_ordinary_replay(ref, path):
    original = bundle_from_document(duplicate_document())
    plan = execute(original, [duplicate(original, 'occ-0')])
    value = State(original).get(ref, path)['value']
    with pytest.raises(CommandError, match='intervening'):
        replay(plan, original, intervening_records=[later(ref, path, value)])


@pytest.mark.parametrize('ref,path', SOURCE_GUARDS[1:2] + SOURCE_GUARDS[3:])
def test_equal_value_sub_path_publication_on_ordinary_replay_reproduces_same_copy(ref, path):
    # Ordinary replay checks values on the final head (identities_only), so an
    # equal-value sub-path ABA is admitted only because the copy is identical; the
    # inverse path above remains history-sensitive and refuses it.
    original = bundle_from_document(duplicate_document())
    plan = execute(original, [duplicate(original, 'occ-0')])
    value = State(original).get(ref, path)['value']
    rebased = replay(plan, original, intervening_records=[later(ref, path, value)])
    assert State(rebased.bundle).entity('occurrence:occ-copy') == State(plan.bundle).entity('occurrence:occ-copy')


@pytest.mark.parametrize('change', ['membership', 'source-shot', 'shot-content', 'metadata', 'duplicate'])
def test_changed_source_or_identity_refuses_atomically(change):
    original = bundle_from_document(duplicate_document())
    command = duplicate(original, 'occ-0')
    head = copy.deepcopy(original)
    state = State(head)
    if change == 'membership':
        state.config('occurrence:occ-0')['clips'].pop()
    elif change == 'source-shot':
        state.entity('occurrence:occ-0')['shot_id'] = head['placements'][1]['shot_id']
    elif change == 'shot-content':
        head['shots'][state.occurrence('occurrence:occ-0')['shot_id']]['payload']['metadata'] = None
    elif change == 'metadata':
        state.entity('occurrence:occ-0')['metadata'] = {}
    else:
        head['placements'].append({**copy.deepcopy(head['placements'][1]), 'occurrence_id': 'occ-copy'})
    before = copy.deepcopy(head)
    with pytest.raises(CommandError):
        execute(head, [command])
    assert head == before


@pytest.mark.parametrize('occurrence_id,shot_id', [
    ('occ-1', 'shot-copy'), ('occ-copy', 'chapter-shot'),
])
@pytest.mark.parametrize('fields', [(), ('stable_deep_link', 'output_identity')])
def test_reserved_identity_collisions_refuse(occurrence_id, shot_id, fields):
    original = bundle_from_document(identity_document(2, fields, None))
    with pytest.raises(CommandError) as error:
        execute(original, [capture_occurrence_duplicate(original, 'occurrence:occ-0', occurrence_id, shot_id)])
    assert error.value.kind == 'identity_collision'


def test_copy_rederives_only_root_identity_against_compiled_destination():
    original = bundle_from_document(duplicate_document())
    State(original).entity('occurrence:occ-0').update(
        stable_deep_link='deep:occ-0', output_identity='out:occ-0', parent_document_id='doc-1',
        output_records=[{'output_identity': 'out:occ-0'}])
    plan = execute(original, [duplicate(original, 'occ-0')])
    compiled = document_from_bundle(plan.bundle)
    rows = {row['occurrence_id']: row for row in compiled['parent_revision']['payload']['occurrences']}
    source, copied = rows['occ-0'], rows['occ-copy']
    revision = copied['shot_revision_id']
    assert copied['stable_deep_link'] == stable_occurrence_deep_link(
        'project-1', 'doc-1', 'shot-copy', revision, 'occ-copy')
    assert copied['output_identity'] == stable_output_identity('project-1', 'doc-1', 'occ-copy')
    assert {(row['shot_id'], row['revision_id']) for row in compiled['shot_revisions']} >= {('shot-copy', revision)}
    assert source['stable_deep_link'] == 'deep:occ-0' and source['output_identity'] == 'out:occ-0'
    assert copied['output_records'] == source['output_records'] == [{'output_identity': 'out:occ-0'}]
    assert {k: v for k, v in copied.items() if k not in (
        'occurrence_id', 'shot_id', 'shot_revision_id', 'stable_deep_link', 'output_identity')} == {
        k: v for k, v in source.items() if k not in (
            'occurrence_id', 'shot_id', 'shot_revision_id', 'stable_deep_link', 'output_identity')}


def test_derived_item_identity_collision_refuses_without_mutation():
    original = bundle_from_document(duplicate_document())
    command = duplicate(original, 'occ-0')
    planted = _identity('shot-item-placement', 'project-1', 'film', 'occ-copy', 'shot-copy', 'item-1')
    original['shots']['other-shot'] = {'shot_id': 'other-shot', 'base_payload': {}, 'payload': {'items': [
        {'item_id': planted}]}, 'base_internal_timeline': {}, 'internal_timeline': {}}
    before = copy.deepcopy(original)
    with pytest.raises(CommandError) as error:
        execute(original, [command])
    assert error.value.kind == 'identity_collision'
    assert original == before


def test_legacy_group_refuses_with_migration_needed_and_no_mutation():
    original = bundle_from_document(duplicate_document())
    original['parent']['config']['pinnedShotGroups'] = [{'shotId': 'chapter-shot', 'trackId': 'picture',
                                                       'clipIds': ['clip-a', 'clip-b']}]
    before = copy.deepcopy(original)
    with pytest.raises(CommandError) as error:
        duplicate(original, 'occ-0')
    assert error.value.kind == 'migration_needed'
    assert original == before


@pytest.mark.parametrize('select', ['parent', 'shot:chapter-shot', 'occ-0', 'occurrence:occ-0/clip:clip-a',
                                    'occurrence:missing'])
def test_explicit_single_occurrence_identity_only(select):
    original = bundle_from_document(duplicate_document())
    with pytest.raises(CommandError):
        capture_occurrence_duplicate(original, select, 'occ-copy', 'shot-copy')


def test_portable_native_capture_golden_matches_fixture():
    assert portable_golden() == load_fixture(FIXTURE)


@pytest.mark.parametrize('field,value', [
    ('args', {'occurrence_id': 'occ-copy', 'shot_id': 'shot-copy', 'shared': True}),
    ('args', {'occurrence_id': 'a::b', 'shot_id': 'shot-copy'}),
    ('intent', {'ownership': 'shared'}),
    ('version', '2'),
])
def test_closed_occurrence_duplicate_schema(field, value):
    command = duplicate(bundle_from_document(duplicate_document()), 'occ-0')
    validator = Draft202012Validator(_SCHEMA)
    assert not list(validator.iter_errors(command))
    assert list(validator.iter_errors(dict(command, **{field: value})))
