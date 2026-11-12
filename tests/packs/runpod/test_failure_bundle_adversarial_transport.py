"""Receiving limits use the real spawned transport, with hostile size claims."""
import base64
import gzip
import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from astrid.packs.runpod import failure_bundle as fb
from astrid.packs.runpod import machine as rm
from astrid.packs.runpod.remote_placement import _HttpsOps


@pytest.fixture
def hostile_agent():
    files, requests = {}, []

    class Agent(BaseHTTPRequestHandler):
        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(request)
            body = files[request['path']]
            length = files.get('__chunk_size', len(body))
            chunk = body[request['offset']:request['offset'] + length]
            reply = json.dumps({'data': base64.b64encode(chunk).decode(),
                                'offset': request['offset'], 'total': len(body),
                                'sha256': hashlib.sha256(chunk).hexdigest(),
                                'etag': hashlib.sha256(body).hexdigest(),
                                'padding': files.get('__padding', '')}).encode()
            if files.get('__gzip'):
                reply = gzip.compress(reply)
            self.send_response(200)
            if files.get('__gzip'):
                self.send_header('Content-Encoding', 'gzip')
            if not files.get('__omit_length'):
                self.send_header('Content-Length', str(len(reply)))
            self.end_headers()
            try:
                self.wfile.write(reply)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Agent)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        ops = _HttpsOps('pod1', 'x' * 32,
                        base_url=f'http://127.0.0.1:{server.server_port}').bounded(
                            time.monotonic() + 30, abort_event=threading.Event())
        yield ops, files, requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize('omit_length', [False, True], ids=['declared-body', 'streamed-body'])
def test_reviewer_11_declared_one_byte_does_not_land_large_transfer(hostile_agent, tmp_path, omit_length):
    ops, files, requests = hostile_agent
    files['__omit_length'] = omit_length
    body = b'x' * (1024 * 1024)
    manifest = json.dumps({'files': {'error-tail.txt': {
        'size': 1, 'sha256': hashlib.sha256(body).hexdigest()}}}).encode()
    files['/bundle/wrapper-manifest.json'] = manifest
    files['/bundle/error-tail.txt'] = body
    get = ops.get

    def observe_receive(remote, local, **kwargs):
        get(remote, local, **kwargs)
        if remote.endswith('/error-tail.txt'):
            assert local.stat().st_size <= 1, 'receiver wrote beyond the admitted one byte'

    ops.get = observe_receive
    with pytest.raises(rm.DigestMismatch):
        rm.RunPodMachine._fetch_verified(ops, '/bundle', tmp_path / 'files', tmp_path,
                                        hashlib.sha256(manifest).hexdigest(), admit=rm._admit_bundle)
    assert not (tmp_path / 'files/error-tail.txt').exists()
    assert not (tmp_path / 'files/error-tail.txt.https-part').exists()
    assert len(requests) == 2


def test_reviewer_11_manifest_overflow_stops_in_real_bounded_transport(hostile_agent, tmp_path):
    ops, files, requests = hostile_agent
    manifest = b' ' * (fb.MANIFEST_CAP + 1) + b'{"files":{"error-tail.txt":{"size":1}}}'
    files['/bundle/wrapper-manifest.json'] = manifest
    with pytest.raises(rm.BundleOverCap):
        rm.RunPodMachine._fetch_verified(ops, '/bundle', tmp_path / 'files', tmp_path,
                                        hashlib.sha256(manifest).hexdigest(), admit=rm._admit_bundle)
    assert not (tmp_path / 'wrapper-manifest.json').exists()
    assert not (tmp_path / 'wrapper-manifest.json.https-part').exists()
    assert len(requests) == 1



def test_transport_caps_cumulative_wire_envelopes(hostile_agent, tmp_path):
    from astrid.packs.runpod.remote_placement import TransferOverCap

    ops, files, requests = hostile_agent
    files['/padded'] = b'x' * 32
    files['__chunk_size'] = 1
    files['__padding'] = ' ' * 1000
    local = tmp_path / 'padded'
    with pytest.raises(TransferOverCap):
        ops.get('/padded', local, max_bytes=32)
    assert len(requests) == 2
    assert not local.exists() and not local.with_name('padded.https-part').exists()


def test_transport_refuses_compressed_download_before_decoding(hostile_agent, tmp_path):
    from astrid.packs.runpod.remote_placement import RemotePlacementError

    ops, files, requests = hostile_agent
    files['/compressed'] = b'x' * 32
    files['__gzip'] = True
    local = tmp_path / 'compressed'
    with pytest.raises(RemotePlacementError):
        ops.get('/compressed', local, max_bytes=32)
    assert len(requests) == 1
    assert not local.exists() and not local.with_name('compressed.https-part').exists()
