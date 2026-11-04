"""Redo must reacquire foreign authority even while a sibling retains the bytes."""
from __future__ import annotations

import copy

import pytest

from tests.editing_conformance import test_commit_runtime_source_copy as source_fixture
from tests.editing_conformance.test_historical_shared_inverse_owners import inverse
from tests.editing_conformance.test_occurrence_shared_historical import prepare, with_media

pytestmark = pytest.mark.integration
source_copy = source_fixture.source_copy


def retained_redo(source_copy, *, media=False, prepared=None):
    if prepared is None:
        prepared = with_media(source_copy) if media else prepare(source_copy)
    service, document, request, commit, call, *extra = prepared
    commit('destination', request('destination', [call]), 'retained-owner')
    other = copy.deepcopy(call)
    other['args']['occurrence_id'] = 'redo-owner'
    other['created'] = ['occurrence:redo-owner']
    accepted = commit('destination', request('destination', [other]), 'redo-owner')
    after = document('destination')
    undone = commit('destination', request('destination', [inverse(accepted)]), 'undo-redo-owner')
    return service, document, request, commit, call, after, undone, extra


@pytest.mark.parametrize('damage', ['none', 'media-positive', 'new-head', 'archived', 'media'])
def test_retained_shared_redo_revalidates_source(source_copy, damage):
    from runtime_protocol.errors import RuntimeErrorBase

    service, document, request, commit, call, after, undone, extra = retained_redo(
        source_copy, media=damage in ('media', 'media-positive'))
    before = document('destination')
    body = request('destination', [inverse(undone)])
    if damage == 'archived':
        project = call['args']['source_ref']['project_id']
        row = service.get_project_timeline(project, 'source')
        service.archive_timeline('source', {'expected_version': row['version']}, idempotency_key='archive')
    elif damage == 'media':
        service.cas.path_for(extra[0]['object_id'].removeprefix('sha256:')).write_bytes(b'changed')
    elif damage == 'new-head':
        commit('source', request('source', [{'schema_version': 1, 'op': 'shot.set', 'version': '1',
            'select': 'occurrence:one/shot', 'args': {'values': {'name': 'New head'}, 'clear': []}}]), 'new-head')
        assert document('source')['shot_revisions'] != after['shot_revisions']
    snapshot = tuple(service.store.conn.iterdump())
    if damage in ('none', 'media-positive', 'new-head'):
        preview = commit('destination', {**body, 'dry_run': True}, 'preview-redo')
        assert preview['receipt'] is None and preview['data']['would_status'] == 'accepted'
        assert tuple(service.store.conn.iterdump()) == snapshot
        result = commit('destination', body, 'redo')
        assert result['data']['canonical_document'] == after
        assert document('destination') == after
        assert commit('destination', body, 'redo') == result
        undo_again = commit('destination', request('destination', [inverse(result)]), 'undo-again')
        assert document('destination') == before
        commit('destination', request('destination', [inverse(undo_again)]), 'redo-again')
        assert document('destination') == after
    else:
        with pytest.raises(RuntimeErrorBase):
            commit('destination', body, 'redo')
        if damage != 'media':
            assert document('destination') == before
        assert tuple(service.store.conn.iterdump()) == snapshot


def test_shared_inverse_loses_source_access_after_forward(source_copy):
    from runtime_protocol.errors import RuntimeErrorBase

    service, document, request, commit, call = prepare(source_copy)
    commit('destination', request('destination', [call]), 'first-owner')
    other = copy.deepcopy(call)
    other['args']['occurrence_id'] = 'other'
    other['created'] = ['occurrence:other']
    accepted = commit('destination', request('destination', [other]), 'other')
    project = call['args']['source_ref']['project_id']
    row = service.get_project_timeline(project, 'source')
    service.archive_timeline('source', {'expected_version': row['version']}, idempotency_key='archive')
    before = document('destination')
    snapshot = tuple(service.store.conn.iterdump())
    with pytest.raises(RuntimeErrorBase, match='timeline not found'):
        commit('destination', request('destination', [inverse(accepted)]), 'undo')
    assert document('destination') == before
    assert tuple(service.store.conn.iterdump()) == snapshot


