"""Attached public verbs through the real provider, with renderer unavailable.

Run centrally with commit-integration --files tests/editing_conformance/test_commit_runtime_native_checkout.py.
"""
from __future__ import annotations

import copy
import json
import subprocess

import pytest

from astrid.sdk.timeline.sync import CommitConflict, TimelineSession
from astrid.sdk.timeline_checkout import Checkout
from astrid.sdk.workspace_client import WorkspaceClientError
from tests.editing_conformance import test_commit_runtime_m1 as m1

base_film = m1.film
renderer_off, wire = m1.renderer_off, m1.wire
pytestmark = pytest.mark.integration


@pytest.fixture
def film(base_film):
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.timeline_commit_provider import load_timeline_commit_provider

    from astrid.sdk.timeline.commit import bundle_from_document
    from astrid.sdk.timeline.emitter import emit_document_commands

    fixture, parent, _ = base_film
    parent = copy.deepcopy(parent)
    # The historical audio row is media on an audio track. Trim consumes the
    # literal authored duration, without probing or fetching a mutable source.
    parent['clips'][0]['clipType'] = 'media'
    parent['registry']['assets']['music-bed']['duration'] = 5.5
    parent['config']['clips'] = copy.deepcopy(parent['clips'])
    service = RuntimeService(fixture.duplicate_root)
    try:
        service.register_timeline_commit_provider(load_timeline_commit_provider('astrid'))
        head = service.get_project_timeline(fixture.project_id, fixture.timeline_id)
        document = service._timeline_commits()[0].revision_closure(
            fixture.project_id, fixture.timeline_id, head['head_revision_id'])
        before = bundle_from_document(document)
        after = copy.deepcopy(before)
        after['parent'] = parent
        commands = emit_document_commands(before, after)
        body = {
            'api_version': 1, 'project_id': fixture.project_id, 'timeline_id': fixture.timeline_id,
            'base_revision_id': head['head_revision_id'], 'base_publication_id': head['head_publication_id'],
            'commands': commands, 'label': 'Native checkout baseline', 'merge': 'atomic', 'reads': [],
            'dry_run': False, 'source': {'via': 'python'}, 'rationale': 'Native checkout fixture',
            'group_id': 'native-checkout-baseline',
            'steps': [{'id': 'baseline', 'label': 'Native checkout baseline',
                       'intent': 'Prepare native checkout fixture', 'tags': ['integration'],
                       'command_ordinals': list(range(len(commands))),
                       'patch': [change for command in commands for change in command['args']['changes']],
                       'reads': []}],
        }
        baseline = service.commit_timeline(
            fixture.project_id, fixture.timeline_id, body,
            idempotency_key='native-checkout-baseline', publisher='owner',
            invocation_path=f'/v1/projects/{fixture.project_id}/timelines/{fixture.timeline_id}/commit',
            authorize=lambda: None,
        )
    finally:
        service.close()
    return fixture, parent, baseline


@pytest.fixture
def gate3_film(base_film):
    """Keep Gate3 on the captured, ordinary admitted closure.

    The shared ``film`` fixture changes the historical audio row to the
    legacy ``media`` shape for trim coverage.  That is useful for the
    neighbouring editing cases but the captured child closure also contains
    a historical partial element reference.  Gate3 publishes a fresh parent
    through Runtime with that child removed and a real built-in element
    declaration, so composed preflight reaches the unavailable executor.
    """
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.timeline_commit_provider import load_timeline_commit_provider

    from astrid.sdk.timeline.commit import bundle_from_document
    from astrid.sdk.timeline.emitter import emit_document_commands

    fixture, parent, _ = base_film
    parent = copy.deepcopy(parent)
    parent['occurrences'] = []
    parent['clips'].append({
        'id': 'gate3-title',
        'at': 0,
        'track': 'picture',
        'clipType': 'text-card',
        'hold': 1,
        'params': {'content': 'Gate3'},
        'elementRef': {'packId': 'rendering', 'kind': 'effects', 'id': 'text-card'},
    })
    parent['config']['clips'] = copy.deepcopy(parent['clips'])
    service = RuntimeService(fixture.duplicate_root)
    try:
        service.register_timeline_commit_provider(load_timeline_commit_provider('astrid'))
        head = service.get_project_timeline(fixture.project_id, fixture.timeline_id)
        document = service._timeline_commits()[0].revision_closure(
            fixture.project_id, fixture.timeline_id, head['head_revision_id'])
        before = bundle_from_document(document)
        after = copy.deepcopy(before)
        after['parent'] = parent
        commands = emit_document_commands(before, after)
        body = {
            'api_version': 1, 'project_id': fixture.project_id, 'timeline_id': fixture.timeline_id,
            'base_revision_id': head['head_revision_id'], 'base_publication_id': head['head_publication_id'],
            'commands': commands, 'label': 'Gate3 valid baseline', 'merge': 'atomic', 'reads': [],
            'dry_run': False, 'source': {'via': 'python'}, 'rationale': 'Gate3 valid fixture',
            'group_id': 'gate3-valid-baseline',
            'steps': [{'id': 'baseline', 'label': 'Gate3 valid baseline',
                       'intent': 'Prepare Gate3 fixture', 'tags': ['integration'],
                       'command_ordinals': list(range(len(commands))),
                       'patch': [change for command in commands for change in command['args']['changes']],
                       'reads': []}],
        }
        baseline = service.commit_timeline(
            fixture.project_id, fixture.timeline_id, body,
            idempotency_key='gate3-valid-baseline', publisher='owner',
            invocation_path=f'/v1/projects/{fixture.project_id}/timelines/{fixture.timeline_id}/commit',
            authorize=lambda: None,
        )
    finally:
        service.close()
    return fixture, parent, baseline


