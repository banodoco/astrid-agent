"""Ordinary attached verbs capture intent before exposing a local preview."""
from __future__ import annotations

import copy
import json

import pytest

from astrid.core.timeline.authoring_bundle import compile_authoring_candidate, open_authoring_bundle
from astrid.sdk.timeline._command_state import CommandError, State
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline._track_commands import capture_track_set
from astrid.sdk.timeline.capture import capture_track_create
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import bundle_from_document
from astrid.sdk.timeline.commit_session import Timeline
from astrid.sdk.timeline.sync import CommitConflict
from astrid.sdk.timeline_checkout import Checkout
from astrid.sdk.timeline_editing import add_track
from astrid.sdk.workspace_client import WorkspaceClientError
from tests.editing_conformance.test_recipe_clip_scalar_fields import app_closure
from tests.editing_conformance.test_recipe_m2_clip_trim import media_document
from tests.sdk import test_timeline_sync as sync

connected = sync.connected
open_session = sync.open_session


def attached(connected, bundle=None):
    _, generated, _ = connected
    if bundle is not None:
        generated.bundles['r0'] = copy.deepcopy(bundle)
        generated.bundles['r0'].update(project_id='p', timeline_id='t')
    return Checkout.attach(open_session(connected))


def test_function_commit_adapts_parent_text_tracks_delete_and_hold_then_undoes_once(connected):
    from astrid.sdk import timeline_intent as intent

    _, generated, _ = connected
    before = copy.deepcopy(generated.bundles["r0"])
    clips = before["parent"]["clips"]
    intent.set_for(clips[0], 7)
    publication = compile_authoring_candidate(before).publication
    document = {
        "schema_version": 1,
        "project_id": "p",
        "timeline_id": "t",
        "parent_revision": {"revision_id": "r0", "content_digest": publication["content_digest"],
                            "payload": publication["parent_composition"]},
        "shot_revisions": publication["shot_revisions"],
        "internal_timeline_revisions": publication["internal_timeline_revisions"],
        "dependency_manifest": {**publication["dependency_manifest"], "documents": []},
    }
    before = bundle_from_document(document)
    generated.bundles["r0"] = before
    session = open_session(connected)
    state = session._state()
    tl = Timeline(session, session._bundle(state), state["base"])

    def edit(t):
        t.track("titles", above="picture").rename("Opening")
        t.add_text("Chapter one", at=1, hold=3, track="titles", id="chapter-one")
        t.clip("a").set_track("titles").hold_for(2.5)
        t.clip("b").remove()

    result = tl.commit(edit)
    assert result["receipt"]["result"]["status"] == "accepted"
    assert len(generated.calls) == 1
    submitted = json.loads(generated.calls[0][0])["commands"]
    added = next(command for command in submitted if command["op"] == "clip.add")
    assert added["created"] == ["parent/clip:chapter-one"]
    removed = next(command for command in submitted if command["op"] == "clip.delete")
    assert removed["select"] == "parent/clip:b"
    after = copy.deepcopy(generated.bundles[generated.head["revision_id"]])
    assert [row["id"] for row in after["parent"]["config"]["tracks"]] == ["titles", "picture"]
    assert after["parent"]["config"]["tracks"][0]["label"] == "Opening"
    assert [row["id"] for row in after["parent"]["clips"]] == ["a", "chapter-one"]
    assert after["parent"]["clips"][0]["hold"] == 2.5
    assert intent.for_s(after["parent"]["clips"][0]) == 2.5

    with pytest.raises(CommandError, match="authenticated physical binding normalization"):
        execute(after, [{"schema_version": 1, "op": "timeline.inverse", "version": "1",
                         "select": None, "args": {"publications": [result["receipt"]["receipt_id"]]}}],
                inverse_records=generated.records[result["receipt"]["receipt_id"]])

    undone = tl.undo(publications=[result["receipt"]["receipt_id"]])
    assert undone["data"]["status"] == "accepted"
    restored = generated.bundles[generated.head["revision_id"]]
    assert restored["parent"]["config"]["tracks"] == before["parent"]["config"]["tracks"]
    assert restored["parent"]["clips"] == before["parent"]["clips"]

    result = tl.commit(edit)
    latest = generated.bundles[generated.head["revision_id"]]
    State(latest).set("parent/clip:a", ["params", "color"], {"state": "present", "value": "teal"})
    undone = tl.undo(publications=[result["receipt"]["receipt_id"]])
    assert undone["data"]["status"] == "accepted"
    selective = generated.bundles[generated.head["revision_id"]]
    assert selective["parent"]["clips"][0] == {
        **before["parent"]["clips"][0], "params": {**before["parent"]["clips"][0].get("params", {}),
                                                        "color": "teal"}}


