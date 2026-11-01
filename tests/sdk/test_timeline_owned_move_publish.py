"""Explicitly attached draft publication never discovers another outbox."""
from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from astrid.core.timeline.authoring_bundle import open_authoring_bundle
from astrid.sdk.timeline import commands
from astrid.sdk.timeline._command_state import CommandError, State
from astrid.sdk.timeline._common import PARENT_SHOT, TimelineEditError
from astrid.sdk.timeline.commit import bundle_from_document
from astrid.sdk.timeline.model import Clip
from astrid.sdk.timeline.sync import TimelineSession
from astrid.sdk.timeline_checkout import Checkout
from astrid.sdk.workspace_client import WorkspaceClientError
from tests.editing_conformance.test_recipe_clip_scalar_fields import app_closure
from tests.editing_conformance.test_recipe_m2_clip_trim import media_document
from tests.sdk.test_timeline_sync import connected, open_session  # noqa: F401


def state(connected):
    return json.loads(connected[2].read_bytes())


def test_friendly_split_stages_native_command_for_function_commit():
    calls = []

    class StagingTimeline:
        def command(self, command, *, label):
            calls.append((copy.deepcopy(command), label))

    clip = Clip(StagingTimeline(), PARENT_SHOT, {'id': 'clip-a', 'clipType': 'media'})
    assert clip.split(2, id='right') is clip
    assert calls == [({
        'schema_version': 1, 'op': 'clip.split', 'version': '1',
        'select': 'parent/clip:clip-a', 'args': {'on': 2, 'right_id': 'right'},
        'created': ['parent/clip:right'],
    }, 'Split clip')]


def test_friendly_split_refuses_shot_owned_clips():
    calls = []

    class StagingTimeline:
        def command(self, command, *, label):
            calls.append((command, label))

    clip = Clip(StagingTimeline(), 'shot-1', {'id': 'clip-a', 'clipType': 'media'})
    with pytest.raises(TimelineEditError, match='attached parent media clips'):
        clip.split(2, id='right')
    assert calls == []


def test_parent_publish_rebases_original_capture_reloads_and_undoes(connected):
    client, generated, _ = connected
    tl = Checkout.attach(open_session(connected))
    original = copy.deepcopy(tl.clip('a').data)
    generated.bundles['peer'] = copy.deepcopy(generated.bundles['r0'])
    State(generated.bundles['peer']).entity('parent/track:picture')['label'] = 'Peer'
    generated.head = {'revision_id': 'peer', 'publication_id': 'peer-publication'}
    assert tl.clip('a').on(2).start == 2
    captured = state(connected)['pending']
    accepted = tl.publish('Make room for the opening', idempotency_key='owned-move')
    body = json.loads(generated.calls[-1][0])
    assert body['commands'] == captured
    assert body['base_revision_id'] == 'r0' and body['base_publication_id'] == 'pub0'
    assert body['rationale'] == accepted['message'] == 'Make room for the opening'
    assert accepted['receipt']['result']['status'] == 'rebased'
    assert accepted['idempotency_key'] == 'owned-move'
    loaded = Checkout.attach(open_session(connected))
    assert loaded.clip('a').start == 2
    assert State(loaded.bundle).entity('parent/track:picture')['label'] == 'Peer'
    loaded.undo([accepted['receipt']['receipt_id']])
    assert loaded.clip('a').data == original
    assert State(loaded.bundle).entity('parent/track:picture')['label'] == 'Peer'
    assert client.reads


def test_parent_publish_captures_source_hold_set_and_undoes_without_legacy_writer(connected):
    client, generated, _ = connected
    State(generated.bundles['r0']).entity('parent/clip:a')['speed'] = 2
    tl = Checkout.attach(open_session(connected))
    original = copy.deepcopy(tl.clip('a').data)
    clip = tl.clip('parent/clip:a')
    clip.hold_for(2)
    command = state(connected)['pending'][0]
    assert clip.duration == 2
    assert command['op'] == 'clip.set'
    assert command['select'] == 'parent/clip:a'
    assert command['args'] == {'values': {'hold': 4}, 'clear': []}

    accepted = tl.publish('Hold the opening', idempotency_key='owned-hold')
    assert json.loads(generated.calls[-1][0])['commands'] == [command]
    loaded = Checkout.attach(open_session(connected))
    assert loaded.clip('parent/clip:a').data == dict(original, hold=4)
    loaded.undo([accepted['receipt']['receipt_id']])
    assert loaded.clip('parent/clip:a').data == original
    assert client.reads


