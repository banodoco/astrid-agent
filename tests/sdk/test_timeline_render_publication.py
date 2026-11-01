"""Requester render handoff; generated acceptance/admission remain transport fakes."""
from __future__ import annotations

import copy
import hashlib
import json

import pytest

from astrid.core.receipts.canonical import canonical_json
from astrid.packs.rendering.shared.runtime_stitch import publish_authoring_proposal
from astrid.sdk.contracts import DomainResult
from astrid.sdk.remote import RemoteTasks
from astrid.sdk.timeline.assembly import read_assembly_proposal
from astrid.sdk.timeline.capture import capture_move
from astrid.sdk.timeline.sync import TimelineSession
from astrid.sdk.workspace_client import WorkspaceClientError
from tests.sdk.test_timeline_assembly import DIGEST, _admit, _requester
from tests.sdk.test_timeline_producer_guard import proposal_for
from tests.sdk.test_timeline_sync import initial_bundle


def requester(tmp_path):
    session, generated, selectors, _, admissions = _requester(tmp_path)
    _admit(session, selectors)
    target = admissions[0]['spec']['runtime_dependencies']['publication']['admitted_target']
    proposal = proposal_for(target, [capture_move(initial_bundle(), 'parent/clip:a', 3)])
    proposal['producer']['task_id'] = 'assembler'
    proposal['proposal_digest'] = 'sha256:' + hashlib.sha256(canonical_json({
        key: value for key, value in proposal.items() if key != 'proposal_digest'
    }).encode()).hexdigest()
    raw = canonical_json(proposal).encode()
    object_id = 'sha256:' + hashlib.sha256(raw).hexdigest()
    output = {'name': 'authoring_proposal', 'kind': 'object', 'digest': object_id,
              'size': len(raw), 'media_type': 'application/json'}
    transport = session.client._remote._transport
    transport.get_task = lambda task_id: {
        'task_id': task_id, 'project_id': 'p', 'run_id': 'run', 'state': 'succeeded',
        'attempt_id': 'attempt', 'capability_id': 'rendering.assemble_timeline',
        'lease_fence': 1, 'runtime_epoch': 2,
        'spec': {'spec': admissions[0]['spec']}, 'result': {'outputs': [output]},
    }
    association = {'task_id': 'assembler', 'attempt_id': 'attempt', 'project_id': 'p',
                   'run_id': 'run', 'output_port': 'authoring_proposal',
                   'object_id': object_id, 'size': len(raw), 'media_type': 'application/json',
                   'role': 'output', 'group_key': 'default', 'variant_key': '0', 'ordinal': 0,
                   'durability': 'durable', 'lifecycle': {'state': 'available'},
                   'provenance': {**proposal['producer'], 'capability_id': 'rendering.assemble_timeline'}}
    transport.list_managed_outputs = lambda task_id: ([copy.deepcopy(association)], None)
    transport.get_object = lambda digest: {'data': raw}
    assert read_assembly_proposal(session, "assembler", "attempt") == proposal
    session.client.tasks = RemoteTasks(transport)
    renders = []

    def create(**body):
        renders.append(copy.deepcopy(body))
        return DomainResult.success({'task_id': 'render-1'}, idempotency_key=body['idempotency_key'])

    session.client.tasks.create = create
    return session, generated, proposal, renders, association


def publish(session, proposal, **options):
    return publish_authoring_proposal(
        session.client, proposal=proposal, timeline_id='t', expected_version=1,
        render_capability_digest=DIGEST, output_name='chain.mp4',
        requester_session=session, idempotency_key='publish', **options,
    )