def pending(connected):
    return json.loads(connected[2].read_bytes())


@pytest.mark.parametrize('type_key', ['clipType', 'clip_type'])
def test_raw_element_alias_keeps_canvas_units_after_acceptance(connected, type_key):
    row = State(connected[1].bundles['r0']).entity('parent/clip:a')
    row.pop('clipType')
    row[type_key] = 'am-sprite'
    row['params'] = {'x': 27, 'scale': 6, 'opaque': [None, False, 1, 1.0]}
    tl = attached(connected)
    tl.clip('a').set(x=600)
    command = pending(connected)['pending'][0]
    assert command['expect']['parent/clip:a::params.x'] == {'state': 'present', 'value': 27}
    assert {'ref': 'parent/clip:a', 'path': [type_key],
            'state': {'state': 'present', 'value': 'am-sprite'}} in command['reads']
    accepted = tl.flush()
    stored = State(bundle_from_document(accepted['data']['canonical_document'])).entity('parent/clip:a')
    assert stored['params'] == {'x': 100, 'scale': 6, 'opaque': [None, False, 1, 1.0]}
    assert stored[type_key] == 'am-sprite'
    assert ('clip_type' if type_key == 'clipType' else 'clipType') not in stored
    assert accepted['data']['commands'] == [command]


def test_move_is_durable_before_preview_and_unchanged_write_is_captured(connected, monkeypatch):
    tl = attached(connected)
    clip = tl.clip('a')
    before = copy.deepcopy(tl.bundle)
    write = tl.session.outbox.write

    def observe(state):
        assert tl.bundle == before
        assert state['pending'][0]['op'] == 'clip.move'
        write(state)

    monkeypatch.setattr(tl.session.outbox, 'write', observe)
    assert clip.on(0) is clip
    first = pending(connected)['pending'][0]
    assert first['expect']['parent/clip:a::at'] == {'state': 'present', 'value': 0}
    monkeypatch.setattr(tl.session.outbox, 'write', write)
    assert clip.on(2) is clip
    assert clip.start == 2
    assert len(pending(connected)['pending']) == 2
    assert tl._journal == []


def test_failed_durable_write_keeps_preview_unchanged(connected, monkeypatch):
    tl = attached(connected)
    before = copy.deepcopy(tl.bundle)
    state = pending(connected)

    def fail(state):
        raise OSError('disk full')

    monkeypatch.setattr(tl.session.outbox, 'write', fail)
    with pytest.raises(OSError, match='disk full'):
        tl.clip('a').on(2)
    assert tl.bundle == before
    assert pending(connected) == state


def test_literal_params_clear_and_opaque_values_use_one_captured_command(connected):
    tl = attached(connected)
    clip = tl.clip('a')
    clip.set(text='next', opaque={'array': [None, False, 0]}, _allow_new=True)
    clip.set_param('text', None)
    assert clip.params == {'opaque': {'array': [None, False, 0]}}
    calls = pending(connected)['pending']
    assert [call['op'] for call in calls] == ['clip.set-params', 'clip.set-params']
    assert calls[1]['args'] == {'values': {}, 'clear': ['text']}
    accepted = tl.flush()
    assert accepted['data']['commands'] == calls
    loaded = Checkout.attach(open_session(connected))
    assert loaded.clip('a').params == clip.params
    loaded.undo([accepted['receipt']['receipt_id']])
    assert loaded.clip('a').params == {'text': 'café'}


