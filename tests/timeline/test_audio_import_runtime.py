"""Native WAV import, publication, and inverse through the real Runtime store."""
from __future__ import annotations

import hashlib
import wave
from types import SimpleNamespace

import pytest

from astrid.sdk.contracts import DomainResult
from astrid.sdk.timeline._command_state import CommandError
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.commit import bundle_from_document
from astrid.sdk.timeline.sync import CommitOutcomeUnknown, TimelineSession
from tests.timeline.test_authoring_commit_media import realm  # noqa: F401


def session_for(service, writer, candidate, path):
    project, timeline = candidate['project_id'], candidate['timeline_id']

    class Transport:
        _last_handshake = {'realm_id': service.realm['id'], 'actor_id': 'author'}

        def list_generations(self, project_id, *, cursor=None, limit=50):
            page = service.list_generations(project_id, cursor=cursor, limit=limit)
            return page['items'], page.get('next_cursor')

        def list_variants(self, generation_id, *, cursor=None, limit=50):
            page = service.list_variants(generation_id, cursor=cursor, limit=limit)
            return page['items'], page.get('next_cursor')

        def import_project_media(self, project_id, payload, *, filename, **kwargs):
            return service.import_media(project_id, payload, original_name=filename,
                                        actor_id='author', **kwargs)

        def get_project_media_selection(self, project_id, selectors):
            return writer.get_project_media_selection(project_id, selectors)

        def get_project_verified_media(self, project_id, pin):
            return service.get_project_verified_media(project_id, pin, authorize=lambda: None)

    def opened(project_id, timeline_id, *, revision_id, publication_id):
        document = service._timeline_commits()[0].revision_closure(project_id, timeline_id, revision_id)
        bundle = bundle_from_document(document)
        bundle['base_parent']['publication_id'] = publication_id
        return DomainResult.success({'bundle': bundle})

    def committed(project_id, timeline_id, body, **kwargs):
        result = writer.commit_timeline(project_id, timeline_id, body, **kwargs)
        return SimpleNamespace(ok=True, as_dict=lambda: result)

    transport = Transport()
    timelines = SimpleNamespace(
        _client=transport,
        open_composition=lambda *args: DomainResult.success({
            'native_inspection': {'project_id': project, 'timeline_id': timeline},
            'summary': {'head_revision_id': writer.list_timeline_commits(project, timeline)['head']['revision_id']},
        }),
        history=lambda *args, **kwargs: DomainResult.success(writer.list_timeline_commits(*args, **kwargs)),
        open_bundle=opened,
        commit=committed,
    )
    client = SimpleNamespace(_remote=SimpleNamespace(_transport=transport), timelines=timelines)
    return TimelineSession.open(project, timeline, client=client, outbox_path=path)


def wav(path):
    with wave.open(str(path), 'wb') as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8000)
        output.writeframes(b'\0\0' * 16000)
    return path


@pytest.mark.parametrize('track', [None, 'A2'])
def test_wav_import_one_receipt_exact_undo_retains_catalog(realm, tmp_path, track):  # noqa: F811
    service, writer, candidate = realm
    session = session_for(service, writer, candidate, tmp_path / 'outbox.json')
    project, timeline = candidate['project_id'], candidate['timeline_id']
    existing_tracks = {row['id'] for row in candidate['parent']['config']['tracks']}
    session.commit_many([
        {'schema_version': 1, 'op': 'track.create', 'version': '1', 'select': 'parent',
         'args': {'row': {'id': identity, 'kind': kind}}}
        for identity, kind in [('V1', 'visual'), ('A1', 'audio')]
        if identity not in existing_tracks
    ] + [
        {'schema_version': 1, 'op': 'clip.add', 'version': '1', 'select': 'parent',
         'args': {'row': {'id': identity, 'track': 'V1', 'clipType': 'text',
                          'at': index * 3, 'hold': 1, 'params': {'text': identity}}}}
        for index, identity in enumerate(['opening', 'closing'])
    ], idempotency_key='setup')
    history = writer.list_timeline_commits(project, timeline)
    before = service._timeline_commits()[0].revision_closure(project, timeline, history['head']['revision_id'])
    source = wav(tmp_path / 'voice.wav')
    digest = 'sha256:' + hashlib.sha256(source.read_bytes()).hexdigest()
    added = session.add_media(source, on=0.5, duration=1.5, track=track,
                              clip_id='voice', idempotency_key='add-voice')
    assert added['receipt']['receipt_id']
    assert len(writer.list_timeline_commits(project, timeline)['items']) == len(history['items']) + 1
    parent = added['data']['canonical_document']['parent_revision']['payload']
    clip = next(row for row in parent['clips'] if row['id'] == 'voice')
    assert {key: clip[key] for key in ('track', 'from', 'to', 'hold', 'speed')} == {
        'track': track or 'A1', 'from': 0, 'to': 1.5, 'hold': 1.5, 'speed': 1}
    binding = parent['registry']['assets'][clip['asset']]
    assert binding['type'] == 'audio' and binding['duration'] == 2
    assert binding['media_id'] == digest
    assert added['imports'][0]['object_id'] == digest
    assert added['imports'][0]['catalog_write'] is True
    assert added['imports'][0]['retained_on_undo'] is True
    pin = session.client._remote._transport.get_project_media_selection(project, {
        'asset': clip['asset'], 'generation_id': binding['generationId'],
        'variant_id': binding['variantId'],
    })
    if 'data' in pin:
        pin = pin['data']
    source.rename(tmp_path / 'removed.wav')
    assert session.add_media(source, on=0.5, duration=1.5, track=track,
                             clip_id='voice', idempotency_key='add-voice')['receipt'] == added['receipt']
    undone = session.undo([added['receipt']['receipt_id']], idempotency_key='undo-voice')
    assert undone['data']['canonical_document'] == before
    assert len(writer.list_timeline_commits(project, timeline)['items']) == len(history['items']) + 2
    verified = session.client._remote._transport.get_project_verified_media(project, pin)
    assert verified['selection']['object_id'] == digest
    assert verified['duration_seconds'] == 2