def test_registered_requester_commits_original_target_then_admits_exact_renderer(tmp_path):
    session, generated, proposal, renders, _ = requester(tmp_path)
    result = publish(session, proposal, merge='atomic', reads=[])
    body = json.loads(generated.calls[0][0])
    assert (body['project_id'], body['timeline_id'], body['base_revision_id'],
            body['base_publication_id']) == ('p', 't', 'r0', 'pub0')
    assert body['commands'][0]['op'] == 'producer.guard'
    assert body['commands'][1:] == proposal['commands']
    assert body['commands'][0]['args']['pin']['fence'] == 1
    assert body['label'] == 'Assemble timeline' and body['merge'] == 'atomic'
    assert body['reads'] == [] and body['dry_run'] is False
    assert body['source'] == {'via': 'python'}
    assert 'group_id' not in body
    assert body['steps'] == [{'id': 'assembly', 'label': 'Assemble timeline',
                              'intent': proposal['rationale'], 'tags': [],
                              'command_ordinals': [0, 1], 'patch': []}]
    assert result['receipt'] == generated.accepted['publish'][1]['receipt']
    assert result['render_task_id'] == 'render-1'
    render, = renders
    assert render['capability'] == 'rendering.render'
    assert render['idempotency_key'] == 'publish:render'
    pin = render['spec']['params']['canonical_revision']
    assert pin == {'project_id': 'p', 'timeline_id': 't', 'revision_id': 'r1',
                   'publication_id': 'pub1', 'content_digest':
                   result['data']['canonical_document']['parent_revision']['content_digest']}
    assert render['spec']['params']['output_name'] == 'chain.mp4'
    assert 'config' not in render['spec'] and 'timeline_snapshot' not in render['spec']


def test_requester_lost_commit_ack_retries_identical_v2_flight_after_restart(tmp_path):
    session, generated, proposal, renders, _ = requester(tmp_path)
    generated.lose_ack = True
    with pytest.raises(WorkspaceClientError):
        publish(session, proposal, group_id='stitch-group')
    assert not renders and len(generated.accepted) == 1
    saved = session.outbox.read()['flight']
    restarted = TimelineSession.open('p', 't', client=session.client, outbox_path=generated.path)
    result = publish(restarted, proposal, group_id='stitch-group')
    assert generated.calls[0] == generated.calls[1]
    assert saved['body'] == generated.calls[0][0].decode()
    assert json.loads(generated.calls[0][0])['group_id'] == 'stitch-group'
    assert json.loads(saved['body'])['group_id'] == 'stitch-group'
    assert saved['key'] == 'publish' and result['render_task_id'] == 'render-1'
    assert len(generated.accepted) == 1


def test_changed_group_id_cannot_replace_same_pending_assembly_request(tmp_path):
    session, generated, proposal, renders, _ = requester(tmp_path)
    generated.lose_ack = True
    with pytest.raises(WorkspaceClientError):
        publish(session, proposal, group_id='stitch-group')
    sealed = session.outbox.read()
    with pytest.raises(WorkspaceClientError, match='retry the original assembly commit'):
        publish(session, proposal, group_id='different-group')
    assert session.outbox.read() == sealed
    assert len(generated.calls) == 1 and not renders


def test_lost_render_ack_replays_original_acceptance_after_peer_head_moves(tmp_path):
    session, generated, proposal, renders, _ = requester(tmp_path)
    create = session.client.tasks.create

    def lose_ack(**body):
        create(**body)
        raise WorkspaceClientError(0, 'transport_error', 'lost render acknowledgement')

    session.client.tasks.create = lose_ack
    with pytest.raises(WorkspaceClientError, match='lost render acknowledgement'):
        publish(session, proposal)
    generated.head = {'revision_id': 'peer', 'publication_id': 'peer-pub'}
    session.client.tasks.create = create
    result = publish(session, proposal)
    assert result['data']['accepted_revision_id'] == 'r1'
    assert len(generated.accepted) == 1
    assert generated.calls[0] == generated.calls[1]
    assert renders[0] == renders[1]


def test_preview_never_admits_renderer_or_advances_base(tmp_path):
    session, generated, proposal, renders, _ = requester(tmp_path)
    result = publish(session, proposal, dry_run=True)
    assert result['data']['status'] == 'preview' and result['receipt'] is None
    assert not renders and not generated.accepted
    assert session.outbox.read()['base'] == {'revision_id': 'r0', 'publication_id': 'pub0'}


