"""Media commands reuse their pinned capture without weakening outbox fencing."""
from __future__ import annotations

import copy
import hashlib

import pytest

from astrid.sdk.timeline import media_add
from astrid.sdk.timeline._common import TimelineEditError
from tests.sdk import test_timeline_sync as sync_test

connected = sync_test.connected


def _verified_media(monkeypatch, tmp_path):
    source = tmp_path / 'image.png'
    source.write_bytes(b'image bytes')
    digest = 'sha256:' + hashlib.sha256(source.read_bytes()).hexdigest()
    pin = {'asset': 'image', 'object_id': digest, 'generation_id': 'generation',
           'variant_id': 'variant', 'catalog_digest': 'sha256:' + 'a' * 64}
    verified = {'selection': copy.deepcopy(pin), 'media_type': 'image/png',
                'size': source.stat().st_size, 'width': 1, 'height': 1}
    monkeypatch.setattr(media_add, '_selection', lambda *_args: (copy.deepcopy(pin), copy.deepcopy(verified)))
    original_execute = sync_test.execute
    monkeypatch.setattr(sync_test, 'execute', lambda bundle, commands, **kwargs: original_execute(
        bundle, commands, verified_media=[verified], **kwargs))
    original_replay = sync_test.replay
    monkeypatch.setattr(sync_test, "replay", lambda plan, bundle, **kwargs: original_replay(
        plan, bundle, verified_media=[verified], **kwargs))
    return source


def test_each_media_command_reads_its_pinned_bundle_once(connected, monkeypatch, tmp_path):
    source = _verified_media(monkeypatch, tmp_path)
    client, generated, _ = connected
    for index in range(2):
        session = sync_test.open_session(connected)
        result = session.add_media(source, on=index * 3, duration=2, track='picture',
                                   clip_id=f'image-{index}', idempotency_key=f'image-{index}')
        assert result['receipt']['receipt_id']
        assert client.bundle_reads == [(f'r{i}', f'pub{i}') for i in range(index + 1)]
        assert len(generated.calls) == index + 1


def test_media_capture_refuses_if_outbox_changes_during_selection(connected, monkeypatch, tmp_path):
    source = _verified_media(monkeypatch, tmp_path)
    client, generated, _ = connected
    session = sync_test.open_session(connected)
    selection = media_add._selection

    def change_outbox(session, bundle, source):
        result = selection(session, bundle, source)
        with session.outbox.locked():
            state = session._state()
            state['concurrent_marker'] = 'preserve'
            session.outbox.write(state)
        return result

    monkeypatch.setattr(media_add, '_selection', change_outbox)
    with pytest.raises(TimelineEditError, match='outbox changed during capture'):
        session.add_media(source, on=0, duration=2, track='picture', clip_id='image',
                          idempotency_key='race')
    state = session.outbox.read()
    assert state['concurrent_marker'] == 'preserve'
    assert state['pending'] == [] and state['flight'] is None
    assert generated.calls == []
    assert client.bundle_reads == [('r0', 'pub0')]

@pytest.mark.parametrize('conflict', [False, True], ids=['disjoint', 'conflict'])
def test_head_change_after_media_capture_merges_or_refuses(connected, monkeypatch, tmp_path, conflict):
    source = _verified_media(monkeypatch, tmp_path)
    client, generated, _ = connected
    session = sync_test.open_session(connected)
    selection = media_add._selection
    original_commit = generated.commit_timeline
    reads_at_send = []

    def commit(*args, **kwargs):
        reads_at_send.append(copy.deepcopy(client.bundle_reads))
        return original_commit(*args, **kwargs)

    monkeypatch.setattr(generated, "commit_timeline", commit)

    def advance_head(session, bundle, source):
        result = selection(session, bundle, source)
        peer = copy.deepcopy(generated.bundles['r0'])
        if conflict:
            clip = copy.deepcopy(peer['parent']['clips'][0])
            clip['id'] = 'image'
            peer['parent']['clips'].append(clip)
        else:
            peer['parent']['clips'][0]['at'] = 5
        generated.bundles['peer'] = peer
        generated.publications['peer'] = 'peer-publication'
        generated.head = {'revision_id': 'peer', 'publication_id': 'peer-publication'}
        return result

    monkeypatch.setattr(media_add, '_selection', advance_head)
    if conflict:
        with pytest.raises(sync_test.CommitConflict):
            session.add_media(source, on=0, duration=2, track='picture', clip_id='image',
                              idempotency_key='head-race')
        assert generated.head['revision_id'] == 'peer'
    else:
        result = session.add_media(source, on=0, duration=2, track='picture', clip_id='image',
                                   idempotency_key='head-race')
        assert result['data']['status'] == 'rebased'
        clips = generated.bundles[generated.head['revision_id']]['parent']['clips']
        assert next(clip for clip in clips if clip['id'] == 'a')['at'] == 5
        assert any(clip['id'] == 'image' for clip in clips)
    assert reads_at_send == [[('r0', 'pub0')]]
    assert len(generated.calls) == 1
