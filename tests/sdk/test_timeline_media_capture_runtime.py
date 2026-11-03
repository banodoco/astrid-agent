"""Timeline free edits admit a locally imported media selection with their capture."""
import copy
import hashlib
import json

import pytest

pytest_plugins = ("tests.sdk.test_timeline_normal_publish",)


def test_free_edit_import_capture(restructure_runtime, tmp_path, monkeypatch):
    from PIL import Image
    from astrid.sdk.timeline import Timeline

    env = restructure_runtime
    transport = env.client._remote._transport
    transport.import_project_media = lambda project, payload, **kw: env.service.import_media(
        project, payload, media_type=kw.pop('media_type'), original_name=kw.pop('filename'),
        actor_id='editor', **kw)
    transport.get_project_media_selection = lambda project, selection: env.service.get_project_media_selection(
        project, selection, authorize=lambda: None)
    transport.get_project_verified_media = lambda project, selection: env.service.get_project_verified_media(
        project, selection, authorize=lambda: None)
    path = tmp_path / 'marker.png'
    Image.new('RGB', (16, 16), 'blue').save(path)
    digest = 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()
    timeline = Timeline.open(env.project, 't', client=env.client, outbox_path=tmp_path / 'outbox.json')
    timeline.bundle['parent']['config']['app'] = {'astrid': {'slots': {}}}
    timeline.clip('cross').swap_asset(str(path))

    original = env.generated.commit_timeline
    failures = []

    def verify_guards(project, timeline_id, body, *, idempotency_key):
        from runtime_protocol.errors import RuntimeErrorBase

        before = copy.deepcopy(env.document())
        forged = copy.deepcopy(body)
        forged['steps'][0]['patch'][0]['value'] = 'forged'
        with pytest.raises(RuntimeErrorBase) as mismatch:
            original(project, timeline_id, forged, idempotency_key='forged-capture')
        failures.append(str(mismatch.value))
        forged = copy.deepcopy(body)
        forged['commands'][-1]['args']['inputs'][0]['pin']['content_digest'] = 'sha256:' + '0' * 64
        with pytest.raises(RuntimeErrorBase) as invalid_media:
            original(project, timeline_id, forged, idempotency_key='forged-media')
        failures.append(str(invalid_media.value))
        assert env.document() == before
        return original(project, timeline_id, body, idempotency_key=idempotency_key)

    monkeypatch.setattr(env.generated, 'commit_timeline', verify_guards)
    result = timeline.commit('Imported edit', key='imported-edit')
    assert 'step patch' in failures[0]
    assert failures[1]
    assert result['status'] in {'accepted', 'merged', 'landed'}
    body = env.calls[-1][0]
    assert [c['op'] for c in body['commands']] == ['doc.patch', 'media.admit']
    assert [step['command_ordinals'] for step in body['steps']] == [[0], [1]]
    assert body['steps'][0]['patch'] == body['commands'][0]['args']['changes']
    admission = body['commands'][1]['args']['inputs'][0]
    assert admission['pin'] == {'media_id': digest, 'content_digest': digest}
    assert admission['selection']['object_id'] == digest
    document = result['data']['canonical_document']
    internal = document['internal_timeline_revisions'][0]['payload']
    asset_key = next(row['asset'] for row in internal['clips'] if row['id'] == 'cross')
    assert internal['registry']['assets'][asset_key]['media_id'] == digest
    assert {'media_id': digest, 'content_digest': digest} in document['dependency_manifest']['media']


def test_detached_cli_commit_after_file_import(restructure_runtime, tmp_path, monkeypatch, capsys):
    from PIL import Image
    from astrid.packs.timeline import cli
    from astrid.sdk.contracts import DomainResult
    from astrid.sdk.remote import RemoteMedia
    from astrid.sdk.timeline import Timeline
    from astrid.sdk.timeline.checkout import Checkout

    env = restructure_runtime
    transport = env.client._remote._transport
    transport.import_project_media = lambda project, payload, **kw: env.service.import_media(
        project, payload, media_type=kw.pop('media_type'), original_name=kw.pop('filename'),
        actor_id='editor', **kw)
    transport.get_project_media_selection = lambda project, selection: env.service.get_project_media_selection(
        project, selection, authorize=lambda: None)
    transport.get_project_verified_media = lambda project, selection: env.service.get_project_verified_media(
        project, selection, authorize=lambda: None)
    media = RemoteMedia(transport)

    def ingest(operation, project, stream, **kw):
        assert operation == 'ingest_project_object_stream'
        return DomainResult.success(env.service.ingest(
            project, stream.read(), media_type=kw['media_type'], original_name=kw['filename'],
            idempotency_key=kw['idempotency_key'])['data'])

    monkeypatch.setattr(media, '_typed', ingest)
    monkeypatch.setattr('astrid.core.execution.thumbnail_backfill.ensure_imported_source_thumbnail',
                        lambda *args, **kwargs: None)
    path = tmp_path / 'output.png'
    Image.new('RGB', (16, 16), 'red').save(path)
    digest = 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()
    imported = media.import_file(project=env.project, path=path, idempotency_key='file-import')
    assert imported.ok and imported.data['object_id'] == digest

    timeline = Timeline.open(env.project, 't', client=env.client, outbox_path=tmp_path / 'edit-outbox.json')
    timeline.clip('cross').swap_asset(str(path))  # Prepare the managed catalog association.
    timeline.bundle['parent']['config'].setdefault('app', {}).setdefault('astrid', {})['slots'] = {}
    candidate = copy.deepcopy(timeline.bundle)
    candidate['base_parent']['publication_id'] = timeline.head['publication_id']
    candidate_path = tmp_path / 'candidate.json'
    Checkout(candidate).save(candidate_path, quiet=True)
    parsed = cli.build_parser(env.client).parse_args([
        'commit', 't', '--project', env.project, '--file', str(candidate_path),
        '--message', 'Agent marker', '--idempotency-key', 'cli-marker', '--json'])
    assert parsed.handler(parsed) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['ok'] and result['receipt']
    body = env.calls[-1][0]
    assert [call['op'] for call in body['commands']] == ['doc.patch', 'media.admit']
    assert [step['command_ordinals'] for step in body['steps']] == [[0], [1]]
    assert body['steps'][0]['patch'] == body['commands'][0]['args']['changes']
    assert body['steps'][1]['patch'] == []
    assert body['commands'][1]['args']['inputs'][0]['selection']['object_id'] == digest
    document = result['data']['canonical_document']
    internal = document['internal_timeline_revisions'][0]['payload']
    asset_key = next(row['asset'] for row in internal['clips'] if row['id'] == 'cross')
    assert internal['registry']['assets'][asset_key]['media_id'] == digest
    assert {'media_id': digest, 'content_digest': digest} in document['dependency_manifest']['media']


