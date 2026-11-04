"""Native registry commands share the M1 outbox without recapturing claims."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from astrid.sdk.timeline._command_state import CommandError, State
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline._track_commands import capture_track_set
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.sync import CommitConflict
from astrid.sdk.workspace_client import WorkspaceClientError
from tests.sdk import test_timeline_sync
from tests.sdk.test_timeline_sync import initial_bundle, open_session

connected = test_timeline_sync.connected
REF = 'parent/track:picture'


def label(value='', *, clear=False):
    return {'schema_version': 1, 'op': 'track.set', 'version': '1', 'select': REF,
            'args': {'values': {} if clear else {'label': value},
                     'clear': ['label'] if clear else []}}


@pytest.mark.parametrize('captured', [False, True])
def test_native_queue_and_commit_match_full_portable_golden(connected, captured):
    _, generated, _ = connected
    fixture = json.loads((Path(__file__).parents[1] / 'editing_conformance/fixtures/'
                         'recipe_m2_app_track_label.json').read_text())
    generated.bundles['r0'] = copy.deepcopy(fixture['bundle'])
    session = open_session(connected)
    supplied = copy.deepcopy(fixture['command'] if captured else fixture['request'])
    assert session.queue(supplied) == fixture['command']
    assert session.commit(supplied)['data']['commands'] == [fixture['command']]
    assert generated.bundles['r1'] == fixture['after_bundle']
    assert supplied == (fixture['command'] if captured else fixture['request'])


@pytest.mark.parametrize('before', [None, '', 'Old'])
@pytest.mark.parametrize('clear', [False, True])
def test_native_session_preserves_presence_clear_and_unchanged_history(connected, before, clear):
    _, generated, _ = connected
    if before is not None:
        State(generated.bundles['r0']).entity(REF)['label'] = before
    original = copy.deepcopy(generated.bundles['r0'])
    session = open_session(connected)
    result = session.commit(label(clear=clear))
    track = State(generated.bundles['r1']).entity(REF)
    assert ('label' not in track) if clear else track['label'] == ''
    writes = generated.records['pub1'][0]['writes']
    assert any(row['path'] == ['label'] for row in writes)
    if (clear and before is None) or (not clear and before == ''):
        assert any(row['before'] == row['after'] for row in writes)
    session.undo([result['receipt']['receipt_id']])
    assert generated.bundles['r2'] == original


def test_native_queue_uses_prior_pending_commands_and_stable_created_identity(connected):
    _, generated, _ = connected
    session = open_session(connected)
    create = {'schema_version': 1, 'op': 'track.create', 'version': '1', 'select': 'parent',
              'args': {'row': {'id': 'new', 'kind': 'visual'}}}
    first = session.queue(create)
    second = label('New')
    second['select'] = 'parent/track:new'
    captured = session.queue(second)
    session.flush()
    assert json.loads(generated.calls[0][0])['commands'] == [first, captured]
    assert State(generated.bundles['r1']).entity('parent/track:new')['label'] == 'New'


def test_native_move_ingress_matches_existing_move_capture(connected):
    from astrid.sdk.timeline.capture import capture_move

    command = capture_move(initial_bundle(), 'parent/clip:a', 5)
    assert open_session(connected).commit(command)['data']['commands'] == [command]


@pytest.mark.parametrize('intent', [{}, {'ripple': 'none'},
                                 {'ripple': 'none', 'anchor': 'fixed', 'position': 'absolute'}])
def test_native_move_with_default_intent_retries_without_recapture(connected, intent):
    client, generated, _ = connected
    raw = {'schema_version': 1, 'op': 'clip.move', 'version': '1', 'select': 'parent/clip:a',
           'args': {'on': 5}, 'intent': intent}
    generated.lose_ack = True
    with pytest.raises(WorkspaceClientError):
        open_session(connected).commit(raw)
    open_session(connected).commit(raw)
    assert generated.calls[0] == generated.calls[1]
    assert client.reads == ['r0']


@pytest.mark.parametrize('value,changed', [(True, 1), (False, 0)])
@pytest.mark.parametrize('nested', [False, True])
def test_native_lost_ack_refuses_changed_json_type_before_send(connected, value, changed, nested):
    client, generated, path = connected
    raw = {'schema_version': 1, 'op': 'track.create', 'version': '1', 'select': 'parent',
           'args': {'row': {'id': 'new', 'kind': 'visual',
                            'opaque': {'items': [value]} if nested else value}}}
    generated.lose_ack = True
    with pytest.raises(WorkspaceClientError):
        open_session(connected).commit(raw, idempotency_key='original')
    persisted = path.read_bytes()
    accepted = copy.deepcopy(generated.accepted['original'][1])
    changed_command = copy.deepcopy(raw)
    changed_command['args']['row']['opaque'] = {'items': [changed]} if nested else changed
    session = open_session(connected)
    with pytest.raises(TimelineEditError, match='pending intent differs'):
        session.commit(changed_command)
    assert len(generated.calls) == 1
    assert path.read_bytes() == persisted
    retried = session.commit(raw)
    assert retried['receipt'] == accepted['receipt']
    assert generated.calls == [(json.loads(persisted)['flight']['body'].encode(), 'original')] * 2
    assert client.reads == ['r0']


@pytest.mark.parametrize('field', ['reads', 'expect'])
def test_native_lost_ack_refuses_changed_boolean_capture_claim(connected, field):
    client, generated, path = connected
    clip = State(generated.bundles['r0']).entity('parent/clip:a')
    clip.update(enabled=True, app={'astrid': {'fixed_start': True}})
    raw = {'schema_version': 1, 'op': 'clip.move', 'version': '1', 'select': 'parent/clip:a',
           'args': {'on': 5}}
    captured = execute(generated.bundles['r0'], [raw]).commands[0]
    changed = copy.deepcopy(captured)
    if field == 'reads':
        claim = next(read['state'] for read in changed['reads']
                     if read['ref'] == 'parent/clip:a' and read['path'] == ['enabled'])
    else:
        claim = changed['expect']['parent/clip:a::app.astrid.fixed_start']
    assert claim['value'] is True
    claim['value'] = 1
    generated.lose_ack = True
    with pytest.raises(WorkspaceClientError):
        open_session(connected).commit(captured, idempotency_key='captured')
    persisted = path.read_bytes()
    accepted = copy.deepcopy(generated.accepted['captured'][1])
    session = open_session(connected)
    with pytest.raises(TimelineEditError, match='pending intent differs'):
        session.commit(changed)
    assert len(generated.calls) == 1
    assert path.read_bytes() == persisted
    assert session.commit(captured)['receipt'] == accepted['receipt']
    assert generated.calls[0] == generated.calls[1]
    assert client.reads == ['r0']


def test_native_lost_ack_numeric_equivalence_retries_original_bytes(connected):
    client, generated, _ = connected
    raw = {'schema_version': 1, 'op': 'track.create', 'version': '1', 'select': 'parent',
           'args': {'row': {'id': 'new', 'kind': 'visual', 'opaque': 2}}}
    generated.lose_ack = True
    with pytest.raises(WorkspaceClientError):
        open_session(connected).commit(raw, idempotency_key='numeric')
    accepted = copy.deepcopy(generated.accepted['numeric'][1])
    raw['args']['row']['opaque'] = 2.0
    assert open_session(connected).commit(raw)['receipt'] == accepted['receipt']
    assert generated.calls[0] == generated.calls[1]
    assert b'"opaque":2}' in generated.calls[1][0]
    assert client.reads == ['r0']


@pytest.mark.parametrize('peer', ['disjoint', 'label', 'aba'])
def test_native_lost_ack_retry_precedes_original_suffix_with_fresh_key(connected, peer):
    client, generated, path = connected
    session = open_session(connected)
    generated.lose_ack = True
    with pytest.raises(WorkspaceClientError):
        session.commit(label('A'), idempotency_key='A')
    original_flight = json.loads(path.read_bytes())['flight']
    accepted = copy.deepcopy(generated.accepted['A'][1])
    session = open_session(connected)
    suffix = session.queue(label('B'))
    assert suffix == capture_track_set(generated.bundles['r1'], REF, values={'label': 'B'})
    changed = copy.deepcopy(generated.bundles['r1'])
    if peer == 'disjoint':
        State(changed).entity('parent/clip:b')['params']['text'] = 'Peer'
    else:
        changed = execute(changed, [label('Peer')]).bundle
        if peer == 'aba':
            changed = execute(changed, [label('A')]).bundle
    generated.bundles['peer'] = changed
    generated.head = {'revision_id': 'peer', 'publication_id': 'peer-publication'}
    retried = open_session(connected).flush()
    assert retried['receipt'] == accepted['receipt']
    assert generated.calls[:2] == [(original_flight['body'].encode(), 'A')] * 2
    state = json.loads(path.read_bytes())
    assert state['base'] == {'revision_id': 'r1', 'publication_id': 'pub1'}
    assert state['pending'] == [suffix] and state['flight'] is None
    session = open_session(connected)
    if peer == 'label':
        with pytest.raises(CommitConflict):
            session.flush()
        state = json.loads(path.read_bytes())
        assert state['pending'] == [] and state['flight'] is None
        assert len(state['pending_changes']) == 1
        record = next(iter(state['pending_changes'].values()))
        assert record['parts'][0]['commands'] == [suffix]
        assert record['failure']['details']['status'] == 'conflict'
    else:
        session.flush()
        assert State(generated.bundles['r2']).entity(REF)['label'] == 'B'
        if peer == 'disjoint':
            assert State(generated.bundles['r2']).entity('parent/clip:b')['params']['text'] == 'Peer'
    body, key = generated.calls[-1]
    body = json.loads(body)
    assert body['commands'] == [suffix]
    assert body['base_revision_id'] == 'r1' and body['base_publication_id'] == 'pub1'
    assert key != 'A'
    assert client.reads == (['r0', 'r0', 'r1', 'r1'] if peer == 'label' else ['r0', 'r0'])


def test_native_one_shot_retry_never_reads_or_recaptures(connected):
    client, generated, path = connected
    generated.lose_ack = True
    with pytest.raises(WorkspaceClientError):
        open_session(connected).commit(label('Original'), idempotency_key='original')
    persisted = path.read_bytes()
    session = open_session(connected)
    with pytest.raises(TimelineEditError, match='pending intent differs'):
        session.commit(label('Changed'))
    assert path.read_bytes() == persisted
    session.commit(label('Original'))
    assert generated.calls[0] == generated.calls[1]
    assert client.reads == ['r0']


@pytest.mark.parametrize('selected', [None, ['step-1']])
def test_public_undo_freezes_v2_metadata_and_retries_lost_ack(connected, selected):
    client, generated, path = connected
    session = open_session(connected)
    accepted = session.commit(label('Original'))
    publication = accepted['receipt']['receipt_id']
    steps = None if selected is None else {publication: selected}
    generated.lose_ack = True
    with pytest.raises(WorkspaceClientError):
        session.undo([publication], steps=steps, idempotency_key='undo', via='cli')
    persisted = path.read_bytes()
    state = json.loads(persisted)
    body = json.loads(state['flight']['body'])
    assert body['base_revision_id'] == 'r1' and body['base_publication_id'] == publication
    assert body['label'] == 'Undo timeline edit' and body['merge'] == 'fields'
    assert body['steps'] == [{'id': 'inverse', 'label': body['label'], 'intent': '',
                              'tags': [], 'command_ordinals': [0], 'patch': []}]
    assert 'reads' not in body and 'reads' not in body['steps'][0]
    assert body['source'] == {'via': 'cli'} and body['dry_run'] is False
    assert body['commands'][0]['args'] == ({'publications': [publication]} if steps is None else
                                          {'publications': [publication], 'steps': steps})
    session = open_session(connected)
    for options in ({'idempotency_key': 'changed'}, {'dry_run': True}):
        with pytest.raises(TimelineEditError, match='immutable'):
            session.undo([publication], steps=steps, **options)
        assert path.read_bytes() == persisted
    with pytest.raises(TimelineEditError, match='pending intent'):
        session.undo(['another-publication'])
    assert path.read_bytes() == persisted
    retried = session.undo([publication], steps=steps)
    assert retried['receipt'] == generated.accepted['undo'][1]['receipt']
    assert generated.calls[-2:] == [(state['flight']['body'].encode(), 'undo')] * 2
    assert client.reads == ['r0']
    assert json.loads(path.read_bytes())['pending'] == []


def test_undo_preserves_pending_intent_and_metadata(connected):
    _, generated, path = connected
    session = open_session(connected)
    session.queue(label('Pending'))
    with session.outbox.locked():
        state = session._state()
        state['commit_options'] = {'label': 'Pending label', 'merge': 'atomic', 'steps': []}
        session.outbox.write(state)
    persisted = path.read_bytes()
    with pytest.raises(TimelineEditError, match='flush pending intent'):
        session.undo(['pub0'])
    assert path.read_bytes() == persisted and generated.calls == []


def test_undo_preview_keeps_base_and_does_not_reserve_its_key(connected):
    _, generated, path = connected
    session = open_session(connected)
    publication = session.commit(label('Original'))['receipt']['receipt_id']
    preview = session.undo([publication], dry_run=True, idempotency_key='preview')
    assert preview['data']['status'] == 'preview' and preview['receipt'] is None
    state = json.loads(path.read_bytes())
    assert state['base'] == {'revision_id': 'r1', 'publication_id': publication}
    assert state['pending'] == [] and state['flight'] is None
    assert 'commit_options' not in state
    accepted = session.undo([publication], idempotency_key='preview')
    assert accepted['receipt'] is not None
    assert generated.calls[-1][1] == 'preview'


def test_undo_retry_preserves_legacy_flight_without_adding_metadata(connected):
    _, generated, path = connected
    session = open_session(connected)
    publication = session.commit(label('Original'))['receipt']['receipt_id']
    generated.refusal = ConnectionError('unknown outcome')
    with pytest.raises(WorkspaceClientError):
        session.undo([publication], idempotency_key='legacy')
    with session.outbox.locked():
        state = session._state()
        body = json.loads(state['flight']['body'])
        for key in ('label', 'steps', 'merge'):
            body.pop(key)
        state.pop('commit_options')
        state['flight']['body'] = test_timeline_sync.request_bytes(body).decode()
        session.outbox.write(state)
    original = copy.deepcopy(state['flight'])
    generated.refusal = None
    open_session(connected).undo([publication])
    assert generated.calls[-1] == (original['body'].encode(), 'legacy')


def test_stale_captured_claims_are_refused_without_refresh_or_persistence(connected):
    _, generated, path = connected
    session = open_session(connected)
    command = capture_track_set(initial_bundle(), REF, values={'label': 'B'})
    session.queue(label('A'))
    persisted = path.read_bytes()
    with pytest.raises(CommandError):
        session.queue(command)
    assert path.read_bytes() == persisted
    assert generated.calls == []


@pytest.mark.parametrize('damage', ['unknown-op', 'patch', 'inverse', 'restore', 'null', 'overlap'])
def test_native_ingress_rejects_unsupported_operations_and_open_patches(connected, damage):
    _, generated, path = connected
    session = open_session(connected)
    command = label('Picture')
    if damage in ('unknown-op', 'inverse', 'restore'):
        command['op'] = {'unknown-op': 'track.patch', 'inverse': 'timeline.inverse',
                         'restore': 'timeline.restore'}[damage]
        if damage == 'inverse':
            command.update(select=None, args={'publications': ['pub0']})
    elif damage == 'patch':
        command['args']['values']['opaque'] = 'overwrite'
    elif damage == 'null':
        command['args']['values']['label'] = None
    else:
        command['args']['clear'] = ['label']
    persisted = path.read_bytes()
    with pytest.raises(TimelineEditError):
        session.commit(command)
    assert path.read_bytes() == persisted
    assert generated.calls == []
