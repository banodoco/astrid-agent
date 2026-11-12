"""CPU proof of the fixed failure protocol; no provider or pod is contacted."""
import json
import os
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


def emit(tmp_path, raw, graph_body=None):
    path = tmp_path / 'result.json'
    path.write_bytes(raw)
    graph = tmp_path / 'source.json'
    graph.write_text(json.dumps(graph_body if graph_body is not None else {'nodes': {'17': {'class_type': 'KSampler'}}}))
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


@pytest.mark.parametrize('mode', [
    pytest.param('partial', marks=pytest.mark.skipif(sys.platform == 'win32', reason='raw pipe FD is POSIX-only')),
    pytest.param('oversized', marks=pytest.mark.skipif(sys.platform == 'win32', reason='raw pipe FD is POSIX-only')),
    'late', 'eof',
])
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


@pytest.mark.skipif(sys.platform == 'win32', reason='raw partial-frame pipe writing is POSIX-only')
@pytest.mark.parametrize('early_abort', [False, True])
def test_partial_ipc_from_unkillable_caller_returns_with_pending_fence(monkeypatch, early_abort):
    import multiprocessing
    import os
    import struct
    from threading import Event, Timer
    import time
    from astrid.packs.runpod import remote_placement as rp

    real = multiprocessing.get_context('spawn')
    children = []
    class Unkillable:
        def __init__(self, *, target, args):
            self.send = args[0]
            self.daemon = False
            self.writer = None
            self.kills = 0
            self.alive = True
            children.append(self)
        def start(self):
            self.writer = os.dup(self.send.fileno())
            os.write(self.writer, struct.pack('!i', 100) + b'{')
        def is_alive(self): return self.alive
        def kill(self): self.kills += 1
        def join(self, timeout=None): pass
        def close(self):
            assert not self.alive, 'an unkillable caller must stay fenced'

    monkeypatch.setattr(rp.multiprocessing, 'get_context',
                        lambda method: SimpleNamespace(Pipe=real.Pipe, Process=Unkillable))
    abort = Event()
    timer = Timer(0.1, abort.set) if early_abort else None
    deadline = time.monotonic() + (120 if early_abort else 0.5)
    ops = rp._HttpsOps('pod1', 'private-token', deadline=deadline,
                       abort_event=abort if early_abort else None)
    try:
        if timer is not None:
            timer.start()
        start = time.monotonic()
        with pytest.raises(rp.BoundedObserverUnreaped):
            ops.job_poll('a' * 32)
        assert time.monotonic() - start < 1.0
        assert ops.unreaped_child.process is children[0] and children[0].kills > 0
        assert 'private-token' not in repr(ops.unreaped_child)
    finally:
        if timer is not None:
            timer.cancel()
        for child in children:
            if child.writer is not None:
                os.close(child.writer)
            child.alive = False
        if getattr(ops, 'unreaped_child', None) is not None:
            ops.unreaped_child.join(0.1)
            ops.unreaped_child.close()


def test_portable_blocked_pipe_reader_is_fenced_after_early_abort(monkeypatch):
    import multiprocessing
    from threading import Event, Timer
    import time
    from astrid.packs.runpod import remote_placement as rp

    unblock = Event()
    class Receive:
        def recv_bytes(self, maxlength):
            assert maxlength == 2 * 1024 * 1024
            unblock.wait(10)
            raise EOFError
        def close(self): pass
    class Send:
        def close(self): pass
    class SurvivingProcess:
        alive = True
        daemon = False
        def __init__(self, *, target, args): pass
        def start(self): pass
        def is_alive(self): return self.alive
        def kill(self): pass
        def join(self, timeout=None): pass
        def close(self): assert not self.alive

    process = SurvivingProcess(target=None, args=())
    monkeypatch.setattr(rp.multiprocessing, 'get_context', lambda method: SimpleNamespace(
        Pipe=lambda duplex: (Receive(), Send()), Process=lambda **kwargs: process))
    abort = Event()
    timer = Timer(0.1, abort.set)
    ops = rp._HttpsOps('pod1', 'private-token', deadline=time.monotonic() + 120,
                       abort_event=abort)
    try:
        timer.start()
        start = time.monotonic()
        with pytest.raises(rp.BoundedObserverUnreaped):
            ops.job_poll('a' * 32)
        assert time.monotonic() - start < 1.0
        assert ops.unreaped_child.process is process and ops.unreaped_child.is_alive()
    finally:
        timer.cancel()
        process.alive = False
        unblock.set()
        ops.unreaped_child.join(0.1)
        ops.unreaped_child.close()


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
    # The tail carries no logs marker, so exactly one bounded marker read follows it.
    assert calls == [f'tail -c {fd.MAX_FRAME_BYTES + 1} /fake/job/stdout',
                     f"grep -a '^{rm.LOGS_MANIFEST_MARK}' /fake/job/stdout | tail -n 1"]
    assert run.lease.failure_logs == {'unavailable': 'not_reported'}


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

