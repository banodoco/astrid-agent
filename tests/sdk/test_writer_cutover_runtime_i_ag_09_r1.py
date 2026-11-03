"""Requester writer cutover against the Runtime selected by ASTRID_RUNTIME_CHECKOUT.

The producer and renderer are controlled workers; no actual video encoding is claimed.
"""
from __future__ import annotations

import hashlib
import urllib.error
import urllib.request

import pytest

from astrid.core.contracts.runtime_orchestration import OrchestrationContractError
from astrid.packs.rendering.shared.runtime_stitch import publish_authoring_proposal
from astrid.sdk.timeline import commit
from astrid.sdk.timeline.capture import capture_track_create
from astrid.sdk.timeline.sync import TimelineSession
from tests.editing_conformance.test_commit_runtime_m1 import open_client
from tests.helpers.runtime import use_runtime
from tests.sdk.test_timeline_producer_guard_runtime import settle_output

pytestmark = pytest.mark.integration


def test_requester_commit_exact_render_settlement_and_retrieval(real_runtime, tmp_path, monkeypatch):
    use_runtime()
    from runtime_protocol.server import RuntimeHandler

    identities = []
    original_identity = RuntimeHandler._identity

    def observe_identity(handler, scope):
        identity = original_identity(handler, scope)
        identities.append((handler.command, handler.path, scope, identity['actor']))
        return identity

    monkeypatch.setattr(RuntimeHandler, '_identity', observe_identity)
    daemon = real_runtime(support_root=tmp_path / 'support', authoring_profile='astrid')
    service = daemon.service
    project = service.create_project({'slug': 'writer-proof', 'name': 'Writer proof'})['id']
    service.create_timeline(project, 'film', idempotency_key='timeline')
    timeline = service.get_project_timeline(project, 'film')
    target = {'project_id': project, 'timeline_id': 'film',
              'base_revision_id': timeline['head_revision_id'],
              'base_publication_id': timeline['head_publication_id']}
    producer_cap = 'fixture.propose-track'
    child_cap = 'fixture.producer-input'
    render_cap = 'rendering.render'
    digests = {cap: commit._digest(cap) for cap in (producer_cap, child_cap, render_cap)}
    for cap, digest in digests.items():
        service.register_capability({'capability_id': cap, 'definition_digest': digest, 'status': 'ready'})
    service.register_executor({'executor_id': 'worker', 'capabilities': list(digests)}, idempotency_key='executor')
    children = []
    for index in range(2):
        child = service.create_task({'project': project, 'capability_id': child_cap,
            'capability_digest': digests[child_cap], 'input_object_ids': [], 'spec': {'index': index},
            'idempotency_key': f'input-{index}'})
        attempt = service.claim_next({'executor_id': 'worker', 'capability_ids': [child_cap],
            'runtime_epoch': service.health()['runtime_epoch']}, idempotency_key=f'claim-input-{index}')
        settle_output(service, attempt, commit._json({'index': index}).encode(),
                      'data', 'application/json', f'settle-input-{index}')
        children.append(child['task']['id'])
    producer = service.create_task({'project': project, 'capability_id': producer_cap,
        'capability_digest': digests[producer_cap], 'input_object_ids': [], 'idempotency_key': 'producer',
        'spec': {'runtime_dependencies': {'publication': {'admitted_target': target},
            'edges': [{'from_task_id': child, 'to': 'self', 'requires_event': 'task.succeeded',
                       'fence': 'runtime_task'} for child in children],
            'aggregation': {'kind': 'ordered_cas_inputs'}},
            'outputs': {'authoring_proposal': {'type': 'file', 'artifact_type': 'timeline/authoring-proposal'}}}})
    attempt = service.claim_next({'executor_id': 'worker', 'capability_ids': [producer_cap],
        'runtime_epoch': service.health()['runtime_epoch']}, idempotency_key='claim-producer')
    assert attempt['task_id'] == producer['task']['id']
    document = service._timeline_commits()[0].revision_closure(project, 'film', target['base_revision_id'])
    commands = [capture_track_create(commit.bundle_from_document(document), 'parent',
                                     {'id': 'proposed', 'kind': 'visual'})]
    proposal = {'schema_version': 1, 'producer': {'task_id': attempt['task_id'],
        'attempt_id': attempt['attempt_id'], 'executor_id': 'worker', 'fence': attempt['fence'],
        'runtime_epoch': attempt['runtime_epoch']}, 'admitted_target': target,
        'rationale': 'Requester proof', 'commands': commands}
    proposal['proposal_digest'] = commit._digest(proposal)
    settle_output(service, attempt, commit._json(proposal).encode(),
                  'authoring_proposal', 'application/json', 'settle-producer')

    _, credential = daemon.credentials.provision('requester', [
        'handshake', 'projects:read', 'projects:write', 'tasks:read', 'tasks:write', 'objects:read', 'objects:write'])
    client = open_client(daemon, actor='requester', credential=credential)
    session = TimelineSession.open(project, 'film', client=client, outbox_path=tmp_path / 'outbox.json')
    assert session._state()['scope']['actor_id'] == 'requester'
    owner_client = open_client(daemon)
    with pytest.raises(OrchestrationContractError, match='original requester client'):
        publish_authoring_proposal(owner_client, proposal=proposal, timeline_id='film', expected_version=1,
            render_capability_digest=digests[render_cap], requester_session=session, idempotency_key='foreign')
    assert service.get_project_timeline(project, 'film')['head_revision_id'] == target['base_revision_id']
    # A credential without projects:write cannot use either writer route.
    denied, _ = daemon.credentials.provision('reader', ['handshake', 'projects:read'])
    path = f'/v1/projects/{project}/timelines/film/commit'
    request = urllib.request.Request(daemon.endpoint + path, data=b'{}', method='POST',
        headers={'Authorization': 'Bearer ' + denied, 'Content-Type': 'application/json',
                 'Idempotency-Key': 'denied'})
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(request)
    assert error.value.code == 401
    assert service.get_project_timeline(project, 'film')['head_revision_id'] == target['base_revision_id']
    result = publish_authoring_proposal(client, proposal=proposal, timeline_id='film', expected_version=1,
        render_capability_digest=digests[render_cap], requester_session=session, idempotency_key='publish')
    assert result['data']['publisher'] == 'requester'
    commit_path = f'/v1/projects/{project}/timelines/film/commit'
    assert identities.count(('POST', commit_path, 'projects:write', 'requester')) >= 2
    assert ('POST', '/v1/tasks', 'tasks:write', 'requester') in identities
    accepted = result['data']['accepted_revision_id']
    publication = result['receipt']['receipt_id']
    digest = result['data']['canonical_document']['parent_revision']['content_digest']
    task_id = result['render_task_id']
    task = service.task(task_id)['task']
    pin = task['spec']['spec']['params']['canonical_revision']
    assert pin == {'project_id': project, 'timeline_id': 'film', 'revision_id': accepted,
                   'publication_id': publication, 'content_digest': digest}
    claim = service.claim_next({'executor_id': 'worker', 'capability_ids': [render_cap],
        'runtime_epoch': service.health()['runtime_epoch']}, idempotency_key='claim-render')
    assert claim['task_id'] == task_id
    raw = b'controlled render output for exact revision'
    settle_output(service, claim, raw, 'render', 'video/mp4', 'settle-render')
    shown = client.tasks.show(task_id)
    assert shown.ok, shown.as_dict()
    assert shown.data['state'] == 'succeeded'
    assert shown.data['attempt_id'] == claim['attempt_id']
    assert shown.data['spec']['spec']['params']['canonical_revision'] == pin
    outputs = client.tasks.list_managed_outputs(task_id)
    assert outputs.ok, outputs.as_dict()
    rows, cursor = outputs.data
    assert cursor is None
    association, = [row for row in rows if row['output_port'] == 'render']
    assert association['task_id'] == task_id and association['attempt_id'] == claim['attempt_id']
    assert association['project_id'] == project
    assert association['object_id'] == 'sha256:' + hashlib.sha256(raw).hexdigest()
    assert client._remote._transport.get_object(association['object_id'])['data'] == raw




def test_retired_writer_refuses_before_body_idempotency_and_publication():
    use_runtime()
    from runtime_protocol.errors import RetiredRouteError
    from runtime_protocol.server import RuntimeHandler
    from types import SimpleNamespace

    def forbidden(*args, **kwargs):
        pytest.fail('retired route touched body, idempotency, or publication')

    handler = object.__new__(RuntimeHandler)
    handler.server = SimpleNamespace(runtime=SimpleNamespace(publish_parent_composition=forbidden))
    handler.command = 'POST'
    handler.path = '/v1/projects/project-1/timelines/timeline-1/composition-revisions'
    handler.headers = {'Idempotency-Key': ''}
    seen = []
    handler._identity = lambda scope: seen.append(scope) or {'actor': 'requester'}
    handler._body = handler._raw_body = handler._idempotency_key = forbidden
    with pytest.raises(RetiredRouteError) as error:
        handler._route()
    assert error.value.code == 'retired_route'
    assert seen == ['projects:write']