@pytest.fixture
def gate3_renderer_off(gate3_film, monkeypatch):
    m1._disable_scene(monkeypatch)


@pytest.mark.parametrize('conflicting', [False, True])
def test_attached_move_trim_lost_ack_restart_original_suffix_and_guarded_undo(
    tmp_path, monkeypatch, film, wire, real_runtime, conflicting,
):
    fixture, _, _ = film
    monkeypatch.setenv('ASTRID_RUNTIME_DATA_ROOT', str(tmp_path / 'client-data'))
    observed, lose = wire
    support = tmp_path / 'support'
    outbox = tmp_path / 'ordinary-outbox.json'

    def start(port=0):
        return real_runtime(fixture.duplicate_root, support_root=support, port=port, authoring_profile='astrid')

    def attach(client):
        return Checkout.attach(TimelineSession.open(fixture.project_id, fixture.timeline_id,
                                                    client=client, outbox_path=outbox))

    daemon = start()
    client = m1.open_client(daemon)
    tl = attach(client)
    clip = tl.clip('parent/clip:music-bed')
    clip.on(1)
    lose.add('ordinary-move')
    with pytest.raises(WorkspaceClientError):
        tl.flush(idempotency_key='ordinary-move')
    moved = copy.deepcopy(observed[-1]['response'])
    flight = json.loads(outbox.read_bytes())['flight']
    clip.trim(1, 4)
    suffix = json.loads(outbox.read_bytes())['pending'][1]
    assert suffix['op'] == 'clip.trim'
    assert clip.start == 1 and clip.data['hold'] == 3
    peer = Checkout.attach(TimelineSession.open(fixture.project_id, fixture.timeline_id,
                                               client=client, outbox_path=tmp_path / 'peer.json'))
    peer.clip('music-other').on(2)
    peer.flush()
    if conflicting:
        peer.clip('music-bed').trim(0, 5)
        peer.flush()
    port = daemon.httpd.server_port
    daemon.stop()
    daemon = start(port)
    client = m1.open_client(daemon)
    tl = attach(client)
    assert json.loads(outbox.read_bytes())['flight'] == flight
    retried = tl.flush()
    assert observed[-1]['response'] == moved
    assert retried['receipt'] == moved['receipt']
    assert retried['data']['accepted_publication_id'] == moved['receipt']['receipt_id']
    assert observed[-1]['body'] == flight['body'].encode()
    assert observed[-1]['key'] == flight['key']
    assert json.loads(outbox.read_bytes())['pending'] == [suffix]
    if conflicting:
        with pytest.raises(CommitConflict):
            tl.flush()
        assert json.loads(outbox.read_bytes())['pending'] == [suffix]
        assert tl.clip('music-bed').data['from'] == 1
        return
    accepted = tl.flush()
    assert observed[-1]['key'] != flight['key']
    assert accepted['data']['commands'] == [suffix]
    loaded = attach(client)
    assert loaded.clip('music-bed').data['hold'] == 3
    assert loaded.clip('music-other').start == 2
    undone = loaded.undo([accepted['receipt']['receipt_id']])
    assert loaded.clip('music-bed').start == 1
    assert loaded.clip('music-bed').data['hold'] == 5.5
    assert loaded.clip('music-other').start == 2
    loaded.undo([undone['receipt']['receipt_id']])
    assert loaded.clip('music-bed').data['hold'] == 3
    with pytest.raises(CommitConflict):
        loaded.undo([accepted['receipt']['receipt_id']])
    assert m1.history(client, fixture)['head']['publication_id'] != accepted['receipt']['receipt_id']


