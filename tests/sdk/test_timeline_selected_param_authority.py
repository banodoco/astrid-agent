"""Selected-PARAM authority through native commands and accepted history."""
from __future__ import annotations

import copy
import json

import pytest

from astrid.core.element import project_packs
from astrid.sdk.timeline._clip_commands import capture_clip_add
from astrid.sdk.timeline._clip_scalar_commands import capture_clip_set_element
from astrid.sdk.timeline._command_state import State
from astrid.sdk.timeline._scalar_commands import capture_clip_set_params, capture_track_set
from astrid.sdk.timeline.commit import _digest, _history, create_runtime_commit_service
from astrid.sdk.timeline.native_occurrence_duplicate import capture_occurrence_duplicate
from tests.core import test_project_packs
from tests.editing_conformance.test_recipe_m2_clip_add import empty_document
from tests.editing_conformance.test_recipe_scalar_commands import schema_options
from tests.sdk.test_timeline_commit_provider import (
    CommitRejected,
    DetachedPort,
    DetachedRealm,
    inverse,
    restore,
    track_create,
)
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi
from tests.sdk.test_timeline_commit_provider_document import document_fixture
from tests.sdk.test_timeline_commit_provider_scalar import current

REF = 'parent/clip:effect'
configured_project_pack = test_project_packs.configured


@pytest.fixture
def two_pins(configured_project_pack):
    root, directory, manifest = test_project_packs.seed('project-1')
    record = project_packs.admit_project_pack('realm', 'project-1')['elements'][0]
    element = {key: record['descriptor'][key] for key in ('packId', 'id', 'kind', 'revision')}
    first = schema_options(configured_project_pack, element)
    test_project_packs.add_second_element(root, directory, manifest)
    project_packs.admit_project_pack('realm', 'project-1')
    second = schema_options(configured_project_pack, element)
    assert first['schema_source'] != second['schema_source']
    return element, first, second


def _realm(two_pins, document=None):
    realm = DetachedRealm(document or empty_document())
    for options in two_pins[1:]:
        verified, objects = options['schema_inputs'][0]
        realm.live_sources[_digest(verified['pin'])] = copy.deepcopy(verified)
        realm.snapshot_objects.update(copy.deepcopy(objects))
    return realm


def _commit(realm, calls, *, base=None):
    context = realm.context(calls, base=base)
    context['realm_id'] = 'realm'
    return create_runtime_commit_service().commit(context, DetachedPort(realm, context))


def _birth(realm, element):
    _commit(realm, [track_create('picture')])
    row = {'id': 'effect', 'clipType': element['id'], 'elementRef': element,
           'at': 0, 'hold': 2, 'track': 'picture', 'params': {'seed': 4}}
    _commit(realm, [capture_clip_add(current(realm), 'parent', row)])


def _reselect(realm, element, options, ref=REF):
    return _commit(realm, [capture_clip_set_element(
        current(realm), ref, element=element, **options)])


def _source(record, ref, element):
    # Only the single-target direct producer triple is decoded here. This is
    # deliberately not an inverse/restore/ancestry authority implementation.
    schema, = [dep for dep in record['dependencies'] if dep['key'] == 'params.schema']
    snapshot, = [dep for dep in record['dependencies'] if dep['key'] == 'params.snapshot']
    binding, = [item for item in record['bindings'] if item['key'] == 'params.element']
    assert schema['kind'] == 'source' and schema['policy'] == 'exact'
    assert snapshot['kind'] == 'entity' and snapshot['policy'] == 'exact'
    assert binding == {'key': 'params.element', 'ref': ref, 'policy': 'fixed',
                       'dependencies': ['params.schema', 'params.snapshot']}
    assert record['command']['select'] == ref
    assert record['command']['args']['schema_source'] == schema['pin']
    assert snapshot['pin']['source'] == schema['pin']
    assert snapshot['pin']['element'] == element
    return schema['pin']


