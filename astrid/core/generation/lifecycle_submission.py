"""Attempt-local HTTP gateway; supervisor authority never enters the worker.

The installation lock remains with ManagedEnginePreparation. Only /prompt is
special: the owner rechecks the live generation under lifecycle exclusion and
posts over a socket attested to that exact retained child. Other HTTP operations
keep the existing live validation, upload, history and attributed-output paths.
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import CancelledError
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler
from urllib.parse import urlsplit

from .engine_contracts import CustodyUnresolved, EngineError
from .engine_transport import _GatewayServer, _peer_closed


class LifecycleGateway:
    def __init__(self, preparation):
        self.preparation = preparation
        self.attempted = False
        self.lock = threading.Lock()
        gateway = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def reply(self, status, value):
                data = json.dumps(value, separators=(',', ':')).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                self.relay()

            def do_POST(self):
                if self.path not in {'/prompt', '/api/prompt'}:
                    return self.relay()
                with gateway.lock:
                    if gateway.attempted:
                        return self.reply(409, {'error': 'prompt already attempted; reconcile original attempt'})
                    gateway.attempted = True
                try:
                    length = self.headers.get('Content-Length', '')
                    if not length.isdecimal() or not 0 < int(length) <= 8 * 1024 * 1024:
                        return self.reply(400, {'error': 'invalid prompt length'})
                    self.connection.settimeout(min(15, max(0.001, preparation.deadline - time.monotonic())))
                    body = json.loads(self.rfile.read(int(length)))
                    if not isinstance(body, dict):
                        return self.reply(400, {'error': 'invalid prompt'})
                    expected_child_generation = self.headers.get('X-Astrid-Child-Generation')
                    if expected_child_generation != preparation._expected_lifecycle['child_generation']:
                        return self.reply(409, {'error': 'submission generation differs from preparation'})
                    # The parent detects a disconnected worker before any POST,
                    # including after full preparation on a known mismatch.
                    preparation._submission_cancelled = lambda: _peer_closed(self.connection)
                    result = preparation.submit_bound(body)
                    report = getattr(preparation, 'report', None)
                    return self.reply(200, {**result, **({'astrid_managed_preparation': report}
                                                        if report is not None else {})})
                except CustodyUnresolved:
                    preparation.uncertain = True
                    return self.reply(503, {'error': 'submission custody unresolved; do not resubmit'})
                except (EngineError, OSError, ValueError, CancelledError):
                    return self.reply(503, {'error': 'submission unconfirmed; do not resubmit'})
                finally:
                    preparation._submission_cancelled = lambda: False

            def relay(self):
                path = urlsplit(self.path)
                allowed = (self.command == 'GET' and (
                    path.path in {'/queue', '/history', '/system_stats', '/object_info',
                                  '/internal/folder_paths', '/view'} or path.path.startswith('/history/'))
                    or self.command == 'POST' and path.path in {
                        '/queue', '/interrupt', '/api/free', '/upload/image', '/upload/mask'})
                if not allowed or path.scheme or path.netloc or self.headers.get('Transfer-Encoding'):
                    return self.reply(403, {'error': 'unsupported engine operation'})
                remaining = preparation.deadline - time.monotonic()
                if remaining <= 0:
                    return self.reply(504, {'error': 'attempt deadline exceeded'})
                upstream = HTTPConnection('127.0.0.1', preparation.recipe.start.port, timeout=min(30, remaining))
                try:
                    length = self.headers.get('Content-Length', '0')
                    if not length.isdecimal() or int(length) > 1024 * 1024 * 1024:
                        return self.reply(400, {'error': 'invalid body length'})
                    self.connection.settimeout(min(30, remaining))
                    headers = {key: value for key, value in self.headers.items()
                               if key.lower() not in {'host', 'connection', 'authorization'}}
                    upstream.putrequest(self.command, self.path)
                    for key, value in headers.items():
                        upstream.putheader(key, value)
                    upstream.putheader('Connection', 'close')
                    upstream.endheaders()
                    pending = int(length)
                    while pending:
                        block = self.rfile.read(min(pending, 1024 * 1024))
                        if not block:
                            raise ValueError('short engine body')
                        upstream.send(block)
                        pending -= len(block)
                    response = upstream.getresponse()
                    self.send_response(response.status)
                    for key, value in response.getheaders():
                        if key.lower() not in {'connection', 'transfer-encoding'}:
                            self.send_header(key, value)
                    self.send_header('Connection', 'close')
                    self.end_headers()
                    while block := response.read(1024 * 1024):
                        self.wfile.write(block)
                except (OSError, ValueError):
                    self.close_connection = True
                finally:
                    upstream.close()

        self.server = _GatewayServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def endpoint(self):
        return f'http://127.0.0.1:{self.server.server_port}'

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=1)
