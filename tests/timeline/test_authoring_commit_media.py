from __future__ import annotations

import copy
import io
import json
import wave

import pytest
from PIL import Image

from astrid.core.timeline.authoring_bundle import AuthoringBundleError, publish_authoring_candidate
from astrid.sdk.timeline.authoring_commit import authoring_commit_request
from astrid.sdk.timeline.commands import CommandError, execute
from astrid.sdk.timeline.commit import bundle_from_document, create_runtime_commit_service
from astrid.sdk.timeline.native_media_admission import capture_media_admissions


@pytest.fixture
def realm(tmp_path):
    from tests.helpers.runtime import use_runtime

    use_runtime()
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.store import RealmStore

    root = tmp_path / 'realm'
    RealmStore.initialize(root).close()
    service = RuntimeService(root)
    service.register_timeline_commit_provider(create_runtime_commit_service())
    project = service.create_project({'slug': 'admission', 'name': 'Admission'}, idempotency_key='project')['id']
    service.create_timeline(project, 'film', idempotency_key='film')

    class Writer:
        def __init__(self):
            self.requests = []

        def __getattr__(self, name):
            return getattr(service, name)

        def list_timeline_commits(self, project_id, timeline_id, **kwargs):
            return service.list_timeline_commits(project_id, timeline_id, authorize=lambda: None, **kwargs)

        def get_project_media_selection(self, project_id, selection):
            return service.get_project_media_selection(project_id, selection, authorize=lambda: None)

        def commit_timeline(self, project_id, timeline_id, body, **kwargs):
            self.requests.append(copy.deepcopy(body))
            return service.commit_timeline(project_id, timeline_id, body, publisher='author',
                invocation_path=f'/v1/projects/{project_id}/timelines/{timeline_id}/commit',
                authorize=lambda: None, **kwargs)

    writer = Writer()
    page = writer.list_timeline_commits(project, 'film')
    document = service._timeline_commits()[0].revision_closure(project, 'film', page['head']['revision_id'])
    candidate = bundle_from_document(document)
    candidate['base_parent']['publication_id'] = page['head']['publication_id']
    try:
        yield service, writer, candidate
    finally:
        service.close()


def test_production_authoring_writer_commit_retry_and_strict_guards(realm):
    from runtime_protocol.errors import RuntimeErrorBase

    service, writer, candidate = realm
    candidate['parent']['config']['authoring_note'] = 'saved'
    before = writer.list_timeline_commits(candidate['project_id'], 'film')
    result = publish_authoring_candidate(candidate, writer, idempotency_key='authoring')
    retry = publish_authoring_candidate(candidate, writer, idempotency_key='authoring')
    assert result == retry
    assert writer.requests[0] == writer.requests[1]
    assert all(call['op'] == 'doc.patch' for call in writer.requests[0]['commands'])
    assert writer.requests[0]['merge'] == 'atomic'
    assert 'reads' not in writer.requests[0]
    after = writer.list_timeline_commits(candidate['project_id'], 'film')
    assert len(after['items']) == len(before['items']) + 1
    assert result['publication']['canonical_document']['parent_revision']['payload']['config']['authoring_note'] == 'saved'
    assert result['publication']['receipt']['result']['accepted_revision_id'] == after['head']['revision_id']

    changed = copy.deepcopy(candidate)
    changed['parent']['config']['authoring_note'] = 'modified'
    with pytest.raises(RuntimeErrorBase) as modified:
        publish_authoring_candidate(changed, writer, idempotency_key='authoring')
    assert 'idempotency' in str(modified.value).lower()
    with pytest.raises(RuntimeErrorBase) as stale:
        publish_authoring_candidate(changed, writer, idempotency_key='stale')
    assert 'changed' in str(stale.value).lower() or 'conflict' in str(stale.value).lower()
    forged = copy.deepcopy(candidate)
    forged['base_parent_payload']['config']['forged'] = True
    with pytest.raises((AuthoringBundleError, ValueError)):
        publish_authoring_candidate(forged, writer, idempotency_key='forged')
    foreign = copy.deepcopy(candidate)
    foreign['project_id'] = service.create_project({'slug': 'foreign', 'name': 'Foreign'}, idempotency_key='foreign')['id']
    with pytest.raises(RuntimeErrorBase):
        publish_authoring_candidate(foreign, writer, idempotency_key='foreign-candidate')
    assert writer.list_timeline_commits(candidate['project_id'], 'film')['head'] == after['head']


def _timeline_state(service):
    tables = ('timeline_commit_history', 'parent_composition_revisions', 'shot_revisions',
              'internal_timeline_revisions', 'parent_composition_heads', 'timeline_events',
              'command_idempotency')
    return [list(map(tuple, service.store.conn.execute('SELECT * FROM ' + table))) for table in tables]


