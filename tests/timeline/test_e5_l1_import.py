"""Focused E5 evidence for WAV import identity and exact retry behavior."""
from __future__ import annotations

import hashlib

import pytest

from astrid.sdk.workspace_client import WorkspaceClientError
from tests.timeline.test_audio_import_runtime import realm, session_for, wav  # noqa: F401


def test_wav_import_uses_content_identity_verified_duration_and_exact_retry(realm, tmp_path):  # noqa: F811
    service, writer, candidate = realm
    session = session_for(service, writer, candidate, tmp_path / 'outbox.json')
    source = wav(tmp_path / 'voice.wav')
    digest = 'sha256:' + hashlib.sha256(source.read_bytes()).hexdigest()

    result = session.add_media(source, on=0, duration=1, clip_id='voice',
                               idempotency_key='l1-wav-import')
    item = result['imports'][0]
    assert item['object_id'] == digest
    assert item['generation_id'] and item['variant_id']
    assert item['catalog_write'] is True
    assert item['retained_on_undo'] is True

    parent = result['data']['canonical_document']['parent_revision']['payload']
    clip = next(row for row in parent['clips'] if row['id'] == 'voice')
    binding = parent['registry']['assets'][clip['asset']]
    assert binding['media_id'] == digest
    assert binding['duration'] == 2
    pin = session.client._remote._transport.get_project_media_selection(candidate['project_id'], {
        'asset': clip['asset'], 'generation_id': binding['generationId'],
        'variant_id': binding['variantId'],
    })
    verified = session.client._remote._transport.get_project_verified_media(candidate['project_id'], pin)
    assert verified['selection']['object_id'] == digest
    assert verified['duration_seconds'] == 2

    original_request = writer.requests[-1]
    commit_count = len(writer.list_timeline_commits(candidate['project_id'], candidate['timeline_id'])['items'])
    source.rename(tmp_path / 'missing.wav')
    retried = session.add_media(source, on=0, duration=1, clip_id='voice',
                                idempotency_key='l1-wav-import')
    assert retried['receipt'] == result['receipt']
    assert retried['imports'] == result['imports']
    assert writer.requests[-1] == original_request
    assert len(writer.list_timeline_commits(candidate['project_id'], candidate['timeline_id'])['items']) == commit_count


def test_lost_wav_acknowledgement_replays_original_request_and_import_disclosure(
        realm, tmp_path, monkeypatch):  # noqa: F811
    service, writer, candidate = realm
    outbox = tmp_path / 'outbox.json'
    session = session_for(service, writer, candidate, outbox)
    source = wav(tmp_path / 'voice.wav')
    original_commit = writer.commit_timeline

    def accept_then_lose_ack(*args, **kwargs):
        original_commit(*args, **kwargs)
        raise ConnectionError('lost acknowledgement after acceptance')

    monkeypatch.setattr(writer, 'commit_timeline', accept_then_lose_ack)
    with pytest.raises(WorkspaceClientError, match='No reply could be verified') as unknown:
        session.add_media(source, on=0, duration=1, clip_id='voice',
                          idempotency_key='l1-lost-wav-ack')
    assert unknown.value.cause['message'] == 'lost acknowledgement after acceptance'

    flight = session.outbox.read()['flight']
    original_request = writer.requests[-1]
    saved_imports = flight['imports']
    history = writer.list_timeline_commits(candidate['project_id'], candidate['timeline_id'])
    source.rename(tmp_path / 'missing.wav')
    monkeypatch.setattr(writer, 'commit_timeline', original_commit)

    reopened = session_for(service, writer, candidate, outbox)
    retried = reopened.add_media(source, on=0, duration=1, clip_id='voice',
                                 idempotency_key='l1-lost-wav-ack')
    assert writer.requests[-1] == original_request
    assert retried['imports'] == saved_imports
    assert retried['imports'][0]['catalog_write'] is True
    assert retried['imports'][0]['retained_on_undo'] is True
    assert writer.list_timeline_commits(candidate['project_id'], candidate['timeline_id']) == history
