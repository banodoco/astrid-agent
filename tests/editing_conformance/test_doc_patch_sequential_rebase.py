"""Physical sequential patch/read inputs survive authenticated head turnover."""
from __future__ import annotations

import copy
import json
import sqlite3

import pytest

from astrid.sdk.timeline._command_state import present
from astrid.sdk.timeline._doc_patch import Closure
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import bundle_from_document
from tests.editing_conformance import test_commit_runtime_source_copy
from tests.editing_conformance.test_doc_patch_v2 import change, patch, step
from tests.sdk.test_timeline_restructure import flat_film

saved_source = test_commit_runtime_source_copy.saved_source
pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def origins(request):
    from tests.helpers.runtime import use_runtime

    use_runtime()
    import runtime_protocol.timeline_commit_port as port

    import astrid.sdk.timeline._commit_patch as provider

    for phase in ('before', 'after'):
        print(json.dumps({'case': request.node.nodeid, 'phase': phase,
                          'provider': provider.__file__, 'runtime': port.__file__}))
        if phase == 'before':
            yield


def dump(tmp_path):
    with sqlite3.connect(tmp_path / 'saved-source' / 'realm.sqlite3') as connection:
        return list(connection.iterdump())


def setup(saved_source, *, declared=True):
    client, project, document, _ = saved_source(flat_film())
    original = document('t')
    base = client.timelines.history(project, 't', limit=100).data['head']
    bundle = bundle_from_document(original)
    ref = Closure(bundle).resolve('internal:t')
    first = patch(change(['clips', 'id:cross', 'at'], present(3.5), 4.0, ref=ref))
    projected = execute(bundle, [first], commit_request={'reads': []})
    staged = Closure(projected.bundle).resolve('internal:t')
    second = patch(change(['clips', 'id:cross', 'at'], present(4.0), 4.1, ref=staged))
    reads = []
    if declared:
        clips = Closure(projected.bundle).payload(staged)['clips']
        reads = [
            {'kind': 'field', 'ref': staged, 'path': ['clips', 'id:cross'],
             'state': present(next(row for row in clips if row['id'] == 'cross'))},
            {'kind': 'field', 'ref': staged, 'path': ['clips', 'id:vo2', 'app'],
             'state': present(next(row for row in clips if row['id'] == 'vo2')['app'])},
            {'kind': 'collection', 'ref': staged, 'path': ['clips'],
             'members': sorted(row['id'] for row in clips),
             'order': [row['id'] for row in clips]},
        ]
    request = {'api_version': 1, 'project_id': project, 'timeline_id': 't',
               'base_revision_id': base['revision_id'], 'base_publication_id': base['publication_id'],
               'commands': [first, second], 'label': 'Two nudges', 'merge': 'fields', 'reads': [],
               'steps': [step('first', 0, first), step('second', 1, second, reads=reads)],
               'dry_run': False, 'source': {'via': 'python'}, 'rationale': 'Sequential physical inputs',
               'group_id': 'sequential'}
    for item in request['steps']:
        item.update(intent='Nudge cross', tags=['nudge'])
    send = client._remote._transport._generated.commit_timeline
    return client, project, document, send, request, ref


def peer_request(client, project, call):
    head = client.timelines.history(project, 't', limit=100).data['head']
    return {'api_version': 1, 'project_id': project, 'timeline_id': 't',
            'base_revision_id': head['revision_id'], 'base_publication_id': head['publication_id'],
            'commands': [call], 'label': 'Peer', 'merge': 'fields', 'reads': []}