@pytest.mark.parametrize(
    ('initial', 'values', 'clear', 'expected'),
    [(None, {'label': 'Picture'}, [], 'Picture'),
     ('Existing', {'label': ''}, [], ''),
     ('Existing', {}, ['label'], None)],
)
def test_parent_track_label_publish_uses_the_existing_captured_outbox(connected, initial, values, clear, expected):
    _, generated, _ = connected
    bundle = copy.deepcopy(generated.bundles['r0'])
    track = State(bundle).entity('parent/track:picture')
    if initial is None:
        track.pop('label', None)
    else:
        track['label'] = initial
    generated.bundles['r0'] = bundle

    session = open_session(connected)
    command = capture_track_set(bundle, 'parent/track:picture', values=values, clear=clear)
    assert session.queue(command) == command
    tl = Checkout.attach(session)
    assert pending(connected)['pending'] == [command]
    accepted = tl.publish('rename picture', idempotency_key='label-A')

    body = json.loads(generated.calls[-1][0])
    assert body['base_revision_id'] == 'r0'
    assert body['base_publication_id'] == 'pub0'
    assert body['commands'] == [command]
    assert body['rationale'] == 'rename picture'
    assert accepted['receipt']['idempotency_key'] == 'label-A'
    loaded = Checkout.attach(open_session(connected))
    label = State(loaded.bundle).entity('parent/track:picture')
    assert label.get('label') == expected
    if expected is None:
        assert 'label' not in label


@pytest.mark.parametrize('row', [
    {'id': 'overlay', 'kind': 'visual', 'role': 'output'},
    {'id': 'overlay', 'kind': 'visual', 'role': 'output', 'label': ''},
    {'id': 'overlay', 'kind': 'visual', 'role': 'source', 'label': None},
])
def test_parent_track_create_publish_uses_the_existing_captured_outbox(connected, row):
    _, generated, _ = connected
    bundle = generated.bundles['r0']
    session = open_session(connected)
    command = capture_track_create(bundle, 'parent', row)
    assert session.queue(command) == command
    tl = Checkout.attach(session)
    assert pending(connected)['pending'] == [command]

    accepted = tl.publish('create overlay track', idempotency_key='track-create-A')

    body = json.loads(generated.calls[-1][0])
    assert body['commands'] == [command]
    assert body['rationale'] == 'create overlay track'
    assert accepted['receipt']['idempotency_key'] == 'track-create-A'
    loaded = Checkout.attach(open_session(connected))
    tracks = State(loaded.bundle).config('parent')['tracks']
    assert tracks[-1] == row


def test_attached_add_track_queues_existing_track_create_capture(connected):
    tl = Checkout.attach(open_session(connected))
    track = add_track(tl, kind='visual', name='Overlay', track_id='overlay')

    assert track == {'id': 'overlay', 'kind': 'visual', 'label': 'Overlay', 'name': 'Overlay'}
    command = pending(connected)['pending'][0]
    assert command['op'] == 'track.create'
    assert command['select'] == 'parent'
    assert command['args']['row'] == track
    assert connected[1].calls == []

    tl.publish('create overlay track', idempotency_key='track-create-A')
    loaded = Checkout.attach(open_session(connected))
    assert State(loaded.bundle).entity('parent/track:overlay') == track


def test_attached_add_track_keeps_mixed_draft_on_existing_path(connected):
    tl = Checkout.attach(open_session(connected))
    tl.clip('a').on(2)
    before = pending(connected)

    with pytest.raises(TimelineEditError, match='cannot be mixed'):
        add_track(tl, kind='visual', track_id='overlay')
    assert pending(connected) == before


