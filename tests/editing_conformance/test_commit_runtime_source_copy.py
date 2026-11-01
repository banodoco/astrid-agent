"""D17 semantics through the staged Runtime source-authority port."""
from __future__ import annotations

import copy

import pytest

from tests.helpers.runtime import use_runtime

pytestmark = pytest.mark.integration


@pytest.fixture
def source_copy(tmp_path):
    use_runtime()
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.store import RealmStore
    from runtime_protocol.timeline_commit_provider import load_timeline_commit_provider

    from astrid.sdk.timeline.commit import bundle_from_document
    from astrid.sdk.timeline.native_occurrence_create import capture_occurrence_create

    root = tmp_path / 'realm'
    RealmStore.initialize(root).close()
    service = RuntimeService(root)
    project = service.create_project({'slug': 'copy', 'name': 'Copy'})['id']
    service.register_timeline_commit_provider(load_timeline_commit_provider('astrid'))
    for timeline in ('source', 'destination'):
        service.create_timeline(project, timeline, idempotency_key='create-' + timeline)

    def document(timeline):
        head = service.get_project_timeline(project, timeline)
        return service._timeline_commits()[0].revision_closure(project, timeline, head['head_revision_id'])

    def request(timeline, calls):
        head = service.get_project_timeline(project, timeline)
        return {'api_version': 1, 'project_id': project, 'timeline_id': timeline,
                'base_revision_id': head['head_revision_id'], 'base_publication_id': head['head_publication_id'],
                'commands': calls, 'label': 'Copy source', 'merge': 'atomic', 'reads': [],
                'steps': [{'id': 'copy', 'label': 'Copy source', 'intent': 'Keep full source',
                           'tags': ['copy'], 'command_ordinals': list(range(len(calls))), 'patch': [], 'reads': []}],
                'dry_run': False, 'source': {'via': 'python'}, 'rationale': 'D17', 'group_id': 'copy'}

    def commit(timeline, body, key):
        return service.commit_timeline(project, timeline, body, idempotency_key=key, publisher='editor',
            invocation_path=f'/v1/projects/{project}/timelines/{timeline}/commit', authorize=lambda: None)

    from astrid.sdk.timeline.capture import capture_track_create
    bundle = bundle_from_document(document('source'))
    commit('source', request('source', [capture_track_create(bundle, 'parent', {'id': 'picture', 'kind': 'visual'})]), 'track')
    bundle = bundle_from_document(document('source'))
    commit('source', request('source', [capture_occurrence_create(bundle, 'one', 'shared', name='Shared',
        track='picture', on=0, duration=10)]), 'shot')
    source = document('source')
    head = service.get_project_timeline(project, 'source')
    command = {'schema_version': 1, 'op': 'timeline.duplicate', 'version': '1', 'select': 'parent',
               'args': {'source_ref': {'project_id': project, 'timeline_id': 'source',
                        'revision_id': head['head_revision_id'], 'publication_id': head['head_publication_id'],
                        'content_digest': source['parent_revision']['content_digest']},
                        'shots': {'shared': 'copied'}, 'occurrences': {'one': 'copy-one'}}}
    command['created'] = ['shot:copied', 'occurrence:copy-one']
    try:
        yield service, document, request, commit, command
    finally:
        service.close()


def test_source_copy_real_port_receipts_preview_undo_redo(source_copy):
    service, document, request, commit, command = source_copy
    before, source = document('destination'), document('source')
    body = request('destination', [command])
    preview = commit('destination', {**body, 'dry_run': True}, 'preview')
    assert preview['receipt'] is None and preview['data']['would_status'] == 'accepted'
    assert document('destination') == before
    result = commit('destination', body, 'copy')
    after = document('destination')
    rows = after['parent_revision']['payload']['occurrences']
    assert [row['occurrence_id'] for row in rows] == ['copy-one']
    assert {row['shot_id'] for row in rows} == {'copied'}
    assert len({row['shot_revision_id'] for row in rows}) == 1
    assert rows[0]['placement']['start_ms'] == 0
    assert len(after['shot_revisions']) == len(after['internal_timeline_revisions']) == 1
    assert document('source') == source
    assert commit('destination', body, 'copy') == result
    assert result['data']['steps'][0]['patch']
    record = service.list_timeline_commits(after['project_id'], 'destination', authorize=lambda: None)['items'][-1]['records']['commands'][0]
    assert {item['ref'] for item in record['created']} == {'shot:copied', 'occurrence:copy-one'}
    def inverse(accepted, key):
        call = {'schema_version': 1, 'op': 'timeline.inverse', 'version': '1', 'select': None,
                'args': {'publications': [accepted['receipt']['receipt_id']]}}
        return commit('destination', request('destination', [call]), key)
    undo = inverse(result, 'undo')
    assert document('destination') == before
    inverse(undo, 'redo')
    assert document('destination') == after


