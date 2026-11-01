"""Historical shared physical ownership through the authenticated Runtime port."""
from __future__ import annotations

import copy

import pytest

from astrid.sdk.timeline.commit import bundle_from_document
from astrid.sdk.timeline.native_occurrence_shared import foreign_shared_command
from tests.editing_conformance import test_commit_runtime_source_copy as source_fixture

pytestmark = pytest.mark.integration
source_copy = source_fixture.source_copy


def prepare(source_copy, *, track='picture', track_kind='visual'):
    from astrid.sdk.timeline.capture import capture_track_create

    service, document, request, commit, duplicate = source_copy
    commit('destination', request('destination', [capture_track_create(
        bundle_from_document(document('destination')), 'parent', {'id': track, 'kind': track_kind})]), 'dest-track')
    call = foreign_shared_command(duplicate['args']['source_ref'], 'one', 'shared-copy', on=12)
    return service, document, request, commit, call


@pytest.mark.parametrize('source_form', ['inline', 'reference'])
def test_historical_shared_exact_physical_retry_preview(source_copy, source_form):
    service, document, request, commit, call = prepare(source_copy)
    if source_form == 'reference':
        from astrid.sdk.timeline.capture import capture_track_create

        project = call['args']['source_ref']['project_id']
        service.create_timeline(project, 'reference-source', idempotency_key='reference-bootstrap')
        commit('reference-source', request('reference-source', [capture_track_create(
            bundle_from_document(document('reference-source')), 'parent',
            {'id': 'picture', 'kind': 'visual'})]), 'reference-track')
        referenced = copy.deepcopy(call)
        referenced['args']['occurrence_id'] = 'reference-one'
        referenced['created'] = ['occurrence:reference-one']
        accepted = commit('reference-source', request('reference-source', [referenced]), 'reference')
        image = document('reference-source')['parent_revision']
        call['args']['source_ref'].update(timeline_id='reference-source', revision_id=image['revision_id'],
            publication_id=accepted['receipt']['receipt_id'], content_digest=image['content_digest'])
        call['args']['source_occurrence_id'] = 'reference-one'
    before, source = document('destination'), document('source')
    historical = copy.deepcopy(source)
    # Advance the project-wide shot head after taking the authentic source pin.
    commit('source', request('source', [{'schema_version': 1, 'op': 'shot.set', 'version': '1',
        'select': 'occurrence:one/shot', 'args': {'values': {'name': 'Newer head'}, 'clear': []}}]), 'advance')
    source = document('source')
    assert source['shot_revisions'][0]['revision_id'] != historical['shot_revisions'][0]['revision_id']
    global_head = service.store.conn.execute(
        'SELECT revision_id FROM shot_revision_heads WHERE shot_id=?', ('shared',)).fetchone()[0]
    assert global_head == source['shot_revisions'][0]['revision_id']
    physical_counts = tuple(service.store.conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                            for table in ('shot_revisions', 'internal_timeline_revisions'))
    body = request('destination', [call])
    snapshot = tuple(service.store.conn.iterdump())
    preview = commit('destination', {**body, 'dry_run': True}, 'preview')
    assert preview['receipt'] is None and preview['data']['would_status'] == 'accepted'
    assert tuple(service.store.conn.iterdump()) == snapshot
    accepted = commit('destination', body, 'shared')
    after = document('destination')
    assert after['shot_revisions'] == historical['shot_revisions']
    assert after['internal_timeline_revisions'] == historical['internal_timeline_revisions']
    row = after['parent_revision']['payload']['occurrences'][0]
    assert row['occurrence_id'] == 'shared-copy' and row['placement']['start_ms'] == 12000
    assert row['shot_id'] == 'shared' and row['shot_revision_id'] == historical['shot_revisions'][0]['revision_id']
    assert accepted['data']['canonical_document'] == after
    assert document('source') == source
    assert service.store.conn.execute(
        'SELECT revision_id FROM shot_revision_heads WHERE shot_id=?', ('shared',)).fetchone()[0] == global_head
    assert tuple(service.store.conn.execute('SELECT COUNT(*) FROM ' + table).fetchone()[0]
                 for table in ('shot_revisions', 'internal_timeline_revisions')) == physical_counts
    assert commit('destination', body, 'shared') == accepted
    record = service.list_timeline_commits(after['project_id'], 'destination', authorize=lambda: None)['items'][-1]['records']['commands'][0]
    assert record['created'] == [{'ref': 'occurrence:shared-copy', 'kind': 'occurrence', 'owner': 'parent'}]
    assert before['parent_revision']['payload']['occurrences'] == []
    ref = call['args']['source_ref']
    source_row = service.get_project_timeline(ref['project_id'], ref['timeline_id'])
    service.archive_timeline(ref['timeline_id'], {'expected_version': source_row['version']}, idempotency_key='archive-source')
    assert commit('destination', body, 'shared') == accepted