def _refused_without_writes(service, writer, request, key, match=None):
    from runtime_protocol.errors import RuntimeErrorBase

    before = _timeline_state(service)
    with pytest.raises(RuntimeErrorBase, match=match):
        writer.commit_timeline(request['project_id'], request['timeline_id'], request, idempotency_key=key)
    assert _timeline_state(service) == before


def _publish_media_twice(service, writer, candidate):
    project, timeline = candidate['project_id'], candidate['timeline_id']
    before = writer.list_timeline_commits(project, timeline)
    preview_request = {**authoring_commit_request(candidate, writer), 'dry_run': True}
    preview_state = _timeline_state(service)
    preview = writer.commit_timeline(project, timeline, preview_request, idempotency_key='media-preview')
    assert preview['data']['status'] == 'preview'
    assert preview['data']['would_status'] == 'accepted'
    assert preview['receipt'] is None
    assert _timeline_state(service) == preview_state
    result = publish_authoring_candidate(candidate, writer, idempotency_key='media-commit')
    accepted_state = _timeline_state(service)
    retry = publish_authoring_candidate(candidate, writer, idempotency_key='media-commit')
    assert retry == result
    assert writer.requests[-1] == writer.requests[-2]
    assert _timeline_state(service) == accepted_state
    after = writer.list_timeline_commits(project, timeline)
    assert len(after['items']) == len(before['items']) + 1
    saved = result['publication']
    assert saved['status'] == 'accepted'
    assert saved['new_head'] == after['head']['revision_id']
    assert saved['canonical_document']['parent_revision']['payload']['registry'] == candidate['parent']['registry']
    entry = next(row for row in after['items'] if row['revision_id'] == saved['new_head'])
    assert entry['records']['commands'][-1]['dependencies']
    assert result['summary']['new_head'] == saved['new_head']
    changed = copy.deepcopy(candidate)
    changed['parent']['registry']['assets']['another'] = {'media_id': next(iter(
        candidate['parent']['registry']['assets'].values()))['media_id']}
    changed_request = authoring_commit_request(changed, writer)
    _refused_without_writes(service, writer, changed_request, 'media-commit')
    _refused_without_writes(service, writer, changed_request, 'stale-media-commit')
    return result


def _port(service, request):
    from runtime_protocol.timeline_commit_port import RequestCommitPort
    from runtime_protocol.timeline_commit_store import request_identity

    context = {'request': copy.deepcopy(request), 'realm_id': service.realm['id'], 'publisher': 'author',
        'idempotency_key': 'capture', 'invocation': {'operation': 'commitTimeline',
            'path': f"/v1/projects/{request['project_id']}/timelines/{request['timeline_id']}/commit",
            'body': copy.deepcopy(request)}}
    context['request_hash'] = request_identity(context)
    return RequestCommitPort(service, service._timeline_commits()[0], context, lambda: None)


def test_raw_media_admission_requires_real_declared_project_input(realm):
    from runtime_protocol.timeline_commit import CommitRejected

    service, writer, candidate = realm
    project = candidate['project_id']
    media = service.ingest(project, b'raw-audio', media_type='audio/wav', idempotency_key='raw')['data']['object_id']
    candidate['parent']['registry'] = {'assets': {'audio': {'media_id': media, 'type': 'audio'}}}
    request = authoring_commit_request(candidate, writer)
    assert request['commands'][-1]['op'] == 'media.admit'
    base = bundle_from_document(candidate['source_mapping']['_runtime_document'])
    port = _port(service, request)
    try:
        verified = capture_media_admissions(port, request['commands'])
        plan = execute(base, request['commands'], verified_media=verified, commit_request=request)
        assert plan.bundle['parent']['registry']['assets']['audio']['media_id'] == media
        assert plan.records[-1]['dependencies'][0]['pin'] == {'media_id': media, 'content_digest': media}
        with pytest.raises(CommandError, match='exact Runtime input'):
            execute(base, request['commands'][-1:])
        with pytest.raises(CommandError, match='outside Runtime-verified inputs'):
            execute(base, request['commands'], commit_request=request)
        unreferenced = copy.deepcopy(request['commands'])
        unreferenced[0]['args']['changes'] = []
        with pytest.raises(CommandError):
            execute(base, unreferenced, verified_media=verified)
        forged = copy.deepcopy(request['commands'])
        unused_request = copy.deepcopy(request)
        unused_change = {'op': 'set', 'ref': 'parent', 'path': ['config', 'unrelated'],
                         'before': {'state': 'absent'}, 'value': True}
        unused_request['commands'][0]['args']['changes'] = [unused_change]
        unused_request['commands'][0].pop('created', None)
        unused_request['steps'][0]['patch'] = [unused_change]
        _refused_without_writes(service, writer, unused_request, 'unreferenced', 'not referenced')
        forged[-1]['args']['inputs'][0]['pin']['content_digest'] = 'sha256:' + 'f' * 64
        with pytest.raises((CommandError, CommitRejected)):
            capture_media_admissions(port, forged)
        _refused_without_writes(service, writer, {**request, 'commands': forged}, 'forged-pin')
        undeclared_request = {**request, 'commands': request['commands'][:-1], 'steps': request['steps'][:-1]}
        _refused_without_writes(service, writer, undeclared_request, 'undeclared-pin', 'outside Runtime-verified inputs')
        undeclared = _port(service, undeclared_request)
        try:
            with pytest.raises(CommitRejected, match='declared pin'):
                capture_media_admissions(undeclared, request['commands'])
        finally:
            undeclared.close()
        foreign = service.create_project({'slug': 'foreign-media', 'name': 'Foreign'}, idempotency_key='foreign-media')['id']
        foreign_media = service.ingest(foreign, b'foreign', media_type='image/png', idempotency_key='foreign')['data']['object_id']
        foreign_request = copy.deepcopy(request)
        foreign_request['commands'][-1]['args']['inputs'][0]['pin'] = {
            'media_id': foreign_media, 'content_digest': foreign_media}
        foreign_port = _port(service, foreign_request)
        try:
            with pytest.raises(CommandError, match='managed project object') as foreign_error:
                capture_media_admissions(foreign_port, foreign_request['commands'])
            assert foreign_error.value.kind == 'invalid_media_selection'
            assert foreign_error.value.path == ['commands', '1', 'args', 'inputs', '0', 'pin']
        finally:
            foreign_port.close()
    finally:
        port.close()
    _refused_without_writes(service, writer, foreign_request, 'foreign-media-pin', 'managed project object')
    _publish_media_twice(service, writer, candidate)