def test_parent_track_create_publish_rejects_uncaptured_row(connected):
    _, generated, _ = connected
    session = open_session(connected)
    raw = {'schema_version': 1, 'op': 'track.create', 'version': '1',
           'select': 'parent',
           'args': {'row': {'id': 'overlay', 'kind': 'visual'}}}
    with session.outbox.locked():
        state = session._state()
        state['pending'] = [raw]
        session.outbox.write(state)

    with pytest.raises(TimelineEditError, match='held'):
        Checkout.attach(session).publish('forged track', idempotency_key='track-forged')
    assert pending(connected)['pending'] == [raw]
    assert pending(connected)['flight'] is None
    assert generated.calls == []


def test_parent_track_create_publish_retries_original_request_after_lost_ack(connected):
    _, generated, _ = connected
    session = open_session(connected)
    command = capture_track_create(generated.bundles['r0'], 'parent', {
        'id': 'overlay', 'kind': 'visual', 'role': 'output', 'label': ''})
    session.queue(command)
    generated.lose_ack = True
    with pytest.raises(WorkspaceClientError):
        Checkout.attach(session).publish('create overlay track', idempotency_key='track-create-A')
    flight = pending(connected)['flight']

    retried = Checkout.attach(open_session(connected))
    accepted = retried.publish(idempotency_key='track-create-A')
    assert generated.calls[0] == generated.calls[1] == (flight['body'].encode(), 'track-create-A')
    assert accepted['receipt']['idempotency_key'] == 'track-create-A'
    assert State(retried.bundle).entity('parent/track:overlay')['label'] == ''


def test_parent_track_label_publish_retries_the_original_request_after_lost_ack(connected):
    _, generated, _ = connected
    bundle = generated.bundles['r0']
    session = open_session(connected)
    command = capture_track_set(bundle, 'parent/track:picture', values={'label': 'Picture'})
    session.queue(command)
    generated.lose_ack = True
    with pytest.raises(WorkspaceClientError):
        Checkout.attach(session).publish('rename picture', idempotency_key='label-A')
    flight = pending(connected)['flight']

    retried = Checkout.attach(open_session(connected))
    accepted = retried.publish(idempotency_key='label-A')
    assert generated.calls[0] == generated.calls[1] == (flight['body'].encode(), 'label-A')
    assert accepted['receipt']['idempotency_key'] == 'label-A'
    assert pending(connected)['pending'] == []
    assert State(retried.bundle).entity('parent/track:picture')['label'] == 'Picture'


def test_parent_track_label_publish_keeps_original_capture_on_conflict(connected):
    _, generated, _ = connected
    bundle = generated.bundles['r0']
    session = open_session(connected)
    command = capture_track_set(bundle, 'parent/track:picture', values={'label': 'Picture'})
    session.queue(command)
    generated.bundles['peer'] = copy.deepcopy(bundle)
    State(generated.bundles['peer']).entity('parent/track:picture')['label'] = 'Peer'
    generated.head = {'revision_id': 'peer', 'publication_id': 'peer-publication'}

    with pytest.raises(CommitConflict):
        Checkout.attach(session).publish('rename picture', idempotency_key='label-A')
    state = pending(connected)
    assert state['pending'] == [command]
    body = json.loads(state['flight']['body'])
    assert body['base_revision_id'] == 'r0'
    assert body['base_publication_id'] == 'pub0'
    assert body['commands'] == [command]
    assert body['rationale'] == 'rename picture'
    assert generated.head == {'revision_id': 'peer', 'publication_id': 'peer-publication'}


