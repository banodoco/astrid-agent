"""Bounded original SDK verbs through the generated client and Runtime HTTP.

Manager-run only, using the current qualified Runtime/public Composition pair.
The first two cases inject the real HTTP handler transport; the ambiguity case
uses the existing live Runtime fixture. Neither is run by this worker.
"""
from __future__ import annotations

import copy
import json

import pytest

from astrid.sdk.timeline._command_state import State
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.commands import execute
from astrid.sdk.timeline.commit import bundle_from_document
from astrid.sdk.timeline.emitter import emit_document_commands
from astrid.sdk.timeline.native_occurrence_delete import capture_occurrence_delete
from astrid.sdk.timeline.sync import TimelineSession
from astrid.sdk.timeline_checkout import Checkout
from astrid.sdk.workspace_client import WorkspaceClientError
from tests.editing_conformance import test_commit_runtime_m1 as m1
from tests.editing_conformance import test_native_batch_ingress as ingress
from tests.editing_conformance.test_native_sheet_current_runtime import seed

pytestmark = pytest.mark.integration
runtime = ingress.runtime
film = m1.film
renderer_off = m1.renderer_off
wire = m1.wire
REF = 'parent/clip:title'


def attach(runtime, name='owned'):
    return Checkout.attach(runtime.session(name=name))


def _commit_parent_variant(fixture, parent, *, key, label):
    """Seed a variant through the current provider, retaining the source payload."""
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.timeline_commit_port import route_path
    from runtime_protocol.timeline_commit_provider import load_timeline_commit_provider

    service = RuntimeService(fixture.duplicate_root)
    try:
        service.register_timeline_commit_provider(load_timeline_commit_provider('astrid'))
        head = service.get_project_timeline(fixture.project_id, fixture.timeline_id)
        document = service._timeline_commits()[0].revision_closure(
            fixture.project_id, fixture.timeline_id, head['head_revision_id'])
        before = bundle_from_document(document)
        semantic = []
        working = before
        # Canonical rows retain physical shot pins; placements use authoring IDs.
        original_occurrences = {row['occurrence_id']: row
                                for row in document['parent_revision']['payload']['occurrences']}
        requested = {row['occurrence_id']: row for row in parent['occurrences']}
        requested_order = [row['occurrence_id'] for row in parent['occurrences']]
        original_order = [row['occurrence_id'] for row in before['placements']]
        if [identity for identity in original_order if identity in requested] != requested_order:
            raise ValueError('owned baseline changes managed occurrence membership/order')
        for identity, occurrence in requested.items():
            if identity not in original_occurrences or occurrence != original_occurrences[identity]:
                raise ValueError('owned baseline changes a managed occurrence edge')
        removed = [occurrence for occurrence in before['placements']
                   if occurrence['occurrence_id'] not in requested]
        for occurrence in removed:
            owner = 'occurrence:' + occurrence['occurrence_id']
            command = capture_occurrence_delete(working, owner)
            working = execute(working, [command]).bundle
            semantic.append(command)
        after = copy.deepcopy(working)
        after['parent'] = copy.deepcopy(parent)
        # Occurrences live in placements, already updated by semantic deletes.
        after['parent'].pop('occurrences')
        patches = emit_document_commands(working, after)
        commands = [*semantic, *patches]
        changes = [change for command in commands if command['op'] == 'doc.patch'
                   for change in command['args']['changes']]
        request = {
            'api_version': 1, 'project_id': fixture.project_id,
            'timeline_id': fixture.timeline_id,
            'base_revision_id': head['head_revision_id'],
            'base_publication_id': head['head_publication_id'],
            'commands': commands, 'label': label,
            'steps': [{'id': key, 'label': label, 'intent': label,
                       'tags': ['integration'],
                       'command_ordinals': list(range(len(commands))),
                       'patch': changes, 'reads': []}],
            'merge': 'atomic', 'reads': [], 'dry_run': False,
            'source': {'via': 'python'},
            'rationale': 'Prepare the owned parent variant.',
            'group_id': 'D86-owned-move8',
        }
        return service.commit_timeline(
            fixture.project_id, fixture.timeline_id, request,
            idempotency_key=key, publisher='owned-move8',
            invocation_path=route_path(fixture.project_id, fixture.timeline_id),
            authorize=lambda: None,
        )
    finally:
        service.close()