def test_parent_publish_captures_asset_clear_and_undoes_without_legacy_writer(connected):
    client, generated, _ = connected
    generated.bundles['r0'] = bundle_from_document(media_document())
    generated.bundles['r0'].update(project_id='p', timeline_id='t')
    tl = Checkout.attach(open_session(connected))
    original = copy.deepcopy(tl.clip('parent/clip:clip-a').data)

    clip = tl.clip('parent/clip:clip-a')
    clip.clear_asset()
    command = state(connected)['pending'][0]
    assert command['op'] == 'clip.set'
    assert command['select'] == 'parent/clip:clip-a'
    assert command['args'] == {'values': {}, 'clear': ['asset']}
    assert 'asset' not in clip.data

    accepted = tl.publish('Clear the source', idempotency_key='owned-asset-clear')
    assert json.loads(generated.calls[-1][0])['commands'] == [command]
    loaded = Checkout.attach(open_session(connected))
    assert 'asset' not in loaded.clip('parent/clip:clip-a').data
    loaded.undo([accepted['receipt']['receipt_id']])
    assert loaded.clip('parent/clip:clip-a').data == original
    assert client.reads


def test_parent_publish_captures_transition_clear_and_undoes_without_legacy_writer(connected):
    client, generated, _ = connected
    generated.bundles['r0'] = bundle_from_document(
        media_document(transition={'type': 'crossfade', 'duration': .25}))
    generated.bundles['r0'].update(project_id='p', timeline_id='t')
    tl = Checkout.attach(open_session(connected))
    original = copy.deepcopy(tl.clip('parent/clip:clip-a').data)
    neighbor = copy.deepcopy(tl.clip('parent/clip:clip-b').data)

    clip = tl.clip('parent/clip:clip-a')
    clip.set(transition=None)
    command = state(connected)['pending'][0]
    assert command['op'] == 'clip.set'
    assert command['select'] == 'parent/clip:clip-a'
    assert command['args'] == {'values': {}, 'clear': ['transition']}
    assert 'transition' not in clip.data

    accepted = tl.publish('Clear the transition', idempotency_key='owned-transition-clear')
    assert json.loads(generated.calls[-1][0])['commands'] == [command]
    loaded = Checkout.attach(open_session(connected))
    assert 'transition' not in loaded.clip('parent/clip:clip-a').data
    assert loaded.clip('parent/clip:clip-b').data == neighbor
    loaded.undo([accepted['receipt']['receipt_id']])
    assert loaded.clip('parent/clip:clip-a').data == original
    assert loaded.clip('parent/clip:clip-b').data == neighbor
    assert client.reads


@pytest.mark.parametrize('params', [
    {'transition': 'crossfade'},
    {'transition': ''},
    {'transition': {}},
    {'transition': None, 'text': 'Still local'},
])
def test_attached_transition_set_refuses_unsupported_or_mixed_calls_atomically(connected, params):
    _, generated, path = connected
    tl = Checkout.attach(open_session(connected))
    before, draft = path.read_bytes(), copy.deepcopy(tl.bundle)

    with pytest.raises(TimelineEditError, match='held'):
        tl.clip('parent/clip:a').set(**params)

    assert path.read_bytes() == before
    assert tl.bundle == draft
    assert generated.calls == []


def test_attached_transition_clear_refuses_occurrence_owner_atomically(connected):
    _, generated, path = connected
    parent, shots, internals = app_closure()
    parent.update(project_id='p', timeline_id='t')
    for image in [*shots, *internals]:
        image['project_id'] = 'p'
    generated.bundles['r0'] = open_authoring_bundle(
        parent, shot_revisions=shots, internal_timeline_revisions=internals)
    tl = Checkout.attach(open_session(connected))
    before, draft = path.read_bytes(), copy.deepcopy(tl.bundle)

    with pytest.raises(TimelineEditError, match='held'):
        tl.clip('occurrence:f2-occurrence/clip:f2-agent-clip').set(transition=None)

    assert path.read_bytes() == before
    assert tl.bundle == draft
    assert generated.calls == []