def with_document(source_copy):
    from astrid.sdk.media_transcript import build_transcript, transcript_document_id
    from astrid.sdk.timeline.capture import capture_track_create
    from astrid.sdk.timeline.commit import _digest, bundle_from_document
    from astrid.sdk.timeline.native_parent_media_commands import capture_clip_add_media

    service, document, request, commit, call, media = with_media(source_copy)
    project = call['args']['source_ref']['project_id']
    commit('source', request('source', [capture_track_create(bundle_from_document(document('source')),
        'parent', {'id': 'overlay', 'kind': 'visual'})]), 'overlay-track')
    verified = service.get_project_verified_media(project, media, authorize=lambda: None)
    added = capture_clip_add_media(bundle_from_document(document('source')),
        {'id': 'target', 'clipType': 'hold', 'asset': media['asset'],
         'track': 'overlay', 'at': 0, 'hold': 0.5}, media, verified_media=[verified])
    commit('source', request('source', [added]), 'target')
    content = build_transcript(media['object_id'], 'line', 'retained', [[0.25, 0.5, 'retained']])
    identity = transcript_document_id(project, media['object_id'], content['source_id'])
    service.create_document(project, {'document_id': identity, 'kind': 'media.transcript',
        'content': content}, idempotency_key='transcript')
    pin = {'document_id': identity, 'version': 1, 'content_digest': _digest(content)}
    anchor = {'schema_version': 1, 'op': 'clip.anchor', 'version': '1', 'select': 'parent/clip:target',
        'args': {'field': 'on', 'declaration': 'retained', 'choice': {
            'source': 'occurrence:one/clip:image', 'document': pin,
            'word_ids': [content['segments'][0]['words'][0]['id']]}}}
    # Capture a new physical shot with its already accepted document dependency.
    # The source, forward placements and inverses all use the registered provider.
    changed = {'schema_version': 1, 'op': 'shot.set', 'version': '1', 'select': 'occurrence:one/shot',
               'args': {'values': {'name': 'Retained document'}, 'clear': []}}
    commit('source', request('source', [anchor]), 'document-anchor')
    accepted = commit('source', request('source', [changed]), 'document-source')
    image = document('source')['parent_revision']
    call['args']['source_ref'].update(revision_id=image['revision_id'],
        publication_id=accepted['receipt']['receipt_id'], content_digest=image['content_digest'])
    return service, document, request, commit, call


@pytest.mark.parametrize('damage', ['none', 'new-document-head', 'changed-capture'])
def test_shared_redo_retains_exact_document_custody(source_copy, monkeypatch, damage):
    from runtime_protocol.errors import RuntimeErrorBase
    from runtime_protocol.timeline_commit_port import RequestCommitPort

    prepared = with_document(source_copy)
    service, document, request, commit, call, after, undone, _ = retained_redo(source_copy, prepared=prepared)
    project = call['args']['source_ref']['project_id']
    pins = after['dependency_manifest']['documents']
    assert len(pins) == 1 and pins[0]['version'] == 1
    if damage == 'new-document-head':
        current = service.get_document(project, pins[0]['document_id'])
        content = copy.deepcopy(current['content'])
        content['segments'][0]['text'] = 'new document head'
        service.update_document(project, pins[0]['document_id'], {'expected_version': 1,
            'content': content}, idempotency_key='update')
    capture = RequestCommitPort.transcript_source
    seen = []

    def config(port, pin):
        result = copy.deepcopy(capture(port, pin))
        seen.append(copy.deepcopy(result))
        if damage == 'changed-capture':
            result['document']['content']['segments'][0]['text'] = 'changed'
        return result

    monkeypatch.setattr(RequestCommitPort, 'transcript_source', config)
    before = document('destination')
    snapshot = tuple(service.store.conn.iterdump())
    body = request('destination', [inverse(undone)])
    if damage == 'changed-capture':
        with pytest.raises(RuntimeErrorBase, match='exact scoped pin'):
            commit('destination', body, 'redo-document')
        assert document('destination') == before
        assert tuple(service.store.conn.iterdump()) == snapshot
    else:
        result = commit('destination', body, 'redo-document')
        assert result['data']['canonical_document'] == after
        assert document('destination')['dependency_manifest']['documents'] == pins
    assert seen and all(captured['pin'] == pins[0] and captured['document']['version'] == 1
                        for captured in seen)


