"""The latency budget observes attempted operations and all submitted launches."""
import asyncio
from types import SimpleNamespace

import pytest

from tests.packs.runpod._budget import RemoteBudget


def test_shell_process_starts_include_nested_groups_but_not_python_bodies():
    command = '''finish_logs() { ( /opt/astrid/bin/python -c 'print("python3 fake")';
/opt/astrid/bin/python -c 'print("astrid generate")' ); }
python3 -c 'print("python ignored")'
if [ "$rc" -ne 0 ]; then /opt/astrid/bin/python -c 'pass'; fi
exec /opt/astrid/bin/astrid generate test
'''
    assert RemoteBudget.process_starts(command) == 5


def test_budget_counts_all_pod_operations_and_failure_attempts(monkeypatch):
    fake = SimpleNamespace(**{name: lambda *a, **kw: None for name in
                             ("exec", "put", "get", "job_start", "job_poll", "job_cancel")})
    budget = RemoteBudget()
    budget.pod(fake, monkeypatch)
    fake.exec("python3 -c 'pass'")
    fake.job_start(command="astrid generate test\npython -c 'pass'")
    for name in ("put", "get", "job_poll", "job_cancel"):
        getattr(fake, name)("fake")
    budget.assert_within({"exec": 1, "put": 1, "get": 1, "job_start": 1,
                          "job_poll": 1, "job_cancel": 1, "process_start": 3})
    with pytest.raises(AssertionError, match="per-kind counts"):
        budget.assert_within({})


def test_https_fake_and_async_api_share_the_counter(monkeypatch):
    async def api():
        raise OSError("fake failure")

    fake = SimpleNamespace(api=api, **{name: lambda *a: None for name in
                           ("execute_command", "put", "get", "detached_start", "detached_poll", "detached_cancel")})
    budget = RemoteBudget()
    budget.https(fake, monkeypatch)
    budget.wrap(monkeypatch, fake, "api", kind="provider.api")
    fake.execute_command("python -c 'pass'")
    fake.detached_start("astrid generate test")
    for name in ("put", "get", "detached_poll", "detached_cancel"):
        getattr(fake, name)("fake")
    with pytest.raises(OSError, match="fake failure"):
        asyncio.run(fake.api())
    assert budget.counts == {"exec": 1, "put": 1, "get": 1, "job_start": 1,
                             "job_poll": 1, "job_cancel": 1, "process_start": 2, "provider.api": 1}


def test_class_transport_wrapper_accepts_keyword_commands(monkeypatch):
    class Transport:
        def execute_command(self, command):
            return command

    budget = RemoteBudget()
    budget.wrap(monkeypatch, Transport, "execute_command", kind="exec", command=True)
    transport = Transport()
    transport.execute_command(command="python3 -c 'pass'")
    transport.execute_command("astrid generate test")
    assert budget.counts == {"exec": 2, "process_start": 2}


def test_hidden_retry_chunks_threads_and_helper_children_share_one_collector(monkeypatch, tmp_path):
    import base64
    import hashlib
    import shlex
    import sys
    from concurrent.futures import ThreadPoolExecutor

    import httpx
    from runpod_lifecycle.https import CHUNK, HttpsPodTransport

    budget = RemoteBudget(tmp_path / 'budget.sqlite')
    budget.http_attempts(monkeypatch)
    data = b'x' * (CHUNK + 1)
    retry = [True]
    def respond(request):
        if request.url.path == '/uploads':
            return httpx.Response(200, json={'id': 'a' * 32, 'offset': 0, 'complete': False})
        if request.method == 'PUT':
            if retry[0]:
                retry[0] = False
                return httpx.Response(503, json={'error': 'injected retry'})
            end = int(request.url.params['offset']) + len(request.content)
            return httpx.Response(200, json={'offset': end, 'complete': end == len(data)})
        body = __import__('json').loads(request.content)
        offset = body['offset']
        chunk = data[offset:offset + CHUNK]
        return httpx.Response(200, json={'offset': offset, 'total': len(data), 'etag': 'one',
            'sha256': hashlib.sha256(chunk).hexdigest(), 'data': base64.b64encode(chunk).decode()})
    source = tmp_path / 'source'
    source.write_bytes(data)
    remote = HttpsPodTransport('fake', 'f' * 32, base_url='http://localhost', poll_interval=0)
    remote.http.close()
    remote.http = httpx.Client(base_url='http://localhost', transport=httpx.MockTransport(respond))
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            pool.submit(remote.put, source, '/fake').result()
            pool.submit(remote.get, '/fake', tmp_path / 'download').result()
        assert (tmp_path / 'download').read_bytes() == data
        assert budget.counts['put.create'] == 1
        assert budget.counts['put.chunk'] == 3  # two chunks plus a real transport retry
        assert budget.counts['get.chunk'] == 2
        # A stale two-chunk ceiling must reject the extra attempt, even in a thread.
        with pytest.raises(AssertionError, match='put.chunk'):
            budget.assert_within({'put.create': 1, 'put.chunk': 2, 'get.chunk': 2})
        child = 'import subprocess,sys; subprocess.run([sys.executable,"-c","pass"],check=True)'
        result = budget.run_pod(f'{shlex.quote(sys.executable)} -c {shlex.quote(child)}')
        assert result.returncode == 0, result.stderr
        assert budget.counts['process_start'] == 2
        # A one-interpreter ceiling rejects a hidden Python grandchild.
        with pytest.raises(AssertionError, match='process_start'):
            budget.assert_within({'put.create': 1, 'put.chunk': 3, 'get.chunk': 2,
                                  'process_start': 1, 'process_launch': 3})
        # The helper child opens exactly the same collector, with an atomic add.
        program = ("from tests.packs.runpod._budget import SharedCounts; "
                   f"SharedCounts({str(budget.counts.path)!r}).add('helper.attempt')")
        result = budget.run_pod(f'{shlex.quote(sys.executable)} -c {shlex.quote(program)}')
        assert result.returncode == 0, result.stderr
        assert budget.counts['helper.attempt'] == 1
        # Fresh helper imports install the request interceptor through the
        # test-only sitecustomize; a parent monkeypatch cannot cover spawn.
        program = ("import httpx; "
                   "c=httpx.Client(base_url='http://localhost', "
                   "transport=httpx.MockTransport(lambda r: httpx.Response(200,json={}))); "
                   "c.get('/hidden-helper')")
        result = budget.run_pod(f'{shlex.quote(sys.executable)} -c {shlex.quote(program)}')
        assert result.returncode == 0, result.stderr
        assert budget.counts['http.get.hidden-helper'] == 1
        with pytest.raises(AssertionError, match='hidden-helper'):
            budget.assert_within({'put.create': 1, 'put.chunk': 3, 'get.chunk': 2,
                                  'process_start': 4, 'process_launch': 7, 'helper.attempt': 1})

    finally:
        remote.disconnect()


def test_shared_collector_does_not_lose_thread_updates(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    budget = RemoteBudget(tmp_path / 'budget.sqlite')
    def attempt(_):
        for _ in range(50):
            budget.counts.add('attempt')
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(attempt, range(4)))
    assert budget.counts['attempt'] == 200


def test_fork_without_exec_is_still_a_pod_process_start():
    import shlex
    import sys
    budget = RemoteBudget()
    program = 'import os; pid=os.fork(); os._exit(0) if pid==0 else os.waitpid(pid,0)'
    result = budget.run_pod(f'{shlex.quote(sys.executable)} -c {shlex.quote(program)}')
    assert result.returncode == 0, result.stderr
    assert budget.counts['process_start'] == 1
    assert budget.counts['process_fork'] == 1