def test_equal_json_opposite_pin_order_has_distinct_accepted_authority(two_pins, tmp_path):
    element, p1, p2 = two_pins
    cases, documents = [], []
    for first, last in ((p1, p2), (p2, p1)):
        realm = _realm(two_pins)
        _birth(realm, element)
        initial = copy.deepcopy(realm.documents[realm.head['revision_id']])
        initial_head = copy.deepcopy(realm.head)
        start = len(realm.entries)
        _reselect(realm, element, first)
        _commit(realm, [capture_clip_set_params(
            current(realm), REF, values={'seed': 9}, **first)])
        before = copy.deepcopy(realm.documents[realm.head['revision_id']])
        accepted = _reselect(realm, element, last)
        document = accepted['data']['canonical_document']
        record = realm.entries[-1]['records']['commands'][0]
        assert _source(record, REF, element) == last['schema_source']
        assert record['writes'] == [{'ref': REF, 'path': ['elementRef'],
                                     'before': {'state': 'present', 'value': element},
                                     'after': {'state': 'present', 'value': element}}]
        assert before['parent_revision']['payload'] == document['parent_revision']['payload']
        documents.append(document)
        # Keep actual record envelopes; omit receipts/results, which repeat
        # whole closures and do not authenticate this in-memory fixture.
        cases.append({'initial_head': initial_head, 'head': realm.head,
                      'expected_schema_source': last['schema_source'],
                      'history': [{key: entry[key] for key in (
                          'publication_id', 'revision_id', 'previous', 'records')}
                          for entry in realm.entries[start:]]})
    assert documents[0] == documents[1]
    assert cases[0]['expected_schema_source'] != cases[1]['expected_schema_source']
    assert len(documents[0]['dependency_manifest']['documents']) == 2
    fixture = {'scope': {'realm_id': 'realm', 'project_id': 'project-1', 'timeline_id': 'film'},
               'select': REF, 'element': element, 'input': initial,
               'schema_inputs': [options['schema_inputs'][0] for options in (p1, p2)],
               'canonical_document': documents[0], 'cases': cases}
    (tmp_path / 'selected-param-original-pin-counterexample.json').write_text(
        json.dumps(fixture, indent=2, sort_keys=True) + '\n')


def test_unchanged_param_and_clear_record_exact_source(two_pins):
    element, p1, _ = two_pins
    realm = _realm(two_pins)
    _birth(realm, element)
    _reselect(realm, element, p1)
    for values, clear in (({'seed': 4}, []), ({}, ['missing'])):
        _commit(realm, [capture_clip_set_params(
            current(realm), REF, values=values, clear=clear, **p1)])
        record = realm.entries[-1]['records']['commands'][0]
        assert _source(record, REF, element) == p1['schema_source']
        assert all(write['before'] == write['after'] for write in record['writes'])


def test_birth_does_not_invent_a_pin(two_pins):
    element, _, _ = two_pins
    realm = _realm(two_pins)
    _birth(realm, element)
    birth = realm.entries[-1]['records']['commands'][0]
    assert birth['created'] == [{'ref': REF, 'kind': 'clip', 'owner': 'parent'}]
    assert not any(dep['key'] == 'params.schema' for dep in birth['dependencies'])
    assert current(realm)['source_mapping']['_runtime_document']['dependency_manifest']['documents'] == []


def test_disjoint_peer_and_paginated_reload_preserve_direct_source(two_pins):
    element, p1, _ = two_pins
    realm = _realm(two_pins)
    _birth(realm, element)
    _reselect(realm, element, p1)
    base = copy.deepcopy(realm.head)
    command = capture_clip_set_params(current(realm), REF, values={'seed': 8}, **p1)
    _commit(realm, [capture_track_set(current(realm), 'parent/track:picture', values={'label': 'Peer'})])
    accepted = _commit(realm, [command], base=base)
    assert accepted['data']['status'] == 'rebased'
    realm.page_size = 1
    entries = _history(DetachedPort(realm, realm.context([])), realm.head, base, CommitRejected, base)
    entries = json.loads(json.dumps(entries))
    assert len(entries) == 2
    assert entries[0]['previous'] == base
    assert _source(entries[1]['records']['commands'][0], REF, element) == p1['schema_source']
    broken = copy.deepcopy(realm.entries)
    realm.entries[-1]['previous'] = {**base, 'publication_id': 'missing'}
    with pytest.raises(CommitRejected, match='ancestry'):
        _history(DetachedPort(realm, realm.context([])), realm.head, base, CommitRejected, base)
    realm.entries = broken


