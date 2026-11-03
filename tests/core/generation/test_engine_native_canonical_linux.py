"""Real installed canonical launcher/fence proof. No fixture dispatcher/proc.

Requires an ordinary Linux UID and a fully installed exact-head plain Comfy
root in ASTRID_NATIVE_INSTALL_ROOT. The recipe still requires its actual CUDA
environment. This does not substitute for detached SDK/Runtime handoff proof.
Missing venue prerequisites fail, never skip or select a synthetic server.
"""
from dataclasses import asdict
import http.client
import io
import json
import os
from pathlib import Path
import secrets
import socket
import sys
import time

from PIL import Image
import pytest

from astrid.core.execution.managed_tool_session import SessionBinding
from astrid.core.generation import engine_custody as custody
from astrid.core.generation.engine_contracts import CustodyUnresolved, EndpointOwnershipError
from astrid.core.generation.engine_execution import create_local_backend
from astrid.core.generation.engine_native_fence import canonical, identity
from astrid.core.generation.engine_recipe import load_recipe
from astrid.core.generation.engine_runner import engine_status

pytestmark = pytest.mark.linux_native


@pytest.fixture
def canonical_owner():
    assert os.uname().sysname == 'Linux'
    assert os.getuid() != 0 and os.geteuid() != 0, 'requires real ordinary Linux UID'
    root = os.environ.get('ASTRID_NATIVE_INSTALL_ROOT')
    assert root and Path(root).is_absolute(), 'requires real exact-head installed plain Comfy root'
    recipe = load_recipe('comfyui', 'linux-x86_64-cu130')
    context, adapter = create_local_backend(engine=recipe.engine, platform=recipe.platform,
        placement='local', root=root, deadline=time.monotonic() + 300)
    launch_request = adapter.start_request(recipe, root, deadline=time.monotonic() + 300)
    launch = {'argv': list(launch_request.argv), 'cwd': launch_request.cwd, 'env': dict(launch_request.env)}
    start_payload = {**context.service_client.identity, 'schema': custody._SCHEMA, 'launch': launch,
        'owner_id': None, 'timeout_seconds': recipe.health.timeout_seconds,
        'poll_interval_seconds': recipe.health.poll_interval_seconds}
    context.service_client.call('start', start_payload, deadline=time.monotonic() + 300)
    # Verify the real receipt/source/model/helper/health evidence first. The
    # native proof then launches a new process; it never adopts this listener.
    receipt = engine_status(recipe.engine, platform=recipe.platform, placement='local', root=root,
        context=context, deadline=time.monotonic() + 300)
    assert receipt is not None and receipt.status == 'installed'
    context.service_client.stop_owned(deadline=time.monotonic() + 30)
    owner = custody._Supervisor(context.service_client.identity, recipe, secrets.token_hex(16))
    channel, peer = socket.socketpair()
    payload = {**owner.identity, 'schema': custody._SCHEMA, 'launch': owner.launch, 'owner_id': None,
        'timeout_seconds': recipe.health.timeout_seconds, 'poll_interval_seconds': recipe.health.poll_interval_seconds}
    try:
        with owner.work:
            result = owner.service('start', payload, time.monotonic() + 300, channel)
            assert result['healthy'] is True
            evidence = owner.release_identity(channel, time.monotonic() + 10)
            native = owner.native_control
            assert native is not None and native.engine is owner.engine
            attestation = native.attest(time.monotonic() + 5)
            assert attestation['engine'] == evidence['engine'] == identity(owner.engine.process.pid)
            binding = SessionBinding(owner.registry, evidence['owner_instance'], evidence['issuer']['birth'],
                evidence['endpoint'], evidence['source_content_digest'], evidence['config_digest'],
                'sha256:' + owner.identity['helper_sha256'], None,
                evidence['engine']['birth'], evidence['engine']['birth'])
            association = secrets.token_hex(16)
            owner.bind_release_native(association, asdict(binding), 1, time.monotonic() + 5)
        print(json.dumps({'kind': 'real_canonical_native', 'uid': os.getuid(), 'engine': evidence,
                          'attestation': attestation, 'binding': asdict(binding), 'generation': 1}, sort_keys=True))
        yield owner, channel, evidence
    finally:
        with owner.work:
            owner.stop(time.monotonic() + 30)
        channel.close()
        peer.close()


