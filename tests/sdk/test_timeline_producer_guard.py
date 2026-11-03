"""Provider capture and requester durability; these tests claim no Runtime acceptance."""
from __future__ import annotations

import copy
import json

import pytest

from astrid.sdk.timeline import build_producer_batch
from astrid.sdk.timeline._common import TimelineEditError
from astrid.sdk.timeline.commit import create_runtime_commit_service
from astrid.sdk.timeline.sync import TimelineSession
from astrid.sdk.workspace_client import WorkspaceClientError
from tests.sdk import test_timeline_sync as session_fixtures
from tests.sdk.test_timeline_commit_provider import (
    CommitRejected,
    DetachedPort,
    DetachedRealm,
)
from tests.sdk.test_timeline_commit_provider import (
    runtime_abi as runtime_abi,
)
from tests.sdk.test_timeline_sync import initial_bundle, open_session

connected = session_fixtures.connected


def proposal_for(target, commands):
    return {'schema_version': 1, 'producer': {
        'task_id': 'task', 'attempt_id': 'attempt', 'executor_id': 'worker',
        'fence': 1, 'runtime_epoch': 2}, 'admitted_target': copy.deepcopy(target),
        'proposal_digest': 'sha256:' + 'a' * 64, 'commands': commands,
        'rationale': 'Reviewed café'}


class CapturePort(DetachedPort):
    def producer_attempt(self, pin):
        self.producer_capture = copy.deepcopy(pin)
        return {'pin': copy.deepcopy(pin)}

    def preview(self, prepared):
        # Observe real provider preparation without simulating acceptance.
        return copy.deepcopy(prepared)

    def accept(self, prepared):
        raise AssertionError('Runtime acceptance must be tested with real Runtime custody')


def provider_request():
    realm = DetachedRealm()
    target = {'project_id': 'project-1', 'timeline_id': 'film',
              'base_revision_id': realm.head['revision_id'],
              'base_publication_id': realm.head['publication_id']}
    batch = build_producer_batch(proposal_for(target, [realm.move()]))
    context = realm.context(batch['commands'], dry_run=True)
    context['request']['rationale'] = batch['rationale']
    return realm, context


def test_provider_requires_and_records_detached_runtime_capture():
    realm, context = provider_request()
    port = CapturePort(realm, context)
    prepared = create_runtime_commit_service().commit(context, port)
    record = prepared['records']['commands'][0]
    pin = context['request']['commands'][0]['args']['pin']
    assert record['dependencies'] == [{'key': 'producer_attempt:task', 'kind': 'authority',
                                        'policy': 'exact', 'pin': pin}]
    assert prepared['expected_head'] == realm.head
    port.producer_capture['fence'] += 1
    assert record['dependencies'][0]['pin'] == pin
    assert not realm.accepted


@pytest.mark.parametrize('result', [None, {}, {'pin': None}, {'pin': {}, 'extra': None},
                                   'changed', 'bool', 'float'])
def test_provider_refuses_missing_forged_or_changed_verified_pin(result):
    realm, context = provider_request()
    port = CapturePort(realm, context)
    def capture(pin):
        if isinstance(result, str):
            pin['fence'] = {'changed': 8, 'bool': True, 'float': 1.0}[result]
            return {'pin': pin}
        return result
    port.producer_attempt = capture
    with pytest.raises(CommitRejected) as error:
        create_runtime_commit_service().commit(context, port)
    assert error.value.failure['details']['reason'] == 'producer_attempt_mismatch'
    assert not realm.accepted


def test_old_ports_fail_closed_only_for_producer_feature():
    realm, context = provider_request()
    with pytest.raises(CommitRejected) as error:
        create_runtime_commit_service().commit(context, DetachedPort(realm, context))
    assert error.value.failure['details']['reason'] == 'producer_dependency_unavailable'
    ordinary = realm.context([realm.move()], dry_run=True)
    create_runtime_commit_service().commit(ordinary, DetachedPort(realm, ordinary))
    assert not realm.accepted