def test_http_file_import_and_detached_commit(real_runtime, tmp_path, capsys):
    from PIL import Image
    from astrid.packs.timeline import cli
    from astrid.sdk.timeline import Timeline
    from astrid.sdk.timeline.checkout import Checkout
    from astrid.sdk.timeline.native_occurrence_create import capture_occurrence_create
    from tests.editing_conformance.test_commit_runtime_m1 import open_client

    daemon = real_runtime(authoring_profile='astrid')
    project = daemon.service.create_project({'slug': 'capture-case', 'name': 'Capture case'})['id']
    client = open_client(daemon)
    assert client.timelines.create_empty(project=project, timeline_id='film', name='Film',
                                         idempotency_key='create-film').ok
    timeline = Timeline.open(project, 'film', client=client, outbox_path=tmp_path / 'edit-outbox.json')
    command = capture_occurrence_create(timeline._execution_bundle(timeline.bundle), 'occ-one', 'shot-one',
                                        name='Shot', track='V1', on=0, duration=3)
    timeline.commit([command], label='Seed shot', key='seed-shot')
    internal = timeline.bundle['shots']['shot-one']['internal_timeline']
    internal['tracks'].append({'id': 'sprite', 'kind': 'visual'})
    internal['clips'].append({'id': 'marker', 'clipType': 'am-type', 'track': 'sprite',
                              'at': 0, 'hold': 1, 'params': {'text': 'placeholder'}})
    timeline.commit('Seed clip', key='seed-clip')

    path = tmp_path / 'output.png'
    Image.new('RGB', (16, 16), 'green').save(path)
    digest = 'sha256:' + hashlib.sha256(path.read_bytes()).hexdigest()
    imported = client.media.import_file(project=project, path=path, idempotency_key='file-import')
    assert imported.ok and imported.data['object_id'] == digest
    timeline.clip('marker').swap_asset(str(path))
    candidate = copy.deepcopy(timeline.bundle)
    candidate['base_parent']['publication_id'] = timeline.head['publication_id']
    candidate_path = tmp_path / 'candidate.json'
    Checkout(candidate).save(candidate_path, quiet=True)
    sent = []
    generated = client._remote._transport._generated
    original = generated.commit_timeline

    def record(*args, **kwargs):
        sent.append(copy.deepcopy(args[2]))
        return original(*args, **kwargs)

    generated.commit_timeline = record
    parsed = cli.build_parser(client).parse_args([
        'commit', 'film', '--project', project, '--file', str(candidate_path),
        '--message', 'Agent marker', '--idempotency-key', 'cli-marker', '--json'])
    assert parsed.handler(parsed) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['ok'] and result['receipt']
    assert [c['op'] for c in sent[0]['commands']] == ['doc.patch', 'media.admit']
    assert [step['command_ordinals'] for step in sent[0]['steps']] == [[0], [1]]
    assert sent[0]['steps'][0]['patch'] == sent[0]['commands'][0]['args']['changes']
    assert sent[0]['steps'][1]['patch'] == []
    admission = sent[0]['commands'][1]['args']['inputs'][0]
    assert admission['pin'] == {'media_id': digest, 'content_digest': digest}
    assert admission['selection']['object_id'] == digest
    document = result['data']['canonical_document']
    internal = document['internal_timeline_revisions'][0]['payload']
    asset = next(row['asset'] for row in internal['clips'] if row['id'] == 'marker')
    assert internal['registry']['assets'][asset]['media_id'] == digest
    assert {'media_id': digest, 'content_digest': digest} in document['dependency_manifest']['media']