def _http(method, path, body=b'', headers=None):
    connection = http.client.HTTPConnection('127.0.0.1', 8188, timeout=3)
    try:
        connection.request(method, path, body, headers or {})
        response = connection.getresponse()
        data = response.read()
        return response.status, data
    finally:
        connection.close()


def _hold_upload(native):
    data = io.BytesIO()
    Image.new('RGB', (2, 2), 'blue').save(data, format='PNG')
    prefix = b'--admission\r\nContent-Disposition: form-data; name="image"; filename="r12-native.png"\r\nContent-Type: image/png\r\n\r\n'
    ending = data.getvalue() + b'\r\n--admission--\r\n'
    connection = http.client.HTTPConnection('127.0.0.1', 8188, timeout=8)
    connection.putrequest('POST', '/upload/image')
    connection.putheader('Content-Type', 'multipart/form-data; boundary=admission')
    connection.putheader('Content-Length', str(len(prefix) + len(ending)))
    connection.endheaders(prefix)
    deadline = time.monotonic() + 3
    while native.request('status', {}, deadline)['handlers'] != 1:
        assert time.monotonic() < deadline
        time.sleep(.01)
    return connection, ending


def _request(native, seconds=8):
    return {**native.bound, 'request_id': secrets.token_hex(16),
            'deadline_ns': time.monotonic_ns() + int(seconds * 1e9)}