@pytest.mark.parametrize('failure,expected', [
    (ValueError(SENTINEL), ('generation_failed', 'execution')),
    ('input', ('input_rejected', 'input')),
    ('download', ('download_failed', 'model')),
    ('unknown_code', ('generation_failed', 'execution')),
])
def test_job_failure_crosses_cli_frame_and_journal_without_private_text(
        monkeypatch, tmp_path, capsys, failure, expected):
    from astrid.core.contracts.errors import AstridError
    from astrid.core.generation import cli, local_job
    from astrid.core.generation.engine_contracts import DownloadError
    from astrid.core.generation.target_inventory import POD_ALIGN

    if failure == 'input':
        failure = local_job.JobInputError(SENTINEL)
    elif failure == 'download':
        failure = DownloadError(SENTINEL)
    elif failure == 'unknown_code':
        failure = AstridError(SENTINEL, code='producer_failed')

    job = tmp_path / 'job.json'
    job.write_text(json.dumps({'schema': local_job.SCHEMA, 'align': 'trust', 'inputs': {}}))
    previous_align = POD_ALIGN.get()
    def fail(*args, **kwargs):
        assert POD_ALIGN.get() == 'trust'
        raise failure
    monkeypatch.setattr(local_job, '_run_job', fail)
    assert cli.main(['cpu-image', '--placement', 'local', '--job', str(job),
                     '--out', str(tmp_path / 'out'), '--json']) == 2
    assert POD_ALIGN.get() == previous_align
    envelope = json.loads(capsys.readouterr().out)
    frame = emit(tmp_path, json.dumps(envelope).encode())
    diagnostic = fd.parse_frame(frame)
    assert (diagnostic['code'], diagnostic['category']) == expected
    journal = fd.host_summary(RuntimeError(SENTINEL), diagnostic, stage='fetch')
    row = rm.lease_row({'pod_id': 'pod-1', 'state': 'claimed',
                        'lease': {'state': 'idle', 'paid_expires_at': 1000, 'idle_expires_at': 900,
                                  'hourly_usd': 1, 'accounted_at': 100,
                                  'last_job': {'outcome': 'failed', 'generation': 1,
                                               'failure': journal}}})
    assert row['last_job']['failure'] == journal
    assert SENTINEL not in frame and SENTINEL not in json.dumps(row)


# The real TTS workflow: vibecomfy ready_templates/sources/custom_nodes/qwen_tts/1038lab/
# qwen3_tts_custom_voice.json (ComfyUI API format, the bundle's ``source`` member;
# entry audio/qwen3_tts_custom_voice). Node class and input names are verbatim.
TTS_PROMPT = 'PROMPT_SECRET say this private sentence aloud'
TTS_GRAPH = {
    '1': {'class_type': 'AILab_Qwen3TTSCustomVoice',
          'inputs': {'text': TTS_PROMPT, 'speaker': 'Ryan', 'model_size': '0.6B', 'language': 'English',
                     'instruct': 'Calm, clear, friendly delivery.', 'unload_models': True, 'seed': 123}},
    '2': {'class_type': 'SaveAudioMP3',
          'inputs': {'audio': ['1', 0], 'filename_prefix': 'audio/qwen3_tts_custom_voice', 'quality': 'V0'}},
}


