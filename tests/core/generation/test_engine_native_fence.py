"""Component diagnostics using real aiohttp dispatch and real kernel channels.

These tests deliberately do not attest a canonical Comfy installation, execute
inference, or stand in for the ordinary-UID production native proof.
"""
import asyncio
import hashlib
import inspect
import os
from pathlib import Path
import socket
import threading
import time
from types import SimpleNamespace

import aiohttp
from aiohttp import web, web_urldispatcher
import pytest

from astrid.core.generation.engine_native_fence import Fence, VERSION, canonical, decode, identity, policy


@pytest.mark.parametrize("path", ["/free", "/api/free", "/settings", "/api/settings/x", "/users",
    "/userdata/x", "/internal/logs/subscribe", "/api/assets", "/api/api/jobs/x/cancel",
    "/vhs/viewvideo", "/api/viewvideo", "/VHS_test", "/ws", "/api/ws", "/unrecognized",
    "/api/api/prompt", "/%70rompt", "/a/../prompt", "//prompt", "/prompt/", "/prompt%2f"])
def test_unsupported_ingress_is_denied_for_every_method(path):
    assert all(policy(method, path) == "deny" for method in ("GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"))


def test_caller_dictionary_never_selects_a_topology():
    from astrid.core.generation.engine_custody import _Supervisor
    owner = _Supervisor.__new__(_Supervisor)
    owner.native_control = None
    assert owner.release_admission_supported(identity(os.getpid()), {"exclusive": True}, time.monotonic() + 1) is False


@pytest.mark.parametrize("data", [b'{}{}', b'{"x":1,"x":2}', b'{"x":NaN}', b'[]', b'', b'x' * 65536])
def test_control_rejects_ambiguous_or_unbounded_frames(data):
    with pytest.raises(ValueError):
        decode(data)