def test_inverse_consumed_pin_is_not_restored_authority(two_pins):
    element, p1, p2 = two_pins
    realm = _realm(two_pins)
    _birth(realm, element)
    _reselect(realm, element, p1)
    prior = copy.deepcopy(realm.head)
    selected = _reselect(realm, element, p2)
    undone = _commit(realm, [inverse([selected['receipt']['receipt_id']])])
    envelope = realm.entries[-1]['records']
    assert envelope['undo_of'] == [selected['receipt']['receipt_id']]
    target = next(entry for entry in realm.entries if entry['publication_id'] == envelope['undo_of'][0])
    assert target['previous'] == prior
    record = envelope['commands'][0]
    assert next(dep['pin'] for dep in record['dependencies'] if dep['key'] == 'params.schema') == p2['schema_source']
    assert not any(binding['key'] == 'params.element' for binding in record['bindings'])
    prior_entry = next(entry for entry in realm.entries if entry['publication_id'] == prior['publication_id'])
    assert _source(prior_entry['records']['commands'][0], REF, element) == p1['schema_source']
    redone = _commit(realm, [inverse([undone['receipt']['receipt_id']])])
    assert redone['data']['canonical_document'] == selected['data']['canonical_document']


def test_revision_restore_cannot_choose_between_equal_json_publications(two_pins):
    element, p1, p2 = two_pins
    realm = _realm(two_pins)
    _birth(realm, element)
    _reselect(realm, element, p1)
    _reselect(realm, element, p2)
    a = copy.deepcopy(realm.entries[-1])
    _reselect(realm, element, p1)
    b = copy.deepcopy(realm.entries[-1])
    assert a['revision_id'] == b['revision_id']
    assert _source(a['records']['commands'][0], REF, element) != _source(b['records']['commands'][0], REF, element)
    restored = _commit(realm, [restore(realm.documents[a['revision_id']])])
    records = realm.entries[-1]['records']
    assert records['restored_from'] == restore(restored['data']['canonical_document'])['args']
    assert set(records['restored_from']) == {'revision_id', 'content_digest'}
    assert records['commands'][0]['writes'][0]['ref'] == 'timeline'
    assert not any(dep['key'].startswith('params.')
                   for dep in records['commands'][0]['dependencies'])


def test_occurrence_materialization_and_duplicate_have_explicit_identity_links(two_pins):
    element, p1, _ = two_pins
    document = document_fixture()
    parent = document['parent_revision']
    for occurrence in parent['payload']['occurrences']:
        occurrence.update(source_offset=0, speed=1)
    parent['content_digest'] = _digest(parent['payload'])
    for internal in document['internal_timeline_revisions']:
        internal['payload']['clips'][0].update(elementRef=element, params={'seed': 4})
        internal['content_digest'] = _digest(internal['payload'])
    document['dependency_manifest']['internal_timelines'][0]['content_digest'] = document['internal_timeline_revisions'][0]['content_digest']
    realm = _realm(two_pins, document)
    ref = 'occurrence:occ-a/clip:clip-a'
    sibling = copy.deepcopy(State(current(realm)).entity('occurrence:occ-b/clip:clip-a'))
    _reselect(realm, element, p1, ref=ref)
    record = realm.entries[-1]['records']['commands'][0]
    assert _source(record, ref, element) == p1['schema_source']
    assert {'key': 'scalar.owner', 'ref': 'occurrence:occ-a', 'policy': 'fixed', 'dependencies': []} in record['bindings']
    assert State(current(realm)).entity('occurrence:occ-b/clip:clip-a') == sibling
    assert State(current(realm)).entity('parent/clip:clip-a').get('elementRef') is None
    _commit(realm, [capture_occurrence_duplicate(current(realm), 'occurrence:occ-a', 'copy', 'copy-shot')])
    duplicate = realm.entries[-1]['records']['commands'][0]
    assert {'key': 'duplicate.owner', 'ref': 'occurrence:occ-a', 'policy': 'fixed', 'dependencies': []} in duplicate['bindings']
    assert {row['ref'] for row in duplicate['created']} == {'occurrence:copy', 'shot:copy-shot'}
    assert not any(dep['key'] == 'params.schema' for dep in duplicate['dependencies'])
    assert State(current(realm)).entity('occurrence:copy/clip:clip-a')['elementRef'] == element


