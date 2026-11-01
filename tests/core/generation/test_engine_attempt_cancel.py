import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import sys
import threading
import time
import urllib.request

import pytest

from astrid.core.generation import engine_attempt
from astrid.core.generation.engine_attempt import AttemptInstaller, ObservedStop, RecoveryHandle
from astrid.core.generation.engine_contracts import CommandRequest, CustodyUnresolved, ModelSpec
from astrid.core.generation.engine_execution import create_local_backend
from astrid.core.generation.engine_install_task import prepare_install


def _installer(tmp_path, callback=lambda _: None, receipt='receipt'):
    binding, _ = prepare_install(root=str(tmp_path / 'engine'))
    deadline = time.monotonic() + 30
    context, adapter = create_local_backend(engine=binding.engine, platform=binding.platform,
        placement=binding.placement, root=binding.root, deadline=deadline)
    return binding, context, AttemptInstaller(context=context, adapter=adapter, deadline=deadline,
                                             receipt_id=receipt, on_checkpoint=callback)


def _wait(predicate):
    limit = time.monotonic() + 8
    while not predicate():
        assert time.monotonic() < limit
        time.sleep(0.02)


def test_real_supervisor_cancellation_waits_for_owned_command_stop(tmp_path, monkeypatch):
    binding, context, installer = _installer(tmp_path)
    marker = tmp_path / 'engine/started'
    def tiny_install(plan, *, context, adapter, deadline, _receipt_id):
        with context.acquire_install_lock(plan.root, deadline=deadline):
            code = "import os,time; open('started','w').write(str(os.getpid())); time.sleep(60)"
            return context.run(CommandRequest((sys.executable, '-c', code), plan.root, {}, deadline))
    monkeypatch.setattr(engine_attempt, 'install_engine', tiny_install)
    attempt = installer.begin_attempt('task', 'attempt', 1, binding)
    try:
        _wait(marker.exists)
        pid = int(marker.read_text())
        outcome = installer.cancel(attempt)
        assert isinstance(outcome, ObservedStop)
        assert outcome.identity == attempt.identity
        assert not Path(f'/proc/{pid}').exists()
        record = json.loads((tmp_path / 'engine/.astrid-engine/attempt-custody.json').read_text())
        assert record['attempts'][0]['state'] == 'stopped'
    finally:
        context.service_client.stop_owned(deadline=time.monotonic() + 10)


def test_blocked_real_download_returns_recovery_until_it_has_stopped(tmp_path, monkeypatch):
    entered, release, reading = threading.Event(), threading.Event(), threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            self.send_response(206)
            self.send_header('Content-Range', 'bytes 0-3/4')
            self.send_header('Content-Length', '4')
            self.end_headers()
            entered.set()
            release.wait(10)
            self.wfile.write(b'test')
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    original_open = urllib.request.urlopen
    class Response:
        def __init__(self, response):
            self.response = response
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.response.close()
        def __getattr__(self, name):
            return getattr(self.response, name)
        def read(self, *args):
            reading.set()
            return self.response.read(*args)
    monkeypatch.setattr(urllib.request, 'urlopen', lambda _request, timeout:
                        Response(original_open(f'http://127.0.0.1:{server.server_port}/tiny', timeout=timeout)))
    model = ModelSpec('tiny', None, None, None, 'https://example.invalid/tiny',
                      'tiny.bin', 4, hashlib.sha256(b'test').hexdigest())
    binding, context, installer = _installer(tmp_path)
    def tiny_install(plan, *, context, adapter, deadline, _receipt_id):
        with context.acquire_install_lock(plan.root, deadline=deadline):
            return context.download_model(model, models_root=plan.root + '/models', deadline=deadline)
    monkeypatch.setattr(engine_attempt, 'install_engine', tiny_install)
    attempt = installer.begin_attempt('task', 'attempt', 1, binding)
    try:
        assert entered.wait(5) and reading.wait(5)
        assert isinstance(installer.cancel(attempt), RecoveryHandle)
        release.set()
        _wait(attempt._done.is_set)
        other = AttemptInstaller(context=context, adapter=context.adapter, deadline=time.monotonic() + 10,
                                 receipt_id='another-receipt', on_checkpoint=lambda _: None)
        with pytest.raises(CustodyUnresolved):
            other.begin_attempt('task', 'another-attempt', 2, binding)
        assert isinstance(installer.cancel(attempt), ObservedStop)
        assert not (tmp_path / 'engine/models/tiny.bin').exists()
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        server_thread.join(5)
