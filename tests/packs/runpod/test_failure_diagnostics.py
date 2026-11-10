"""CPU proof of the fixed failure protocol; no provider or pod is contacted."""
import json
from pathlib import Path
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest

from astrid.packs.runpod import failure_diagnostics as fd
from astrid.packs.runpod import machine as rm

SENTINEL = 'SENTINEL_TOKEN https://private.example/secret private-output'


def _malformed_bounded_child(send, _pod, _token, deadline, _base_url, _name, args, _kwargs):
    import os
    import struct
    import time
    mode, marker = args
    Path(marker).write_text(mode)
    if mode == 'partial':
        os.write(send.fileno(), struct.pack('!i', 100) + b'{')
        time.sleep(10)
    elif mode == 'oversized':
        os.write(send.fileno(), struct.pack('!i', 2 * 1024 * 1024 + 1))
        time.sleep(10)
    elif mode == 'late':
        time.sleep(max(0, deadline - time.monotonic()) + 0.1)
        send.send_bytes(b'{"ok":true,"value":{"id":"late"}}')
    send.close()


def emit(tmp_path, raw):
    path = tmp_path / 'result.json'
    path.write_bytes(raw)
    graph = tmp_path / 'source.json'
    graph.write_text(json.dumps({'nodes': {'17': {'class_type': 'KSampler'}}}))
    job = tmp_path / 'job.json'
    job.write_text(json.dumps({'execution': {'source': str(graph)}}))
    command = rm.generation_command('cpu-image', '/fake/run', mode='t2i')
    # Execute the exact producer projection shipped in the failed-job branch.
    branch = command.split('if [ $rc -ne 0 ]; then ', 1)[1].split('; exit $rc; fi', 1)[0]
    argv = shlex.split(branch)
    argv[0], argv[-2], argv[-1] = sys.executable, str(path), str(job)
    result = subprocess.run(argv, capture_output=True, text=True, check=True)
    assert not result.stderr and SENTINEL not in result.stdout
    return result.stdout


@pytest.mark.parametrize('payload,expected', [
    ({'issues': [{'severity': 'error', 'code': 'input_rejected', 'message': SENTINEL,
                  'details': {'engine_code': 'download_failed', 'url': SENTINEL}}]},
     {'code': 'download_failed', 'category': 'model'}),
    ({'error': {'code': 'deadline_exceeded', 'message': SENTINEL}},
     {'code': 'deadline_exceeded', 'category': 'deadline'}),
    ({'issues': [{'severity': 'warning', 'code': 'missing_model', 'message': SENTINEL}]}, fd.fallback()),
    ({'error': {'code': SENTINEL, 'message': SENTINEL}}, fd.fallback()),
    ({'error': {'code': ['download_failed'], 'message': SENTINEL}}, fd.fallback()),
])
def test_emitted_failure_is_bounded_allowlisted_and_redacted(tmp_path, payload, expected):
    stdout = emit(tmp_path, json.dumps(payload).encode())
    assert fd.parse_frame(stdout) == expected
    assert len(stdout) < 200


@pytest.mark.parametrize('raw', [b'not-json', b'x' * (fd.MAX_RESULT_BYTES + 1), b'[]', b'null'])
def test_unreadable_or_invalid_result_has_generic_fallback(tmp_path, raw):
    assert fd.parse_frame(emit(tmp_path, raw)) == fd.fallback()


def test_result_symlink_and_missing_file_are_not_read(tmp_path):
    secret = tmp_path / 'secret'
    secret.write_text(json.dumps({'code': 'download_failed', 'message': SENTINEL}))
    link = tmp_path / 'link'
    link.symlink_to(secret)
    assert fd.read_result(link) == fd.fallback()
    assert fd.read_result(tmp_path / 'missing') == fd.fallback()


@pytest.mark.parametrize('stdout', [
    SENTINEL, fd.MARK + '{broken', fd.MARK + json.dumps({'code': SENTINEL, 'category': 'model'}),
    fd.MARK + json.dumps({'code': 'download_failed', 'category': SENTINEL}),
    fd.MARK + json.dumps({'code': 'download_failed', 'category': 'model', 'message': SENTINEL}),
    (fd.MARK + '{"code":"download_failed","category":"model"}\n') * 2,
    'x' * (fd.MAX_FRAME_BYTES + 1),
])
def test_untrusted_frames_never_escape_or_select_unknown_categories(stdout):
    assert fd.parse_frame(stdout) == fd.fallback()


