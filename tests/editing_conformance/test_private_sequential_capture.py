"""Private original/landing transport through the actual Agent provider."""
from __future__ import annotations

import copy
import json

import pytest

from astrid.sdk.timeline._command_state import present
from astrid.sdk.timeline._doc_patch import Closure
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import _digest, bundle_from_document, create_runtime_commit_service
from tests.editing_conformance.test_doc_patch_v2 import change, patch, step, submit
from tests.sdk.test_timeline_commit_provider import CommitRejected, DetachedPort, DetachedRealm
from tests.sdk.test_timeline_commit_provider import runtime_abi as runtime_abi


def sequential(realm):
    base = copy.deepcopy(realm.head)
    bundle = bundle_from_document(realm.documents[base['revision_id']])
    first = patch(change(['unknown'], present([1, 2]), [3], ref='internal-revision:internal-1'))
    projected = execute(bundle, [first], commit_request={'reads': []})
    staged = Closure(projected.bundle).resolve('internal:chapter-1')
    second = patch(change(['unknown'], present([3]), [4], ref=staged))
    reads = [{'kind': 'field', 'ref': staged, 'path': ['unknown'], 'state': present([3])}]
    context = realm.context([first, second], base=base)
    context['request'].update(label='Sequential private proof', reads=[], merge='fields',
                              steps=[step('first', 0, first), step('second', 1, second, reads=reads)],
                              group_id='capture')
    context['invocation']['body'] = copy.deepcopy(context['request'])
    context['request_hash'] = _digest(context['invocation'])
    return context, staged


def peer(realm):
    submit(realm, patch(change(['source_window', 'end'], present(9), 8,
                              ref='internal-revision:internal-1')), reads=[])


@pytest.mark.parametrize('dry_run', [False, True])
def test_private_capture_keeps_full_original_and_landing_custody(monkeypatch, dry_run):
    realm = DetachedRealm()
    context, staged = sequential(realm)
    context['request']['dry_run'] = dry_run
    context['invocation']['body'] = copy.deepcopy(context['request'])
    context['request_hash'] = _digest(context['invocation'])
    peer(realm)
    captured, candidates = [], []

    def capture(port, value):
        assert value['context'] == port.context
        captured.append(json.loads(json.dumps(value, allow_nan=False)))
        value['context']['request']['steps'].clear()  # Detached from execution/request custody.

    method = DetachedPort.preview if dry_run else DetachedPort.accept

    def finish(port, prepared):
        assert captured[-1]['prepared_digest'] == _digest(prepared)
        assert captured[-1]['landing']['records'] == prepared['records']
        candidates.append(copy.deepcopy(prepared))
        return method(port, prepared)

    monkeypatch.setattr(DetachedPort, 'capture_sequential_execution', capture, raising=False)
    monkeypatch.setattr(DetachedPort, 'preview' if dry_run else 'accept', finish)
    original_context = copy.deepcopy(context)
    create_runtime_commit_service().commit(context, DetachedPort(realm, context))
    assert context == original_context
    assert len(captured) == len(candidates) == 1
    proof = captured[0]
    assert set(proof) == {'schema_version', 'context', 'original', 'landing',
                          'replay_bindings', 'prepared_digest'}
    assert proof['schema_version'] == 1
    original, landing = proof['original'], proof['landing']
    assert original['evaluated_head'] != landing['evaluated_head']
    assert original['commands'][0]['input'] == realm.documents[context['request']['base_revision_id']]
    assert landing['commands'][0]['input'] == realm.documents[landing['evaluated_head']['revision_id']]
    for execution in (original, landing):
        before, after = execution['commands']
        assert before['output'] == after['input']
        assert execution['records']['label'] == context['request']['label']
        assert execution['records']['undo_steps'] is None
        for ordinal, command in enumerate(execution['commands']):
            boundary = command['boundary']
            assert boundary == {'base_revision_id': context['request']['base_revision_id'],
                                'base_publication_id': context['request']['base_publication_id'],
                                'evaluated_head': execution['evaluated_head'],
                                'step_id': ('first', 'second')[ordinal],
                                'command_ordinal': ordinal, 'phase': 'input'}
            for document in (command['input'], command['output']):
                bundle_from_document(document)  # Verify all immutable digests and managed edges.
            bindings = execution['records']['commands'][ordinal]['bindings']
            assert any(binding['key'].startswith('doc.patch:') for binding in bindings)
            for binding in bindings:
                if 'physical_ref' in binding and binding['boundary']['phase'] == 'input':
                    images = Closure(bundle_from_document(command['input'])).images
                    assert binding['content_digest'] == images[binding['physical_ref']]['content_digest']
        assert execution['records']['steps'][1]['reads'] == context['request']['steps'][1]['reads']
        assert execution['records']['steps'][1]['dependencies']
        materialization = next(binding for binding in execution['records']['commands'][0]['bindings']
                               if binding['key'] == 'commit.materialization'
                               and binding['physical_ref'].startswith('internal-revision:'))
        assert materialization['owners'] == ['occurrence:occ-a/shot', 'occurrence:occ-b/shot']
        assert materialization['owner'] in materialization['owners']
        assert materialization['edge_before'] and materialization['edge_after']
    original_binding = next(binding for binding in original['records']['commands'][1]['bindings']
                            if binding['key'].startswith('doc.patch:'))
    landing_binding = next(binding for binding in landing['records']['commands'][1]['bindings']
                           if binding['key'].startswith('doc.patch:'))
    assert original_binding['ref'] == landing_binding['ref'] == staged
    assert original_binding['physical_ref'] == staged != landing_binding['physical_ref']
    assert original_binding['change']['before'] == landing_binding['change']['before'] == present([3])
    assert original_binding['boundary']['change_ordinal'] == landing_binding['boundary']['change_ordinal'] == 0
    mapped = next(item for item in proof['replay_bindings']
                  if item['command_ordinal'] == 1 and item['kind'] == 'patch')
    assert mapped['change_ordinal'] == 0
    expected_source = Closure(bundle_from_document(landing['commands'][0]['input'])).resolve('internal:chapter-1')
    assert mapped['source'] == {'step_id': 'first', 'source': expected_source,
                                'owner': mapped['source']['owner'],
                                'owners': ['occurrence:occ-a/shot', 'occurrence:occ-b/shot']}
    assert mapped['source']['owner'] in mapped['source']['owners']
    assert any(item['kind'] == 'read' and item['ref'] == staged for item in proof['replay_bindings'])