@pytest.mark.parametrize('status', ['conflict', 'invalid'])
def test_runtime_refusal_keeps_full_structured_details_and_never_admits_render(tmp_path, status):
    from astrid_runtime_client import ApiError

    session, generated, proposal, renders, _ = requester(tmp_path)
    details = {'status': status, 'reason': 'producer_attempt_mismatch',
               'head': {'revision_id': 'peer', 'publication_id': 'peer-pub'},
               'code': 'producer_attempt_mismatch', 'path': ['commands', 0],
               'hint': 'Read the original settled assembly',
               'suggested_command': 'astrid timelines show t --project p'}
    generated.refusal = ApiError(409, 'conflict', 'changed producer', details=details)
    with pytest.raises(WorkspaceClientError) as error:
        publish(session, proposal)
    assert error.value.details == details
    assert not renders and not generated.accepted
    state = session.outbox.read()
    assert state['flight'] is None
    pending, = state['pending_changes'].values()
    assert pending['state'] == 'pending'
    assert pending['flight'] is not None
    assert pending['failure']['details'] == details


def test_video_editing_production_wrapper_uses_requester_commit(tmp_path):
    from astrid.packs.video_editing.shared.runtime_orchestration import publish_stitched_render

    session, generated, proposal, renders, _ = requester(tmp_path)
    result = publish_stitched_render(
        session.client, project='p', timeline_ref='t', expected_version=1,
        proposal=proposal, requester_session=session, render_capability_digest=DIGEST,
        group_id='wrapper-group', idempotency_key='wrapper',
    )
    assert result['render_task_id'] == 'render-1'
    assert generated.calls[0][1] == 'wrapper'
    assert json.loads(generated.calls[0][0])['group_id'] == 'wrapper-group'
    assert renders[0]['idempotency_key'] == 'wrapper:render'


def test_accepted_retry_never_rereads_mutable_producer_lifecycle(tmp_path):
    session, generated, proposal, renders, _ = requester(tmp_path)
    first = publish(session, proposal)

    def gone(*_args):
        raise AssertionError('accepted replay consulted mutable producer lifecycle')

    transport = session.client._remote._transport
    transport.get_task = transport.list_managed_outputs = transport.get_object = gone
    second = publish(session, proposal)
    assert first == second and len(generated.accepted) == 1
    assert generated.calls[0] == generated.calls[1]
    assert renders[0] == renders[1]


@pytest.mark.parametrize('dry_run', [False, True])
def test_v2_authoritative_fields_survive_the_publication_envelope(tmp_path, dry_run):
    session, generated, proposal, renders, _ = requester(tmp_path)
    commit = generated.commit_timeline
    authoritative = {'label': 'Assembly', 'merge_mode': 'atomic', 'steps': [{
        'id': 'assembly', 'label': 'Assembly', 'intent': 'two children', 'tags': ['assembly'],
        'command_ordinals': [0, 1], 'patch': [{'authoritative': 'effect'}],
        'dependencies': [{'authoritative': 'read'}]}]}

    def response(*args, **kwargs):
        result = commit(*args, **kwargs)
        result['data'].update(copy.deepcopy(authoritative))
        if result['receipt'] is not None:
            result['receipt']['result'] = copy.deepcopy(result['data'])
        return result

    generated.commit_timeline = response
    result = publish(session, proposal, dry_run=dry_run)
    for key, value in authoritative.items():
        assert result['data'][key] == value
        if not dry_run:
            assert result['receipt']['result'][key] == value
    assert bool(renders) is not dry_run


def test_original_step_reads_and_tags_are_sent_without_filtering(tmp_path):
    session, generated, proposal, _, _ = requester(tmp_path)
    steps = [{'id': 'assembly', 'label': 'Assembly', 'intent': 'Keep the original intent',
              'tags': ['assembly'], 'command_ordinals': [0, 1], 'patch': [], 'reads': []}]
    publish(session, proposal, steps=steps, merge='atomic', reads=[])
    body = json.loads(generated.calls[0][0])
    assert body['steps'] == steps and body['reads'] == []


@pytest.mark.parametrize('status', ['merged', 'rebased'])
def test_landed_peer_state_and_original_merge_spelling_survive_replay(tmp_path, status):
    session, generated, proposal, renders, _ = requester(tmp_path)
    peer = initial_bundle()
    peer['parent']['peer'] = {'opaque': [None, False, 'keep peer']}
    generated.bundles['peer'] = peer
    generated.head = {'revision_id': 'peer', 'publication_id': 'peer-pub'}
    commit = generated.commit_timeline

    def response(*args, **kwargs):
        result = commit(*args, **kwargs)
        result['data']['status'] = status
        result['receipt']['result']['status'] = status
        return result

    generated.commit_timeline = response
    result = publish(session, proposal)
    assert result['data']['status'] == result['receipt']['result']['status'] == status
    parent = result['data']['canonical_document']['parent_revision']['payload']
    assert parent['peer'] == peer['parent']['peer']
    assert next(row for row in parent['clips'] if row['id'] == 'a')['at'] == 3
    assert result['data']['rebased_onto'] == {'revision_id': 'peer', 'publication_id': 'peer-pub'}
    assert publish(session, proposal) == result
    assert len(generated.accepted) == 1 and renders[0] == renders[1]