def test_wav_lost_ack_retry_keeps_original_import_and_disclosure(realm, tmp_path, monkeypatch):  # noqa: F811
    service, writer, candidate = realm
    path = tmp_path / 'outbox.json'
    session = session_for(service, writer, candidate, path)
    source = wav(tmp_path / 'voice.wav')
    send = writer.commit_timeline

    def lose_ack(*args, **kwargs):
        send(*args, **kwargs)
        raise ConnectionError('lost audio acknowledgement after acceptance')

    monkeypatch.setattr(writer, 'commit_timeline', lose_ack)
    with pytest.raises(CommitOutcomeUnknown, match='No reply could be verified'):
        session.add_media(source, on=0, duration=1, clip_id='voice', idempotency_key='lost-voice')
    flight = session.outbox.read()['flight']
    history = writer.list_timeline_commits(candidate['project_id'], candidate['timeline_id'])
    source.rename(tmp_path / 'removed.wav')
    monkeypatch.setattr(writer, 'commit_timeline', send)
    session = session_for(service, writer, candidate, path)
    retried = session.add_media(source, on=0, duration=1, clip_id='voice', idempotency_key='lost-voice')
    assert retried['imports'] == flight['imports']
    assert retried['imports'][0]['catalog_write'] is True
    assert writer.requests[-1] == writer.requests[-2]
    assert writer.list_timeline_commits(candidate['project_id'], candidate['timeline_id']) == history
    assert session.add_media(source, on=0, duration=1, clip_id='voice',
                             idempotency_key='lost-voice')['imports'] == retried['imports']


def test_wav_reimport_reuses_catalog_and_rejects_excess_source_window(realm, tmp_path):  # noqa: F811
    service, writer, candidate = realm
    session = session_for(service, writer, candidate, tmp_path / 'outbox.json')
    source = wav(tmp_path / 'voice.wav')
    first = session.add_media(source, on=0, duration=1, clip_id='voice-1', idempotency_key='voice-1')
    second = session.add_media(source, on=2, duration=1, clip_id='voice-2', idempotency_key='voice-2')
    assert first['imports'] == second['imports']
    parent = second['data']['canonical_document']['parent_revision']['payload']
    assert [clip['id'] for clip in parent['clips']] == ['voice-1', 'voice-2']
    assert len(parent['registry']['assets']) == 1
    history = writer.list_timeline_commits(candidate['project_id'], candidate['timeline_id'])
    with pytest.raises(CommandError, match='exceeds media'):
        session.add_media(source, on=4, duration=3, idempotency_key='too-long')
    assert writer.list_timeline_commits(candidate['project_id'], candidate['timeline_id']) == history


def test_corrupt_wav_is_refused_before_catalog_or_timeline_write(realm, tmp_path):  # noqa: F811
    service, writer, candidate = realm
    session = session_for(service, writer, candidate, tmp_path / 'outbox.json')
    source = tmp_path / 'broken.wav'
    source.write_bytes(b'not a wave file')
    history = writer.list_timeline_commits(candidate['project_id'], candidate['timeline_id'])
    with pytest.raises(TimelineEditError, match='invalid local media.*broken.wav'):
        session.add_media(source, on=0, duration=1)
    assert writer.list_timeline_commits(candidate['project_id'], candidate['timeline_id']) == history
    assert not writer.requests