def test_parent_publish_captures_source_trim_rebases_reloads_and_undoes(connected, monkeypatch):
    client, generated, _ = connected
    generated.bundles['r0'] = bundle_from_document(media_document())
    generated.bundles['r0'].update(project_id='p', timeline_id='t')
    original = copy.deepcopy(State(generated.bundles['r0']).entity('parent/clip:clip-a'))
    tl = Checkout.attach(open_session(connected))
    generated.bundles['peer'] = copy.deepcopy(generated.bundles['r0'])
    State(generated.bundles['peer']).entity('parent/track:picture')['label'] = 'Peer'
    generated.head = {'revision_id': 'peer', 'publication_id': 'peer-publication'}

    clip = tl.clip('parent/clip:clip-a')
    clip.trim(1.0000004, 5.0000006)
    command = state(connected)['pending'][0]
    assert command['op'] == 'clip.trim'
    assert command['select'] == 'parent/clip:clip-a'
    assert command['args'] == {'in_s': 1.0000004, 'out_s': 5.0000006}
    assert clip.data['from'] == 1.0 and clip.data['to'] == 5.000001
    assert clip.data['hold'] == 4.000001

    generated.lose_ack = True
    with pytest.raises(WorkspaceClientError):
        tl.publish('Trim the opening', idempotency_key='owned-trim')
    first = copy.deepcopy(generated.calls[-1])

    def no_capture(*args):
        pytest.fail('trim retry attempted to recapture intent')

    loaded = Checkout.attach(open_session(connected))
    monkeypatch.setattr(commands, 'execute', no_capture)
    accepted = loaded.publish()
    assert generated.calls[-1] == first
    assert state(connected)['flight'] is None
    assert accepted['receipt']['result']['status'] == 'rebased'
    assert accepted['message'] == 'Trim the opening'
    assert loaded.clip('parent/clip:clip-a').data['from'] == 1.0
    assert loaded.clip('parent/clip:clip-a').data['to'] == 5.000001
    assert loaded.clip('parent/clip:clip-a').data['hold'] == 4.000001
    assert State(loaded.bundle).entity('parent/track:picture')['label'] == 'Peer'

    undone = loaded.undo([accepted['receipt']['receipt_id']])
    assert undone['data']['status'] == 'accepted'
    assert loaded.clip('parent/clip:clip-a').data == original
    assert State(loaded.bundle).entity('parent/track:picture')['label'] == 'Peer'
    assert client.reads


def test_parent_publish_captures_delete_retries_reloads_and_undoes(connected, monkeypatch):
    client, generated, _ = connected
    tl = Checkout.attach(open_session(connected))
    original = copy.deepcopy(tl.clip('parent/clip:a').data)
    tl.clip('parent/clip:a').remove()
    command = state(connected)['pending'][0]
    assert command['op'] == 'clip.delete'
    assert command['select'] == 'parent/clip:a'
    assert command['args'] == {}
    assert not State(tl.bundle).entity('parent/clip:a', optional=True)

    generated.lose_ack = True
    with pytest.raises(WorkspaceClientError):
        tl.publish('Remove the opening', idempotency_key='owned-delete')
    first = copy.deepcopy(generated.calls[-1])

    def no_capture(*args):
        pytest.fail('delete retry attempted to recapture intent')

    loaded = Checkout.attach(open_session(connected))
    monkeypatch.setattr(commands, 'execute', no_capture)
    accepted = loaded.publish()
    assert generated.calls[-1] == first
    body = json.loads(first[0])
    assert body['commands'] == [command]
    assert body['rationale'] == accepted['message'] == 'Remove the opening'
    assert accepted['receipt']['result']['status'] == 'accepted'
    assert not State(loaded.bundle).entity('parent/clip:a', optional=True)
    assert loaded.clip('b').data['params'] == {'text': 'café'}

    undone = loaded.undo([accepted['receipt']['receipt_id']])
    assert undone['data']['status'] == 'accepted'
    assert loaded.clip('parent/clip:a').data == original
    assert client.reads