@pytest.mark.parametrize("fault", ["controller_loss", "malformed_control", "timeout", "stale_generation"])
def test_real_dispatch_closes_before_drain_and_retains_handler_obligation(fault):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    child, parent = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
    parent.setblocking(False)
    enrollment = {"controller": identity(os.getpid()), "component_diagnostic": True}
    effects = []
    entered = asyncio.Event()
    finish = asyncio.Event()

    async def object_info(request):
        effects.append("startup_info")
        return web.json_response({"ComponentNode": {}})

    async def mutation(request):
        effects.append("mutation")
        entered.set()
        await finish.wait()
        effects.append("mutation_settled")
        return web.json_response({"accepted": True})

    async def read(request):
        return web.json_response({"read": True})

    async def forbidden(request):
        effects.append("forbidden")
        return web.Response()

    app = web.Application()
    app.router.add_get('/object_info', object_info)
    for path in ('/prompt', '/api/prompt', '/upload/image', '/api/upload/image', '/upload/mask', '/queue', '/interrupt'):
        app.router.add_post(path, mutation)
    app.router.add_get('/system_stats', read)
    for path in ('/free', '/api/free', '/settings', '/internal/logs/subscribe', '/ws', '/api/ws', '/vhs/viewvideo'):
        app.router.add_route('*', path, forbidden)
    sources = {str(p.resolve()): hashlib.sha256(p.read_bytes()).hexdigest()
               for p in (Path(__file__), Path(inspect.getsourcefile(web_urldispatcher._default_expect_handler)))}
    fence = Fence(child, enrollment, sources)
    server = SimpleNamespace(app=app, loop=loop, asset_manager=SimpleNamespace(enabled=False))
    args = SimpleNamespace(listen='127.0.0.1', port=8188, disable_assets=True, enable_manager=False, enable_agent=False)
    fence.install_server(server, args)
    q = SimpleNamespace(queue=[], currently_running={}, flags={})
    fence.queue = q
    sequence = 0
    binding = {'association_id': 'component', 'binding': {'complete': 'diagnostic'}, 'generation': 1}
    request = {'request_id': 'a' * 32, 'deadline_ns': time.monotonic_ns() + 5_000_000_000}

    async def control(op, payload):
        nonlocal sequence
        sequence += 1
        await loop.sock_sendall(parent, canonical({'version': VERSION, 'enrollment': enrollment,
            'sequence': sequence, 'operation': op, 'payload': payload}))
        return decode(await asyncio.wait_for(loop.sock_recv(parent, 65536), 2))

    async def exercise():
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        url = 'http://127.0.0.1:' + str(site._server.sockets[0].getsockname()[1])
        try:
            assert (await control('enroll', {}))['state'] == 'OPEN'
            await control('bind', binding)
            async with aiohttp.ClientSession() as client:
                pending = asyncio.create_task(client.post(url + '/prompt', data=b'{}'))
                await asyncio.wait_for(entered.wait(), 2)
                closed = await control('release', {**binding, 'request': request})
                assert closed['state'] == 'DRAINING' and len(fence.handlers) == 1
                fence.worker_thread = threading.get_ident()
                fence.boundary(q, None, False, 0)
                assert fence.record['status'] == 'draining'
                for path in ('/prompt', '/api/prompt', '/upload/image', '/api/upload/image', '/upload/mask', '/queue', '/interrupt'):
                    async with client.post(url + path) as response:
                        assert response.status == 503
                for path in ('/free', '/api/free', '/settings', '/internal/logs/subscribe', '/ws', '/api/ws', '/vhs/viewvideo'):
                    async with client.post(url + path) as response:
                        assert response.status == 403
                with pytest.raises(aiohttp.WSServerHandshakeError):
                    await client.ws_connect(url + '/ws')
                async with client.get(url + '/system_stats') as response:
                    assert response.status == 200 and await response.json() == {'read': True}
                finish.set()
                response = await pending
                assert response.status == 200
                await response.read()
                # Even an HTTP-completed mutation does not settle a worker
                # continuation or pending flag/GC cleanup.
                q.queue.append('descendant')
                fence.boundary(q, None, False, 0)
                assert fence.record['status'] == 'draining'
                q.queue.clear()
                q.flags['free_memory'] = True
                fence.boundary(q, None, False, 0)
                assert fence.record['status'] == 'draining'
                q.flags.clear()
                assert fence.boundary(q, None, True, 100) == 0
                assert fence.record['status'] == 'draining'
                fence.boundary(q, None, False, 0)
                settled = await control('observe', request)
                assert settled['record']['status'] == 'settled'
                assert settled['record']['effect_sent'] is False
                assert effects == ['startup_info', 'mutation', 'mutation_settled']
                if fault == 'controller_loss':
                    # No async-reader scheduling assumption on this check.
                    parent.close()
                elif fault == 'timeout':
                    await asyncio.sleep(max(0, (request['deadline_ns'] - time.monotonic_ns()) / 1e9) + .01)
                    fence.boundary(q, None, False, 0)
                    assert fence.record['status'] == 'unresolved'
                    assert fence.record['effect_sent'] is False
                else:
                    sequence_before = sequence
                    payload = ({**binding, 'generation': 0} if fault == 'stale_generation' else {})
                    op = 'bind' if fault == 'stale_generation' else 'unrecognized'
                    await loop.sock_sendall(parent, canonical({'version': VERSION, 'enrollment': enrollment,
                        'sequence': sequence_before + 1, 'operation': op, 'payload': payload}))
                    assert await asyncio.wait_for(loop.sock_recv(parent, 65536), 2) == b''
                async with client.post(url + '/prompt') as response:
                    assert response.status in (403, 503)
                assert fence.state == 'UNCERTAIN'
        finally:
            finish.set()
            await runner.cleanup()
    try:
        loop.run_until_complete(exercise())
    finally:
        parent.close()
        child.close()
        os.close(fence.controller_fd)
        for task in asyncio.all_tasks(loop):
            task.cancel()
        loop.run_until_complete(asyncio.gather(*asyncio.all_tasks(loop), return_exceptions=True))
        loop.close()