def test_param_cannot_reselect_pin_after_relevant_peer_selection(two_pins):
    element, p1, p2 = two_pins
    realm = _realm(two_pins)
    _birth(realm, element)
    _reselect(realm, element, p1)
    base = copy.deepcopy(realm.head)
    command = capture_clip_set_params(current(realm), REF, values={'seed': 8}, **p1)
    _reselect(realm, element, p2)
    before = copy.deepcopy((realm.head, realm.entries, realm.documents))
    with pytest.raises(CommitRejected):
        _commit(realm, [command], base=base)
    assert (realm.head, realm.entries, realm.documents) == before


def test_legacy_unchanged_param_requires_selection_authority(two_pins):
    element, _, _ = two_pins
    realm = _realm(two_pins)
    _birth(realm, element)
    command = capture_clip_set_params(current(realm), REF, values={'seed': 4})
    with pytest.raises(CommitRejected):
        _commit(realm, [command])


def _params(options, values=None, clear=(), ref=REF):
    return {'schema_version': 1, 'op': 'clip.set-params', 'version': '1', 'select': ref,
            'args': {'values': values or {}, 'clear': list(clear),
                     'schema_source': options['schema_source']}}


@pytest.mark.parametrize('values,clear', [({'seed': 4}, []), ({}, ['missing']), ({}, [])])
def test_param_cannot_bootstrap_selected_authority(two_pins, values, clear):
    element, p1, _ = two_pins
    realm = _realm(two_pins)
    _birth(realm, element)
    before = copy.deepcopy((realm.head, realm.entries, realm.documents))
    with pytest.raises(CommitRejected):
        _commit(realm, [_params(p1, values, clear)])
    assert (realm.head, realm.entries, realm.documents) == before


def test_batch_reselection_changes_stage_authority_without_changing_capture(two_pins):
    element, p1, p2 = two_pins
    realm = _realm(two_pins)
    _birth(realm, element)
    _reselect(realm, element, p1)
    reselect = capture_clip_set_element(current(realm), REF, element=element, **p2)
    param = capture_clip_set_params(current(realm), REF, values={'seed': 8}, **p2)
    accepted = _commit(realm, [reselect, param])
    assert accepted['data']['commands'] == [reselect, param]
    assert _source(realm.entries[-1]['records']['commands'][1], REF, element) == p2['schema_source']
    with pytest.raises(CommitRejected, match='source changed'):
        _commit(realm, [_params(p1, {'seed': 9})])


@pytest.mark.parametrize('redo', [False, True])
def test_param_after_inverse_uses_original_target_before_selection(two_pins, redo):
    element, p1, p2 = two_pins
    realm = _realm(two_pins)
    _birth(realm, element)
    _reselect(realm, element, p1)
    selected = _reselect(realm, element, p2)
    undone = _commit(realm, [inverse([selected['receipt']['receipt_id']])])
    if redo:
        _commit(realm, [inverse([undone['receipt']['receipt_id']])])
    expected, wrong = (p2, p1) if redo else (p1, p2)
    with pytest.raises(CommitRejected, match='source changed'):
        _commit(realm, [_params(wrong, {'seed': 8})])
    _commit(realm, [_params(expected, {'seed': 8})])


def test_param_after_ambiguous_restore_refuses_until_explicit_reselection(two_pins):
    element, p1, p2 = two_pins
    realm = _realm(two_pins)
    _birth(realm, element)
    _reselect(realm, element, p1)
    _reselect(realm, element, p2)
    document = copy.deepcopy(realm.documents[realm.head['revision_id']])
    _reselect(realm, element, p1)
    _commit(realm, [restore(document)])
    for options in (p1, p2):
        with pytest.raises(CommitRejected, match='authority is unavailable'):
            _commit(realm, [_params(options, {'seed': 8})])
    _reselect(realm, element, p2)
    _commit(realm, [_params(p2, {'seed': 8})])