@pytest.fixture
def trim_film(film):
    """Reuse the captured film/CAS and make its parent media clip trim-ready."""
    fixture, original, _ = film
    parent = copy.deepcopy(original)
    parent['clips'][0]['clipType'] = 'media'
    parent['clips'][0].pop('app', None)
    parent['registry']['assets']['music-bed']['duration'] = 5.5
    parent['config']['clips'] = copy.deepcopy(parent['clips'])
    baseline = _commit_parent_variant(
        fixture, parent, key='owned-trim-baseline', label='Owned trim baseline')
    return fixture, parent, baseline


@pytest.fixture
def transition_film(trim_film):
    """Reuse the trim closure with an authored parent transition."""
    fixture, original, _ = trim_film
    parent = copy.deepcopy(original)
    for rows in (parent['clips'], parent['config']['clips']):
        next(row for row in rows if row['id'] == 'music-bed')['transition'] = {
            'type': 'crossfade', 'duration': .25}
    baseline = _commit_parent_variant(
        fixture, parent, key='owned-transition-baseline', label='Owned transition baseline')
    return fixture, parent, baseline


@pytest.fixture
def split_film(trim_film):
    """Reuse the verified-media trim closure with an explicit source window."""
    fixture, original, _ = trim_film
    parent = copy.deepcopy(original)
    clip = parent['clips'][0]
    clip.pop('app', None)
    clip.pop('effects', None)
    clip.update({'from': 0, 'to': 5.5, 'hold': 5.5, 'speed': 2})
    parent['config']['clips'] = copy.deepcopy(parent['clips'])
    baseline = _commit_parent_variant(
        fixture, parent, key='owned-split-baseline', label='Owned split baseline')
    return fixture, parent, baseline


@pytest.fixture
def delete_film(trim_film):
    """Reuse the trim harness with its occurrence dependencies removed."""
    fixture, original, _ = trim_film
    parent = copy.deepcopy(original)
    parent['occurrences'] = []
    parent['config']['clips'] = copy.deepcopy(parent['clips'])
    baseline = _commit_parent_variant(
        fixture, parent, key='owned-delete-baseline', label='Owned delete baseline')
    return fixture, parent, baseline


def test_http_original_sdk_move_publish_disjoint_rebase_reload_undo(runtime):
    seed(runtime)
    tl = attach(runtime)
    original = copy.deepcopy(tl.clip(REF).data)
    base = copy.deepcopy(runtime.history()['head'])
    runtime.session(name='peer').commit(ingress.label('Peer label'))
    assert tl.clip(REF).on(3.017).start == pytest.approx(tl.quantize(3.017))
    captured = json.loads(tl.session.outbox.path.read_bytes())['pending']
    accepted = tl.publish('Make room for the opening', idempotency_key='sdk-parent-move')
    data = accepted['receipt']['result']
    body = json.loads(runtime.calls[-1][0])
    assert body['base_revision_id'] == base['revision_id']
    assert body['base_publication_id'] == base['publication_id']
    assert body['commands'] == captured == data['commands']
    assert body['rationale'] == data['rationale'] == accepted['message'] == 'Make room for the opening'
    assert data['status'] == 'merged'
    runtime.restart()
    loaded = attach(runtime)
    assert loaded.clip(REF).start == tl.clip(REF).start
    stored = runtime.client().timelines.open_bundle(runtime.project, 'film', revision_id=accepted['new_head'])
    assert State(stored.data['bundle']).entity(REF) == loaded.clip(REF).data
    loaded.undo([accepted['receipt']['receipt_id']])
    assert loaded.clip(REF).data == original
    assert State(loaded.bundle).entity('parent/track:picture')['label'] == 'Peer label'


def test_http_original_sdk_parent_hold_publish_reload_and_undo(runtime):
    seed(runtime)
    tl = attach(runtime)
    original = copy.deepcopy(tl.clip(REF).data)
    clip = tl.clip(REF)
    clip.hold_for(2)
    captured = json.loads(tl.session.outbox.path.read_bytes())['pending']
    assert len(captured) == 1
    assert captured[0]['op'] == 'clip.set'
    assert captured[0]['select'] == REF
    assert captured[0]['args'] == {'values': {'hold': 2.0}, 'clear': []}

    accepted = tl.publish('Hold the opening', idempotency_key='sdk-parent-hold')
    body = json.loads(runtime.calls[-1][0])
    assert body['commands'] == captured
    assert accepted['receipt']['result']['commands'] == captured
    runtime.restart()
    loaded = attach(runtime)
    assert loaded.clip(REF).data == dict(original, hold=2.0)
    loaded.undo([accepted['receipt']['receipt_id']])
    assert loaded.clip(REF).data == original