@pytest.mark.parametrize('field', ['project_id', 'timeline_id', 'base_revision_id', 'base_publication_id'])
def test_provider_refuses_pin_for_different_original_target_before_capture(field):
    realm, context = provider_request()
    context['request']['commands'][0]['args']['pin']['admitted_target'][field] = 'other'
    port = CapturePort(realm, context)
    with pytest.raises(CommitRejected) as error:
        create_runtime_commit_service().commit(context, port)
    assert error.value.failure['details']['reason'] == 'producer_attempt_mismatch'
    assert not hasattr(port, 'producer_capture') and not realm.accepted


def test_runtime_capture_refusal_propagates_without_preparation():
    realm, context = provider_request()
    port = CapturePort(realm, context)
    def refuse(pin):
        raise CommitRejected({'message': 'authoritative artifact changed'})
    port.producer_attempt = refuse
    with pytest.raises(CommitRejected, match='authoritative artifact changed'):
        create_runtime_commit_service().commit(context, port)
    assert not realm.prepared and not realm.accepted


def test_provider_rebase_retains_original_admitted_base_and_capture():
    realm, context = provider_request()
    original_base = copy.deepcopy(realm.head)
    realm.call([realm.move('parent/clip:clip-b', 8)])
    port = CapturePort(realm, context)
    prepared = create_runtime_commit_service().commit(context, port)
    assert prepared['expected_head'] == realm.head != original_base
    target = port.producer_capture['admitted_target']
    assert target['base_revision_id'] == original_base['revision_id']
    assert target['base_publication_id'] == original_base['publication_id']
    assert prepared['records']['commands'][1]['command'] == context['request']['commands'][1]


def test_proposal_uses_existing_outbox_original_base_reason_and_exact_flight(connected):
    client, generated, path = connected
    session = open_session(connected)
    from astrid.sdk.timeline.capture import capture_move
    target = {'project_id': 'p', 'timeline_id': 't', 'base_revision_id': 'r0',
              'base_publication_id': 'pub0'}
    proposal = proposal_for(target, [capture_move(initial_bundle(), 'parent/clip:a', 3)])
    generated.head = {'revision_id': 'new-head', 'publication_id': 'new-publication'}
    # Reopening a clean session observes the newer head, but proposal queueing
    # must retain the producer's original admission pair and captured claims.
    session = open_session(connected)
    queued = session.queue_proposal(proposal)
    assert queued == build_producer_batch(proposal)['commands']
    assert session.outbox.read()['base'] == {'revision_id': 'r0', 'publication_id': 'pub0'}
    with pytest.raises(TimelineEditError, match='producer proposal'):
        session.queue_move('parent/clip:b', 4)
    with pytest.raises(TimelineEditError, match='pending intent'):
        session.queue_proposal(proposal)
    bodies = []
    def lost_ack(project, timeline, body, *, idempotency_key):
        flight = json.loads(path.read_text())['flight']
        assert json.loads(flight['body']) == body
        bodies.append((copy.deepcopy(body), idempotency_key))
        raise ConnectionError('outcome unknown')
    generated.commit_timeline = lost_ack
    with pytest.raises(WorkspaceClientError, match='No reply could be verified'):
        session.flush()
    flight = session.outbox.read()['flight']
    assert flight['key'].startswith('timeline-edit-')
    session = TimelineSession.open('p', 't', client=client, outbox_path=path)
    with pytest.raises(WorkspaceClientError, match='No reply could be verified'):
        session.flush()
    assert bodies[0] == bodies[1]
    body = bodies[0][0]
    assert body['rationale'] == proposal['rationale']
    assert body['commands'][1:] == proposal['commands']
    assert body['base_revision_id'] == 'r0' and body['base_publication_id'] == 'pub0'


def test_queue_proposal_cannot_mix_with_another_pending_edit(connected):
    session = open_session(connected)
    session.queue_move('parent/clip:a', 2)
    proposal = proposal_for({'project_id': 'p', 'timeline_id': 't', 'base_revision_id': 'r0',
                            'base_publication_id': 'pub0'}, session.outbox.read()['pending'])
    before = session.outbox.read()
    with pytest.raises(TimelineEditError):
        session.queue_proposal(proposal)
    assert session.outbox.read() == before