def test_attached_param_commands_keep_exact_cli_ingress_and_unchanged_write_history(
    tmp_path, monkeypatch, capsys, film, wire, real_runtime,
):
    fixture, _, _ = film
    monkeypatch.setenv('ASTRID_RUNTIME_DATA_ROOT', str(tmp_path / 'client-data'))
    daemon = real_runtime(fixture.duplicate_root, support_root=tmp_path / 'support', authoring_profile='astrid')
    client = m1.open_client(daemon)
    path = tmp_path / 'params-outbox.json'
    tl = Checkout.attach(TimelineSession.open(fixture.project_id, fixture.timeline_id,
                                              client=client, outbox_path=path))
    tl.clip('music-bed').set(opaque={'literal': [None, False, 0]}, _allow_new=True)
    captured = json.loads(path.read_bytes())
    commands_file = tmp_path / 'params.json'
    commands_file.write_text(json.dumps(captured['pending']))
    accepted = m1.cli(client, fixture, capsys, 'edit', 'commands-file', fixture.timeline_id,
                      str(commands_file), '--base-revision-id', captured['base']['revision_id'],
                      '--base-publication-id', captured['base']['publication_id'])
    assert accepted['data']['commands'] == captured['pending']
    # Reopen on a separate empty outbox: an unchanged public setter is still an
    # authored write, so it prevents undoing the earlier publication.
    other = Checkout.attach(TimelineSession.open(fixture.project_id, fixture.timeline_id,
                                                 client=client, outbox_path=tmp_path / 'same.json'))
    other.clip('music-bed').set(opaque={'literal': [None, False, 0]})
    again = other.flush()
    assert again['data']['accepted_revision_id'] == accepted['data']['accepted_revision_id']
    assert again['receipt']['receipt_id'] != accepted['receipt']['receipt_id']
    other.clip('music-bed').set(opaque=None)
    cleared = other.flush()
    assert 'opaque' not in other.clip('music-bed').params
    other.undo([cleared['receipt']['receipt_id']])
    assert other.clip('music-bed').params['opaque'] == {'literal': [None, False, 0]}
    with pytest.raises(CommitConflict):
        other.undo([accepted['receipt']['receipt_id']])


def test_authenticated_v1_commit_reload_undo_survives_stopped_renderer_and_preview_reports_unavailable(
    tmp_path, monkeypatch, gate3_film, gate3_renderer_off, real_runtime,
):
    """The Runtime door accepts authoring while the production renderer is stopped."""
    fixture, _, _ = gate3_film
    daemon = real_runtime(
        fixture.duplicate_root,
        support_root=tmp_path / 'support',
        authoring_profile='astrid',
    )
    client = m1.open_client(daemon)
    outbox = tmp_path / 'gate3-outbox.json'

    def attach():
        return Checkout.attach(TimelineSession.open(
            fixture.project_id,
            fixture.timeline_id,
            client=client,
            outbox_path=outbox,
        ))

    def renderer_process(*_args, **_kwargs):
        raise AssertionError('the stopped renderer must not be launched by commit, reload, undo, or preview')

    with monkeypatch.context() as stopped:
        stopped.setattr(subprocess, 'Popen', renderer_process)
        stopped.setattr(subprocess, 'run', renderer_process)

        editing = attach()
        editing.clip('parent/clip:music-bed').on(1)
        accepted = editing.flush(idempotency_key='gate3-stopped-renderer')
        assert accepted['data']['accepted_revision_id']

        reloaded = attach()
        assert reloaded.clip('parent/clip:music-bed').start == 1
        undone = reloaded.undo([accepted['receipt']['receipt_id']])
        assert undone['data']['accepted_revision_id']

        after_undo = attach()
        assert after_undo.clip('parent/clip:music-bed').start == 0
        preview = client.timelines.visualize(
            fixture.project_id,
            fixture.timeline_id,
            mode='composed',
            options={'formats': ['png']},
        )

    assert not preview.ok
    assert preview.error['sdk_error'] == 'CapabilityRuntimeError', preview.error
    assert preview.error['sdk_category'] == 'runtime'
    assert preview.error['code'] == 'not_found'
    assert preview.error['details']['capability_id'] == 'rendering.timeline_visualize'
    assert preview.error['message'] == 'capability is not registered'