def test_http_original_sdk_parent_asset_clear_lost_ack_reload_retry_undo(
    tmp_path, trim_film, wire, real_runtime,
):
    fixture, _, _ = trim_film
    observed, lose = wire
    support = tmp_path / 'support'
    outbox = tmp_path / 'owned-asset-clear.json'

    def start(port=0):
        return real_runtime(fixture.duplicate_root, support_root=support,
                            port=port, authoring_profile='astrid')

    daemon = start()
    client = m1.open_client(daemon)
    tl = Checkout.attach(TimelineSession.open(
        fixture.project_id, fixture.timeline_id, client=client, outbox_path=outbox))
    original = copy.deepcopy(tl.clip('parent/clip:music-bed').data)
    tl.clip('parent/clip:music-bed').clear_asset()
    captured = json.loads(outbox.read_bytes())['pending']
    assert captured[0]['op'] == 'clip.set'
    assert captured[0]['select'] == 'parent/clip:music-bed'
    assert captured[0]['args'] == {'values': {}, 'clear': ['asset']}
    assert 'asset' not in tl.clip('parent/clip:music-bed').data

    lose.add('owned-asset-clear')
    with pytest.raises(WorkspaceClientError):
        tl.publish('Clear the source', idempotency_key='owned-asset-clear')
    first = copy.deepcopy(observed[-1])
    durable = json.loads(outbox.read_bytes())
    assert json.loads(first['body'])['commands'] == captured
    assert json.loads(first['body'])['rationale'] == 'Clear the source'
    assert durable['flight']['body'].encode() == first['body']
    assert durable['flight']['key'] == 'owned-asset-clear'

    port = daemon.httpd.server_port
    daemon.stop()
    daemon = start(port)
    loaded = Checkout.attach(TimelineSession.open(
        fixture.project_id, fixture.timeline_id, client=m1.open_client(daemon),
        outbox_path=outbox))
    retried = loaded.publish()
    assert observed[-1]['body'] == first['body']
    assert observed[-1]['key'] == first['key']
    assert retried['receipt'] == first['response']['receipt']
    assert retried['message'] == 'Clear the source'
    assert 'asset' not in loaded.clip('parent/clip:music-bed').data

    loaded.undo([retried['receipt']['receipt_id']])
    assert loaded.clip('parent/clip:music-bed').data == original


def test_http_original_sdk_parent_transition_clear_lost_ack_reload_retry_undo(
    tmp_path, transition_film, wire, real_runtime,
):
    fixture, _, _ = transition_film
    observed, lose = wire
    support = tmp_path / 'support'
    outbox = tmp_path / 'owned-transition-clear.json'

    def start(port=0):
        return real_runtime(fixture.duplicate_root, support_root=support,
                            port=port, authoring_profile='astrid')

    daemon = start()
    client = m1.open_client(daemon)
    session = TimelineSession.open(fixture.project_id, fixture.timeline_id,
                                   client=client, outbox_path=outbox)
    tl = Checkout.attach(session)
    original = copy.deepcopy(tl.clip('parent/clip:music-bed').data)
    peer = Checkout.attach(TimelineSession.open(
        fixture.project_id, fixture.timeline_id, client=client,
        outbox_path=tmp_path / 'peer.json'))
    peer.clip('music-other').on(2)
    peer.flush(idempotency_key='owned-transition-peer')

    clip = tl.clip('parent/clip:music-bed')
    clip.set(transition=None)
    captured = json.loads(outbox.read_bytes())['pending']
    assert captured[0]['op'] == 'clip.set'
    assert captured[0]['select'] == 'parent/clip:music-bed'
    assert captured[0]['args'] == {'values': {}, 'clear': ['transition']}
    assert 'transition' not in clip.data

    lose.add('owned-transition-clear')
    with pytest.raises(WorkspaceClientError):
        tl.publish('Clear the transition', idempotency_key='owned-transition-clear')
    first = copy.deepcopy(observed[-1])
    durable = json.loads(outbox.read_bytes())
    assert json.loads(first['body'])['commands'] == captured
    assert json.loads(first['body'])['rationale'] == 'Clear the transition'
    assert durable['flight']['body'].encode() == first['body']
    assert durable['flight']['key'] == 'owned-transition-clear'

    port = daemon.httpd.server_port
    daemon.stop()
    daemon = start(port)
    loaded = Checkout.attach(TimelineSession.open(
        fixture.project_id, fixture.timeline_id, client=m1.open_client(daemon),
        outbox_path=outbox))
    retried = loaded.publish()
    assert observed[-1]['body'] == first['body']
    assert observed[-1]['key'] == first['key']
    assert retried['receipt'] == first['response']['receipt']
    assert retried['message'] == 'Clear the transition'
    assert 'transition' not in loaded.clip('parent/clip:music-bed').data
    assert loaded.clip('music-other').start == 2

    loaded.undo([retried['receipt']['receipt_id']])
    assert loaded.clip('parent/clip:music-bed').data == original
    assert loaded.clip('music-other').start == 2