def test_parent_track_label_publish_rejects_uncaptured_label_shaped_record(connected):
    _, generated, _ = connected
    session = open_session(connected)
    raw = {'schema_version': 1, 'op': 'track.set', 'version': '1',
           'select': 'parent/track:picture',
           'args': {'values': {'label': 'forged'}, 'clear': []}}
    with session.outbox.locked():
        state = session._state()
        state['pending'] = [raw]
        session.outbox.write(state)

    with pytest.raises(TimelineEditError, match='held'):
        Checkout.attach(session).publish('forged rationale', idempotency_key='forged')
    state = pending(connected)
    assert state['pending'] == [raw]
    assert state['flight'] is None
    assert generated.calls == []


def test_move_then_trim_then_reload_preserves_source_hold_and_opaque_fields(connected):
    tl = attached(connected, bundle_from_document(media_document()))
    clip = tl.clip('parent/clip:clip-a')
    original = copy.deepcopy(clip.data)
    assert clip.on(3.003).trim(1, 5) is clip
    assert clip.start == 3.003
    assert clip.duration == 2
    assert clip.data['hold'] == 4
    assert clip.data['app']['astrid']['opaque'] == [None, False]
    assert original['from'] == 2
    reloaded = Checkout.attach(open_session(connected))
    assert reloaded.clip('parent/clip:clip-a').data == clip.data
    assert [call['op'] for call in pending(connected)['pending']] == ['clip.move', 'clip.trim']
    accepted = reloaded.flush()
    reloaded.undo([accepted['receipt']['receipt_id']])
    assert reloaded.clip('parent/clip:clip-a').data == original


def test_source_window_duration_follows_hold_and_windowless_stays_windowless(connected):
    original = bundle_from_document(media_document(hold=4, app={}))
    tl = attached(connected, original)
    clip = tl.clip('parent/clip:clip-a')
    clip.hold_for('18f')
    assert clip.duration == pytest.approx(.6006)
    assert clip.data['to'] - clip.data['from'] == pytest.approx(clip.data['hold'])
    assert pending(connected)['pending'][-1]['op'] == 'clip.trim'
    tl = attached(connected)
    # The other parent clip is windowless. Raw source hold never fabricates one.
    other = tl.clip('parent/clip:clip-b')
    other.hold_for(3)
    assert other.data['hold'] == 3.003
    assert 'from' not in other.data and 'to' not in other.data


@pytest.mark.parametrize('verb', [lambda clip: clip.on('word:later'), lambda clip: clip.until('end'),
                                  lambda clip: clip.slip(1), lambda clip: clip.set_track('other'),
                                  lambda clip: clip.set_look(opacity=.5),
                                  lambda clip: clip.set(opacity=.5, _allow_new=True),
                                  lambda clip: clip.set(text='ok', **{'states[0].at': 1})])
def test_unsupported_clip_families_leave_preview_and_outbox_intact(connected, verb):
    tl = attached(connected)
    before, state = copy.deepcopy(tl.bundle), pending(connected)
    with pytest.raises(TimelineEditError):
        verb(tl.clip('a'))
    assert tl.bundle == before
    assert pending(connected) == state


def test_selected_pack_params_never_consult_local_schema(connected, monkeypatch):
    bundle = connected[1].bundles['r0']
    State(bundle).entity('parent/clip:a')['elementRef'] = {'packId': 'selected', 'id': 'a', 'kind': 'element', 'revision': 'v1'}
    tl = attached(connected)
    before = pending(connected)
    from astrid.sdk import timeline_address

    def forbidden(*args):
        pytest.fail('selected pack edit used a local schema')

    monkeypatch.setattr(timeline_address, 'element_schema', forbidden)
    with pytest.raises(TimelineEditError, match='exact schema inputs'):
        tl.clip('a').set(text='next')
    assert pending(connected) == before


