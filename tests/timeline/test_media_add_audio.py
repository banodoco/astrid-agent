from __future__ import annotations

import copy
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from astrid.sdk.timeline import media_add
from astrid.sdk.timeline._common import TimelineEditError

PIN = {
    'asset': 'voice-asset',
    'object_id': 'sha256:audio-object',
    'generation_id': 'generation-1',
    'variant_id': 'variant-1',
}
VERIFIED = {'media_type': 'audio/wav', 'duration_seconds': 12.0}


class _Outbox:
    def __init__(self, session):
        self.session = session

    def locked(self):
        return nullcontext()

    def write(self, state):
        self.session.state = state


class _Session:
    project_id = 'project-1'
    path = 'outbox.json'

    def __init__(self, tracks=()):
        self.bundle = {
            'parent': {'config': {'tracks': copy.deepcopy(list(tracks)), 'clips': []}},
            'placements': [], 'shots': {}, 'source_mapping': {},
            'project_id': self.project_id, 'timeline_id': 'timeline-1',
        }
        self.state = {'pending': [], 'flight': None, 'acknowledged': {}}
        self.outbox = _Outbox(self)
        self.last_commands = None
        self.imports_at_commit = None

    def _state(self):
        return copy.deepcopy(self.state)

    def _bundle(self, _state):
        return self.bundle

    def commit_many(self, commands, *, idempotency_key=None, **_kwargs):
        self.last_commands = copy.deepcopy(commands)
        self.imports_at_commit = copy.deepcopy(self.state.get('media_imports', []))
        result = {'status': 'accepted', 'data': {'receipt_id': 'receipt-1'},
                  'imports': copy.deepcopy(self.state.get('media_imports', []))}
        if idempotency_key:
            self.state.setdefault('acknowledged', {})[idempotency_key] = {
                'media_input': copy.deepcopy(self.state.get('media_input')),
                'imports': copy.deepcopy(self.state.get('media_imports', [])),
            }
        return result

    def retry(self, key, *, matches, **_kwargs):
        assert matches({'commands': []})
        return {'status': 'already_applied', 'data': {'receipt_id': 'receipt-1'},
                'imports': copy.deepcopy(self.state['acknowledged'][key].get('imports', []))}

    def flush(self, **_kwargs):
        return {'status': 'accepted', 'imports': copy.deepcopy(self.state.get('media_imports', []))}


@pytest.fixture(autouse=True)
def _capture_without_runtime(monkeypatch):
    monkeypatch.setattr(media_add, '_selection', lambda _session, _bundle, _source: (PIN, VERIFIED))
    monkeypatch.setattr('astrid.sdk.timeline.commands.execute',
                        lambda _bundle, commands, **_kwargs: SimpleNamespace(commands=commands))


def _add(session, source, *, track=None, key=None):
    return media_add.add_media(session, source, on=2, duration=4, track=track, clip_id='voice-1',
                               values=None, idempotency_key=key, dry_run=False, via='python')


def test_audio_defaults_to_first_active_audio_lane_and_captures_source_window(tmp_path):
    source = tmp_path / 'voice.wav'
    source.write_bytes(b'RIFF fake')
    session = _Session([
        {'id': 'V1', 'kind': 'visual'},
        {'id': 'A1', 'kind': 'audio', 'role': 'output'},
    ])

    result = _add(session, source)

    row = session.last_commands[-1]['args']['row']
    assert row == {'id': 'voice-1', 'clipType': 'audio', 'track': 'A1', 'asset': 'voice-asset',
                   'at': 2.0, 'hold': 4.0, 'from': 0, 'to': 4.0, 'speed': 1}
    assert len(session.last_commands) == 1
    assert result['imports'] == [{
        'object_id': 'sha256:audio-object', 'generation_id': 'generation-1',
        'variant_id': 'variant-1', 'catalog_write': True, 'retained_on_undo': True,
    }]
    assert session.imports_at_commit == result['imports']


def test_audio_creates_a_missing_audio_lane_with_verified_kind(tmp_path):
    source = tmp_path / 'voice.wav'
    source.write_bytes(b'RIFF fake')
    session = _Session([{'id': 'A1', 'kind': 'visual'}])

    _add(session, source)

    assert session.last_commands[0]['op'] == 'track.create'
    assert session.last_commands[0]['args']['row'] == {'id': 'A2', 'kind': 'audio', 'label': 'A2'}


def test_omitted_visual_track_keeps_v1_default(tmp_path, monkeypatch):
    source = tmp_path / 'picture.png'
    source.write_bytes(b'image')
    # The import selector is verified authority; this case only checks the legacy lane default.
    monkeypatch.setattr(media_add, '_selection',
                        lambda _session, _bundle, _source: (PIN, {'media_type': 'image/png'}))
    session = _Session([])

    _add(session, source)

    assert session.last_commands[0]['args']['row'] == {'id': 'V1', 'kind': 'visual', 'label': 'V1'}
    assert session.last_commands[1]['args']['row']['track'] == 'V1'


@pytest.mark.parametrize('track, row', [
    ('V1', {'id': 'V1', 'kind': 'visual'}),
    ('A1', {'id': 'A1', 'kind': 'audio', 'enabled': False}),
])
def test_audio_refuses_incompatible_or_disabled_explicit_lane(tmp_path, track, row):
    source = tmp_path / 'voice.wav'
    source.write_bytes(b'RIFF fake')
    session = _Session([row])

    with pytest.raises(TimelineEditError):
        _add(session, source, track=track)

    assert session.last_commands is None


def test_new_local_audio_import_is_reported_again_on_exact_key_retry(tmp_path):
    source = tmp_path / 'voice.wav'
    source.write_bytes(b'RIFF fake')
    session = _Session()

    first = _add(session, source, key='add-voice')
    source.unlink()
    second = _add(session, source, key='add-voice')

    assert second['status'] == 'already_applied'
    assert second['imports'] == first['imports']


def test_unknown_commit_retry_discloses_import_saved_with_pending_intent(tmp_path):
    source = tmp_path / 'voice.wav'
    source.write_bytes(b'RIFF fake')
    imports = [{
        'object_id': 'sha256:audio-object', 'generation_id': 'generation-1',
        'variant_id': 'variant-1', 'catalog_write': True, 'retained_on_undo': True,
    }]
    session = _Session()
    session.state.update({
        'pending': [{'op': 'clip.add-media'}],
        'flight': {'key': 'add-voice'},
        'media_input': {'source': str(source), 'on': 2.0, 'duration': 4.0,
                        'track': None, 'clip_id': 'voice-1', 'values': {}},
        'media_imports': imports,
    })

    result = _add(session, source, key='add-voice')

    assert result['imports'] == imports


def test_non_audio_local_file_is_rejected_before_import(tmp_path):
    from astrid.sdk.authoring_media import _metadata

    source = tmp_path / 'notes.txt'
    source.write_text('not media')

    with pytest.raises(ValueError, match='Unsupported local media'):
        _metadata(source)