@pytest.mark.parametrize('method', [None, False])
def test_required_proof_fails_closed_on_unsupported_port(monkeypatch, method):
    realm = DetachedRealm()
    context, _ = sequential(realm)
    peer(realm)
    before = copy.deepcopy((realm.head, realm.documents, realm.entries, realm.accepted, realm.prepared))
    monkeypatch.setattr(DetachedPort, 'capture_sequential_execution', method, raising=False)
    with pytest.raises(CommitRejected) as error:
        create_runtime_commit_service().commit(context, DetachedPort(realm, context))
    assert error.value.failure['details']['reason'] == 'sequential_proof_unavailable'
    assert (realm.head, realm.documents, realm.entries, realm.accepted, realm.prepared) == before


@pytest.mark.parametrize('dry_run', [False, True])
def test_private_port_refusal_cannot_fall_through_to_accept(monkeypatch, dry_run):
    """Port verification is Runtime-owned; every refusal must stop the provider."""
    realm = DetachedRealm()
    context, _ = sequential(realm)
    context['request']['dry_run'] = dry_run
    context['invocation']['body'] = copy.deepcopy(context['request'])
    context['request_hash'] = _digest(context['invocation'])
    peer(realm)
    before = copy.deepcopy((realm.head, realm.documents, realm.entries, realm.accepted, realm.prepared))

    def refuse(port, capture):
        assert capture['context'] == port.context
        raise CommitRejected({'http_status': 422, 'code': 'invalid', 'message': 'rejected proof',
                              'details': {'status': 'invalid', 'reason': 'invalid_records'}})

    monkeypatch.setattr(DetachedPort, 'capture_sequential_execution', refuse, raising=False)
    with pytest.raises(CommitRejected) as error:
        create_runtime_commit_service().commit(context, DetachedPort(realm, context))
    assert error.value.failure['message'] == 'rejected proof'
    assert (realm.head, realm.documents, realm.entries, realm.accepted, realm.prepared) == before


