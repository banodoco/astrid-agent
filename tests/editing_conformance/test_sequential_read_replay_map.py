"""Replay maps index physical turnover without dropping declared read facts."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._command_state import present
from tests.editing_conformance import test_commit_runtime_source_copy
from tests.editing_conformance.test_doc_patch_sequential_rebase import dump, peer_request, setup
from tests.editing_conformance.test_doc_patch_v2 import change, patch

saved_source = test_commit_runtime_source_copy.saved_source
pytestmark = pytest.mark.integration


def read_case(saved_source):
    client, project, document, send, request, ref = setup(saved_source)
    theme = document('t')['parent_revision']['payload']['config']['theme_overrides']
    read = {'kind': 'field', 'ref': 'parent', 'path': ['config', 'theme_overrides'],
            'state': present(theme)}
    request['reads'] = [read]
    for step in request['steps']:
        step.setdefault('reads', []).append(copy.deepcopy(read))
    return client, project, document, send, request, ref


def move_head(client, project, send, ref):
    peer = patch(change(['clips', 'id:late', 'params', 'text'], present('hi'), 'peer', ref=ref))
    send(project, 't', peer_request(client, project, peer), idempotency_key='peer')


def test_parent_reads_stay_authenticated_and_physical_turnover_stays_complete(
        saved_source, monkeypatch):
    from runtime_protocol.timeline_commit_port import RequestCommitPort

    client, project, document, send, request, ref = read_case(saved_source)
    move_head(client, project, send, ref)
    receive = RequestCommitPort.capture_sequential_execution
    captures = []

    def capture(port, value):
        captures.append(copy.deepcopy(value))
        return receive(port, value)

    monkeypatch.setattr(RequestCommitPort, 'capture_sequential_execution', capture)
    result = send(project, 't', request, idempotency_key='read-map')
    assert result['data']['status'] == 'merged'
    assert result['receipt']['result'] == result['data']
    assert result['data']['canonical_document'] == document('t')
    assert len(captures) == 1
    proof = captures[0]
    assert set(proof) == {'schema_version', 'context', 'original', 'landing',
                          'replay_bindings', 'prepared_digest'}
    assert proof['context']['request'] == request
    expected = set()
    for ordinal, record in enumerate(proof['original']['records']['commands']):
        landing = proof['landing']['records']['commands'][ordinal]
        assert any(binding.get('key') == 'commit.read' and binding['ref'] == 'parent'
                   for binding in record['bindings'])
        assert any(binding.get('key') == 'commit.read' and binding['ref'] == 'parent'
                   for binding in landing['bindings'])
        expected.update((ordinal, 'patch', index)
                        for index in range(len(request['commands'][ordinal]['args']['changes'])))
        for binding in record['bindings']:
            if binding.get('key') != 'commit.read':
                continue
            match = next(other for other in landing['bindings']
                         if other.get('key') == 'commit.read' and other['ref'] == binding['ref']
                         and other['path'] == binding['path']
                         and other['boundary']['phase'] == binding['boundary']['phase'])
            if binding['physical_ref'] != match['physical_ref']:
                expected.add((ordinal, 'read', binding['ref'], tuple(binding['path']),
                              binding['boundary']['phase']))
    actual = {(item['command_ordinal'], 'patch', item['change_ordinal']) if item['kind'] == 'patch'
              else (item['command_ordinal'], 'read', item['ref'], tuple(item['path']), item['phase'])
              for item in proof['replay_bindings']}
    assert len(actual) == len(proof['replay_bindings'])
    assert actual == expected
    assert any(key[1] == 'read' for key in actual)
    assert not any(item.get('ref') == 'parent' for item in proof['replay_bindings'])
    for execution in (proof['original'], proof['landing']):
        assert all(request['reads'][0] in step['dependencies']
                   for step in execution['records']['steps'])


@pytest.mark.parametrize('damage', ['extra', 'omitted', 'duplicate', 'owner', 'revision', 'order'])
def test_forged_replay_maps_refuse_without_writes(saved_source, tmp_path, monkeypatch, damage):
    from runtime_protocol.errors import RuntimeErrorBase
    from runtime_protocol.timeline_commit_port import RequestCommitPort

    client, project, document, send, request, ref = read_case(saved_source)
    move_head(client, project, send, ref)
    receive = RequestCommitPort.capture_sequential_execution

    def capture(port, value):
        value = copy.deepcopy(value)
        replay = value['replay_bindings']
        if damage == 'extra':
            replay.append({'command_ordinal': 0, 'kind': 'read', 'ref': 'parent',
                           'path': ['config', 'theme_overrides'], 'phase': 'batch-read',
                           'source': 'parent'})
        elif damage == 'omitted':
            replay.pop(next(i for i, item in enumerate(replay) if item['kind'] == 'read'))
        elif damage == 'duplicate':
            replay.append(copy.deepcopy(replay[0]))
        elif damage == 'revision':
            binding = next(binding for binding in value['original']['records']['commands'][0]['bindings']
                           if binding.get('key') == 'commit.read' and binding['ref'] == 'parent')
            binding['revision_id'] = 'forged-revision'
        else:
            item = next(item for item in replay if isinstance(item['source'], dict))
            item['source']['owner' if damage == 'owner' else 'step_id'] = (
                'occurrence:unrelated/shot' if damage == 'owner' else request['steps'][1]['id'])
        return receive(port, value)

    monkeypatch.setattr(RequestCommitPort, 'capture_sequential_execution', capture)
    before, landed = dump(tmp_path), copy.deepcopy(document('t'))
    with pytest.raises(RuntimeErrorBase) as error:
        send(project, 't', request, idempotency_key='forged-map')
    assert error.value.details['reason'] == 'invalid_records'
    assert dump(tmp_path) == before
    assert document('t') == landed


@pytest.mark.parametrize('damage', ['wrong-before', 'declared-read', 'same-field', 'collection-order', 'ABA'])
def test_read_and_history_guards_survive_map_filtering(saved_source, tmp_path, damage):
    from runtime_protocol.errors import RuntimeErrorBase

    client, project, document, send, request, ref = read_case(saved_source)
    if damage == 'wrong-before':
        request['commands'][0]['args']['changes'][0]['before'] = present(0)
        request['steps'][0]['patch'] = copy.deepcopy(request['commands'][0]['args']['changes'])
        move_head(client, project, send, ref)
    else:
        if damage == 'declared-read':
            path, before, after, physical = ['config', 'theme_overrides', 'visual', 'canvas', 'width'], 320, 640, 'parent'
        elif damage == 'collection-order':
            path, physical = ['clips'], ref
            before = document('t')['internal_timeline_revisions'][0]['payload']['clips']
            after = list(reversed(before))
        else:
            path, before, after, physical = ['clips', 'id:cross', 'at'], 3.5, 3.7, ref
        peer = patch(change(path, present(before), after, ref=physical))
        send(project, 't', peer_request(client, project, peer), idempotency_key='guard-peer')
        if damage == 'ABA':
            current = 'internal-revision:' + document('t')['internal_timeline_revisions'][0]['revision_id']
            restore = patch(change(path, present(after), before, ref=current))
            send(project, 't', peer_request(client, project, restore), idempotency_key='guard-restore')
    before_db, landed = dump(tmp_path), copy.deepcopy(document('t'))
    with pytest.raises(RuntimeErrorBase):
        send(project, 't', request, idempotency_key='guard-refusal')
    assert dump(tmp_path) == before_db
    assert document('t') == landed
