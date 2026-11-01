"""Requester render handoff, from guarded commit through completed render retrieval."""
from __future__ import annotations

import base64
import copy
import hashlib
import json

import pytest

from astrid.core.receipts.canonical import canonical_json
from astrid.packs.rendering.shared.runtime_stitch import publish_authoring_proposal
from astrid.sdk.contracts import DomainResult
from astrid.sdk.remote import RemoteTasks
from astrid.sdk.timeline.assembly import read_assembly_proposal
from astrid.sdk.timeline.capture import capture_move, capture_track_create
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
def test_requester_render_commits_exact_revision_settles_and_retrieves_over_real_daemon(
        real_runtime, tmp_path):
    """The requester route, render admission, settlement, and reads use the real daemon."""
    from tests.editing_conformance.test_commit_runtime_m1 import open_client
    from tests.helpers.runtime import executor_token, use_runtime, vendored_runtime_commit
    from astrid.sdk.timeline import commit
    from astrid.sdk.workspace_client import WorkspaceClient

    use_runtime(commit=vendored_runtime_commit())
    daemon = real_runtime(
        tmp_path / "realm", support_root=tmp_path / "support",
        commit=vendored_runtime_commit(), authoring_profile="astrid",
    )
    service = daemon.service
    project = service.create_project({"slug": "requester-render", "name": "Requester render"})["id"]
    service.create_timeline(project, "film", idempotency_key="timeline")
    timeline = service.get_project_timeline(project, "film")
    target = {
        "project_id": project,
        "timeline_id": "film",
        "base_revision_id": timeline["head_revision_id"],
        "base_publication_id": timeline["head_publication_id"],
    }
    producer_capability = "fixture.requester-proposal"
    producer_digest = commit._digest(producer_capability)
    child_capability = "fixture.requester-input"
    child_digest = commit._digest(child_capability)
    service.register_capability({"capability_id": producer_capability, "definition_digest": producer_digest})
    service.register_capability({"capability_id": child_capability, "definition_digest": child_digest})
    service.register_executor(
        {"executor_id": "proposal-worker", "capabilities": [producer_capability, child_capability]},
        idempotency_key="proposal-worker",
    )
    children = []
    for index in range(2):
        child = service.create_task({
            "project": project,
            "capability_id": child_capability,
            "capability_digest": child_digest,
            "input_object_ids": [],
            "spec": {"index": index},
            "idempotency_key": f"input-{index}",
        })
        claimed = service.claim_next({
            "executor_id": "proposal-worker",
            "capability_ids": [child_capability],
            "runtime_epoch": service.health()["runtime_epoch"],
        }, idempotency_key=f"claim-input-{index}")
        raw = commit._json({"index": index}).encode("utf-8")
        service.settle_attempt(claimed["attempt_id"], {
            **{key: claimed[key] for key in ("lease_id", "fence", "runtime_epoch")},
            "outputs": [{
                "name": "data",
                "output_port": "data",
                "kind": "object",
                "filename": "data.json",
                "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
                "size": len(raw),
                "media_type": "application/json",
                "durability": "durable",
                "data_base64": base64.b64encode(raw).decode("ascii"),
            }],
        }, idempotency_key=f"settle-input-{index}")
        children.append(child["task"]["id"])
    admitted = service.create_task({
        "project": project,
        "capability_id": producer_capability,
        "capability_digest": producer_digest,
        "input_object_ids": [],
        "idempotency_key": "proposal",
        "spec": {
            "runtime_dependencies": {
                "publication": {"admitted_target": target},
                "edges": [{
                    "from_task_id": child,
                    "to": "self",
                    "requires_event": "task.succeeded",
                    "fence": "runtime_task",
                } for child in children],
                "aggregation": {"kind": "ordered_cas_inputs"},
            },
            "outputs": {"authoring_proposal": {
                "type": "file",
                "artifact_type": "timeline/authoring-proposal",
            }},
        },
    })
    proposal_attempt = service.claim_next({
        "executor_id": "proposal-worker",
        "capability_ids": [producer_capability],
        "runtime_epoch": service.health()["runtime_epoch"],
    }, idempotency_key="claim-proposal")
    assert proposal_attempt["task_id"] == admitted["task"]["id"]
    document = service._timeline_commits()[0].revision_closure(
        project, "film", target["base_revision_id"]
    )
    proposal = {
        "schema_version": 1,
        "producer": {
            "task_id": proposal_attempt["task_id"],
            "attempt_id": proposal_attempt["attempt_id"],
            "executor_id": "proposal-worker",
            "fence": proposal_attempt["fence"],
            "runtime_epoch": proposal_attempt["runtime_epoch"],
        },
        "admitted_target": target,
        "rationale": "Requester guarded proposal",
        "commands": [capture_track_create(
            commit.bundle_from_document(document), "parent", {"id": "proposed", "kind": "visual"}
        )],
    }
    proposal["proposal_digest"] = commit._digest(proposal)
    proposal_raw = commit._json(proposal).encode("utf-8")
    service.settle_attempt(proposal_attempt["attempt_id"], {
        **{key: proposal_attempt[key] for key in ("lease_id", "fence", "runtime_epoch")},
        "outputs": [{
            "name": "authoring_proposal",
            "output_port": "authoring_proposal",
            "kind": "object",
            "filename": "proposal.json",
            "digest": "sha256:" + hashlib.sha256(proposal_raw).hexdigest(),
            "size": len(proposal_raw),
            "media_type": "application/json",
            "durability": "durable",
            "data_base64": base64.b64encode(proposal_raw).decode("ascii"),
        }],
    }, idempotency_key="settle-proposal")
    daemon.service.register_capability({
        "capability_id": "rendering.render", "definition_digest": DIGEST, "status": "ready",
    })
    client = open_client(daemon)
    from astrid.sdk.remote import RemoteTasks

    assert isinstance(client._remote._transport, WorkspaceClient)
    assert isinstance(client.tasks, RemoteTasks)
    session = TimelineSession.open(
        project, "film", client=client,
        outbox_path=tmp_path / "real-render-outbox.json",
    )
    first = publish_authoring_proposal(
        client, proposal=proposal, timeline_id="film", expected_version=1,
        render_capability_digest=DIGEST, output_name="real.mp4", selector="rendering.ffmpeg",
        requester_session=session, idempotency_key="real-publish",
    )
    second = publish_authoring_proposal(
        client, proposal=proposal, timeline_id="film", expected_version=1,
        render_capability_digest=DIGEST, output_name="real.mp4", selector="rendering.ffmpeg",
        requester_session=session, idempotency_key="real-publish",
    )
    assert first["render_task_id"] == second["render_task_id"]
    assert first["data"]["publisher"] == "owner"
    assert daemon.service.store.conn.execute(
        "SELECT COUNT(*) FROM tasks WHERE capability='rendering.render'"
    ).fetchone()[0] == 1
    task = daemon.service.task(first["render_task_id"])["task"]
    assert task["spec"]["spec"]["params"]["selector"] == "rendering.ffmpeg"
    assert task["spec"]["spec"]["params"]["output_name"] == "real.mp4"
    assert task["spec"]["spec"]["params"]["canonical_revision"]["revision_id"] == first["data"]["accepted_revision_id"]
    client._remote._transport.register_executor({
        "executor_id": "render-worker",
        "protocol": "workspace.v1",
        "resource_keys": [],
        "capabilities": [{
            "capability_id": "rendering.render",
            "definition_digest": DIGEST,
            "status": "ready",
        }],
    }, idempotency_key="register-render-worker")
    worker = WorkspaceClient(daemon.endpoint, executor_token(daemon, "render-worker"))
    render_attempt = worker.claim_task(
        executor_id="render-worker",
        capability_ids=["rendering.render"],
        idempotency_key="claim-render",
    )
    assert render_attempt["task_id"] == first["render_task_id"]
    settled = worker.settle_attempt(render_attempt["attempt_id"], {
        **{key: render_attempt[key] for key in ("lease_id", "fence", "runtime_epoch")},
        "outputs": [],
        "effect": None,
    }, idempotency_key="settle-render")
    assert settled["data"]["state"] == "succeeded"
    retrieved = client.tasks.show(first["render_task_id"])
    assert retrieved.ok and retrieved.data["state"] == "succeeded"