def test_host_summary_keeps_only_bounded_failure_facts():
    diagnostic = {'code': 'generation_failed', 'category': 'execution', 'node_id': '17',
                  'node_class': 'KSampler', 'field': SENTINEL, 'constraint': 'min_value'}
    summary = fd.host_summary(RuntimeError(SENTINEL), diagnostic, stage='fetch', node_ids={'17'})
    assert summary == {'exception': 'RuntimeError', 'stage': 'fetch', 'code': 'generation_failed',
                       'category': 'execution', 'node_id': '17', 'node_class': 'KSampler',
                       'constraint': 'min_value'}
    assert SENTINEL not in json.dumps(summary)


def test_remote_issue_fields_are_allowlisted_and_redacted(tmp_path):
    payload = {'issues': [{'severity': 'error', 'code': 'generation_failed', 'message': SENTINEL,
                           'details': {'node_id': '17', 'node_class': 'KSampler',
                                       'field': SENTINEL, 'constraint': 'min_value'}}]}
    frame = emit(tmp_path, json.dumps(payload).encode())
    assert fd.parse_frame(frame, node_ids={'17'}) == {'code': 'generation_failed', 'category': 'execution',
                                     'node_id': '17', 'node_class': 'KSampler',
                                     'constraint': 'min_value'}
    assert SENTINEL not in frame


def test_journal_summary_is_revalidated_before_display():
    stored = {'exception': 'RuntimeError', 'stage': 'fetch', 'code': 'generation_failed',
              'category': 'execution', 'node_id': SENTINEL, 'prompt': SENTINEL}
    result = fd.safe_stored_summary(stored)
    assert result == {'exception': 'RuntimeError', 'stage': 'fetch',
                      'code': 'generation_failed', 'category': 'execution'}
    assert SENTINEL not in json.dumps(result)
    hostile_class = type('SENTINEL_TOKEN', (Exception,), {})
    assert fd.host_summary(hostile_class(SENTINEL))['exception'] == 'UnknownError'


def test_valid_node_facts_survive_owner_journal_round_trip():
    source = {'exception': 'MachineError', 'stage': 'fetch', 'code': 'generation_failed',
              'category': 'execution', 'node_id': '0017', 'node_class': 'KSampler',
              'field': 'seed', 'constraint': 'min_value'}
    projected = fd.safe_stored_summary(source, node_ids={'17'})
    assert projected == {**source, 'node_id': '17'}
    assert fd.safe_stored_summary(projected, node_ids={'17'}) == projected
    row = rm.lease_row({'pod_id': 'pod-1', 'state': 'claimed',
                        'lease': {'state': 'idle', 'paid_expires_at': 1000, 'idle_expires_at': 900,
                                  'hourly_usd': 1, 'accounted_at': 100,
                                  'last_job': {'outcome': 'failed', 'generation': 1,
                                               'failure': projected, 'structural_node_ids': ['17']}}})
    assert row['last_job']['failure'] == projected
    assert 'node_id' not in fd.safe_stored_summary(source)


def test_synthetic_identifier_secrets_are_omitted_from_node_facts():
    source = {'code': 'generation_failed', 'category': 'execution', 'node_id': 'idABC123',
              'node_class': 'SENTINELTOKEN123', 'field': 'private-key-123',
              'constraint': 'SENTINELTOKEN123'}
    projected = fd.host_summary(RuntimeError(SENTINEL), source, stage='fetch')
    assert projected == {'exception': 'RuntimeError', 'stage': 'fetch',
                         'code': 'generation_failed', 'category': 'execution'}
    assert 'SENTINEL' not in json.dumps(projected)


def test_digits_only_secret_is_not_structural_graph_node(tmp_path):
    payload = {'issues': [{'severity': 'error', 'code': 'generation_failed',
                           'details': {'node_id': '999999999999', 'node_class': 'KSampler'}}]}
    frame = emit(tmp_path, json.dumps(payload).encode())
    assert '999999999999' not in frame
    projected = fd.parse_frame(frame, node_ids={'17'})
    assert projected == {'code': 'generation_failed', 'category': 'execution',
                         'node_class': 'KSampler'}
    assert '999999999999' not in json.dumps(fd.safe_stored_summary(
        fd.host_summary(RuntimeError(), projected, stage='fetch', node_ids={'17'})))