def test_production_session_shared_redo_uses_runtime_authority(source_copy, tmp_path):
    from types import SimpleNamespace

    from astrid.sdk.contracts import DomainResult
    from astrid.sdk.remote import RemoteTimelines
    from astrid.sdk.timeline.commit import bundle_from_document
    from astrid.sdk.timeline.sync import TimelineSession
    from astrid.sdk.workspace_client import WorkspaceClient

    service, document, _, commit, call = prepare(source_copy)
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
    generated.commit_timeline = lambda p, t, body, idempotency_key: commit(t, body, idempotency_key)
    path = tmp_path / 'outbox.json'
    session = TimelineSession.open(project, 'destination', client=client, outbox_path=path)
    session.commit(call, idempotency_key='first')
    other = copy.deepcopy(call)
    other['args']['occurrence_id'] = 'redo-owner'
    other['created'] = ['occurrence:redo-owner']
    accepted = session.commit(other, idempotency_key='second')
    after = document('destination')
    undone = session.undo([accepted['receipt']['receipt_id']], idempotency_key='undo')
    restarted = TimelineSession.open(project, 'destination', client=client, outbox_path=path)
    redone = restarted.undo([undone['receipt']['receipt_id']], idempotency_key='redo')
    assert redone['data']['canonical_document'] == after
    assert document('destination') == after


@pytest.mark.parametrize('damage', ['denied', 'cross-project', 'stale-pin', 'wrong-owner',
                                  'equal-bytes-retarget', 'forged-history'])
def test_shared_redo_refuses_lost_or_inconsistent_authority(source_copy, monkeypatch, damage):
    from runtime_protocol.errors import ForbiddenError, RuntimeErrorBase
    from runtime_protocol.timeline_commit_port import RequestCommitPort

    service, document, request, commit, call, _, undone, _ = retained_redo(source_copy)
    before = document('destination')
    snapshot = tuple(service.store.conn.iterdump())
    body = request('destination', [inverse(undone)])
    seen = []
    if damage == 'forged-history':
        body['commands'][0]['args']['publications'] = ['forged-publication']
    elif damage == 'denied':
        authorize = service._timeline_authorize

        def check(project, timeline, callback, **options):
            if timeline == 'source':
                seen.append(timeline)
                raise ForbiddenError('source access revoked')
            return authorize(project, timeline, callback, **options)

        monkeypatch.setattr(service, '_timeline_authorize', check)
    elif damage == 'equal-bytes-retarget':
        capture = RequestCommitPort.shot_closure

        def retarget(port, shot_id, revision_id):
            result = copy.deepcopy(capture(port, shot_id, revision_id))
            seen.append(copy.deepcopy(result))
            result['shot_revision']['revision_id'] = 'equal-bytes-different-revision'
            return result

        monkeypatch.setattr(RequestCommitPort, 'shot_closure', retarget)
    else:
        capture = RequestCommitPort.source_revision_closure

        def changed(port, ref):
            seen.append(copy.deepcopy(ref))
            assert ref == call['args']['source_ref']
            if damage in ('cross-project', 'stale-pin'):
                ref = copy.deepcopy(ref)
                ref['project_id' if damage == 'cross-project' else 'revision_id'] = 'outside'
            result = copy.deepcopy(capture(port, ref))
            if damage == 'wrong-owner':
                result['parent_revision']['payload']['occurrences'][0]['shot_id'] = 'outside'
            return result

        monkeypatch.setattr(RequestCommitPort, 'source_revision_closure', changed)
    with pytest.raises(RuntimeErrorBase):
        commit('destination', body, 'refused-redo')
    assert damage == 'forged-history' or seen
    assert document('destination') == before
    assert tuple(service.store.conn.iterdump()) == snapshot


def test_shared_redo_rechecks_source_archive_at_acceptance(source_copy, monkeypatch):
    from runtime_protocol.errors import RuntimeErrorBase
    from runtime_protocol.timeline_commit_port import RequestCommitPort

    service, document, request, commit, call, _, undone, _ = retained_redo(source_copy)
    project = call['args']['source_ref']['project_id']
    before = document('destination')
    capture = RequestCommitPort.source_revision_closure
    snapshots = []

    def archive_after_capture(port, ref):
        result = capture(port, ref)
        row = service.get_project_timeline(project, 'source')
        service.archive_timeline('source', {'expected_version': row['version']}, idempotency_key='archive-captured')
        snapshots.append(tuple(service.store.conn.iterdump()))
        return result

    monkeypatch.setattr(RequestCommitPort, 'source_revision_closure', archive_after_capture)
    with pytest.raises(RuntimeErrorBase, match='timeline not found'):
        commit('destination', request('destination', [inverse(undone)]), 'redo-archived-capture')
    assert snapshots and tuple(service.store.conn.iterdump()) == snapshots[-1]
    assert document('destination') == before