def _tts_failure(tmp_path, details, graph=TTS_GRAPH):
    payload = {'issues': [{'severity': 'error', 'code': 'input_rejected', 'message': SENTINEL, 'details': details}]}
    frame = emit(tmp_path, json.dumps(payload).encode(), graph)
    source = tmp_path / 'source.json'
    ids, names = fd.graph_facts(source)
    return frame, fd.parse_frame(frame, node_ids=ids, node_names=names), ids, names


def test_tts_failure_names_the_real_node_class_and_field(tmp_path):
    frame, projected, ids, names = _tts_failure(
        tmp_path, {'node_id': '1', 'node_class': 'AILab_Qwen3TTSCustomVoice', 'field': 'speaker'})
    assert projected == {'code': 'input_rejected', 'category': 'input', 'node_id': '1',
                         'node_class': 'AILab_Qwen3TTSCustomVoice', 'field': 'speaker'}
    assert 'AILab_Qwen3TTSCustomVoice' in frame and 'speaker' in frame  # survived the pod-side projection too
    # The same frame on the controller with no graph is reduced as before.
    assert fd.parse_frame(frame) == {'code': 'input_rejected', 'category': 'input'}
    assert ids == {'1', '2'} and {'AILab_Qwen3TTSCustomVoice', 'SaveAudioMP3', 'model_size', 'speaker'} <= names


def test_names_outside_the_submitted_graph_are_still_unknown(tmp_path):
    frame, projected, _ids, _names = _tts_failure(
        tmp_path, {'node_id': '1', 'node_class': 'SomeOtherCustomNode', 'field': 'not_in_this_graph'})
    assert projected == {'code': 'input_rejected', 'category': 'input', 'node_id': '1'}
    assert 'SomeOtherCustomNode' not in frame and 'not_in_this_graph' not in frame


def test_prompt_values_and_unsafe_names_never_authorize_or_appear(tmp_path):
    graph = {**TTS_GRAPH, '3': {'class_type': 'Load Image (private note)',
                                'inputs': {'a sentence typed as a key': 1, 'k' * 65: 1, TTS_PROMPT: 2,
                                           'ok_name': {'nested_key': 'v'}}}}
    for claim in (TTS_PROMPT, 'say this private sentence aloud', 'Ryan', '0.6B', 'Calm, clear, friendly delivery.',
                  'a sentence typed as a key', 'k' * 65, 'nested_key', 'Load Image (private note)'):
        frame, projected, _ids, names = _tts_failure(tmp_path, {'node_class': claim, 'field': claim}, graph)
        assert projected == {'code': 'input_rejected', 'category': 'input'}, claim
        assert 'PROMPT_SECRET' not in frame and 'PROMPT_SECRET' not in json.dumps(projected)
    assert 'ok_name' in names and not any('PROMPT_SECRET' in n or ' ' in n or len(n) > 64 for n in names)
    # A value that is also identifier-shaped is still not a name unless used as a class/input name.
    assert 'Ryan' not in names and 'English' not in names


def test_graph_names_accept_api_and_editor_graphs_and_fail_closed_when_large(tmp_path):
    editor = tmp_path / 'editor.json'
    editor.write_text(json.dumps({'nodes': [{'id': 5, 'type': 'KSampler', 'inputs': [{'name': 'model'}],
                                            'widgets_values': ['v_secret_value']}]}))
    assert fd.graph_facts(editor) == (frozenset({'5'}), frozenset({'KSampler', 'model'}))
    many_nodes = tmp_path / 'many_nodes.json'
    many_nodes.write_text(json.dumps({str(i): {'class_type': 'N'} for i in range(fd.MAX_GRAPH_NODE_IDS + 1)}))
    assert fd.graph_facts(many_nodes) == (frozenset(), frozenset())
    many_names = tmp_path / 'many_names.json'
    many_names.write_text(json.dumps({'1': {'class_type': 'N', 'inputs': {f'f{j}': 0 for j in range(fd.MAX_GRAPH_NAMES + 5)}}}))
    assert fd.graph_facts(many_names) == (frozenset(), frozenset())
    assert fd.graph_facts(tmp_path / 'missing') == (frozenset(), frozenset())
    assert fd.graph_facts(None) == (frozenset(), frozenset())


