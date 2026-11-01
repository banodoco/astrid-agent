"""Same-step fork allocation survives the production provider's rebase scan."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._command_state import ABSENT, present
from astrid.sdk.timeline._commit_patch import Closure, effect_diff
from astrid.sdk.timeline.commit import _digest, bundle_from_document
from tests.editing_conformance.test_doc_patch_v2 import change, patch, step, submit
from tests.editing_conformance.test_recipe_occurrence_fork import fork_document
from tests.sdk.test_timeline_commit_provider import CommitRejected, DetachedPort, DetachedRealm
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi

MEDIA = {'asset': 'added', 'object_id': 'sha256:' + '4' * 64,
         'generation_id': 'generation-added', 'variant_id': 'variant-added',
         'catalog_digest': 'sha256:' + '5' * 64}


@pytest.fixture
def scenario(monkeypatch):
    document = fork_document()
    internal = document['internal_timeline_revisions'][0]
    internal['payload'].pop('source_window', None)
    internal['payload']['meta'] = {'org.example.popup': {'nullable': None, 'opaque': [False]}}
    internal['payload']['clips'].append({'id': 'raw-v2-occupied', 'track': 'picture',
        'clipType': 'media', 'asset': 'picture', 'at': 6, 'hold': 1, 'params': None})
    internal['content_digest'] = _digest(internal['payload'])
    document['dependency_manifest']['internal_timelines'][0]['content_digest'] = internal['content_digest']
    realm = DetachedRealm(document)
    realm.executions = []
    calls = [
        {'schema_version': 1, 'op': 'occurrence.fork', 'version': '1',
         'select': 'occurrence:occ-0', 'args': {'shot_id': 'shot-fork'},
         'intent': {'ownership': 'occurrence'}, 'created': ['shot:shot-fork']},
        {'schema_version': 1, 'op': 'occurrence.add-media', 'version': '1',
         'select': 'occurrence:occ-0', 'args': {'media': MEDIA, 'row': {
             'id': 'added', 'clipType': 'hold', 'track': 'picture', 'at': 0, 'hold': 5,
             'opacity': 1, 'asset': 'added'}}, 'intent': {'ownership': 'occurrence'},
         'created': ['occurrence:occ-0/asset:added', 'occurrence:occ-0/clip:added']},
    ]

    def selected_media(port, selection):
        assert selection == MEDIA
        return {'selection': copy.deepcopy(selection), 'media_type': 'image/png',
                'size': 1234, 'duration_seconds': None}

    monkeypatch.setattr(DetachedPort, 'selected_media', selected_media)
    monkeypatch.setattr(DetachedPort, 'capture_sequential_execution',
                        lambda port, witness: port.realm.executions.append(copy.deepcopy(witness)), raising=False)
    return realm, calls


def controls(calls, separate=False):
    steps = [step('popup', 0, calls[0])]
    if separate:
        steps.append(step('add', 1, calls[1]))
    else:
        steps[0]['command_ordinals'] = [0, 1]
    return {'label': 'Edit shot timeline', 'steps': steps, 'merge': 'fields',
            'source': {'via': 'app'}, 'rationale': 'Isolate and add media', 'group_id': 'popup'}


def peer(realm):
    return submit(realm, patch(change(['meta'], ABSENT,
        {'org.example.peer': {'retained': [None, False]}})), reads=[])


@pytest.mark.parametrize('moved', [False, True])
@pytest.mark.parametrize('separate', [False, True])
def test_production_fork_add_rebase_preserves_canonical_closure_and_bindings(scenario, moved, separate):
    realm, calls = scenario
    original = copy.deepcopy(realm.documents[realm.head['revision_id']])
    base = copy.deepcopy(realm.head)
    if moved:
        peer(realm)
    head = copy.deepcopy(realm.head)
    landing = copy.deepcopy(realm.documents[head['revision_id']])
    request = controls(calls, separate)
    preview = submit(realm, *calls, base=base, dry_run=True, **request)
    assert preview['receipt'] is None
    assert preview['data']['would_status'] == ('merged' if moved else 'accepted')
    assert realm.head == head
    result = submit(realm, *calls, base=base, **request)
    # DetachedPort retains its historical ABI spelling; this is provider-only proof.
    assert result['data']['status'] == ('rebased' if moved else 'accepted')
    assert result['receipt']['result'] == result['data']
    output = result['data']['canonical_document']
    assert output == preview['data']['canonical_document']
    assert result['data']['document_digest'] == _digest(output)
    assert output['parent_revision']['payload']['occurrences'][1:] == original['parent_revision']['payload']['occurrences'][1:]
    assert all(row in output['shot_revisions'] for row in original['shot_revisions'])
    assert all(row in output['internal_timeline_revisions'] for row in original['internal_timeline_revisions'])
    if moved:
        assert output['parent_revision']['payload']['meta'] == landing['parent_revision']['payload']['meta']
    shot = next(row for row in output['shot_revisions'] if row['shot_id'] == 'shot-fork')
    child = next(row for row in output['internal_timeline_revisions']
                 if row['revision_id'] == shot['internal_timeline_revision_id'])
    source = original['internal_timeline_revisions'][0]['payload']
    assert child['payload']['clips'][:-1] == source['clips']
    assert child['payload']['clips'][-1] == calls[1]['args']['row']
    assert child['payload']['meta'] == source['meta']
    assert child['payload']['registry']['assets']['added'] == {
        'media_id': MEDIA['object_id'], 'type': 'image',
        'generationId': MEDIA['generation_id'], 'variantId': MEDIA['variant_id']}
    records = realm.prepared[-1]['records']
    execution = realm.executions[-1]['landing']
    assert execution['records'] == records
    assert execution['evaluated_head'] == head
    assert records['merge_mode'] == 'fields'
    assert [row['command_ordinals'] for row in records['steps']] == ([[0], [1]] if separate else [[0, 1]])
    if not separate:
        assert records['steps'][0]['patch'] == effect_diff(
            Closure(bundle_from_document(landing)), Closure(bundle_from_document(output)))
    assert all(row['dependencies'] for row in records['steps'])
    assert all(row['reads'] and row['bindings'] and row['created'] for row in records['commands'])
    assert result['data']['commands'] == [row['command'] for row in records['commands']]
    for ordinal, record in enumerate(records['commands']):
        command_input = Closure(bundle_from_document(execution['commands'][ordinal]['input']))
        for binding in record['bindings']:
            if 'physical_ref' not in binding:
                continue
            assert binding['boundary']['evaluated_head'] == head
            assert binding['boundary']['command_ordinal'] == ordinal
            assert binding['boundary']['step_id'] == ('add' if separate and ordinal else 'popup')
            assert binding['revision_id']
            assert binding['content_digest'].startswith('sha256:')
            if binding['boundary']['phase'] == 'input':
                image = command_input.images[binding['physical_ref']]
                assert binding['revision_id'] == image['revision_id']
                assert binding['content_digest'] == image['content_digest']
                assert binding.get('owners') == command_input.binding(
                    binding['ref'], binding['physical_ref']).get('owners')
            if binding['physical_ref'] != 'parent':
                assert binding['owners']
    for actual, declared in zip(records['steps'], request['steps']):
        assert all(actual[key] == declared[key]
                   for key in ('id', 'label', 'intent', 'tags', 'command_ordinals'))
        first, last = actual['command_ordinals'][0], actual['command_ordinals'][-1]
        boundaries = [Closure(bundle_from_document(execution['commands'][first]['input'])),
                      Closure(bundle_from_document(execution['commands'][last]['output']))]
        bindings = records['commands'][last]['bindings']
        for index, effect in enumerate(actual['patch']):
            for side, closure in zip(('before', 'after'), boundaries):
                if effect['ref'] not in closure.images:
                    continue
                binding = next(item for item in bindings if item['key'] == 'commit.effect'
                    and item['effect_ordinal'] == index and item['boundary']['side'] == side)
                assert binding['physical_ref'] == effect['ref']
                assert binding['path'] == effect['path']
                assert binding['content_digest'] == closure.images[effect['ref']]['content_digest']


@pytest.mark.parametrize('damage', ['unbound', 'wrong-owner', 'forged', 'wrong-step'])
def test_invalid_same_step_inputs_refuse_without_acceptance(scenario, damage):
    realm, calls = scenario
    base = copy.deepcopy(realm.head)
    request = controls(calls)
    if damage == 'unbound':
        calls[1] = patch(change(['name'], present('Chapter One'), 'forged',
                               ref='shot-revision:unbound'))
        request['steps'][0]['patch'] = copy.deepcopy(calls[1]['args']['changes'])
    elif damage == 'wrong-owner':
        calls[1]['select'] = 'occurrence:missing'
    elif damage == 'forged':
        calls[0]['bindings'] = {'fork.source': {'revision_id': 'forged'}}
    else:
        request['steps'][0]['command_ordinals'] = [1, 0]
    peer(realm)
    before = copy.deepcopy((realm.head, realm.documents, realm.entries, realm.accepted))
    with pytest.raises(CommitRejected) as error:
        submit(realm, *calls, base=base, **request)
    assert error.value.failure['details']['reason']
    assert (realm.head, realm.documents, realm.entries, realm.accepted) == before


def test_cas_retry_rechecks_same_step_roots(scenario):
    realm, calls = scenario
    realm.before_accept = lambda: peer(realm)
    result = submit(realm, *calls, **controls(calls))
    assert result['data']['status'] == 'rebased'
    assert result['data']['canonical_document']['parent_revision']['payload']['meta'] == {
        'org.example.peer': {'retained': [None, False]}}


@pytest.mark.parametrize('damage', ['owner', 'successor', 'digest', 'edge', 'step'])
def test_forged_history_materialization_refuses(scenario, monkeypatch, damage):
    realm, calls = scenario
    base = copy.deepcopy(realm.head)
    submit(realm, patch(change(['unknown'], present([1, 2]), [3],
                              ref='internal:chapter-1')), reads=[])
    history = DetachedPort.history

    def corrupted(port, *args, **kwargs):
        result = history(port, *args, **kwargs)
        for entry in result['items']:
            for record in (entry.get('records') or {}).get('commands', []):
                for binding in record['bindings']:
                    if binding['key'] == 'commit.materialization':
                        key = {'owner': 'owner', 'successor': 'successor', 'digest': 'content_digest',
                               'edge': 'edge_before', 'step': 'step_id'}[damage]
                        binding[key] = 'foreign'
        return result

    monkeypatch.setattr(DetachedPort, 'history', corrupted)
    before = copy.deepcopy((realm.head, realm.documents, realm.entries))
    with pytest.raises(CommitRejected) as error:
        submit(realm, *calls, base=base, **controls(calls))
    assert error.value.failure['details']['reason'] == 'history_unavailable'
    assert (realm.head, realm.documents, realm.entries) == before


@pytest.mark.parametrize('damage', ['field', 'aba', 'order', 'membership'])
def test_fork_source_dependencies_still_refuse(scenario, damage):
    realm, calls = scenario
    base = copy.deepcopy(realm.head)
    ref = 'internal:chapter-1'
    if damage in ('field', 'aba'):
        submit(realm, patch(change(['unknown'], present([1, 2]), [3], ref=ref)), reads=[])
        if damage == 'aba':
            submit(realm, patch(change(['unknown'], present([3]), [1, 2], ref=ref)), reads=[])
    else:
        clips = realm.documents[realm.head['revision_id']]['internal_timeline_revisions'][0]['payload']['clips']
        submit(realm, patch(change(['clips'], present(clips[-1]), op=(
            'list.move' if damage == 'order' else 'list.remove'), ref=ref,
            id=clips[-1]['id'], order_before=[row['id'] for row in clips],
            **({'after_id': None} if damage == 'order' else {}))), reads=[])
    before = copy.deepcopy((realm.head, realm.documents, realm.entries))
    with pytest.raises(CommitRejected) as error:
        submit(realm, *calls, base=base, **controls(calls))
    assert error.value.failure['details']['reason'] == (
        'selection_changed' if damage in ('order', 'membership') else 'dependency_changed')
    assert (realm.head, realm.documents, realm.entries) == before
