"""Synthesized steps retain real child input and inverse effect authority."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline.capture import capture_track_create
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import (
    bundle_from_document,
    create_runtime_commit_service,
    document_from_bundle,
)
from astrid.sdk.timeline.native_occurrence_create import capture_occurrence_create
from tests.helpers.runtime import use_runtime

pytestmark = pytest.mark.integration


@pytest.fixture
def native_child(tmp_path):
    use_runtime()
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.store import RealmStore
    from runtime_protocol.timeline_commit_port import route_path

    root = tmp_path / 'realm'
    RealmStore.initialize(root).close()
    service = RuntimeService(root)
    service.register_timeline_commit_provider(create_runtime_commit_service())
    project = service.create_project({'slug': 'native-child', 'name': 'Native child'})['id']
    service.create_timeline(project, 'film', idempotency_key='create-film')

    def document():
        head = service.get_project_timeline(project, 'film')
        return service._timeline_commits()[0].revision_closure(project, 'film', head['head_revision_id'])

    def submit(calls, key):
        head = service.get_project_timeline(project, 'film')
        body = {'api_version': 1, 'project_id': project, 'timeline_id': 'film',
                'base_revision_id': head['head_revision_id'],
                'base_publication_id': head['head_publication_id'], 'commands': calls}
        original = copy.deepcopy(body)
        result = service.commit_timeline(project, 'film', body, idempotency_key=key,
            publisher='author', invocation_path=route_path(project, 'film'), authorize=lambda: None)
        assert body == original
        return result

    try:
        bundle = bundle_from_document(document())
        submit([capture_track_create(bundle, 'parent', {'id': 'picture', 'kind': 'visual'})], 'parent')
        bundle = bundle_from_document(document())
        submit([capture_occurrence_create(bundle, 'child', 'child-shot', name='Child',
                track='picture', on=0, duration=10)], 'child')
        yield service, document, submit
    finally:
        service.close()


def child_tracks(document):
    bundle = bundle_from_document(document)
    first = capture_track_create(bundle, 'occurrence:child', {'id': 'one', 'kind': 'visual'})
    staged = execute(bundle, [first]).bundle
    second = capture_track_create(staged, 'occurrence:child', {'id': 'two', 'kind': 'visual'})
    return [first, second]


def test_native_child_batch_captures_materialized_input(native_child):
    _, document, _ = native_child
    bundle = bundle_from_document(document())
    calls = child_tracks(document())
    intermediate = document_from_bundle(execute(bundle, calls[:1]).bundle)
    plan = execute(bundle, calls, commit_request={})
    image = intermediate['internal_timeline_revisions'][0]
    binding, = [row for row in plan.records[1]['bindings'] if row['key'] == 'commit.input'
                and row['physical_ref'] == 'internal-revision:' + image['revision_id']]
    assert binding['revision_id'] == image['revision_id']
    assert binding['content_digest'] == image['content_digest']
    assert binding['boundary']['command_ordinal'] == 1
    assert binding['boundary']['phase'] == 'input'


def test_native_child_inverse_uses_authenticated_effect_images(native_child):
    service, document, submit = native_child
    before = document()
    accepted = submit(child_tracks(before)[:1], 'track')
    assert accepted['data']['status'] == 'accepted'
    assert accepted['receipt']['result'] == accepted['data']
    records = service.list_timeline_commits(before['project_id'], 'film', authorize=lambda: None)['items'][-1]['records']
    assert records['steps'][0]['command_ordinals'] == [0]
    assert any(binding['key'] == 'commit.effect' and binding['boundary']['side'] == 'before'
               for binding in records['commands'][0]['bindings'])
    assert [row['id'] for row in document()['internal_timeline_revisions'][0]['payload']['tracks']] == ['one']
    inverse = {'schema_version': 1, 'op': 'timeline.inverse', 'version': '1', 'select': None,
               'args': {'publications': [accepted['receipt']['receipt_id']]}}
    assert submit([inverse], 'undo')['data']['canonical_document'] == before


@pytest.mark.parametrize('damage', ['revision', 'digest', 'physical-ref'])
def test_native_child_effect_tampering_cannot_write(native_child, monkeypatch, damage):
    from runtime_protocol.errors import RuntimeErrorBase
    from runtime_protocol.timeline_commit_port import RequestCommitPort

    service, document, submit = native_child
    before = document()
    stored = tuple(service.store.conn.iterdump())
    accept = RequestCommitPort.accept

    def damaged(port, prepared):
        prepared = copy.deepcopy(prepared)
        bindings = prepared['records']['commands'][0]['bindings']
        binding = next(row for row in bindings if row['key'] == 'commit.effect'
                       and row['boundary']['side'] == 'before'
                       and row['physical_ref'].startswith('internal-revision:'))
        if damage == 'physical-ref':
            binding['physical_ref'] = 'internal-revision:foreign'
        elif damage == 'revision':
            binding['revision_id'] = 'foreign-revision'
        else:
            binding['content_digest'] = 'sha256:' + 'f' * 64
        return accept(port, prepared)

    monkeypatch.setattr(RequestCommitPort, 'accept', damaged)
    with pytest.raises(RuntimeErrorBase) as error:
        submit(child_tracks(before)[:1], 'tampered')
    assert error.value.status == 422
    assert error.value.details['reason'] == 'invalid_records'
    assert document() == before
    assert tuple(service.store.conn.iterdump()) == stored
