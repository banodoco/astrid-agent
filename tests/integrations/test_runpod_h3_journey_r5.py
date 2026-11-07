"""H3 parent and staged local run through the retained RunPod endpoint."""
from __future__ import annotations

import http.client
import hashlib
import json
import os
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from astrid.core.execution.generic_host import GenericPackHost, RuntimeProtocolClient
from astrid.core.generation.delivery import deliver_generation
from astrid.packs.runpod import target_selection
from astrid.sdk import AstridClient
from astrid.sdk.generation import GenerationFacade
from astrid.sdk.remote import RemoteAstridClient
from astrid.sdk.workspace_client import WorkspaceClient
from tests.packs.runpod.test_generation_preparation import retained, small_lease, lease_selection_case, sources
from tests.sdk.test_h3_endpoint_r4 import h3_inputs
from tests.integrations.test_runpod_host_endpoint_r5 import tuple_sources

pytestmark = pytest.mark.integration


def test_runtime_h3_parent_runs_local_child_on_retained_endpoint(retained, h3_inputs, tmp_path, monkeypatch, real_runtime, tuple_sources):
    from astrid.core.generation import engine_platforms, engine_recipe, engine_runner
    from astrid.packs.h3_av.shared.generation import generation_timing
    from astrid.packs.runpod import remote_placement
    from astrid.core.execution._child_bridge import ChildBridgeError, HostChildBridge
    from runpod_lifecycle import https
    from tests.core.generation import test_engine_runner as canonical
    from tests.helpers.h3_attached_engine import AttachedEngine
    from tests.packs.runpod.test_remote_placement import FakePod

    c = retained
    c.host.close()
    rejections = []
    host_errors = []
    run_task = GenericPackHost.run_task
    def observe_run_task(self, task, **kwargs):
        try:
            return run_task(self, task, **kwargs)
        except Exception as exc:
            data = task.get('task', task)
            host_errors.append((data.get('capability', data.get('capability_id')), type(exc).__name__, str(exc)[:1000]))
            raise
    monkeypatch.setattr(GenericPackHost, 'run_task', observe_run_task)
    dispatch = HostChildBridge.dispatch
    def observe_dispatch(self, request):
        try:
            return dispatch(self, request)
        except Exception as exc:
            rejections.append(str(exc) if isinstance(exc, ChildBridgeError) else type(exc).__name__)
            raise
    monkeypatch.setattr(HostChildBridge, 'dispatch', observe_dispatch)
    c.placement.source_root = tuple_sources
    monkeypatch.setattr(remote_placement, 'subprocess', subprocess)
    c.pod.marker = (f'{remote_placement.BOOTSTRAP_ROOT}/'
                    f'{remote_placement.source_digest(c.placement.source_root)}/ok')
    monkeypatch.setattr(target_selection, 'resolve_runtime_data_root', lambda: tmp_path / 'machine')
    monkeypatch.setattr(target_selection, 'read_runpod_preferences', lambda: c.prefs)
    monkeypatch.setenv('ASTRID_RUNPOD_SOURCE_ROOT', str(c.placement.source_root))
    engine_root = '/workspace/astrid/engines/vibecomfy-h3'
    recipe = engine_recipe.load_recipe('vibecomfy-h3', 'linux-x86_64-cu130')
    plan = engine_runner.plan_install(recipe, root=engine_root, placement='local')
    monkeypatch.setattr(canonical, 'ROOT', engine_root)
    context = canonical.FakeContext()
    context.deadline = time.monotonic() + 600
    monkeypatch.setattr(engine_platforms, 'platform_adapter', lambda key: context.adapter)
    pod = c.pod

    def execute(command, timeout=600):
        if 'engines status' in command:
            pod.exec_log.append(command)
            if not pod.installed:
                return 0, 'null', ''
            status = engine_runner.engine_status('vibecomfy-h3', platform='linux-x86_64-cu130',
                placement='local', root=engine_root, context=context, deadline=context.deadline)
            return 0, engine_runner._json(status), ''
        return FakePod.exec(pod, command, timeout=timeout)

    def start(command, *, job_id):
        assert c.read()['lease']['preparation']['job'] == {'id': job_id, 'state': 'starting'}
        old = FakePod.job_start(pod, command)
        pod.jobs[job_id] = pod.jobs.pop(old['id'])
        directory = '/workspace/.astrid-jobs/' + job_id
        for name in ('stdout', 'stderr'):
            pod.files[directory + '/' + name] = pod.files.pop(old['dir'] + '/' + name)
        if 'engines install' in command and not pod.jobs[job_id]:
            receipt = engine_runner.install_engine(plan, context=context, adapter=context.adapter,
                                                   deadline=context.deadline)
            pod.files[engine_root + '/.astrid-engine/install.json'] = engine_runner._json(receipt).encode()
        return {'id': job_id, 'dir': directory}

    monkeypatch.setattr(pod, 'exec', execute)
    monkeypatch.setattr(pod, 'job_start', start)
    timing = generation_timing(4)
    engine = AttachedEngine(tmp_path / 'engine-home', generated_seconds=timing['raw_frames'] / timing['fps'])
    events = []
    def enable(self, endpoint, port):
        events.append(('enable', endpoint, port))
    def relay(self, handler, endpoint):
        binding = c.read()['lease']['endpoint_binding']
        assert binding['state'] in {'bound', 'ready'}
        events.append(('request', handler.command, handler.path))
        target = urlsplit(engine.url)
        connection = http.client.HTTPConnection(target.hostname, target.port, timeout=60)
        size = int(handler.headers.get('Content-Length', '0'))
        body = handler.rfile.read(size) if size else None
        connection.request(handler.command, handler.path, body=body,
                           headers={'Content-Type': handler.headers.get('Content-Type', 'application/json')})
        response = connection.getresponse()
        data = response.read()
        handler.send_response(response.status)
        handler.send_header('Content-Type', response.getheader('Content-Type', 'application/json'))
        handler.send_header('Content-Length', str(len(data)))
        handler.end_headers()
        handler.wfile.write(data)
        connection.close()
    monkeypatch.setattr(https.HttpsPodTransport, 'enable_engine_proxy', enable, raising=False)
    monkeypatch.setattr(https.HttpsPodTransport, 'relay_engine', relay, raising=False)
    daemon = real_runtime()
    # The existing H3 delegated-child route requires the ordinary host's
    # credential-verified machine identity. This is test-owned Runtime state,
    # not a provider placement or a new Runtime execution contract.
    from runtime_protocol.daemon import WORKER_SCOPES
    worker_token, _ = daemon.credentials.provision('astrid-pack-host', list(WORKER_SCOPES), rotate=True,
        metadata={'execution_binding': {'actual': {'kind': 'machine', 'id': 'h3-r5-test-machine'},
            'executor_incarnation': 'h3-r5-test', 'verification': {'method': 'credential_claim',
                'verified': True, 'evidence_digest': 'sha256:' + hashlib.sha256(b'test-machine').hexdigest()}}})
    host = None
    worker = None
    errors = []
    try:
        owner = WorkspaceClient(daemon.endpoint, daemon.token)
        client = AstridClient(remote=RemoteAstridClient(owner))
        invoke = client.invoke_result
        def bounded(capability, **kwargs):
            kwargs.setdefault('timeout_seconds', 600)
            return invoke(capability, **kwargs)
        client.invoke_result = bounded
        project = client.projects.create(name='H3 retained endpoint', slug='h3-retained', idempotency_key='project')
        assert project.ok, project.error
        project_id = project.data['project_id']
        owner.select_project(project_id, idempotency_key='select-project')
        packs = Path(__file__).resolve().parents[2] / 'astrid' / 'packs'
        host = GenericPackHost(pack_roots=[packs / 'vibecomfy', packs / 'h3_av', packs / 'runpod'],
            client=RuntimeProtocolClient(daemon.endpoint, worker_token), max_concurrency=4,
            credential_source={'FAKE_RUNPOD_CREDENTIAL': 'fake-only'}, attempt_base=tmp_path / 'attempts')
        host.discover()
        host.preflight()
        for capability in ('vibecomfy.run', 'h3_av.transform', 'runpod.status', 'runpod.release'):
            assert host.capabilities[capability].ready, host.capabilities[capability].preflight
        host.register()
        def serve():
            try:
                host.run(poll_seconds=0.02)
            except BaseException as exc:
                errors.append(exc)
        worker = threading.Thread(target=serve, daemon=True)
        worker.start()
        quote = GenerationFacade().describe(target='runpod', client=client)
        deadline = (datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()
        result = GenerationFacade().video(entry='h3', target='runpod', client=client, project=project_id,
            consent=('paid',), max_usd=1.0, deadline=deadline, offer_digest=quote['offer_digest'],
            preparation_seconds=60, execution_seconds=300, idle_seconds=1800, **h3_inputs,
            recovery_path=tmp_path / 'h3-recovery.json')
        assert result.ok, '\n'.join(f'{capability}: {kind}: {message}'
                                   for capability, kind, message in host_errors) or str(result.error)
        delivered = deliver_generation(result, client=client, out=tmp_path / 'delivered', project=project_id)
        assert len(delivered['image_paths']) >= 1 and Path(delivered['image_paths'][0]).stat().st_size > 0
        assert c.creates == 1 and [e[0] for e in events].count('enable') == 1
        retained_owner = host._target_bindings.provider.lease_owner
        resumed = GenerationFacade().video(entry='h3', target='runpod', client=client, project=project_id,
            consent=('paid',), max_usd=1.0, deadline=deadline, offer_digest=quote['offer_digest'],
            preparation_seconds=60, execution_seconds=300, idle_seconds=1800, **h3_inputs,
            recovery_path=tmp_path / 'h3-recovery.json', resume=True, wait=False)
        assert resumed.kernel_task_id == result.kernel_task_id
        assert c.creates == 1 and [e[0] for e in events].count('enable') == 1
        status = GenerationFacade().describe(target='runpod', client=client)
        assert status['pod_id'] == 'pod-1' and status['lease']['state'] == 'idle'
        assert host._target_bindings.provider.lease_owner is retained_owner
        binding = c.read()['lease']['endpoint_binding']
        assert binding['endpoint_id'] == 'vibecomfy-h3' and binding['state'] == 'ready'
        assert binding['installed_receipt_sha256'] == c.read()['lease']['preparation']['installed']['receipt_sha256']
        assert c.read()['paid_limits']['idle_seconds'] == 1800
        parent = client.tasks.show(result.kernel_task_id)
        assert parent.ok
        spec = parent.data['spec'].get('spec', parent.data['spec'])
        plan = spec['inputs']['plan']
        assert plan['workflow_members'] and plan['workflow_inputs_sha256'].startswith('sha256:')
        assert plan['controls']['target_policy']['offer_digest'] == quote['offer_digest']
        assert not getattr(client, '_generation_prepared_owners', {})
        outputs = result.outputs['managed_outputs']
        digests = {row.get('object_id') or row.get('digest') for row in outputs}
        assert all('sha256:' + hashlib.sha256(Path(path).read_bytes()).hexdigest() in digests
                   for path in delivered['image_paths'])
        released = GenerationFacade().release(engine='comfyui', target='runpod', client=client, wait=True)
        assert released.ok and c.read()['state'] == 'cleanup_complete'
        assert host._target_bindings.provider.lease_owner is retained_owner
        assert c.deletes == ['pod-1'] and c.read()['cleanup']['absence']['exact_get_status'] == 404
        host._shutdown.set()
        worker.join(timeout=30)
        assert not worker.is_alive() and not errors
    finally:
        if host is not None:
            host.shutdown()
        if worker is not None:
            worker.join(timeout=5)
        daemon.stop()
        engine.close()