def test_parent_publish_captures_split_retries_reloads_and_undoes_exact_rows(connected, monkeypatch):
    client, generated, _ = connected
    document = media_document(
        hold=4,
        app={'astrid': {'opaque': [None, False]},
             'org.example': {'literal': 'clip-a', 'clear': None}},
        opaque={'nested': [None, False, 0, {}, []]},
    )
    generated.bundles['r0'] = bundle_from_document(document)
    generated.bundles['r0'].update(project_id='p', timeline_id='t')
    original_rows = copy.deepcopy(generated.bundles['r0']['parent']['clips'])
    original_config_rows = copy.deepcopy(generated.bundles['r0']['parent']['config']['clips'])
    original = copy.deepcopy(State(generated.bundles['r0']).entity('parent/clip:clip-a'))
    tl = Checkout.attach(open_session(connected))
    generated.bundles['peer'] = copy.deepcopy(generated.bundles['r0'])
    State(generated.bundles['peer']).entity('parent/track:picture')['label'] = 'Peer'
    generated.head = {'revision_id': 'peer', 'publication_id': 'peer-publication'}

    clip = tl.clip('parent/clip:clip-a')
    clip.split(2, id='right')
    command = state(connected)['pending'][0]
    assert command['op'] == 'clip.split'
    assert command['select'] == 'parent/clip:clip-a'
    assert command['args'] == {'on': 2, 'right_id': 'right'}
    assert command['created'] == ['parent/clip:right']
    right = State(tl.bundle).entity('parent/clip:right')
    assert right['opaque'] == original['opaque']
    assert right['speed'] == original['speed'] == 2
    assert (right['at'], right['from'], right['to'], right['hold']) == (2, 4, 6, 2)

    generated.lose_ack = True
    with pytest.raises(WorkspaceClientError):
        tl.publish('Split the opening', idempotency_key='owned-split')
    first = copy.deepcopy(generated.calls[-1])
    loaded = Checkout.attach(open_session(connected))

    def no_capture(*args):
        pytest.fail('split retry attempted to recapture intent')

    monkeypatch.setattr(commands, 'execute', no_capture)
    accepted = loaded.publish()
    assert generated.calls[-1] == first
    assert accepted['message'] == 'Split the opening'
    assert loaded.clip('parent/clip:right').data == right
    assert State(loaded.bundle).entity('parent/track:picture')['label'] == 'Peer'

    undone = loaded.undo([accepted['receipt']['receipt_id']])
    assert undone['data']['status'] == 'accepted'
    assert loaded.bundle['parent']['clips'] == original_rows
    assert loaded.bundle['parent']['config']['clips'] == original_config_rows
    assert loaded.clip('parent/clip:clip-a').data == original
    assert State(loaded.bundle).entity('parent/clip:right', optional=True) is None
    assert State(loaded.bundle).entity('parent/track:picture')['label'] == 'Peer'
    assert client.reads


def test_parent_split_dependency_refusal_keeps_draft_atomic(connected):
    _, generated, path = connected
    generated.bundles['r0'] = bundle_from_document(media_document(
        hold=4,
        app={'astrid': {'opaque': [None, False]},
             'org.example': {'literal': 'clip-a', 'clear': None}},
    ))
    generated.bundles['r0'].update(project_id='p', timeline_id='t')
    for rows in (generated.bundles['r0']['parent']['clips'],
                 generated.bundles['r0']['parent']['config']['clips']):
        rows[1]['transition'] = 'fade'
    tl = Checkout.attach(open_session(connected))
    before, draft = path.read_bytes(), copy.deepcopy(tl.bundle)
    with pytest.raises(CommandError, match='parent transitions'):
        tl.clip('parent/clip:clip-a').split(2, id='right')
    assert path.read_bytes() == before
    assert tl.bundle == draft
    assert generated.calls == []


def test_parent_publish_restart_keeps_exact_request_key_and_rationale(connected, monkeypatch):
    _, generated, _ = connected
    tl = Checkout.attach(open_session(connected))
    tl.clip('a').on(2)
    generated.lose_ack = True
    with pytest.raises(WorkspaceClientError):
        tl.publish('Opening', idempotency_key='original')
    durable = connected[2].read_bytes()
    flight = state(connected)['flight']
    loaded = Checkout.attach(open_session(connected))
    with pytest.raises(TimelineEditError, match='immutable'):
        loaded.publish('Changed reason')
    with pytest.raises(TimelineEditError, match='immutable'):
        loaded.publish(idempotency_key='replacement')
    assert connected[2].read_bytes() == durable

    def no_capture(*args):
        pytest.fail('publish retry attempted to recapture intent')

    monkeypatch.setattr(commands, 'execute', no_capture)
    accepted = loaded.publish()
    assert generated.calls[-1] == generated.calls[-2] == (flight['body'].encode(), 'original')
    assert accepted['message'] == 'Opening'
    assert loaded.clip('a').start == 2
    assert len(generated.accepted) == 1
    assert state(connected)['pending'] == [] and state(connected)['flight'] is None


