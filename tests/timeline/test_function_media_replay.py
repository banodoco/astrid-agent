"""Function media identity through real Runtime commits, undo, and restart."""
from __future__ import annotations

import copy
import json

import pytest
from PIL import Image

from astrid.sdk.timeline.commit_session import Timeline
from astrid.sdk.timeline.sync import CommitOutcomeUnknown
from tests.timeline.test_audio_import_runtime import realm, session_for, wav  # noqa: F401


def opened(session):
    return Timeline.open(session.project_id, session.timeline_id, client=session.client,
                         outbox_path=session.outbox.path)


def watch_imports(session, monkeypatch):
    transport = session.client._remote._transport
    original = transport.import_project_media
    calls = []

    def imported(*args, **kwargs):
        calls.append(copy.deepcopy(kwargs))
        return original(*args, **kwargs)

    monkeypatch.setattr(transport, 'import_project_media', imported)
    return calls


def test_function_content_imports_one_commit_and_exact_undo(realm, tmp_path, monkeypatch):  # noqa: F811
    service, writer, candidate = realm
    session = session_for(service, writer, candidate, tmp_path / 'outbox.json')
    tl = opened(session)
    imports = watch_imports(session, monkeypatch)
    picture = tmp_path / 'picture.png'
    Image.new('RGB', (16, 12), 'red').save(picture)
    same_content = tmp_path / 'another-name.png'
    same_content.write_bytes(picture.read_bytes())
    voice = wav(tmp_path / 'voice.wav')
    project, timeline = session.project_id, session.timeline_id
    history = writer.list_timeline_commits(project, timeline)
    before = service._timeline_commits()[0].revision_closure(
        project, timeline, history['head']['revision_id'])

    def place(t):
        t.add(voice, at=0, hold=1, id='voice')
        t.add(picture, at=0, hold=1, id='picture-1')
        t.add(same_content, at=2, hold=1, id='picture-2')

    result = tl.commit(place, idempotency_key='place-media')
    assert len(imports) == len(result['imports']) == 2
    assert all(row['catalog_write'] and row['retained_on_undo'] for row in result['imports'])
    parent = result['data']['canonical_document']['parent_revision']['payload']
    assert [row['id'] for row in parent['clips']] == ['voice', 'picture-1', 'picture-2']
    assert parent['clips'][1]['asset'] == parent['clips'][2]['asset']
    assert parent['clips'][0]['clipType'] == 'audio'
    assert parent['clips'][0]['hold'] == parent['clips'][0]['to'] == 1
    assert len(writer.list_timeline_commits(project, timeline)['items']) == len(history['items']) + 1
    undone = tl.undo(publications=[result['receipt']['receipt_id']])
    assert undone['data']['canonical_document'] == before
    assert len(writer.list_timeline_commits(project, timeline)['items']) == len(history['items']) + 2
    assert len(service.list_generations(project)['items']) == 2