@pytest.mark.integration
def test_requester_render_reaches_real_task_admission_after_commit(real_runtime, tmp_path, monkeypatch):
    """The mounted requester path uses the real RemoteTasks admission method."""
    from tests.editing_conformance.test_commit_runtime_m1 import open_client
    from tests.helpers.runtime import use_runtime, vendored_runtime_commit

    runtime = use_runtime(commit=vendored_runtime_commit())
    monkeypatch.syspath_prepend(str(runtime / "tests"))
    from timeline_commit_film_fixture import build_film_fixture

    fixture = build_film_fixture(tmp_path)
    daemon = real_runtime(
        fixture.duplicate_root, support_root=tmp_path / "support",
        commit=vendored_runtime_commit(), authoring_profile="astrid",
    )
    daemon.service.register_capability({
        "capability_id": "rendering.render", "definition_digest": DIGEST, "status": "ready",
    })
    client = open_client(daemon)
    from astrid.sdk.remote import RemoteTasks
    from astrid.sdk.workspace_client import WorkspaceClient

    assert isinstance(client._remote._transport, WorkspaceClient)
    assert isinstance(client.tasks, RemoteTasks)
    session = TimelineSession.open(
        fixture.project_id, fixture.timeline_id, client=client,
        outbox_path=tmp_path / "real-render-outbox.json",
    )
    state = session._state()
    bundle = session._bundle(state)
    clip = bundle["parent"]["clips"][0]
    command = capture_move(bundle, f"parent/clip:{clip['id']}", 2)
    body = {
        "api_version": 1, "project_id": fixture.project_id, "timeline_id": fixture.timeline_id,
        "base_revision_id": state["base"]["revision_id"],
        "base_publication_id": state["base"]["publication_id"],
        "commands": [command], "label": "Real requester render control",
        "steps": [{"id": "edit", "label": "Real requester render control", "intent": "control",
                    "tags": [], "command_ordinals": [0], "patch": []}],
        "merge": "atomic", "reads": [], "dry_run": False, "source": {"via": "python"},
    }
    proposal = {
        "admitted_target": {"project_id": fixture.project_id, "timeline_id": fixture.timeline_id,
                            **state["base"]},
        "producer": {"attempt_id": "attempt"},
    }

    def requester_commit(_attempt_id, _proposal, *, idempotency_key, **_options):
        result = client.timelines.commit(
            fixture.project_id, fixture.timeline_id, body, idempotency_key=idempotency_key,
        )
        assert result.ok, result.as_dict()
        return result

    client.tasks.publish_timeline_render = requester_commit
    first = publish_authoring_proposal(
        client, proposal=proposal, timeline_id=fixture.timeline_id, expected_version=1,
        render_capability_digest=DIGEST, output_name="real.mp4", selector="rendering.ffmpeg",
        requester_session=session, idempotency_key="real-publish",
    )
    second = publish_authoring_proposal(
        client, proposal=proposal, timeline_id=fixture.timeline_id, expected_version=1,
        render_capability_digest=DIGEST, output_name="real.mp4", selector="rendering.ffmpeg",
        requester_session=session, idempotency_key="real-publish",
    )
    assert first["render_task_id"] == second["render_task_id"]
    assert daemon.service.store.conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE capability='rendering.render'"
    ).fetchone()[0] == 1
    task = daemon.service.task(first["render_task_id"])["task"]
    assert task["spec"]["spec"]["params"]["selector"] == "rendering.ffmpeg"
    assert task["spec"]["spec"]["params"]["output_name"] == "real.mp4"
    assert task["spec"]["spec"]["params"]["canonical_revision"]["revision_id"] == first["data"]["accepted_revision_id"]