@pytest.mark.parametrize('declared', [False, True])
@pytest.mark.parametrize('moved', [False, True])
def test_sequential_physical_inputs_land_at_actual_step_boundaries(saved_source, tmp_path, declared, moved):
    client, project, document, send, request, ref = setup(saved_source, declared=declared)
    if moved:
        peer = patch(change(['clips', 'id:late', 'params', 'text'], present('hi'), 'peer', ref=ref))
        send(project, 't', peer_request(client, project, peer), idempotency_key='peer')
    landing = copy.deepcopy(document('t'))
    head = client.timelines.history(project, 't', limit=100).data['head']
    before = dump(tmp_path)
    try:
        preview = send(project, 't', {**request, 'dry_run': True}, idempotency_key='preview')
    finally:
        assert dump(tmp_path) == before
        assert document('t') == landing
    assert preview['receipt'] is None
    assert preview['data']['would_status'] == ('merged' if moved else 'accepted')
    assert dump(tmp_path) == before
    result = send(project, 't', request, idempotency_key='sequential')
    assert result['data']['status'] == ('merged' if moved else 'accepted')
    assert result['receipt']['result'] == result['data']
    assert document('t') == result['data']['canonical_document']
    clips = document('t')['internal_timeline_revisions'][0]['payload']['clips']
    assert next(row for row in clips if row['id'] == 'cross')['at'] == 4.1
    assert next(row for row in clips if row['id'] == 'late')['params']['text'] == ('peer' if moved else 'hi')
    history = client.timelines.history(project, 't', limit=100).data
    entry = next(row for row in history['items'] if row['publication_id'] == result['receipt']['receipt_id'])
    assert all(entry[key] == request[key] for key in ('source', 'rationale', 'group_id'))
    assert [row['command_ordinals'] for row in entry['records']['steps']] == [[0], [1]]
    for actual, submitted in zip(entry['records']['steps'], request['steps']):
        assert all(actual[key] == submitted[key]
                   for key in ('id', 'label', 'intent', 'tags', 'command_ordinals'))
    first = execute(bundle_from_document(landing), [patch(change(
        ['clips', 'id:cross', 'at'], present(3.5), 4.0,
        ref='internal-revision:' + landing['internal_timeline_revisions'][0]['revision_id']))],
        commit_request={'reads': []})
    inputs = [Closure(bundle_from_document(landing)), Closure(first.bundle)]
    for ordinal, record in enumerate(entry['records']['commands']):
        captures = [binding for binding in record['bindings']
                    if binding['key'].startswith('doc.patch:') or binding['key'] == 'commit.read']
        assert captures
        for binding in captures:
            assert binding['boundary']['evaluated_head'] == head
            assert binding['boundary']['command_ordinal'] == ordinal
            assert binding['boundary']['step_id'] == request['steps'][ordinal]['id']
            assert binding['content_digest'] == inputs[ordinal].images[binding['physical_ref']]['content_digest']
    assert send(project, 't', request, idempotency_key='sequential') == result
    assert client.timelines.history(project, 't', limit=100).data == history


@pytest.mark.parametrize('damage', ['field', 'consulted', 'membership', 'source', 'continuation'])
def test_sequential_refusal_has_no_writes(saved_source, tmp_path, monkeypatch, damage):
    from runtime_protocol.errors import RuntimeErrorBase
    from runtime_protocol.timeline_commit_port import RequestCommitPort

    client, project, document, send, request, ref = setup(saved_source)
    if damage == 'field':
        peer = patch(change(['clips', 'id:cross', 'at'], present(3.5), 3.7, ref=ref))
    elif damage == 'consulted':
        peer = patch(change(['clips', 'id:vo2', 'app', 'astrid', 'text'],
                            present('second line'), 'changed', ref=ref))
    elif damage == 'membership':
        clips = document('t')['internal_timeline_revisions'][0]['payload']['clips']
        peer = patch(change(['clips'], present(clips[-1]), op='list.remove', ref=ref,
                            id=clips[-1]['id'], order_before=[row['id'] for row in clips]))
    elif damage == 'source':
        request['commands'][1]['args']['changes'][0]['ref'] = 'internal-revision:unrelated'
        request['steps'][1]['patch'] = copy.deepcopy(request['commands'][1]['args']['changes'])
        peer = patch(change(['clips', 'id:late', 'params', 'text'], present('hi'), 'peer', ref=ref))
    else:
        peer = patch(change(['clips', 'id:late', 'params', 'text'], present('hi'), 'peer', ref=ref))
    send(project, 't', peer_request(client, project, peer), idempotency_key='peer')
    if damage == 'continuation':
        history = RequestCommitPort.history

        def forged_history(port, *args, **kwargs):
            page = history(port, *args, **kwargs)
            for entry in page['items']:
                for record in (entry.get('records') or {}).get('commands', []):
                    for binding in record['bindings']:
                        if binding['key'] == 'commit.materialization':
                            binding['edge_before'] = 'shot-revision:forged'
            return page

        monkeypatch.setattr(RequestCommitPort, 'history', forged_history)
    before, landed = dump(tmp_path), copy.deepcopy(document('t'))
    with pytest.raises(RuntimeErrorBase) as error:
        send(project, 't', request, idempotency_key='refused')
    print(json.dumps({'damage': damage, 'code': error.value.code, 'details': error.value.details}))
    assert error.value.code == 'conflict'
    assert error.value.details['reason'] == {
        'field': 'value_conflict', 'consulted': 'value_conflict', 'membership': 'selection_changed',
        'source': 'outside_closure', 'continuation': 'history_unavailable',
    }[damage]
    assert document('t') == landed
    assert dump(tmp_path) == before