def test_unrelated_and_detached_publish_never_consume_global_pending_or_flight(connected, monkeypatch):
    client, generated, path = connected
    # A default session represents an independent CLI/session outbox.
    from astrid.sdk import storage_root

    monkeypatch.setattr(storage_root, 'resolve_runtime_data_root', lambda: path.parent)
    global_session = TimelineSession.open('p', 't', client=client)
    generated.path = global_session.outbox.path
    global_session.queue_move('parent/clip:a', 3)
    unrelated = Checkout.attach(open_session(connected))
    detached = Checkout.open('p', 't', revision_id='r0', client=client)
    monkeypatch.setattr(detached, '_sync_film_length', lambda: [])
    monkeypatch.setattr(detached, '_check_core', lambda: SimpleNamespace(valid=True))

    def held_candidate(*args, **kwargs):
        raise TimelineEditError('detached candidate retained')

    monkeypatch.setattr(detached, '_commit_document', held_candidate)
    for in_flight in (False, True):
        if in_flight:
            generated.lose_ack = True
            with pytest.raises(WorkspaceClientError):
                global_session.flush(idempotency_key='cli-flight')
        before, calls = global_session.outbox.path.read_bytes(), list(generated.calls)
        with pytest.raises(TimelineEditError, match='held'):
            unrelated.publish()
        with pytest.raises(TimelineEditError, match='held'):
            Checkout.attach(global_session).publish()
        with pytest.raises(TimelineEditError, match='detached candidate retained'):
            detached.publish(client=client)
        assert global_session.outbox.path.read_bytes() == before
        assert generated.calls == calls


def test_mixed_and_unqueued_edits_are_held_without_consuming_draft(connected, monkeypatch):
    tl = Checkout.attach(open_session(connected))
    tl.clip('a').on(2)
    tl.clip('b').set(text='Still local')
    before, draft = connected[2].read_bytes(), copy.deepcopy(tl.bundle)
    with pytest.raises(TimelineEditError, match='held'):
        tl.publish('Mixed')
    assert connected[2].read_bytes() == before and tl.bundle == draft
    assert connected[1].calls == []

    # A second writer arriving after the preview check is fenced under the
    # same lock that seals the flight, before any request is written or sent.
    other = Checkout.attach(open_session(connected))
    monkeypatch.setattr(tl, '_unchanged_preview', lambda: other.clip('b').on(4))
    with pytest.raises(TimelineEditError, match='basis changed'):
        tl.publish()
    assert state(connected)['flight'] is None and connected[1].calls == []


def test_mixed_parent_delete_and_edit_are_held_without_writes(connected):
    tl = Checkout.attach(open_session(connected))
    tl.clip('parent/clip:a').remove()
    tl.clip('parent/clip:b').set(text='Still local')
    before, draft = connected[2].read_bytes(), copy.deepcopy(tl.bundle)
    with pytest.raises(TimelineEditError, match='held'):
        tl.publish('Mixed')
    assert connected[2].read_bytes() == before and tl.bundle == draft
    assert connected[1].calls == []


def test_ambiguous_owner_and_occurrence_publish_refuse_with_draft_intact(connected):
    parent, shots, internals = app_closure()
    parent.update(project_id='p', timeline_id='t')
    for image in [*shots, *internals]:
        image['project_id'] = 'p'
    bundle = open_authoring_bundle(parent, shot_revisions=shots, internal_timeline_revisions=internals)
    for owner in ('parent', 'occurrence:f2-occurrence', 'occurrence:shared-sibling'):
        State(bundle).collection(owner, 'clip')[0]['id'] = 'same'
    connected[1].bundles['r0'] = bundle
    tl = Checkout.attach(open_session(connected))
    before, draft = connected[2].read_bytes(), copy.deepcopy(tl.bundle)
    with pytest.raises(TimelineEditError, match='one exact clip'):
        tl.clip('same').on(1)
    assert connected[2].read_bytes() == before and tl.bundle == draft
    tl.clip('occurrence:f2-occurrence/clip:same').on(1)
    before, draft = connected[2].read_bytes(), copy.deepcopy(tl.bundle)
    with pytest.raises(TimelineEditError, match='held'):
        tl.publish('Occurrence is outside this bounded publisher')
    assert connected[2].read_bytes() == before and tl.bundle == draft
    assert connected[1].calls == []