def test_conflict_then_lost_ack_restart_replays_original_media_bytes(
        realm, tmp_path, monkeypatch):  # noqa: F811
    service, writer, candidate = realm
    path = tmp_path / 'outbox.json'
    session = session_for(service, writer, candidate, path)
    tl = opened(session)
    imports = watch_imports(session, monkeypatch)
    source = wav(tmp_path / 'voice.wav')
    original_send = writer.commit_timeline
    sent, evaluations = [], []

    def send(project, timeline, body, **kwargs):
        sent.append((copy.deepcopy(body), kwargs['idempotency_key']))
        if len(sent) == 1:
            peer = session_for(service, writer, candidate, tmp_path / 'peer.json')
            peer.commit({'schema_version': 1, 'op': 'track.create', 'version': '1',
                         'select': 'parent', 'args': {'row': {'id': 'peer', 'kind': 'visual'}}},
                        idempotency_key='peer-edit')
            source.write_bytes(b'changed local bytes after the verified import')
        result = original_send(project, timeline, body, **kwargs)
        if kwargs['idempotency_key'] != 'peer-edit':
            raise ConnectionError('lost function acknowledgement')
        return result

    monkeypatch.setattr(writer, 'commit_timeline', send)

    def place(t):
        evaluations.append(t.head)
        t.add(source, at=0, hold=1, id='voice')

    with pytest.raises(CommitOutcomeUnknown, match='No reply could be verified'):
        tl.commit(place, idempotency_key='logical-place')
    assert len(evaluations) == 2 and len(imports) == 1
    state = json.loads(path.read_bytes())
    frozen = copy.deepcopy(state['flight'])
    accepted_request = sent[-1]
    history = writer.list_timeline_commits(session.project_id, session.timeline_id)
    source.rename(tmp_path / 'removed.wav')
    monkeypatch.setattr(writer, 'commit_timeline', original_send)
    restarted = opened(session_for(service, writer, candidate, path))

    def forbidden(*args, **kwargs):
        pytest.fail('replay evaluated intent or selected/imported media')

    transport = restarted.session.client._remote._transport
    monkeypatch.setattr(transport, 'import_project_media', forbidden)
    monkeypatch.setattr(transport, 'get_project_media_selection', forbidden)
    monkeypatch.setattr(transport, 'get_project_verified_media', forbidden)
    result = restarted.commit(forbidden, idempotency_key='logical-place')
    assert writer.requests[-1] == accepted_request[0] == json.loads(frozen['body'])
    assert result['receipt']['idempotency_key'] == accepted_request[1] == frozen['key']
    assert result['imports'] == frozen['imports']
    assert len(evaluations) == 2 and len(imports) == 1
    assert writer.list_timeline_commits(session.project_id, session.timeline_id) == history

    restarted.undo(publications=[result['receipt']['receipt_id']])
    latest = opened(restarted.session)
    latest_head, latest_bundle = latest.head, copy.deepcopy(latest.bundle)
    replay = latest.commit(forbidden, idempotency_key='logical-place')
    assert replay['receipt'] == result['receipt']
    assert replay['imports'] == result['imports']
    assert latest.head == latest_head and latest.bundle == latest_bundle
    assert len(evaluations) == 2 and len(imports) == 1


def test_conflict_reevaluation_reuses_generated_clip_identity(realm, tmp_path, monkeypatch):  # noqa: F811
    service, writer, candidate = realm
    session = session_for(service, writer, candidate, tmp_path / 'outbox.json')
    tl = opened(session)
    imports = watch_imports(session, monkeypatch)
    source = wav(tmp_path / 'voice.wav')
    original_send = writer.commit_timeline
    sent, generated_ids, generated_assets, evaluations = [], [], [], []
    original_append = Timeline._append

    def capture_append(self, commands, **kwargs):
        generated_ids.extend(command['args']['row']['id'] for command in commands
                             if command.get('op') == 'clip.add-media')
        generated_assets.extend(command['args']['row']['asset'] for command in commands
                                if command.get('op') == 'clip.add-media')
        return original_append(self, commands, **kwargs)

    def send(project, timeline, body, **kwargs):
        if kwargs['idempotency_key'] == 'peer-edit':
            return original_send(project, timeline, body, **kwargs)
        sent.append(copy.deepcopy(body))
        if len(sent) == 1:
            peer = session_for(service, writer, candidate, tmp_path / 'peer.json')
            peer.commit({'schema_version': 1, 'op': 'track.create', 'version': '1',
                         'select': 'parent', 'args': {'row': {'id': 'peer', 'kind': 'visual'}}},
                        idempotency_key='peer-edit')
        result = original_send(project, timeline, body, **kwargs)
        if len(sent) == 2:
            raise ConnectionError('lost generated-media acknowledgement')
        return result

    monkeypatch.setattr(Timeline, '_append', capture_append)
    monkeypatch.setattr(writer, 'commit_timeline', send)

    def place(t):
        evaluations.append(t.head)
        t.add(source, at=0, hold=1)

    with pytest.raises(CommitOutcomeUnknown, match='No reply could be verified'):
        tl.commit(place, idempotency_key='generated-media')
    assert len(sent) == len(evaluations) == 2
    assert len(generated_ids) == 2 and generated_ids[0] == generated_ids[1]
    assert len(generated_assets) == 2 and generated_assets[0] == generated_assets[1]
    for body, identity, asset in zip(sent, generated_ids, generated_assets):
        command = next(command for command in body['commands'] if command['op'] == 'clip.add-media')
        assert command['args']['row']['id'] == identity
        assert command['args']['row']['asset'] == asset

    accepted_request = copy.deepcopy(sent[-1])
    restarted = opened(session_for(service, writer, candidate, tmp_path / 'outbox.json'))

    def forbidden(*args, **kwargs):
        pytest.fail('restart replay evaluated generated media intent')

    monkeypatch.setattr(writer, 'commit_timeline', original_send)
    result = restarted.commit(forbidden, idempotency_key='generated-media')
    assert result['receipt']['receipt_id']
    assert writer.requests[-1] == accepted_request
    assert len(evaluations) == len(generated_ids) == 2
    assert len(imports) == 1