def test_no_graph_fallback_keeps_the_fixed_list():
    source = {'code': 'generation_failed', 'category': 'execution', 'node_class': 'KSampler',
              'field': 'seed', 'constraint': 'min_value'}
    assert fd.host_summary(RuntimeError(), source, stage='fetch')['node_class'] == 'KSampler'
    tts = {**source, 'node_class': 'AILab_Qwen3TTSCustomVoice', 'field': 'speaker'}
    projected = fd.host_summary(RuntimeError(), tts, stage='fetch')
    assert 'node_class' not in projected and 'field' not in projected and projected['constraint'] == 'min_value'
    # constraint stays on its fixed list even when the graph names it.
    assert 'constraint' not in fd.host_summary(RuntimeError(), {**source, 'constraint': 'speaker'}, stage='fetch',
                                              node_names={'speaker'})


def test_graph_names_survive_the_journal_round_trip_and_are_revalidated():
    names = frozenset({'AILab_Qwen3TTSCustomVoice', 'speaker'})
    source = {'exception': 'MachineError', 'stage': 'fetch', 'code': 'input_rejected', 'category': 'input',
              'node_id': '1', 'node_class': 'AILab_Qwen3TTSCustomVoice', 'field': 'speaker'}
    projected = fd.safe_stored_summary(source, node_ids={'1'}, node_names=names)
    assert projected == source
    row = rm.lease_row({'pod_id': 'pod-1', 'state': 'claimed',
                        'lease': {'state': 'idle', 'paid_expires_at': 1000, 'idle_expires_at': 900,
                                  'hourly_usd': 1, 'accounted_at': 100,
                                  'last_job': {'outcome': 'failed', 'generation': 1, 'failure': projected,
                                               'structural_node_ids': ['1'], 'structural_names': sorted(names)}}})
    assert row['last_job']['failure'] == projected
    assert 'field' not in fd.safe_stored_summary(source, node_ids={'1'})
    assert fd.stored_names(sorted(names)) == names
    assert fd.stored_names(['ok', SENTINEL]) == frozenset() and fd.stored_names('speaker') == frozenset()


def test_large_real_edit_template_yields_names_and_ids():
    qwen_edit = Path(os.environ['VIBECOMFY_SOURCE']).resolve() / 'ready_templates/sources/official/edit/qwen_image_2_1_turbo_edit.json'
    # 100 KB release-cell source (editor format, nodes inside a subgraph definition): above the
    # 64 KiB result-frame cap, which the graph no longer shares.
    assert qwen_edit.stat().st_size > fd.MAX_RESULT_BYTES
    ids, names = fd.graph_facts(qwen_edit)
    assert {'451', '459', '461', '470'} <= ids
    assert {'SaveImageAdvanced', 'LoadImage', 'KSamplerSelect', 'CFGGuider', 'TextEncodeQwenImage21', 'prompt'} <= names
    assert fd.parse_frame(fd.MARK + json.dumps({'code': 'input_rejected', 'category': 'input', 'node_id': '470',
                                               'node_class': 'KSamplerSelect', 'field': 'prompt'}),
                          node_ids=ids, node_names=names) == {
        'code': 'input_rejected', 'category': 'input', 'node_id': '470',
        'node_class': 'KSamplerSelect', 'field': 'prompt'}
    assert fd.MAX_RESULT_BYTES == 64 * 1024 and fd.MAX_GRAPH_BYTES == 8 * 1024 * 1024