def test_historical_shared_undo_redo_retains_physical_source(source_copy):
    _, document, request, commit, call = prepare(source_copy)
    before = document('destination')
    accepted = commit('destination', request('destination', [call]), 'shared')
    after = document('destination')
    for key, expected in [('undo', before), ('redo', after), ('undo-again', before)]:
        call = {'schema_version': 1, 'op': 'timeline.inverse', 'version': '1', 'select': None,
                'args': {'publications': [accepted['receipt']['receipt_id']]}}
        accepted = commit('destination', request('destination', [call]), key)
        assert document('destination') == expected


@pytest.mark.parametrize('damage', ['project', 'timeline', 'same', 'publication', 'digest', 'missing',
                                  'occupied', 'forged', 'pair', 'track', 'track-kind', 'archived'])
def test_historical_shared_refuses_without_writes(source_copy, damage):
    from runtime_protocol.errors import RuntimeErrorBase

    service, document, request, commit, call = prepare(source_copy,
        track='other' if damage == 'track' else 'picture',
        track_kind='audio' if damage == 'track-kind' else 'visual')
    args, ref = call['args'], call['args']['source_ref']
    if damage == 'project':
        ref['project_id'] = 'foreign'
    elif damage == 'timeline':
        ref['timeline_id'] = 'missing'
    elif damage == 'same':
        ref['timeline_id'] = 'destination'
    elif damage == 'publication':
        ref['publication_id'] = 'forged'
    elif damage == 'digest':
        ref['content_digest'] = 'sha256:' + '0' * 64
    elif damage == 'missing':
        args['source_occurrence_id'] = 'missing'
    elif damage == 'occupied':
        commit('destination', request('destination', [call]), 'first')
    elif damage == 'forged':
        args['shot_revision_id'] = 'forged'
    elif damage == 'pair':
        del args['source_occurrence_id']
    elif damage == 'archived':
        row = service.get_project_timeline(ref['project_id'], 'source')
        service.archive_timeline('source', {'expected_version': row['version']}, idempotency_key='archive')
    before = document('destination')
    snapshot = tuple(service.store.conn.iterdump())
    with pytest.raises(RuntimeErrorBase):
        commit('destination', request('destination', [call]), damage)
    assert document('destination') == before
    assert tuple(service.store.conn.iterdump()) == snapshot


def with_media(source_copy):
    from astrid.sdk.timeline.capture import capture_track_create
    from astrid.sdk.timeline.native_occurrence_media_commands import capture_occurrence_add_media
    from tests.editing_conformance.test_occurrence_hold_current_runtime import _import_image

    service, document, request, commit, call = prepare(source_copy)
    bundle = bundle_from_document(document('source'))
    commit('source', request('source', [capture_track_create(bundle, 'occurrence:one',
           {'id': 'picture', 'kind': 'visual'})]), 'child-track')
    media, verified = _import_image(service, call['args']['source_ref']['project_id'], 'historical')
    bundle = bundle_from_document(document('source'))
    add = capture_occurrence_add_media(bundle, 'occurrence:one',
        {'id': 'image', 'clipType': 'hold', 'asset': media['asset'], 'track': 'picture',
         'at': 0, 'hold': 2, 'opaque': [None, False, {'literal': 'shared'}]},
        media, verified_media=[verified])
    accepted = commit('source', request('source', [add]), 'image')
    source = document('source')
    call['args']['source_ref'].update(revision_id=source['parent_revision']['revision_id'],
        publication_id=accepted['receipt']['receipt_id'], content_digest=source['parent_revision']['content_digest'])
    return service, document, request, commit, call, media


@pytest.mark.parametrize('corrupt', [False, True], ids=['full-raw-media', 'changed-media-bytes'])
def test_historical_shared_media_and_opaque_fidelity(source_copy, corrupt):
    from runtime_protocol.errors import RuntimeErrorBase

    service, document, request, commit, call, media = with_media(source_copy)
    source, before = document('source'), document('destination')
    if corrupt:
        service.cas.path_for(media['object_id'].removeprefix('sha256:')).write_bytes(b'corrupt')
        snapshot = tuple(service.store.conn.iterdump())
        with pytest.raises(RuntimeErrorBase):
            commit('destination', request('destination', [call]), 'bad-media')
        assert tuple(service.store.conn.iterdump()) == snapshot
        assert document('destination') == before
    else:
        accepted = commit('destination', request('destination', [call]), 'media-shared')
        after = accepted['data']['canonical_document']
        assert after['shot_revisions'] == source['shot_revisions']
        assert after['internal_timeline_revisions'] == source['internal_timeline_revisions']
        assert after['dependency_manifest']['media'] == source['dependency_manifest']['media']
        assert after['internal_timeline_revisions'][0]['payload']['clips'][0]['opaque'] == [None, False, {'literal': 'shared'}]
        assert document('source') == source