def test_failure_observer_uses_one_scoped_transport_deadline(monkeypatch):
    from runpod_lifecycle import https
    from astrid.packs.runpod.remote_placement import _HttpsOps
    seen = []

    class Transport:
        def __init__(self, pod_id, token, *, deadline=None):
            seen.append((pod_id, deadline))
        def execute_command(self, command, timeout):
            return 0, "ok", ""
        def disconnect(self):
            pass

    monkeypatch.setattr(https, "HttpsPodTransport", Transport)
    monkeypatch.setattr(_HttpsOps, "_bounded_call", _HttpsOps._call_direct)
    original = _HttpsOps("pod-1", "private-token")
    observer = original.bounded(10**12)
    assert observer.exec("status", 12) == (0, "ok", "")
    assert seen == [("pod-1", 10**12)] and original.deadline is None


@pytest.mark.parametrize('mode', ['slow_drip', 'route_retry'])
def test_real_https_slow_drip_is_killed_at_outer_deadline(mode):
    import multiprocessing
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    import time
    from astrid.packs.runpod.remote_placement import _HttpsOps, RemotePlacementError

    class Drip(BaseHTTPRequestHandler):
        def do_POST(self):
            self.do_GET()
        def do_GET(self):
            if mode == 'route_retry':
                self.send_response(404)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            self.send_response(200)
            self.send_header('Content-Length', '100')
            self.end_headers()
            for _ in range(100):
                try:
                    self.wfile.write(b' ')
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
                time.sleep(0.025)
        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Drip)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    token = 'private-token-' + 'x' * 32
    children_before = {child.pid for child in multiprocessing.active_children()}
    try:
        start = time.monotonic()
        ops = _HttpsOps('pod1', token,
                        base_url=f'http://127.0.0.1:{server.server_port}').bounded(start + 0.35)
        with pytest.raises(RemotePlacementError) as caught:
            ops.job_poll('a' * 32)
        assert time.monotonic() - start < 0.85
        assert {child.pid for child in multiprocessing.active_children()} == children_before
        assert token not in str(caught.value)
    finally:
        server.shutdown()
        server.server_close()


@pytest.mark.parametrize('mode', ['slow_drip', 'route_retry'])
def test_real_https_request_enters_then_observer_deadline_fences_result(mode):
    import multiprocessing
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Event, Thread
    import time
    from astrid.packs.runpod.remote_placement import _HttpsOps, RemotePlacementError

    entered = Event()
    requests, writes = [], []
    class Slow(BaseHTTPRequestHandler):
        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            except (ValueError, TypeError):
                body = {}
            requests.append((self.path, body.get('path')))
            self.do_GET()
        def do_GET(self):
            entered.set()
            if mode == 'route_retry':
                self.send_response(404)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            self.send_response(200)
            self.send_header('Content-Length', '1000')
            self.end_headers()
            for _ in range(1000):
                try:
                    self.wfile.write(b' ')
                    self.wfile.flush()
                    writes.append(1)
                except (BrokenPipeError, ConnectionResetError):
                    break
                time.sleep(0.025)
        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Slow)
    Thread(target=server.serve_forever, daemon=True).start()
    children_before = {child.pid for child in multiprocessing.active_children()}
    try:
        start = time.monotonic()
        ops = _HttpsOps('pod1', 'x' * 32,
                        base_url=f'http://127.0.0.1:{server.server_port}').bounded(start + 10)
        with pytest.raises(RemotePlacementError, match='deadline expired'):
            ops.job_poll('a' * 32)
        assert entered.is_set()  # This case exercised actual pinned HTTP.
        assert requests and all(path == '/download-chunk' and isinstance(source, str)
                                and ('a' * 32) in source and source.endswith('/status')
                                for path, source in requests)
        assert (len(requests) > 1 if mode == 'route_retry' else len(writes) > 1)
        assert 8.5 <= time.monotonic() - start < 10.8
        assert {child.pid for child in multiprocessing.active_children()} == children_before
    finally:
        server.shutdown()
        server.server_close()


def test_bounded_observer_has_no_token_in_process_metadata():
    import multiprocessing
    import time
    from astrid.packs.runpod.remote_placement import _bounded_https_call_child

    token = 'secret-child-token-' + 'x' * 32
    context = multiprocessing.get_context('spawn')
    recv, send = context.Pipe(duplex=False)
    child = context.Process(target=_bounded_https_call_child,
                            args=(send, 'pod1', token, time.monotonic() + 10,
                                  'http://127.0.0.1:9', 'detached_poll', ('a' * 32,), {}))
    try:
        child.start()
        assert token.encode() not in Path(f'/proc/{child.pid}/cmdline').read_bytes()
    finally:
        if child.is_alive():
            child.kill()
        child.join(2)
        assert not child.is_alive()
        child.close()
        recv.close()
        send.close()