def test_unchanged_head_sequential_path_keeps_legacy_private_abi():
    realm = DetachedRealm()
    context, _ = sequential(realm)
    accepted = create_runtime_commit_service().commit(context, DetachedPort(realm, context))
    assert accepted['data']['status'] == 'accepted'
    assert set(realm.prepared[0]) == {'expected_head', 'canonical_document', 'document_digest',
                                    'records', 'id_map', 'semantic_events', 'provider_version',
                                    'compiler_version'}


def test_each_cas_retry_captures_its_own_landing_without_replacing_original(monkeypatch):
    realm = DetachedRealm()
    context, _ = sequential(realm)
    captured = []
    monkeypatch.setattr(DetachedPort, 'capture_sequential_execution',
                        lambda port, capture: captured.append(copy.deepcopy(capture)), raising=False)
    realm.before_accept = lambda: peer(realm)
    create_runtime_commit_service().commit(context, DetachedPort(realm, context))
    assert len(captured) == 2
    assert captured[0]['context'] == captured[1]['context'] == context
    assert captured[0]['original'] == captured[1]['original']
    assert captured[0]['landing']['evaluated_head'] != captured[1]['landing']['evaluated_head']
    assert captured[0]['prepared_digest'] != captured[1]['prepared_digest']
    assert captured[0]['replay_bindings'] == []
    assert captured[1]['replay_bindings']
    assert captured[1]['prepared_digest'] == _digest(realm.prepared[-1])


def test_recursive_sequential_lineage_keeps_source_steps_and_complete_owners(monkeypatch):
    realm = DetachedRealm()
    context, _ = sequential(realm)
    request = context['request']
    bundle = bundle_from_document(realm.documents[request['base_revision_id']])
    source = execute(bundle, request['commands'], commit_request=request,
                     evaluated_head=copy.deepcopy(realm.head))
    staged = Closure(source.bundle).resolve('internal:chapter-1')
    third = patch(change(['unknown'], present([4]), [5], ref=staged))
    request['commands'].append(third)
    request['steps'].append(step('third', 2, third))
    context['invocation']['body'] = copy.deepcopy(request)
    context['request_hash'] = _digest(context['invocation'])
    peer(realm)
    captured = []
    monkeypatch.setattr(DetachedPort, 'capture_sequential_execution',
                        lambda port, capture: captured.append(copy.deepcopy(capture)), raising=False)
    create_runtime_commit_service().commit(context, DetachedPort(realm, context))
    proof = captured[0]
    mapping = next(item for item in proof['replay_bindings']
                   if item['kind'] == 'patch' and item['command_ordinal'] == 2)
    assert mapping['change_ordinal'] == 0
    assert mapping['source']['step_id'] == 'second'
    assert mapping['source']['source']['step_id'] == 'first'
    first_input = proof['landing']['commands'][0]['input']
    physical = Closure(bundle_from_document(first_input)).resolve('internal:chapter-1')
    assert mapping['source']['source']['source'] == physical
    for source in (mapping['source'], mapping['source']['source']):
        assert source['owners'] == ['occurrence:occ-a/shot', 'occurrence:occ-b/shot']
        assert source['owner'] in source['owners']
    for execution in (proof['original'], proof['landing']):
        assert len(execution['commands']) == 3
        assert execution['commands'][1]['output'] == execution['commands'][2]['input']


@pytest.mark.parametrize('field', ['original', 'replay_bindings'])
def test_caller_cannot_supply_private_execution_authority(monkeypatch, field):
    realm = DetachedRealm()
    context, _ = sequential(realm)
    context['request'][field] = {'forged': 'caller execution'}
    context['invocation']['body'] = copy.deepcopy(context['request'])
    context['request_hash'] = _digest(context['invocation'])
    before = copy.deepcopy((realm.head, realm.documents, realm.entries, realm.accepted, realm.prepared))

    def forbidden_capture(port, capture):
        pytest.fail('caller execution claims must not reach private capture')

    monkeypatch.setattr(DetachedPort, 'capture_sequential_execution', forbidden_capture, raising=False)
    with pytest.raises(CommitRejected, match='unknown commit control fields'):
        create_runtime_commit_service().commit(context, DetachedPort(realm, context))
    assert (realm.head, realm.documents, realm.entries, realm.accepted, realm.prepared) == before