def test_historical_shared_destination_membership_guards(source_copy):
    from runtime_protocol.errors import RuntimeErrorBase

    service, document, request, commit, call = prepare(source_copy)
    stale = request('destination', [call])
    other = copy.deepcopy(call)
    other['args']['occurrence_id'] = 'other'
    other['created'] = ['occurrence:other']
    commit('destination', request('destination', [other]), 'other')
    before = document('destination')
    snapshot = tuple(service.store.conn.iterdump())
    with pytest.raises(RuntimeErrorBase):
        commit('destination', stale, 'stale-destination')
    assert tuple(service.store.conn.iterdump()) == snapshot
    assert document('destination') == before


@pytest.mark.parametrize('damage', ['shot', 'internal', 'ambiguous'])
def test_historical_shared_rejects_inconsistent_port_images(source_copy, monkeypatch, damage):
    from runtime_protocol.errors import RuntimeErrorBase
    from runtime_protocol.timeline_commit_port import RequestCommitPort

    service, document, request, commit, call = prepare(source_copy)
    if damage == 'ambiguous':
        original = RequestCommitPort.source_revision_closure

        def corrupt_source(self, ref):
            value = copy.deepcopy(original(self, ref))
            rows = value['parent_revision']['payload']['occurrences']
            rows.append(copy.deepcopy(rows[0]))
            return value

        monkeypatch.setattr(RequestCommitPort, 'source_revision_closure', corrupt_source)
    else:
        original = RequestCommitPort.shot_closure

        def corrupt_child(self, shot_id, revision_id):
            value = copy.deepcopy(original(self, shot_id, revision_id))
            key = 'shot_revision' if damage == 'shot' else 'internal_timeline_revision'
            value[key]['payload']['opaque'] = 'forged'
            return value

        monkeypatch.setattr(RequestCommitPort, 'shot_closure', corrupt_child)
    before = document('destination')
    snapshot = tuple(service.store.conn.iterdump())
    with pytest.raises(RuntimeErrorBase):
        commit('destination', request('destination', [call]), damage)
    assert tuple(service.store.conn.iterdump()) == snapshot
    assert document('destination') == before


def test_production_session_freezes_foreign_intent_and_retries_lost_ack(source_copy, tmp_path):
    from types import SimpleNamespace

    from astrid.sdk.contracts import DomainResult
    from astrid.sdk.remote import RemoteTimelines
    from astrid.sdk.timeline.commit import bundle_from_document
    from astrid.sdk.timeline.sync import TimelineSession
    from astrid.sdk.workspace_client import WorkspaceClient, WorkspaceClientError

    service, document, request, commit, call = prepare(source_copy)
    project = call['args']['source_ref']['project_id']
    transport = WorkspaceClient.__new__(WorkspaceClient)
    generated = SimpleNamespace()
    transport._generated = generated
    transport._last_handshake = {'realm_id': service.realm['id'], 'actor_id': 'editor'}
    client = SimpleNamespace(_remote=SimpleNamespace(_transport=transport), timelines=RemoteTimelines(transport))
    client.timelines.open_composition = lambda p, t, **_: DomainResult.success({
        'native_inspection': {'project_id': project, 'timeline_id': t},
        'summary': {'head_revision_id': service.get_project_timeline(project, t)['head_revision_id']}})
    client.timelines.open_bundle = lambda p, t, revision_id=None, **_: DomainResult.success({'bundle':
        bundle_from_document(service._timeline_commits()[0].revision_closure(project, t,
            revision_id or service.get_project_timeline(project, t)['head_revision_id']))})
    client.timelines._resolve_timeline = lambda p, t: DomainResult.success({'project_id': project, 'timeline_id': t})
    generated.list_timeline_commits = lambda p, t, cursor=None, limit=200: service.list_timeline_commits(
        project, t, cursor=cursor, limit=limit, authorize=lambda: None)
    bodies = []

    def send(p, t, body, *, idempotency_key):
        bodies.append(copy.deepcopy(body))
        result = commit(t, body, idempotency_key)
        if len(bodies) == 1:
            raise OSError('lost acknowledgement')
        return result

    generated.commit_timeline = send
    options = {'label': 'Historical placement', 'reads': [], 'merge': 'atomic',
               'idempotency_key': 'production'}
    path = tmp_path / 'outbox.json'
    session = TimelineSession.open(project, 'destination', client=client, outbox_path=path)
    with pytest.raises(WorkspaceClientError, match='lost acknowledgement'):
        session.commit(call, **options)
    after = document('destination')
    session = TimelineSession.open(project, 'destination', client=client, outbox_path=path)
    result = session.commit(call, **options)
    assert result['data']['canonical_document'] == after
    assert bodies[0] == bodies[1]
    assert bodies[0]['commands'] == [call]
    assert {'api_version', 'project_id', 'timeline_id', 'base_revision_id', 'base_publication_id',
            'commands', 'label', 'steps', 'merge', 'reads', 'dry_run', 'source'} <= bodies[0].keys()
    assert result['data']['publisher'] == 'editor'