@pytest.mark.parametrize('mode', ['partial', 'oversized', 'late', 'eof'])
def test_bounded_observer_rejects_malformed_or_late_child_frame(tmp_path, monkeypatch, mode):
    import multiprocessing
    import time
    from astrid.packs.runpod import remote_placement as rp

    marker = tmp_path / 'child-entered'
    monkeypatch.setattr(rp, '_bounded_https_call_child', _malformed_bounded_child)
    before = {child.pid for child in multiprocessing.active_children()}
    start = time.monotonic()
    ops = rp._HttpsOps('pod1', 'x' * 32, deadline=start + 2)
    with pytest.raises(rp.RemotePlacementError) as caught:
        ops._bounded_call('detached_poll', mode, str(marker))
    assert marker.read_text() == mode  # The child reached the malformed IPC case.
    assert time.monotonic() - start < 2.6
    assert 'SENTINEL' not in str(caught.value)
    assert {child.pid for child in multiprocessing.active_children()} == before


@pytest.mark.parametrize("mode", ["blocked_response", "route_retry"])
def test_pinned_https_receipt_read_stops_at_scoped_deadline(tmp_path, monkeypatch, mode):
    import httpx
    import time
    from runpod_lifecycle.https import HttpsPodTransport

    transport = HttpsPodTransport("pod1", "x" * 32, base_url="http://127.0.0.1:8000",
                                  deadline=time.monotonic() + 10,
                                  request_timeout=0.02, poll_interval=0.005, route_grace=1)
    transport.deadline = time.monotonic() + 0.5
    calls = []

    def request(*args, **kwargs):
        calls.append(kwargs.get("timeout"))
        if mode == "blocked_response":
            time.sleep(0.6)  # Simulate a transport that returns after its per-phase timeout.
            raise httpx.ReadTimeout("SENTINEL_TOKEN private response")
        return SimpleNamespace(status_code=404, content=b"", headers={})

    monkeypatch.setattr(transport.http, "request", request)
    try:
        with pytest.raises(Exception) as caught:
            transport.get("/fixed/install.json", tmp_path / "receipt")
        assert "SENTINEL_TOKEN" not in str(caught.value)
        assert calls and all(0 < timeout <= 0.5 for timeout in calls)
        assert not (tmp_path / "receipt").exists()
    finally:
        transport.disconnect()


@pytest.mark.parametrize('exit_code', [2, SENTINEL])
def test_fetch_surfaces_only_fixed_diagnostic_and_safe_exit(tmp_path, exit_code):
    stdout = emit(tmp_path, json.dumps({'error': {'code': 'download_failed', 'message': SENTINEL}}).encode())
    calls = []
    class Ops:
        def job_poll(self, job):
            return {'state': 'exited', 'exit_code': exit_code}
        def exec(self, command, timeout):
            calls.append(command)
            return 0, stdout, SENTINEL
    run = SimpleNamespace(job_id='fake-job', directory='/fake/job',
                          lease=SimpleNamespace(state=None, claim=SimpleNamespace(ops=Ops())))
    machine = rm.RunPodMachine(SimpleNamespace(placement=None))
    machine._checkpoint = lambda *args: None
    with pytest.raises(rm.MachineError) as caught:
        machine.fetch(run, tmp_path / 'unused')
    assert str(caught.value) == ('remote generation failed (exit '
        + ('2' if exit_code == 2 else 'unknown') + '; category=model; code=download_failed)')
    assert SENTINEL not in str(caught.value)
    assert calls == [f'tail -c {fd.MAX_FRAME_BYTES + 1} /fake/job/stdout']


def test_real_job_cli_keeps_structured_failure_code(monkeypatch, tmp_path, capsys):
    from astrid.core.generation import cli, local_job
    from astrid.core.generation.preflight import generation_error
    error = generation_error({'code': 'download_failed', 'message': SENTINEL})
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(local_job, 'run_job', fail)
    assert cli.main(['cpu-image', '--placement', 'local', '--job', str(tmp_path / 'job.json'),
                     '--out', str(tmp_path / 'out'), '--json']) == 2
    envelope = json.loads(capsys.readouterr().out)
    stdout = emit(tmp_path, json.dumps(envelope).encode())
    assert fd.parse_frame(stdout) == {'code': 'download_failed', 'category': 'model'}
    assert SENTINEL not in stdout