def test_mutated_raw_input_is_refused_at_acceptance_without_writes(realm, monkeypatch):
    from runtime_protocol.timeline_commit_port import RequestCommitPort

    service, writer, candidate = realm
    media = service.ingest(candidate['project_id'], b'raw-mutable', media_type='audio/wav',
                           idempotency_key='mutable')['data']['object_id']
    candidate['parent']['registry'] = {'assets': {'audio': {'media_id': media, 'type': 'audio'}}}
    request = authoring_commit_request(candidate, writer)
    capture = RequestCommitPort.raw_media_info
    captures = []

    def mutate_after_capture(port, pin):
        value = capture(port, pin)
        captures.append(copy.deepcopy(value))
        service.store.conn.execute('UPDATE objects SET media_type=? WHERE digest=?',
                                   ('application/octet-stream', media.removeprefix('sha256:')))
        return value

    monkeypatch.setattr(RequestCommitPort, 'raw_media_info', mutate_after_capture)
    _refused_without_writes(service, writer, request, 'mutated-input', 'captured raw media changed')
    assert captures == [{'object_id': media, 'media_type': 'audio/wav', 'size': len(b'raw-mutable')}]


def test_remote_parent_writer_uses_commit_and_exact_immutable_reads(realm):
    from astrid.sdk.remote import RemoteTimelines
    from astrid.sdk.workspace_client import WorkspaceClientError
    from tests.helpers.trusted_seed import seed_trusted_parent
    from tests.timeline.test_authoring_bundle import AUDIO, OLD, _closure

    service, writer, candidate = realm
    project = candidate['project_id']
    parent, shots, internals = _closure(shared=False)
    shots[0]['payload']['text_bindings'] = []
    old = service.ingest(project, b'old-picture', media_type='image/png', idempotency_key='old')['data']['object_id']
    audio = service.ingest(project, b'old-audio', media_type='audio/wav', idempotency_key='audio')['data']['object_id']
    publication = {'project_id': project, 'timeline_id': 'film', 'expected_head': None,
        'parent_revision_id': 'parent-1', 'parent_composition': parent['payload'],
        'shot_revisions': shots, 'internal_timeline_revisions': [{**internals[0], 'timeline_id': 'film'}]}
    publication = json.loads(json.dumps(publication).replace(OLD, old).replace(AUDIO, audio))
    seed_trusted_parent(service, publication, idempotency_key='seed-parent')
    media = service.ingest(project, b'new-picture', media_type='image/png', idempotency_key='new')['data']['object_id']

    class Transport:
        fail = False

        def __getattr__(self, name):
            return getattr(writer, name)

        def list_timelines(self, project_id, **kwargs):
            page = service.list_timelines(project_id, **kwargs)
            return page['items'], page.get('next_cursor')

        def publish_parent_composition(self, *args, **kwargs):
            raise AssertionError('authoring must use the commit door')

        def commit_timeline(self, *args, **kwargs):
            if self.fail:
                raise WorkspaceClientError(409, 'conflict', 'typed commit refusal')
            return writer.commit_timeline(*args, **kwargs)

    transport = Transport()
    remote = RemoteTimelines(transport)
    args = {'occurrence_id': 'occ-1', 'clip_id': 'picture-1', 'source_object_id': media,
            'expected_head': 'parent-1', 'idempotency_key': 'remote-parent'}
    before = writer.list_timeline_commits(project, 'film')
    result = remote.replace_parent_media(project, 'film', **args)
    assert result.ok, result.error
    accepted_state = _timeline_state(service)
    retry = remote.replace_parent_media(project, 'film', **args)
    assert retry == result
    assert _timeline_state(service) == accepted_state
    assert writer.requests[-1] == writer.requests[-2]
    assert len(writer.list_timeline_commits(project, 'film')['items']) == len(before['items']) + 1
    saved = result.data['publication']['publication']
    document = saved['canonical_document']
    child = document['internal_timeline_revisions'][0]['payload']
    clip = next(row for row in child['clips'] if row['id'] == 'picture-1')
    assert clip['media_id'] == media
    assert (clip['at'], clip['hold'], clip['rect'], clip['opaque_clip']) == (
        0, 1, internals[0]['payload']['clips'][0]['rect'], {'literal': 'item-1'})
    assert document['parent_revision']['payload']['occurrences'][0]['transform'] == parent['payload']['occurrences'][0]['transform']
    assert {'media_id': media, 'content_digest': media} in saved['dependency_manifest']['media']
    assert result.receipt.receipt_id == saved['receipt']['receipt_id']
    result.to_json()
    transport.fail = True
    refused = remote.replace_parent_media(project, 'film', **{**args, 'idempotency_key': 'refused-parent'})
    assert not refused.ok and refused.error.message == 'typed commit refusal'
    assert refused.error.code == 'conflict'
    assert _timeline_state(service) == accepted_state


