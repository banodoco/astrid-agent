"""Offline mirror effects through the mounted Agent provider and Runtime port."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline._command_state import ABSENT, present
from astrid.sdk.timeline.commit import create_runtime_commit_service
from tests.editing_conformance.test_doc_patch_v2 import change, patch, step


@pytest.fixture
def mounted(tmp_path):
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.store import RealmStore

    root = tmp_path / 'realm'
    RealmStore.initialize(root).close()
    service = RuntimeService(root)
    try:
        service.register_timeline_commit_provider(create_runtime_commit_service())
        project = service.create_project({'slug': 'mirror-effects', 'name': 'Mirror effects'})['id']
        service.create_timeline(project, 'film', idempotency_key='create')
        yield service, project
    finally:
        service.close()


def document(mounted):
    service, project = mounted
    head = service.get_project_timeline(project, 'film')
    return service._timeline_commits()[0].revision_closure(project, 'film', head['head_revision_id'])


def request(mounted, call, *, dry_run=False):
    service, project = mounted
    head = service.get_project_timeline(project, 'film')
    return {'api_version': 1, 'project_id': project, 'timeline_id': 'film',
            'base_revision_id': head['head_revision_id'],
            'base_publication_id': head['head_publication_id'], 'commands': [call],
            'label': 'Mirror edit', 'steps': [step('mirror', 0, call)],
            'merge': 'fields', 'reads': [], 'dry_run': dry_run, 'source': {'via': 'python'}}


def commit(mounted, body, key='edit'):
    from runtime_protocol.timeline_commit_port import route_path

    service, project = mounted
    return service.commit_timeline(project, 'film', body, idempotency_key=key, publisher='mirror-editor',
                                   invocation_path=route_path(project, 'film'), authorize=lambda: None)


def replacement(mounted):
    before = document(mounted)['parent_revision']['payload']['config']
    return patch(change(['config'], present(before), {'tracks': [{'id': 'picture', 'kind': 'visual'}]}),
                 change(['clips'], ABSENT, []), created=['parent/track:picture'])


def test_complete_effect_projects_output_with_root_mirror_absent(mounted, monkeypatch):
    from runtime_protocol.errors import RuntimeErrorBase
    from runtime_protocol.timeline_commit_auth import images, step_output
    from runtime_protocol.timeline_commit_port import RequestCommitPort

    service, _ = mounted
    source = document(mounted)
    call = replacement(mounted)
    call['args']['changes'][1]['path'] = ['config', 'clips']

    class Captured(RuntimeErrorBase):
        pass

    def observe(port, prepared):
        output = prepared['canonical_document']
        assert 'clips' not in output['parent_revision']['payload']
        assert output['parent_revision']['payload']['config']['clips'] == []
        records = prepared['records']
        assert records['steps'][0]['patch'][0]['path'] == []
        assert step_output(images(source), records['steps'][0], records, images(output)) == images(output)
        # Stop at the real prepared boundary: the wire schema requires root clips.
        raise Captured('captured genuine prepared output')

    monkeypatch.setattr(RequestCommitPort, 'accept', observe)
    before = tuple(service.store.conn.iterdump())
    with pytest.raises(Captured):
        commit(mounted, request(mounted, call))
    assert tuple(service.store.conn.iterdump()) == before


@pytest.mark.parametrize('dry_run', [False, True])
def test_config_replacement_retains_real_root_mirror_removal(mounted, monkeypatch, dry_run):
    from runtime_protocol.timeline_commit_auth import images, step_output
    from runtime_protocol.timeline_commit_port import RequestCommitPort
    from runtime_protocol.timeline_commit_validation import validate_commit_value

    service, project = mounted
    source = document(mounted)
    assert source['parent_revision']['payload'] == {
        'clips': [], 'config': {'clips': [], 'tracks': []}, 'occurrences': [], 'registry': {}}
    body = request(mounted, replacement(mounted), dry_run=dry_run)
    original = copy.deepcopy(body)
    captured = []
    method = RequestCommitPort.preview if dry_run else RequestCommitPort.accept

    def observe(port, prepared):
        captured.append(copy.deepcopy(prepared))
        records = prepared['records']
        # The unchanged Runtime projector must reach the genuine production output.
        assert step_output(images(source), records['steps'][0], records,
                           images(prepared['canonical_document'])) == images(prepared['canonical_document'])
        return method(port, prepared)

    monkeypatch.setattr(RequestCommitPort, 'preview' if dry_run else 'accept', observe)
    before = tuple(service.store.conn.iterdump())
    result = commit(mounted, body)
    assert body == original
    validate_commit_value(body, 'CommitRequest')
    validate_commit_value(result, 'CommitPreviewResponse' if dry_run else 'CommitAcceptedMutation')
    prepared, = captured
    records = prepared['records']
    record, = records['commands']
    output = result['data']['canonical_document']
    payload = output['parent_revision']['payload']
    assert payload['clips'] == [] and 'clips' not in payload['config']
    assert result['data']['commands'] == [record['command']]
    assert {key: record['command'][key] for key in body['commands'][0]} == body['commands'][0]
    assert result['data']['steps'] == records['steps']
    assert records['steps'][0]['patch'] == [{
        'op': 'set', 'ref': 'parent', 'path': [], 'before': present(source['parent_revision']['payload']),
        'value': payload}]
    assert record['created'] == [{'ref': 'parent/track:picture', 'kind': 'track', 'owner': 'parent'}]
    bindings = [item for item in record['bindings'] if item['key'].startswith('doc.patch:')]
    assert [item['change'] for item in bindings] == body['commands'][0]['args']['changes']
    assert bindings[0]['write_range'] == [0, 2]
    assert record['writes'][1] == {'ref': 'parent', 'path': ['clips'], 'before': present([]), 'after': ABSENT}
    assert bindings[1]['write_range'] == [2, 3]
    effects = [item for item in record['bindings'] if item['key'] == 'commit.effect']
    assert [(item['effect_ordinal'], item['boundary']['side'], item['path']) for item in effects] == [
        (0, 'before', []), (0, 'after', [])]
    assert effects[0]['revision_id'] == source['parent_revision']['revision_id']
    assert effects[1]['revision_id'] == output['parent_revision']['revision_id']
    assert effects[0]['content_digest'] == source['parent_revision']['content_digest']
    assert effects[1]['content_digest'] == output['parent_revision']['content_digest']
    assert all(item['boundary']['step_id'] == 'mirror' for item in effects)
    assert {'kind': 'field', 'ref': 'parent', 'path': ['config'],
            'state': present(source['parent_revision']['payload']['config'])} in records['steps'][0]['dependencies']
    if dry_run:
        assert result['receipt'] is None and result['data']['status'] == 'preview'
        assert tuple(service.store.conn.iterdump()) == before
    else:
        assert result['data']['status'] == 'accepted' and result['receipt']['result'] == result['data']
        assert document(mounted) == output
        history = service.list_timeline_commits(project, 'film', authorize=lambda: None)['items']
        assert history[-1]['steps'] == records['steps'] == history[-1]['records']['steps']


def test_nested_registry_and_clip_aliases_keep_synchronized_mirrors(mounted):
    before = document(mounted)['parent_revision']['payload']['config']
    config = {**before, 'registry': {}}
    first = commit(mounted, request(mounted, patch(change(['config'], present(before), config))), 'mirror-birth')
    payload = first['data']['canonical_document']['parent_revision']['payload']
    assert payload['registry'] == payload['config']['registry'] == {}
    assert payload['clips'] == payload['config']['clips'] == []
    assert first['data']['steps'][0]['patch'][0]['path'] == []
    call = patch(change(['config', 'registry', 'meta'], ABSENT, {'org.example.notes': {'note': 'A'}}),
                 change(['registry', 'meta', 'org.example.notes', 'note'], present('A'), 'B'),
                 change(['config', 'clips'], present([]), []))
    result = commit(mounted, request(mounted, call), 'nested')
    payload = result['data']['canonical_document']['parent_revision']['payload']
    assert payload['registry'] == payload['config']['registry'] == {
        'meta': {'org.example.notes': {'note': 'B'}}}
    assert payload['clips'] == payload['config']['clips'] == []
    assert [item['path'] for item in result['data']['steps'][0]['patch']] == [
        ['config', 'registry', 'meta'], ['config', 'registry', 'meta', 'org.example.notes', 'note'],
        ['config', 'clips']]


@pytest.mark.parametrize('damage', ['before', 'created-missing', 'created-extra', 'submitted-step'])
def test_invalid_submitted_claims_cannot_use_effect_completion(mounted, damage):
    from runtime_protocol.errors import RuntimeErrorBase

    service, _ = mounted
    call = replacement(mounted)
    if damage == 'before':
        call['args']['changes'][0]['before'] = ABSENT.copy()
    elif damage == 'created-missing':
        call['created'] = []
    elif damage == 'created-extra':
        call['created'].append('parent/clip:foreign')
    body = request(mounted, call)
    if damage == 'submitted-step':
        body['steps'][0]['patch'] = []
    before = tuple(service.store.conn.iterdump())
    with pytest.raises(RuntimeErrorBase) as error:
        commit(mounted, body)
    assert error.value.details['status'] == 'invalid'
    assert error.value.details['reason'] == ('previous_value_mismatch' if damage == 'before' else 'capture_mismatch')
    assert tuple(service.store.conn.iterdump()) == before


@pytest.mark.parametrize('damage', ['effect-before', 'effect-value', 'effect-path', 'foreign-owner', 'source-binding'])
def test_runtime_rejects_corrupt_effects_and_allocation_custody(mounted, monkeypatch, damage):
    from runtime_protocol.errors import RuntimeErrorBase
    from runtime_protocol.timeline_commit_port import RequestCommitPort

    service, _ = mounted
    original_accept = RequestCommitPort.accept

    def corrupt(port, prepared):
        prepared = copy.deepcopy(prepared)
        record = prepared['records']['commands'][0]
        effect = prepared['records']['steps'][0]['patch'][0]
        if damage == 'effect-before':
            effect['before'] = ABSENT.copy()
        elif damage == 'effect-value':
            effect['value']['config']['clips'] = []
        elif damage == 'effect-path':
            effect['path'] = ['config', 0]
        elif damage == 'foreign-owner':
            effect['ref'] = 'internal-revision:foreign-owner'
        else:
            record['bindings'][0]['revision_id'] = 'foreign-parent'
        return original_accept(port, prepared)

    monkeypatch.setattr(RequestCommitPort, 'accept', corrupt)
    before = tuple(service.store.conn.iterdump())
    with pytest.raises(RuntimeErrorBase) as error:
        commit(mounted, request(mounted, replacement(mounted)))
    assert error.value.details['reason'] == ('invalid_prepared' if damage == 'effect-path' else 'invalid_records')
    assert tuple(service.store.conn.iterdump()) == before