def test_raw_scope_isolates_shared_occurrence_even_with_colons_in_clip_id(connected):
    parent, shots, internals = app_closure()
    parent.update(project_id='p', timeline_id='t')
    for image in [*shots, *internals]:
        image['project_id'] = 'p'
    bundle = open_authoring_bundle(parent, shot_revisions=shots, internal_timeline_revisions=internals)
    for owner in ('parent', 'occurrence:f2-occurrence', 'occurrence:shared-sibling'):
        State(bundle).collection(owner, 'clip')[0]['id'] = 'literal:colon'
    tl = attached(connected, bundle)
    with pytest.raises(TimelineEditError, match='one exact clip'):
        tl.clip('literal:colon')
    child = tl.clip('occurrence:f2-occurrence/clip:literal:colon')
    child.on(1)
    assert pending(connected)['pending'][0]['select'] == 'occurrence:f2-occurrence/clip:literal:colon'
    assert child.start == 1
    child.hold_for(11)
    assert child.data['hold'] == 11
    assert tl.clip('occurrence:shared-sibling/clip:literal:colon').data['hold'] == 12
    assert tl.clip('parent/clip:literal:colon').start == 0
    assert tl.clip('occurrence:shared-sibling/clip:literal:colon').start == 20


@pytest.mark.parametrize('conflicting', [False, True])
def test_lost_ack_reload_retains_dependent_trim_and_fresh_suffix_key(connected, conflicting):
    original = bundle_from_document(media_document(hold=4, app={}))
    tl = attached(connected, original)
    clip = tl.clip('parent/clip:clip-a')
    clip.on(3.003)
    generated = connected[1]
    generated.lose_ack = True
    with pytest.raises(WorkspaceClientError):
        tl.flush(idempotency_key='A')
    flight = pending(connected)['flight']
    clip.trim(1, 5)
    suffix = pending(connected)['pending'][1]
    assert suffix['reads']
    # C advances the accepted head independently. A relevant original basis
    # change conflicts; an unrelated label permits the original B unchanged.
    generated.bundles['peer'] = copy.deepcopy(generated.bundles['r1'])
    if conflicting:
        State(generated.bundles['peer']).entity('parent/clip:clip-a')['from'] = 0
    else:
        State(generated.bundles['peer']).entity('parent/track:picture')['label'] = 'C'
    generated.head = {'revision_id': 'peer', 'publication_id': 'peer-publication'}
    tl = Checkout.attach(open_session(connected))
    assert pending(connected)['flight'] == flight
    assert tl.clip('parent/clip:clip-a').data['hold'] == 4
    tl.flush()
    assert generated.calls[0] == generated.calls[1] == (flight['body'].encode(), 'A')
    assert pending(connected)['pending'] == [suffix]
    if conflicting:
        with pytest.raises(CommitConflict):
            tl.flush()
        rejected = pending(connected)
        assert rejected['pending'] == [] and rejected['flight'] is None
        record = next(iter(rejected['pending_changes'].values()))
        assert record['parts'][0]['commands'] == [suffix]
        assert record['failure']['details']['status'] == 'conflict'
    else:
        result = tl.flush()
        assert result['data']['commands'] == [suffix]
        assert tl.clip('parent/clip:clip-a').start == 3.003
    assert generated.calls[-1][1] != 'A'


def test_attached_writers_and_unqueued_local_mutations_cannot_fallback(connected, monkeypatch):
    tl = attached(connected)
    for verb in (lambda: tl.publish(), lambda: tl.save(), lambda: tl.add('text', at=1),
                 lambda: tl.edit('group'), lambda: tl.retime()):
        with pytest.raises(TimelineEditError, match='held'):
            verb()
    tl.clip('a').params['text'] = 'uncaptured'
    with pytest.raises(TimelineEditError, match='unqueued local changes'):
        tl.clip('a').on(1)
    with pytest.raises(TimelineEditError, match='unqueued local changes'):
        tl.flush()
    assert pending(connected)['pending'] == []
    assert connected[1].calls == []


def test_stale_handle_does_not_recapture_another_outbox_writer(connected):
    tl = attached(connected)
    other = Checkout.attach(open_session(connected))
    other.clip('b').on(2)
    state = pending(connected)
    with pytest.raises(TimelineEditError, match='basis changed'):
        tl.clip('a').on(1)
    assert pending(connected) == state