def test_equal_selection_aba_allows_param_but_inverse_remains_strict(two_pins):
    element, p1, p2 = two_pins
    realm = _realm(two_pins)
    _birth(realm, element)
    _reselect(realm, element, p1)
    base = copy.deepcopy(realm.head)
    command = capture_clip_set_params(current(realm), REF, values={'seed': 8}, **p1)
    selected = _reselect(realm, element, p2)
    _reselect(realm, element, p1)
    _reselect(realm, element, p2)
    with pytest.raises(CommitRejected):
        _commit(realm, [inverse([selected['receipt']['receipt_id']])])
    _reselect(realm, element, p1)
    assert _commit(realm, [command], base=base)['data']['status'] == 'rebased'


@pytest.mark.parametrize('damage', ['schema', 'snapshot', 'binding', 'duplicate', 'object', 'head', 'ancestry'])
def test_inconsistent_or_incomplete_history_never_resurrects_older_pin(two_pins, damage):
    element, p1, _ = two_pins
    realm = _realm(two_pins)
    _birth(realm, element)
    _reselect(realm, element, p1)
    _reselect(realm, element, p1)
    record = realm.entries[-1]['records']['commands'][0]
    if damage in ('schema', 'snapshot'):
        record['dependencies'] = [d for d in record['dependencies'] if d['key'] != 'params.' + damage]
    elif damage == 'binding':
        record['bindings'] = [b for b in record['bindings'] if b['key'] != 'params.element']
    elif damage == 'duplicate':
        record['dependencies'].append(copy.deepcopy(next(d for d in record['dependencies']
                                                       if d['key'] == 'params.schema')))
    elif damage == 'object':
        next(d for d in record['dependencies'] if d['key'] == 'params.snapshot')['pin']['object']['size'] += 1
    elif damage == 'head':
        realm.head['revision_id'] = realm.entries[-2]['revision_id'] + '-wrong'
    else:
        realm.entries[-1]['previous'] = copy.deepcopy(realm.entries[0]['previous'])
    before = copy.deepcopy((realm.head, realm.entries, realm.documents))
    with pytest.raises(CommitRejected):
        _commit(realm, [_params(p1, {'seed': 8})])
    assert (realm.head, realm.entries, realm.documents) == before


@pytest.mark.parametrize('clear', [[], ['missing']])
def test_proven_selection_still_requires_pin_for_unchanged_param(two_pins, clear):
    element, p1, _ = two_pins
    realm = _realm(two_pins)
    _birth(realm, element)
    _reselect(realm, element, p1)
    command = capture_clip_set_params(current(realm), REF, values={'seed': 4}, clear=clear)
    with pytest.raises(CommitRejected, match='requires exact schema_source'):
        _commit(realm, [command])


def test_selected_occurrence_param_keeps_raw_owner_and_unselected_copy_unavailable(two_pins):
    element, p1, _ = two_pins
    document = document_fixture()
    for occurrence in document['parent_revision']['payload']['occurrences']:
        occurrence.update(source_offset=0, speed=1)
    document['parent_revision']['content_digest'] = _digest(document['parent_revision']['payload'])
    for internal in document['internal_timeline_revisions']:
        internal['payload']['clips'][0].update(elementRef=element, params={'seed': 4})
        internal['content_digest'] = _digest(internal['payload'])
    document['dependency_manifest']['internal_timelines'][0]['content_digest'] = document['internal_timeline_revisions'][0]['content_digest']
    realm = _realm(two_pins, document)
    ref = 'occurrence:occ-a/clip:clip-a'
    _reselect(realm, element, p1, ref=ref)
    _commit(realm, [_params(p1, {'seed': 8}, ref=ref)])
    with pytest.raises(CommitRejected, match='authority is unavailable'):
        _commit(realm, [_params(p1, {'seed': 8}, ref='occurrence:occ-b/clip:clip-a')])
    _commit(realm, [capture_occurrence_duplicate(current(realm), 'occurrence:occ-a', 'copy', 'copy-shot')])
    copy_ref = 'occurrence:copy/clip:clip-a'
    with pytest.raises(CommitRejected, match='authority is unavailable'):
        _commit(realm, [_params(p1, {'seed': 8}, ref=copy_ref)])
    _reselect(realm, element, p1, ref=copy_ref)
    _commit(realm, [_params(p1, {'seed': 8}, ref=copy_ref)])