def test_http_original_sdk_parent_trim_stale_rebase_lost_ack_reload_retry_undo(
    tmp_path, trim_film, wire, real_runtime,
):
    fixture, _, _ = trim_film
    observed, lose = wire
    support = tmp_path / 'support'
    outbox = tmp_path / 'owned-trim.json'

    def start(port=0):
        return real_runtime(fixture.duplicate_root, support_root=support,
                            port=port, authoring_profile='astrid')

    daemon = start()
    client = m1.open_client(daemon)
    session = TimelineSession.open(fixture.project_id, fixture.timeline_id,
                                   client=client, outbox_path=outbox)
    tl = Checkout.attach(session)
    original = copy.deepcopy(tl.clip('parent/clip:music-bed').data)
    peer = Checkout.attach(TimelineSession.open(
        fixture.project_id, fixture.timeline_id, client=client,
        outbox_path=tmp_path / 'peer.json'))
    peer.clip('music-other').on(2)
    peer.flush(idempotency_key='owned-trim-peer')

    clip = tl.clip('parent/clip:music-bed')
    clip.trim(1.0000004, 4.0000006)
    captured = json.loads(outbox.read_bytes())['pending']
    assert captured[0]['op'] == 'clip.trim'
    assert captured[0]['select'] == 'parent/clip:music-bed'
    assert clip.data['from'] == 1.0
    assert clip.data['to'] == 4.000001
    assert clip.data['hold'] == 3.000001

    lose.add('owned-trim')
    with pytest.raises(WorkspaceClientError):
        tl.publish('Trim the opening', idempotency_key='owned-trim')
    first = copy.deepcopy(observed[-1])
    durable = json.loads(outbox.read_bytes())
    assert json.loads(first['body'])['commands'] == captured
    assert json.loads(first['body'])['rationale'] == 'Trim the opening'
    assert durable['flight']['body'].encode() == first['body']
    assert durable['flight']['key'] == 'owned-trim'

    port = daemon.httpd.server_port
    daemon.stop()
    daemon = start(port)
    loaded = Checkout.attach(TimelineSession.open(
        fixture.project_id, fixture.timeline_id, client=m1.open_client(daemon),
        outbox_path=outbox))
    retried = loaded.publish()
    assert observed[-1]['body'] == first['body']
    assert observed[-1]['key'] == first['key']
    assert retried['receipt'] == first['response']['receipt']
    assert retried['message'] == 'Trim the opening'
    assert retried['receipt']['result']['status'] == 'rebased'
    assert loaded.clip('parent/clip:music-bed').data['from'] == 1.0
    assert loaded.clip('parent/clip:music-bed').data['to'] == 4.000001
    assert loaded.clip('parent/clip:music-bed').data['hold'] == 3.000001
    assert loaded.clip('music-other').start == 2

    loaded.undo([retried['receipt']['receipt_id']])
    assert loaded.clip('parent/clip:music-bed').data == original
    assert loaded.clip('music-other').start == 2