@pytest.mark.parametrize('damage', ['foreign', 'outside', 'pair', 'digest', 'same', 'occupied', 'sharing', 'order', 'media', 'created', 'archived'])
def test_source_copy_refuses_bad_authority_or_claims_without_writes(source_copy, damage):
    from runtime_protocol.errors import RuntimeErrorBase

    service, document, request, commit, command = source_copy
    call = copy.deepcopy(command)
    args, ref = call['args'], call['args']['source_ref']
    if damage == 'foreign':
        ref['project_id'] = 'foreign'
    elif damage == 'outside':
        ref['timeline_id'] = 'outside'
    elif damage == 'pair':
        ref['publication_id'] = 'wrong'
    elif damage == 'digest':
        ref['content_digest'] = 'sha256:' + '0' * 64
    elif damage == 'same':
        ref['timeline_id'] = 'destination'
    elif damage == 'occupied':
        args['shots']['shared'] = 'shared'
    elif damage == 'sharing':
        args['shots']['unrequested'] = 'extra'
    elif damage == 'order':
        args['occurrences']['two'] = 'copy-one'
    elif damage == 'media':
        args['media'] = [{'media_id': 'sha256:' + 'f' * 64}]
    elif damage == 'archived':
        row = service.get_project_timeline(ref['project_id'], 'source')
        service.archive_timeline('source', {'expected_version': row['version']}, idempotency_key='archive')
    else:
        call['created'] = ['shot:forged']
    before = document('destination')
    history = service.list_timeline_commits(before['project_id'], 'destination', authorize=lambda: None)
    stored = tuple(service.store.conn.iterdump())
    with pytest.raises(RuntimeErrorBase):
        commit('destination', request('destination', [call]), damage)
    assert document('destination') == before
    assert service.list_timeline_commits(before['project_id'], 'destination', authorize=lambda: None) == history
    assert tuple(service.store.conn.iterdump()) == stored


