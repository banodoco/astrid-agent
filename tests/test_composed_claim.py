"""Real Agent registration/selection -> Runtime tasks, leases and reservations.

Only the HTTP transport edge is in process. Discovery, readiness, target
selection, registration, child authority, claim and settlement are real.
"""
import dataclasses
import json
from pathlib import Path
import shutil

import pytest

from installed_canary.contract import ASSETS


def test_parent_declaration_allows_real_h3_child_claim(tmp_path, monkeypatch):
    from astrid.core.execution.generic_host import (
        AdapterRegistry, GenericPackHost, HostError, RuntimeProtocolClient, _configured_claim_target,
    )
    from astrid.sdk.execution_request import normalize_execution_request
    from runtime_protocol.lifecycle import _interruption_report
    from runtime_protocol.service import RuntimeService
    from runtime_protocol.store import RealmStore
    import astrid

    parent_pack = tmp_path / 'installed_canary'
    shutil.copytree(ASSETS, parent_pack)
    agent = Path(astrid.__file__).resolve().parent
    matrix = json.loads((agent.parent / 'config/astrid-beta-capabilities.json').read_text())
    # On the exact base this declaration is absent: use the real CPU adapter's
    # default, as the frozen packet did. The fixed asset declares no mutex.
    probe = GenericPackHost(pack_roots=[parent_pack])
    probe.discover()
    definition = probe.capabilities['installed_canary.parent'].definition
    resources = definition.metadata.get('resource_keys', AdapterRegistry.resolve(definition).resource_keys)
    row = {'id': 'installed_canary.parent', 'disposition': 'optional', 'evidence_reason': 'CPU composition proof',
           'adapter_family': 'cpu', 'resource_keys': list(resources)}
    matrix['capabilities'].append(row)
    selected = tmp_path / 'selected-capabilities.json'
    selected.write_text(json.dumps(matrix))
    RealmStore.initialize(tmp_path / 'realm').close()
    service = RuntimeService(tmp_path / 'realm')
    identity = {'actor': 'astrid-pack-host', 'scopes': ['worker:execute'], 'execution_binding': {
        'actual': {'kind': 'machine', 'id': 'cpu-composition'}, 'executor_incarnation': 'cpu-composition/v1',
        'verification': {'method': 'credential_claim', 'verified': True, 'evidence_digest': 'sha256:' + '1' * 64}}}
    claims = []

    def transport(method, path, headers, body):
        payload = json.loads(body) if body else None
        key = headers.get('Idempotency-Key')
        if path == '/v1/health':
            value = {**service.health(), 'identity': None, 'runtime_instance_id': 'cpu-composition'}
        elif path == '/v1/executors':
            value = service.register_executor(payload, idempotency_key=key, identity=identity)
        elif path == '/v1/tasks/claim':
            value = service.claim_next(payload, idempotency_key=key, identity=identity)
            claims.append((payload, value))
            if value is None:
                return 204, {}, b''
        elif path.startswith('/v1/tasks/') and method == 'GET':
            value = service._task_resource(service.task(path.split('/')[3]))
        elif path.startswith('/v1/attempts/'):
            attempt, operation = path.split('/')[3:5]
            action = {'heartbeat': service.heartbeat_attempt, 'fail': service.fail_attempt,
                      'settle': service.settle_attempt}[operation]
            value = action(attempt, payload, idempotency_key=key, identity=identity)
        else:
            raise AssertionError((method, path))
        return 200, {}, json.dumps(value).encode()

    client = RuntimeProtocolClient('http://127.0.0.1:1', 'synthetic-network-edge')
    client.generated._transport = transport
    host = GenericPackHost(pack_roots=[agent / 'packs/h3_av', parent_pack], client=client,
                           executor_id='astrid-pack-host', max_concurrency=2, capability_matrix=selected)
    monkeypatch.setenv('ASTRID_EXECUTION_TARGET_JSON', '{"kind":"default"}')
    try:
        host.discover()
        host.register()
        caps = host.capabilities
        assert caps['installed_canary.parent'].ready and caps['h3_av.prepare'].ready
        assert caps['h3_av.prepare'].resource_keys == ('cpu',)
        project = service.create_project({'slug': 'composed-claim', 'name': 'Composed claim'})['id']
        service.create_task({'capability_id': 'installed_canary.parent',
            'capability_digest': caps['installed_canary.parent'].capability_digest,
            'project': project, 'input_object_ids': [], 'spec': {}, 'idempotency_key': 'parent',
            'child_delegation': {'capabilities': [{'capability_id': 'h3_av.prepare',
                'capability_digest': caps['h3_av.prepare'].capability_digest}],
                'targets': [{'kind': 'default'}], 'input_object_ids': []},
            'execution_request': normalize_execution_request({'target': {'kind': 'default'}, 'inputs': []})},
            enforce_readiness=True)
        # Both filters are real: omitting or changing the selected target must
        # not claim this targeted parent.
        for index, target in enumerate((None, {'kind': 'machine', 'id': 'other'})):
            assert client.claim_next(executor_id=host.executor_id, capability_ids=['installed_canary.parent'],
                                     idempotency_key='wrong-target-' + str(index), target=target) is None
        parent = dataclasses.asdict(client.claim_next(executor_id=host.executor_id,
            capability_ids=['installed_canary.parent'], idempotency_key='parent-claim', target=_configured_claim_target()))
        lease = {key: parent[key] for key in ('lease_id', 'fence', 'runtime_epoch')}
        authority = service.issue_child_authority(parent['attempt_id'], lease, identity=identity)
        child = service.admit_delegated_child({'authority': authority['authority'], 'task': {
            'capability_id': 'h3_av.prepare', 'capability_digest': caps['h3_av.prepare'].capability_digest,
            'input_object_ids': [], 'spec': {},
            'execution_request': normalize_execution_request({'target': {'kind': 'default'}, 'inputs': []})}},
            idempotency_key='prepare', identity=identity)
        child_id = child['task']['id']
        slot = host._new_claim_slot()
        slot.client.generated._transport = transport
        # Deliberately missing H3 inputs fail through real host preparation and
        # fenced failure; the claim itself must precede that terminal failure.
        try:
            slot.claim_once()
        except HostError as exc:
            assert "missing value for placeholder {request}" in str(exc)
        final = service.store.get_task(child_id)['task']
        assert final['attempt_id'], {'waiting_reason': final.get('waiting_reason'), 'claims': claims}
        assert final['status'] == 'failed'
        assert service.store.execution_binding(child_id)['actual_target'] == identity['execution_binding']['actual']
        assert not service.store.conn.execute(
            'SELECT 1 FROM reservations WHERE task_id=?', (parent['task_id'],)).fetchone()
        child_reservation = service.store.conn.execute(
            'SELECT * FROM reservations WHERE task_id=? AND resource_key=?', (child_id, 'cpu')).fetchone()
        assert child_reservation and child_reservation['released_at']
        service.fail_attempt(parent['attempt_id'], {**lease, 'error': {'code': 'invalid_input',
            'message': 'Expected missing child input failure', 'retryable': False}},
            idempotency_key='parent-fail', identity=identity)
        assert _interruption_report(service.store.conn)['safe']
    finally:
        host.shutdown()
        service.close()


def test_parent_cannot_reserve_child_cpu_even_when_matrix_repinned(tmp_path):
    from test_canary import Fixture
    from installed_canary.common import GateError, read, save, sha
    from installed_canary.contract import validate

    fixture = Fixture(tmp_path)
    path = Path(fixture.value['matrix']['selected_path'])
    matrix = read(path)
    matrix['capabilities'][-1]['resource_keys'] = ['cpu']
    save(path, matrix)
    fixture.value['matrix']['added_row'] = matrix['capabilities'][-1]
    fixture.value['matrix']['selected_sha256'] = sha(path)
    fixture.freeze()
    with pytest.raises(GateError, match='reviewed optional CPU parent row required'):
        validate(fixture.tuple, fixture.task, fixture.work)