def test_nested_execution_diagnostic_round_trip(tmp_path):
    from vibecomfy.runtime.session import _comfy_execution_diagnostic
    diagnostic = _comfy_execution_diagnostic({'messages': [['execution_error', {
        'node_id': '1', 'node_type': 'KSampler', 'exception_type': 'RuntimeError',
        'exception_message': TTS_PROMPT, 'observed': TTS_PROMPT}]]})
    payload = {'issues': [{'severity': 'error', 'code': 'task_failed', 'details': {
        'result': {'error': {'code': 'executor_failed', 'engine_diagnostic': diagnostic}}}}]}
    frame = emit(tmp_path, json.dumps(payload).encode(), TTS_GRAPH | {'1': {'class_type': 'KSampler'}})
    assert fd.parse_frame(frame, node_ids={'1'}, node_names={'KSampler'}) == {
        'code': 'comfy_execution_error', 'category': 'execution', 'node_id': '1',
        'node_class': 'KSampler', 'exception_type': 'RuntimeError'}
    assert TTS_PROMPT not in frame


def test_unknown_exception_and_private_text_are_withheld():
    diagnostic = {'code': 'comfy_execution_error', 'node_id': '1', 'node_class': 'KSampler',
                  'exception_type': 'PrivateError', 'message': TTS_PROMPT, 'observed': TTS_PROMPT, 'oom': True}
    result = fd.summarize({'error': {'engine_diagnostic': diagnostic}}, node_ids={'1'})
    assert result == {'code': 'comfy_execution_error', 'category': 'execution', 'node_id': '1',
                      'node_class': 'KSampler', 'oom': True}
    assert TTS_PROMPT not in json.dumps(result)


def test_session_execution_codes_are_known():
    import ast
    import inspect
    from vibecomfy.runtime import session
    tree = ast.parse(inspect.getsource(session))
    literals = {node_value.value for node in ast.walk(tree) if isinstance(node, ast.Dict)
                for key, node_value in zip(node.keys, node.values)
                if isinstance(key, ast.Constant) and key.value == 'code'
                and isinstance(node_value, ast.Constant) and isinstance(node_value.value, str)
                }
    assert literals <= fd.CODES.keys()


def test_local_job_exception_preserves_execution_diagnostic(monkeypatch, tmp_path, capsys):
    from vibecomfy.runtime.session import _raise_terminal_execution_error
    from astrid.core.generation import cli, local_job
    from astrid.packs.vibecomfy.local_server_engine import _expose_engine_diagnostic

    def fail(*args, **kwargs):
        # The VibeComfy pack's run boundary exposes the engine diagnostic under the
        # engine-neutral name before the shared core sees the exception.
        try:
            _raise_terminal_execution_error({'messages': [['execution_error', {
                'node_id': '1', 'node_type': 'KSampler', 'exception_type': 'RuntimeError'}]]}, None)
        except Exception as exc:
            _expose_engine_diagnostic(exc)
            raise
    monkeypatch.setattr(local_job, 'run_job', fail)
    assert cli.main(['cpu-image', '--placement', 'local', '--job', str(tmp_path / 'job.json'),
                     '--out', str(tmp_path / 'out'), '--json']) == 2
    envelope = json.loads(capsys.readouterr().out)
    assert fd.summarize(envelope, node_ids={'1'}) == {
        'code': 'comfy_execution_error', 'category': 'execution', 'node_id': '1',
        'node_class': 'KSampler', 'exception_type': 'RuntimeError'}


def test_node_class_allowlist_is_scoped_to_engine():
    assert fd.fact_allowlist('comfyui')['node_class']
    assert fd.fact_allowlist('other-engine') == {}
    diagnostic = {'code': 'generation_failed', 'category': 'execution', 'node_class': 'KSampler'}
    assert fd.host_summary(RuntimeError(), diagnostic, engine_id='comfyui')['node_class'] == 'KSampler'
    assert 'node_class' not in fd.host_summary(RuntimeError(), diagnostic, engine_id='other-engine')


def test_engine_boundary_exposes_only_the_engine_diagnostic_under_a_neutral_name():
    from astrid.packs.vibecomfy.local_server_engine import _expose_engine_diagnostic

    class Engine(Exception):
        comfy_diagnostic = {'code': 'comfy_execution_error', 'node_id': '1'}

    exc = Engine()
    _expose_engine_diagnostic(exc)
    assert exc.engine_diagnostic == {'code': 'comfy_execution_error', 'node_id': '1'}
    plain = RuntimeError('no diagnostic')
    _expose_engine_diagnostic(plain)
    assert getattr(plain, 'engine_diagnostic', None) is None