@pytest.mark.parametrize('kind', ['image', 'audio'])
def test_catalog_admission_checks_exact_variant_and_candidate_reference(realm, kind):
    service, writer, candidate = realm
    content = io.BytesIO()
    if kind == 'image':
        Image.new('RGB', (16, 12), 'red').save(content, format='PNG')
    else:
        with wave.open(content, 'wb') as audio:
            audio.setnchannels(1)
            audio.setsampwidth(2)
            audio.setframerate(8000)
            audio.writeframes(b'\0\0' * 8000)
    imported = service.import_media(candidate['project_id'], content.getvalue(),
        media_type=kind + ('/png' if kind == 'image' else '/wav'), original_name='catalog.' + kind,
        width=16 if kind == 'image' else None, height=12 if kind == 'image' else None,
        duration_seconds=1 if kind == 'audio' else None,
        idempotency_key='catalog', actor_id='author')['data']
    candidate['parent']['registry'] = {'assets': {'named': {
        'media_id': imported['asset_id'], 'type': kind, 'generationId': imported['generation_id'],
        'variantId': imported['variant_id']}}}
    request = authoring_commit_request(candidate, writer)
    base = bundle_from_document(candidate['source_mapping']['_runtime_document'])
    port = _port(service, request)
    try:
        verified = capture_media_admissions(port, request['commands'])
        plan = execute(base, request['commands'], verified_media=verified, commit_request=request)
        assert plan.bundle['parent']['registry'] == candidate['parent']['registry']
        assert len(plan.records[-1]['dependencies']) == 2
        forged = copy.deepcopy(request)
        forged['commands'][-1]['args']['inputs'][0]['selection']['catalog_digest'] = 'sha256:' + 'f' * 64
        forged_port = _port(service, forged)
        try:
            with pytest.raises(CommandError, match='catalog digest changed') as stale:
                capture_media_admissions(forged_port, forged['commands'])
            assert stale.value.kind == 'dependency_changed'
            assert stale.value.path == ['commands', '1', 'args', 'inputs', '0', 'selection']
        finally:
            forged_port.close()
        mismatched = copy.deepcopy(request['commands'])
        _refused_without_writes(service, writer, forged, 'stale-catalog', 'catalog digest changed')
        mismatched[-1]['args']['inputs'][0].pop('selection')
        with pytest.raises(CommandError, match='catalog association requires exact Runtime admission'):
            execute(base, mismatched, verified_media=verified)
        receipt = copy.deepcopy(request['commands'])
        _refused_without_writes(service, writer, {**request, 'commands': mismatched},
                                'unselected-catalog', 'requires exact Runtime admission')
        receipt[-1]['args']['importedreceipt'] = imported
        with pytest.raises(CommandError, match='Additional properties'):
            execute(base, receipt, verified_media=verified)
        _refused_without_writes(service, writer, {**request, 'commands': receipt}, 'forged-receipt', 'Additional properties')
    finally:
        port.close()
    _publish_media_twice(service, writer, candidate)