def test_production_duplicate_caller_uses_authenticated_semantic_and_archives_failures(source_copy, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from astrid.sdk.contracts import DomainResult
    from astrid.sdk.remote import RemoteTimelines
    from astrid.sdk.timeline.commit import bundle_from_document
    from astrid.sdk.timeline_duplicate import duplicate_timeline
    from astrid.sdk.workspace_client import WorkspaceClient

    service, document, request, commit, command = source_copy
    project = command['args']['source_ref']['project_id']
    generated = SimpleNamespace()
    transport = WorkspaceClient.__new__(WorkspaceClient)
    transport._generated = generated
    transport._last_handshake = {'realm_id': service.realm['id'], 'actor_id': 'editor'}
    client = SimpleNamespace(_remote=SimpleNamespace(_transport=transport), timelines=RemoteTimelines(transport))
    client.timelines.open_composition = lambda p, t: DomainResult.success({
        'native_inspection': {'project_id': project, 'timeline_id': t},
        'summary': {'head_revision_id': service.get_project_timeline(project, t)['head_revision_id']}})
    client.timelines._resolve_timeline = lambda p, t: DomainResult.success({'project_id': project, 'timeline_id': t})
    client.timelines.open_bundle = lambda p, t, revision_id=None, **kw: DomainResult.success({'bundle': bundle_from_document(
        service._timeline_commits()[0].revision_closure(project, t,
            revision_id or service.get_project_timeline(project, t)['head_revision_id']))})
    generated.list_timeline_commits = lambda p, t, cursor=None, limit=200: service.list_timeline_commits(
        project, t, cursor=cursor, limit=limit, authorize=lambda: None)
    bodies = []
    def send(p, t, body, *, idempotency_key):
        bodies.append(copy.deepcopy(body))
        return commit(t, body, idempotency_key)
    generated.commit_timeline = send
    client.timelines.create_empty = lambda **kw: SimpleNamespace(ok=True, data={'timeline_id': 'destination'})
    archived = []
    client.timelines.archive = lambda p, t: archived.append(t) or SimpleNamespace(ok=True)
    result = duplicate_timeline(project, 'source', slug='destination', client=client)
    assert result['timeline_id'] == 'destination' and result['shots'] == 1
    assert [call['op'] for call in bodies[0]['commands']] == ['timeline.duplicate']
    assert bodies[0]['commands'][0]['args']['source_ref'] == command['args']['source_ref']
    assert document('source')['parent_revision']['revision_id'] == command['args']['source_ref']['revision_id']
    assert archived == []

    # The failed destination publication is cleaned up by the same production wrapper.
    from astrid.sdk.timeline._common import TimelineEditError
    with pytest.raises(TimelineEditError, match='archived again'):
        duplicate_timeline(project, 'source', slug='destination', client=client)
    assert archived == ['destination']


def test_source_copy_rejects_project_wide_occupied_shot_without_writes(source_copy):
    from runtime_protocol.errors import RuntimeErrorBase

    from astrid.sdk.timeline.commit import bundle_from_document
    from astrid.sdk.timeline.native_occurrence_create import capture_occurrence_create

    service, document, request, commit, command = source_copy
    # Advance the source after capturing its exact historical pair. The new
    # shot reserves the proposed destination identity project-wide.
    bundle = bundle_from_document(document('source'))
    born = capture_occurrence_create(bundle, 'occupied-owner', 'copied', name='Occupied',
                                    track='picture', on=20, duration=1)
    commit('source', request('source', [born]), 'occupied-shot')
    before = document('destination')
    service.list_timeline_commits(before['project_id'], 'destination', authorize=lambda: None)
    rows = tuple(service.store.conn.iterdump())
    with pytest.raises(RuntimeErrorBase):
        commit('destination', request('destination', [command]), 'copy-occupied')
    assert document('destination') == before
    assert tuple(service.store.conn.iterdump()) == rows


@pytest.fixture
def saved_source(tmp_path, monkeypatch):
    """Import an existing film at the established trusted fixture acceptance seam.

    Source creation is setup, not semantic evidence. Media is actually ingested;
    source history and every subsequent source capture/commit use the real store.
    """
    use_runtime()
    from types import SimpleNamespace

    from runtime_protocol.service import RuntimeService
    from runtime_protocol.store import RealmStore
    from runtime_protocol.timeline_commit_provider import load_timeline_commit_provider

    from astrid.sdk.contracts import DomainResult
    from astrid.sdk.remote import RemoteTimelines
    from astrid.sdk.timeline.commit import bundle_from_document
    from astrid.sdk.workspace_client import WorkspaceClient
    from tests.helpers.trusted_seed import seed_trusted_parent

    root = tmp_path / 'saved-source'
    RealmStore.initialize(root).close()
    service = RuntimeService(root)
    project = service.create_project({'slug': 'saved', 'name': 'Saved source'})['id']
    service.create_timeline(project, 't', idempotency_key='source')

    def document(timeline):
        head = service.get_project_timeline(project, timeline)
        return service._timeline_commits()[0].revision_closure(project, timeline, head['head_revision_id'])

    bodies = []
    def send(p, t, body, *, idempotency_key):
        bodies.append(copy.deepcopy(body))
        return service.commit_timeline(project, t, body, idempotency_key=idempotency_key,
            publisher='editor', invocation_path=f'/v1/projects/{project}/timelines/{t}/commit',
            authorize=lambda: None)

    generated = SimpleNamespace(commit_timeline=send, list_timeline_commits=lambda p, t, cursor=None, limit=200:
        service.list_timeline_commits(project, t, cursor=cursor, limit=limit, authorize=lambda: None))
    transport = WorkspaceClient.__new__(WorkspaceClient)
    transport._generated = generated
    transport._last_handshake = {'realm_id': service.realm['id'], 'actor_id': 'editor'}
    client = SimpleNamespace(_remote=SimpleNamespace(_transport=transport), timelines=RemoteTimelines(transport))
    client.timelines.open_composition = lambda p, t: DomainResult.success({
        'native_inspection': {'project_id': project, 'timeline_id': t},
        'summary': {'head_revision_id': service.get_project_timeline(project, t)['head_revision_id']}})
    client.timelines._resolve_timeline = lambda p, t: DomainResult.success({'project_id': project, 'timeline_id': t})
    client.timelines.open_bundle = lambda p, t, revision_id=None, **kw: DomainResult.success({'bundle':
        bundle_from_document(service._timeline_commits()[0].revision_closure(project, t,
            revision_id or service.get_project_timeline(project, t)['head_revision_id']))})
    client.timelines.create_empty = lambda **kw: DomainResult.success(service.create_timeline(
        project, kw['timeline_id'], name=kw.get('name'), idempotency_key=kw['idempotency_key']))
    def archive(p, t):
        row = service.get_project_timeline(project, t)
        return DomainResult.success(service.archive_timeline(t, {'expected_version': row['version']}, idempotency_key='archive-' + t))
    client.timelines.archive = archive

    def install(bundle):
        source = copy.deepcopy(bundle)
        def ingest(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key == 'media_id' and isinstance(item, str):
                        value[key] = service.ingest(project, item.encode(), original_name=item + '.bin',
                            media_type='application/octet-stream', idempotency_key='media-' + item)['data']['object_id']
                    else:
                        ingest(item)
            elif isinstance(value, list):
                for item in value:
                    ingest(item)
        ingest(source)
        shots, internals = [], []
        for sid, shot in source['shots'].items():
            internal_id = 'internal-' + sid
            internals.append({'timeline_id': 't', 'revision_id': internal_id, 'payload': shot['internal_timeline']})
            shots.append({'shot_id': sid, 'revision_id': 'shot-' + sid,
                'internal_timeline_revision_id': internal_id, 'payload': shot['payload']})
        parent = source['parent']
        parent['occurrences'] = [{**row, 'shot_revision_id': 'shot-' + row['shot_id']} for row in source['placements']]
        seed_trusted_parent(service, {'project_id': project, 'timeline_id': 't',
            'parent_revision_id': 'saved-parent', 'parent_composition': parent,
            'shot_revisions': shots, 'internal_timeline_revisions': internals}, idempotency_key='saved-source')
        service.register_timeline_commit_provider(load_timeline_commit_provider('astrid'))
        return client, project, document, bodies
    try:
        yield install
    finally:
        service.close()


# D17 must restore repeated physical roots through guarded inverse without
# inventing config mirrors or relaxing authored guards. Single-shot undo is green.
@pytest.mark.parametrize('with_content', [False, True])
def test_repeated_source_copy_real_port_idempotency_and_guarded_inverse(saved_source, with_content):
    from astrid.sdk.timeline import open as open_timeline
    from astrid.sdk.timeline_duplicate import duplicate_timeline
    from tests.sdk.test_timeline_restructure import flat_film

    source = flat_film()
    source['parent']['clips'] = []
    source['parent']['registry'] = {}
    source['shots']['film']['internal_timeline']['clips'] = []
    source['shots']['film']['internal_timeline']['registry'] = {'assets': {}}
    if with_content:
        source['shots']['film']['payload']['items'] = [
            {'item_id': 'source-item', 'media_id': 'picture', 'metadata': {'opaque': [None, False]}}]
        source['shots']['film']['internal_timeline']['clips'] = [
            {'id': 'picture-clip', 'clipType': 'image', 'track': 'plate',
             'asset': 'picture', 'at': 0, 'hold': 2, 'opaque': [None, False]}]
        source['shots']['film']['internal_timeline']['registry']['assets']['picture'] = {
            'type': 'image', 'media_id': 'picture'}
    again = copy.deepcopy(source['placements'][0])
    again.update(occurrence_id='again', placement={'start_ms': 12000})
    source['placements'].append(again)
    client, project, document, bodies = saved_source(source)
    original = document('t')
    result = duplicate_timeline(project, 't', slug='copy', client=client)
    copied = document('copy')
    body = copy.deepcopy(bodies[0])
    history = client.timelines.history(project, 'copy', limit=200).data['items']
    entry = next(row for row in history if row['revision_id'] == result['new_head'])
    key = entry['receipt']['idempotency_key']
    reply = client._remote._transport._generated.commit_timeline(project, 'copy', body, idempotency_key=key)
    assert reply['receipt'] == entry['receipt'] and document('copy') == copied
    timeline = open_timeline(project, 'copy', client=client)
    undone = timeline.undo(publications=[entry['publication_id']], idempotency_key='undo-copy')
    assert document('copy')['parent_revision']['payload']['occurrences'] == []
    assert document('copy')['shot_revisions'] == document('copy')['internal_timeline_revisions'] == []
    timeline.undo(publications=[undone['receipt']['receipt_id']], idempotency_key='redo-copy')
    assert document('copy') == copied
    assert document('t') == original


@pytest.mark.parametrize('accepted_before_disconnect', [False, True])
def test_source_copy_unknown_outcome_retains_destination_and_exact_retry(saved_source, accepted_before_disconnect):
    from astrid.sdk.timeline import open as open_timeline
    from astrid.sdk.timeline._common import TimelineEditError
    from astrid.sdk.timeline_duplicate import duplicate_timeline
    from tests.sdk.test_timeline_restructure import flat_film

    source = flat_film()
    source['parent']['clips'] = []
    source['parent']['registry'] = {}
    source['shots']['film']['internal_timeline']['clips'] = []
    source['shots']['film']['internal_timeline']['registry'] = {'assets': {}}
    client, project, document, bodies = saved_source(source)
    original = document('t')
    generated = client._remote._transport._generated
    send, archive = generated.commit_timeline, client.timelines.archive
    sent, archived, accepted = [], [], []

    def disconnect(p, t, body, *, idempotency_key):
        sent.append((copy.deepcopy(body), idempotency_key))
        if len(sent) == 1:
            if accepted_before_disconnect:
                accepted.append(send(p, t, body, idempotency_key=idempotency_key))
            raise TimeoutError('copy acknowledgement unavailable')
        return send(p, t, body, idempotency_key=idempotency_key)

    def record_archive(p, t):
        archived.append(t)
        return archive(p, t)

    generated.commit_timeline = disconnect
    client.timelines.archive = record_archive
    with pytest.raises(TimelineEditError, match='outcome.*unknown'):
        duplicate_timeline(project, 't', slug='copy', client=client)
    assert archived == []
    restarted = open_timeline(project, 'copy', client=client).session
    with restarted.outbox.locked():
        pending = restarted._state()
    assert pending['flight']['key'] == sent[0][1]
    assert pending['pending'] == sent[0][0]['commands']
    retried = restarted.flush()
    assert sent[1] == sent[0]
    if accepted:
        assert retried['receipt'] == accepted[0]['receipt']
    history = client.timelines.history(project, 'copy', limit=200).data['items']
    assert len([entry for entry in history if entry['intent'] != 'bootstrap']) == 1
    assert len(document('copy')['parent_revision']['payload']['occurrences']) == 1
    assert document('t') == original
    with restarted.outbox.locked():
        final = restarted._state()
    assert final['pending'] == [] and final['flight'] is None


@pytest.mark.parametrize('mirrors', [(), ('clips',), ('registry',), ('clips', 'registry')])
def test_source_copy_real_port_preserves_raw_parent_config_presence(saved_source, mirrors):
    from astrid.sdk.timeline_duplicate import duplicate_timeline
    from tests.sdk.test_timeline_restructure import flat_film

    source = flat_film()
    source['parent']['clips'] = []
    source['parent']['registry'] = {}
    source['shots']['film']['internal_timeline']['clips'] = []
    source['shots']['film']['internal_timeline']['registry'] = {'assets': {}}
    for key in mirrors:
        source['parent']['config'][key] = copy.deepcopy(source['parent'][key])
    client, project, document, bodies = saved_source(source)
    original = document('t')['parent_revision']['payload']
    assert ('clips' in original['config']) == ('clips' in mirrors)
    duplicate_timeline(project, 't', slug='copy', client=client)
    copied = document('copy')['parent_revision']['payload']
    assert {key: value for key, value in copied.items() if key != 'occurrences'} == {
        key: value for key, value in original.items() if key != 'occurrences'}


def test_source_copy_retained_inputs_whole_undo_redo(source_copy, monkeypatch):
    from runtime_protocol.timeline_commit_inputs import (
        DocumentInputLink,
        capture_document_input,
        persist_document_input_links,
        validate_captured_document_inputs,
    )

    from astrid.sdk.timeline.commit import _digest
    from tests.helpers import trusted_seed

    service, document, request, commit, command = source_copy
    project = command['args']['source_ref']['project_id']
    content = {'schema': 'copy.config/v1', 'opaque': [None, False, 2]}
    service.create_document(project, {'document_id': 'retained', 'kind': 'copy.config',
        'content': content}, idempotency_key='retained-config')
    pin = {'document_id': 'retained', 'version': 1, 'content_digest': _digest(content)}
    media = service.ingest(project, b'retained-only', original_name='retained.bin',
        media_type='application/octet-stream', idempotency_key='retained-media')['data']['object_id']
    capture = capture_document_input(service.store.conn, project, pin,
        expected_kind='copy.config', expected_schema=content['schema'])
    class Inputs:
        def validate(self, candidate):
            validate_captured_document_inputs(service.store.conn, [capture])
        def persist(self, candidate, publication_id, revision_id):
            persist_document_input_links(service.store.conn, project_id=project, timeline_id='source',
                publication_id=publication_id, revision_id=revision_id,
                links=[DocumentInputLink(capture, 'source', 0)])

    # Trusted saved-film setup only. The copy and both inverse acceptances use
    # the production provider and real scoped captures without any adapter.
    commits = service._timeline_commits()[0]
    closure, accept = trusted_seed._closure, commits.accept
    def retained_closure(*args):
        result = closure(*args)
        result['dependency_manifest']['documents'] = [pin]
        result['dependency_manifest']['media'].append({'media_id': media, 'content_digest': media})
        return result
    source = document('source')
    with monkeypatch.context() as setup:
        setup.setattr(trusted_seed, '_closure', retained_closure)
        setup.setattr(commits, 'accept', lambda *args, **kw: accept(*args, **kw, inputs=Inputs()))
        trusted_seed.seed_trusted_parent(service, {'project_id': project, 'timeline_id': 'source',
            'parent_revision_id': 'retained-source', 'parent_composition': source['parent_revision']['payload'],
            'shot_revisions': source['shot_revisions'],
            'internal_timeline_revisions': source['internal_timeline_revisions']}, idempotency_key='retained-source')
    original = document('source')
    head = service.get_project_timeline(project, 'source')
    command['args']['source_ref'].update(revision_id=head['head_revision_id'],
        publication_id=head['head_publication_id'], content_digest=original['parent_revision']['content_digest'])
    before = document('destination')
    copied = commit('destination', request('destination', [command]), 'retained-copy')
    after = document('destination')
    assert after['dependency_manifest']['documents'] == [pin]
    assert after['dependency_manifest']['media'] == original['dependency_manifest']['media']
    def inverse(result, key):
        call = {'schema_version': 1, 'op': 'timeline.inverse', 'version': '1', 'select': None,
                'args': {'publications': [result['receipt']['receipt_id']]}}
        return commit('destination', request('destination', [call]), key)
    undone = inverse(copied, 'retained-undo')
    assert document('destination') == before
    inverse(undone, 'retained-redo')
    assert document('destination') == after
    assert document('source') == original


@pytest.mark.parametrize('damage', ['archived-after-capture', 'forged-physical-media'])
def test_source_copy_captured_authority_still_guards_acceptance(source_copy, monkeypatch, damage):
    from runtime_protocol.errors import RuntimeErrorBase
    from runtime_protocol.timeline_commit_port import RequestCommitPort

    service, document, request, commit, command = source_copy
    project = command['args']['source_ref']['project_id']
    media = service.ingest(project, b'outside-source', original_name='outside.bin',
        media_type='application/octet-stream', idempotency_key='outside-media')['data']['object_id']
    original = RequestCommitPort.source_revision_closure
    snapshots = []
    def captured(port, ref):
        result = original(port, ref)
        if damage == 'archived-after-capture':
            head = service.get_project_timeline(project, 'source')
            service.archive_timeline('source', {'expected_version': head['version']},
                                     idempotency_key='archive-captured-source')
        else:
            result['dependency_manifest']['media'].append({'media_id': media, 'content_digest': media})
        snapshots.append(tuple(service.store.conn.iterdump()))
        return result
    monkeypatch.setattr(RequestCommitPort, 'source_revision_closure', captured)
    before = document('destination')
    with pytest.raises(RuntimeErrorBase):
        commit('destination', request('destination', [command]), damage)
    assert snapshots and tuple(service.store.conn.iterdump()) == snapshots[-1]
    assert document('destination') == before


@pytest.mark.parametrize('aba', [False, True])
def test_source_copy_inverse_refuses_later_leaf_writes_atomically(source_copy, aba):
    from runtime_protocol.errors import RuntimeErrorBase

    from astrid.sdk.timeline._command_state import present

    service, document, request, commit, command = source_copy
    copied = commit('destination', request('destination', [command]), 'guard-copy')
    shot = document('destination')['shot_revisions'][0]
    original = shot['payload']['name']
    def rename(before, value, key):
        change = {'op': 'set', 'ref': 'shot-revision:' + shot['revision_id'],
                  'path': ['name'], 'before': present(before), 'value': value}
        call = {'schema_version': 1, 'op': 'doc.patch', 'version': '1', 'select': None,
                'args': {'changes': [change]}}
        body = request('destination', [call])
        body['merge'] = 'fields'
        body['steps'][0]['patch'] = [change]
        return commit('destination', body, key)
    changed = rename(original, 'Later', 'later-leaf')
    if aba:
        shot = changed['data']['canonical_document']['shot_revisions'][0]
        rename('Later', original, 'aba-leaf')
    before = document('destination')
    stored = tuple(service.store.conn.iterdump())
    call = {'schema_version': 1, 'op': 'timeline.inverse', 'version': '1', 'select': None,
            'args': {'publications': [copied['receipt']['receipt_id']]}}
    with pytest.raises(RuntimeErrorBase):
        commit('destination', request('destination', [call]), 'guard-inverse')
    assert document('destination') == before
    assert tuple(service.store.conn.iterdump()) == stored


def test_source_copy_known_refusal_archives_actual_destination(saved_source):
    from astrid.sdk.timeline._common import TimelineEditError
    from astrid.sdk.timeline_duplicate import duplicate_timeline
    from tests.sdk.test_timeline_restructure import flat_film

    source = flat_film()
    source['shots']['film']['internal_timeline']['clips'] = []
    source['shots']['film']['internal_timeline']['registry'] = {'assets': {}}
    client, project, document, bodies = saved_source(source)
    original = document('t')
    generated = client._remote._transport._generated
    send, archive = generated.commit_timeline, client.timelines.archive
    archived = []
    destinations = []
    def refuse(p, t, body, *, idempotency_key):
        destinations.append(document(t))
        invalid = copy.deepcopy(body)
        invalid['commands'][0]['args']['source_ref']['content_digest'] = 'sha256:' + '0' * 64
        return send(p, t, invalid, idempotency_key=idempotency_key)
    def cleanup(p, t):
        result = archive(p, t)
        archived.append((t, result.data))
        return result
    generated.commit_timeline = refuse
    client.timelines.archive = cleanup
    with pytest.raises(TimelineEditError, match='archived again'):
        duplicate_timeline(project, 't', slug='refused-copy', client=client)
    assert len(archived) == 1 and archived[0][0] == 'refused-copy'
    assert archived[0][1]['data']['archived'] is True
    assert archived[0][1]['data']['head_revision_id'] == destinations[0]['parent_revision']['revision_id']
    assert destinations[0]['parent_revision']['payload']['occurrences'] == []
    assert document('t') == original