def test_native_direct_listener_waits_for_admitted_upload_and_unloads_once(canonical_owner):
    owner, channel, evidence = canonical_owner
    native = owner.native_control
    upload, ending = _hold_upload(native)
    # Real pinned core workflow: native queue admission, execution, task_done,
    # flag handling and pending GC all precede the worker's terminal barrier.
    workflow = {'prompt': {
        '1': {'class_type': 'EmptyImage', 'inputs': {'width': 64, 'height': 64, 'batch_size': 1, 'color': 255}},
        '2': {'class_type': 'SaveImage', 'inputs': {'images': ['1', 0], 'filename_prefix': 'r12-native'}}}}
    code, body = _http('POST', '/prompt', json.dumps(workflow).encode(), {'Content-Type': 'application/json'})
    assert code == 200, body
    prompt_id = json.loads(body)['prompt_id']
    request = _request(native)
    deadline = request['deadline_ns'] / 1e9
    try:
        closed = native.release(request, deadline)
        assert closed['status'] == 'draining' and not closed['effect_sent']
        # Every mutable route class from the pinned inventory, including
        # aliases/internal/VHS/optional routes, hits the actual native listener.
        paths = ['/prompt', '/upload/image', '/upload/mask', '/queue', '/interrupt', '/free', '/history',
                 '/settings', '/settings/x', '/users', '/userdata/x', '/userdata/x/move/y',
                 '/api/jobs/x/cancel', '/api/jobs/cancel', '/api/assets', '/api/assets/from-hash',
                 '/api/assets/x', '/api/assets/x/tags', '/api/assets/seed', '/api/assets/seed/cancel',
                 '/api/assets/prune', '/api/assets/export', '/api/deploy/bundle']
        for path in paths:
            for alias in (path, '/api' + path):
                assert _http('POST', alias, b'{}', {'Content-Type': 'application/json'})[0] in (403, 503)
        for method, path in [('PATCH', '/internal/logs/subscribe'), ('DELETE', '/userdata/x'),
            ('PUT', '/api/assets/x'), ('DELETE', '/api/assets/x/tags'), ('GET', '/ws'), ('GET', '/api/ws'),
            ('GET', '/vhs/viewvideo'), ('GET', '/viewvideo'), ('GET', '/api/vhs/viewaudio'),
            ('GET', '/vhs/queryvideo'), ('GET', '/vhs/getpath'), ('GET', '/unknown'), ('POST', '/%70rompt')]:
            assert _http(method, path)[0] in (400, 403, 503)
        assert _http('GET', '/system_stats')[0] == _http('GET', '/queue')[0] == 200
        assert native.release(request, deadline, observe=True)['status'] == 'draining'
        assert not native.release(request, deadline, observe=True)['effect_sent']
        upload.send(ending)
        response = upload.getresponse()
        assert response.status == 200
        response.read()
        while True:
            record = native.release(request, deadline, observe=True)
            if record['status'] == 'settled':
                break
            assert record['status'] == 'draining'
            time.sleep(.01)
        assert not record['effect_sent']
        native.release(request, deadline, unload=True)
        while True:
            record = native.release(request, deadline, observe=True)
            if record['status'] == 'completed':
                break
            assert record['status'] in ('ready_unload', 'unloading')
            time.sleep(.01)
        assert record['effect_sent'] and record['unload_ack'] is True
        assert record['queue_running'] == record['queue_pending'] == 0
        assert record['admitted_serial'] >= 1 and record['worker_iterations'] >= 2
        code, history = _http('GET', '/history/' + prompt_id)
        assert code == 200 and json.loads(history)[prompt_id]['status']['completed'] is True
        assert native.release(request, deadline, observe=True) == record
        assert native.release(request, deadline, unload=True) == record
        # Closed until a new verified association with a strictly newer G.
        assert _http('POST', '/prompt', b'{}')[0] == 503
        native.bind(secrets.token_hex(16), native.bound['binding'], 2, time.monotonic() + 3)
        assert native.request('status', {}, time.monotonic() + 2)['state'] == 'OPEN'
        assert owner.engine.live() and identity(owner.engine.process.pid) == evidence['engine']
        print(json.dumps({'native_terminal': record, 'memory_observed_returned': False}, sort_keys=True))
    finally:
        upload.close()


@pytest.mark.parametrize('fault', ['timeout', 'malformed_control', 'replacement'])
def test_native_uncertainty_retains_original_process_custody(canonical_owner, fault):
    owner, channel, evidence = canonical_owner
    native = owner.native_control
    upload, ending = _hold_upload(native)
    request = _request(native, .25 if fault == 'timeout' else 5)
    deadline = request['deadline_ns'] / 1e9
    try:
        native.release(request, deadline)
        if fault == 'timeout':
            time.sleep(.3)
            status = native.request('status', {}, time.monotonic() + 2)
            assert status['state'] == 'UNCERTAIN' and status['record']['effect_sent'] is False
        elif fault == 'malformed_control':
            native.channel.send(b'{"unknown":true}')
            assert native.channel.recv(65536) == b''
        else:
            # A real replacement process cannot redirect the retained native
            # controller. Keep both actual handles until explicit cleanup.
            original = owner.engine
            from astrid.core.generation.engine_contracts import CommandRequest
            replacement = owner.spawn(CommandRequest((sys.executable, '-c', 'import time; time.sleep(60)'),
                owner.root, {}, time.monotonic() + 5))
            try:
                owner.engine = replacement
                with pytest.raises(CustodyUnresolved):
                    native.request('status', {}, time.monotonic() + 2)
            finally:
                owner.engine = original
                owner.drain(replacement, time.monotonic() + 5)
        assert _http('POST', '/prompt', b'{}')[0] in (403, 503)
        assert owner.engine.live() and identity(owner.engine.process.pid) == evidence['engine']
        assert owner.engine in owner.commands
        print(json.dumps({'fault': fault, 'retained_engine': evidence['engine'], 'unload_sent': False}))
    finally:
        upload.close()