def test_repeated_media_after_undo_keeps_fresh_clip_ids(realm, tmp_path):  # noqa: F811
    from PIL import Image

    _service, _writer, candidate = realm
    session = session_for(_service, _writer, candidate, tmp_path / 'outbox.json')
    tl = opened(session)
    source = tmp_path / 'frame.png'
    Image.new('RGB', (16, 12), 'red').save(source)

    first = tl.commit(lambda t: [t.add(source, at=index * 2, hold=1, id=f'first-{index}')
                                 for index in range(20)], key='first')
    first_asset = first['data']['canonical_document']['parent_revision']['payload']['clips'][0]['asset']
    tl.undo(publications=[first['receipt']['receipt_id']])
    second = tl.commit(lambda t: [t.add(source, at=1000 + index * 2, hold=1, id=f'second-{index}')
                                  for index in range(20)], key='second')
    assert second['receipt']['receipt_id']
    clips = second['data']['canonical_document']['parent_revision']['payload']['clips']
    assert {row['asset'] for row in clips} == {clips[0]['asset']}
    assert clips[0]['asset'] != first_asset


def test_restart_at_conflict_reuses_saved_content_import(realm, tmp_path, monkeypatch):  # noqa: F811
    service, writer, candidate = realm
    path = tmp_path / 'outbox.json'
    session = session_for(service, writer, candidate, path)
    tl = opened(session)
    imports = watch_imports(session, monkeypatch)
    source = wav(tmp_path / 'voice.wav')
    original_send = writer.commit_timeline
    evaluations, sent = [], []

    def send(project, timeline, body, **kwargs):
        sent.append(kwargs['idempotency_key'])
        if len(sent) == 1:
            peer = session_for(service, writer, candidate, tmp_path / 'peer.json')
            peer.commit({'schema_version': 1, 'op': 'track.create', 'version': '1',
                         'select': 'parent', 'args': {'row': {'id': 'peer', 'kind': 'visual'}}},
                        idempotency_key='peer-edit')
        return original_send(project, timeline, body, **kwargs)

    monkeypatch.setattr(writer, 'commit_timeline', send)

    def place(t):
        evaluations.append(t.head)
        if len(evaluations) == 2:
            raise InterruptedError('interrupted reevaluation')
        t.add(source, at=0, hold=1, id='voice')

    with pytest.raises(InterruptedError, match='interrupted reevaluation'):
        tl.commit(place, idempotency_key='logical-place')
    state = json.loads(path.read_bytes())
    assert state['evaluation']['count'] == 1
    cached = next(iter(state['evaluation']['media_cache'].values()))
    assert len(imports) == 1
    source.rename(tmp_path / 'removed-before-restart.wav')
    monkeypatch.setattr(writer, 'commit_timeline', original_send)
    transport = session.client._remote._transport
    monkeypatch.setattr(transport, 'import_project_media',
                        lambda *a, **kw: pytest.fail('reimported saved content after restart'))
    monkeypatch.setattr(transport, 'get_project_media_selection',
                        lambda *a, **kw: pytest.fail('reselected saved content after restart'))
    result = opened(session).commit(place)
    assert result['imports'] == cached['imports']
    assert result['receipt']['idempotency_key'] != 'logical-place'
    assert len(evaluations) == 3
    replay = opened(session).commit(lambda t: pytest.fail('reevaluated acknowledged function'),
                                    idempotency_key='logical-place')
    assert replay['receipt'] == result['receipt']