def test_http_original_sdk_parent_split_stale_rebase_lost_ack_reload_retry_undo(
    tmp_path, split_film, wire, real_runtime,
):
    fixture, _, _ = split_film
    observed, lose = wire
    support = tmp_path / 'support'
    outbox = tmp_path / 'owned-split.json'

    def start(port=0):
        return real_runtime(fixture.duplicate_root, support_root=support,
                            port=port, authoring_profile='astrid')

    daemon = start()
    client = m1.open_client(daemon)
    session = TimelineSession.open(fixture.project_id, fixture.timeline_id,
                                   client=client, outbox_path=outbox)
    tl = Checkout.attach(session)
    original = copy.deepcopy(tl.clip('parent/clip:music-bed').data)
    peer = TimelineSession.open(fixture.project_id, fixture.timeline_id,
                                client=client, outbox_path=tmp_path / 'peer.json')
    peer.commit(ingress.label('Peer label'), idempotency_key='owned-split-peer')

    clip = tl.clip('parent/clip:music-bed')
    clip.split(1, id='music-bed-right')
    captured = json.loads(outbox.read_bytes())['pending']
    assert captured[0]['op'] == 'clip.split'
    assert captured[0]['select'] == 'parent/clip:music-bed'
    assert captured[0]['args'] == {'on': 1, 'right_id': 'music-bed-right'}
    assert captured[0]['created'] == ['parent/clip:music-bed-right']
    assert tl.clip('parent/clip:music-bed-right').data['speed'] == original['speed'] == 2
    assert tl.clip('parent/clip:music-bed-right').data['hold'] == 3.5

    lose.add('owned-split')
    with pytest.raises(WorkspaceClientError):
        tl.publish('Split the opening', idempotency_key='owned-split')
    first = copy.deepcopy(observed[-1])
    durable = json.loads(outbox.read_bytes())
    assert json.loads(first['body'])['commands'] == captured
    assert json.loads(first['body'])['rationale'] == 'Split the opening'
    assert durable['flight']['body'].encode() == first['body']
    assert durable['flight']['key'] == 'owned-split'

    port = daemon.httpd.server_port
    daemon.stop()
    daemon = start(port)
    loaded = Checkout.attach(TimelineSession.open(
        fixture.project_id, fixture.timeline_id, client=m1.open_client(daemon),
        outbox_path=outbox))
    retried = loaded.publish()
    assert observed[-1]['body'] == first['body']
    assert observed[-1]['key'] == first['key']
    assert retried['receipt'] == first['response']['receipt']
    assert retried['message'] == 'Split the opening'
    assert retried['receipt']['result']['status'] == 'rebased'
    assert loaded.clip('parent/clip:music-bed-right').data['from'] == 2
    assert loaded.clip('parent/clip:music-bed-right').data['to'] == 5.5
    assert loaded.clip('parent/clip:music-bed-right').data['hold'] == 3.5
    assert State(loaded.bundle).entity('parent/track:picture')['label'] == 'Peer label'

    loaded.undo([retried['receipt']['receipt_id']])
    assert loaded.clip('parent/clip:music-bed').data == original
    assert State(loaded.bundle).entity('parent/clip:music-bed-right', optional=True) is None
    assert State(loaded.bundle).entity('parent/track:picture')['label'] == 'Peer label'


def test_http_original_sdk_parent_delete_stale_rebase_lost_ack_reload_retry_undo(
    tmp_path, delete_film, wire, real_runtime,
):
    fixture, _, _ = delete_film
    observed, lose = wire
    support = tmp_path / 'support'
    outbox = tmp_path / 'owned-delete.json'

    def start(port=0):
        return real_runtime(fixture.duplicate_root, support_root=support,
                            port=port, authoring_profile='astrid')

    daemon = start()
    client = m1.open_client(daemon)
    session = TimelineSession.open(fixture.project_id, fixture.timeline_id,
                                   client=client, outbox_path=outbox)
    tl = Checkout.attach(session)
    original = copy.deepcopy(tl.clip('parent/clip:music-bed').data)
    peer = Checkout.attach(TimelineSession.open(
        fixture.project_id, fixture.timeline_id, client=client,
        outbox_path=tmp_path / 'peer.json'))
    peer.clip('music-other').on(2)
    peer.flush(idempotency_key='owned-delete-peer')

    clip = tl.clip('parent/clip:music-bed')
    clip.remove()
    captured = json.loads(outbox.read_bytes())['pending']
    assert captured[0]['op'] == 'clip.delete'
    assert captured[0]['select'] == 'parent/clip:music-bed'
    assert captured[0]['args'] == {}
    assert not State(tl.bundle).entity('parent/clip:music-bed', optional=True)

    lose.add('owned-delete')
    with pytest.raises(WorkspaceClientError):
        tl.publish('Remove the opening', idempotency_key='owned-delete')
    first = copy.deepcopy(observed[-1])
    durable = json.loads(outbox.read_bytes())
    assert json.loads(first['body'])['commands'] == captured
    assert json.loads(first['body'])['rationale'] == 'Remove the opening'
    assert durable['flight']['body'].encode() == first['body']
    assert durable['flight']['key'] == 'owned-delete'

    port = daemon.httpd.server_port
    daemon.stop()
    daemon = start(port)
    loaded = Checkout.attach(TimelineSession.open(
        fixture.project_id, fixture.timeline_id, client=m1.open_client(daemon),
        outbox_path=outbox))
    retried = loaded.publish()
    assert observed[-1]['body'] == first['body']
    assert observed[-1]['key'] == first['key']
    assert retried['receipt'] == first['response']['receipt']
    assert retried['message'] == 'Remove the opening'
    assert retried['receipt']['result']['status'] == 'rebased'
    assert not State(loaded.bundle).entity('parent/clip:music-bed', optional=True)
    assert loaded.clip('music-other').start == 2

    loaded.undo([retried['receipt']['receipt_id']])
    assert loaded.clip('parent/clip:music-bed').data == original
    assert loaded.clip('music-other').start == 2


def test_http_lost_ack_restart_exact_retry_and_unrelated_global_nonconsumption(runtime):
    seed(runtime)
    tl = attach(runtime)
    tl.clip(REF).on(2)
    runtime.lost.add('sdk-once')
    with pytest.raises(WorkspaceClientError):
        tl.publish('Opening', idempotency_key='sdk-once')
    first = copy.deepcopy(runtime.calls[-1])
    path = tl.session.outbox.path
    durable = path.read_bytes()
    count = len(runtime.history()['items'])
    runtime.restart()
    # A CLI/global move is distinct from this explicitly selected draft.
    global_session = TimelineSession.open(runtime.project, 'film', client=runtime.client())
    global_session.queue_move(REF, 4)
    global_bytes = global_session.outbox.path.read_bytes()
    calls = len(runtime.calls)
    with pytest.raises(TimelineEditError, match='held'):
        Checkout.attach(global_session).publish()
    with pytest.raises(TimelineEditError, match='held'):
        attach(runtime, 'unrelated').publish()
    assert path.read_bytes() == durable
    assert global_session.outbox.path.read_bytes() == global_bytes
    assert len(runtime.calls) == calls and runtime.calls[-1] == first
    loaded = attach(runtime)
    with pytest.raises(TimelineEditError, match='immutable'):
        loaded.publish('Changed reason')
    with pytest.raises(TimelineEditError, match='immutable'):
        loaded.publish(idempotency_key='changed-key')
    assert path.read_bytes() == durable
    accepted = loaded.publish()
    assert runtime.calls[-1][:2] == first[:2]
    assert accepted['receipt'] == first[3]['receipt']
    assert accepted['message'] == 'Opening' and len(runtime.history()['items']) == count
    assert global_session.outbox.path.read_bytes() == global_bytes
    assert json.loads(path.read_bytes())['pending'] == []


def test_http_ambiguous_owner_and_mixed_edits_keep_original_draft(
    tmp_path, film, real_runtime,
):
    fixture, parent, _ = film
    daemon = real_runtime(fixture.duplicate_root, support_root=tmp_path / 'support', authoring_profile='astrid')
    client = m1.open_client(daemon)
    session = TimelineSession.open(fixture.project_id, fixture.timeline_id,
                                  client=client, outbox_path=tmp_path / 'owned.json')
    original = session._bundle(json.loads(session.outbox.path.read_bytes()))
    owner = 'occurrence:' + parent['occurrences'][0]['occurrence_id']
    identity = State(original).collection(owner, 'clip')[0]['id']
    # The same display ID belongs to two different owners in a real closure.
    session.commit_many([
        ingress.command('track.create', 'parent', {'row': {'id': 'titles', 'kind': 'visual'}}),
        ingress.command('clip.add', 'parent', {'row': {'id': identity, 'clipType': 'am-type',
            'track': 'titles', 'at': 0, 'hold': 1, 'params': {'text': 'Parent'}}}),
    ])
    tl = Checkout.attach(session)
    durable, draft = session.outbox.path.read_bytes(), copy.deepcopy(tl.bundle)
    with pytest.raises(TimelineEditError, match='one exact clip'):
        tl.clip(identity).on(2)
    assert session.outbox.path.read_bytes() == durable and tl.bundle == draft
    ref = 'parent/clip:' + identity
    tl.clip(ref).on(2)
    tl.clip(ref).set(text='Local mixed edit')
    durable, draft = tl.session.outbox.path.read_bytes(), copy.deepcopy(tl.bundle)
    head = copy.deepcopy(m1.history(client, fixture)['head'])
    with pytest.raises(TimelineEditError, match='held'):
        tl.publish('Mixed')
    assert tl.session.outbox.path.read_bytes() == durable and tl.bundle == draft
    assert m1.history(client, fixture)['head'] == head
