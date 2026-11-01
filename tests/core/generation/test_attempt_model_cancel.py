import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
import time
import urllib.request

from astrid.core.generation.engine_contracts import DeadlineExceeded, ModelSpec
from astrid.core.generation.model_downloader import _cancellable_download, download_model


def test_real_blocked_download_observes_cancel_after_response_unblocks(tmp_path):
    entered, release, cancelled = threading.Event(), threading.Event(), threading.Event()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(206)
            self.send_header('Content-Range', 'bytes 0-3/4')
            self.send_header('Content-Length', '4')
            self.end_headers()
            entered.set()
            release.wait(5)
            self.wfile.write(b'test')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    model = ModelSpec('tiny', None, None, None, 'https://example.invalid/tiny',
                      'tiny.bin', 4, hashlib.sha256(b'test').hexdigest())
    errors = []
    def opener(_request, *, timeout):
        return urllib.request.urlopen(f'http://127.0.0.1:{server.server_port}/tiny', timeout=timeout)
    def run():
        try:
            with _cancellable_download(cancelled):
                download_model(model, models_root=str(tmp_path / 'models'),
                               deadline=time.monotonic() + 10, opener=opener)
        except BaseException as exc:
            errors.append(exc)
    worker = threading.Thread(target=run, daemon=True)
    try:
        worker.start()
        assert entered.wait(5)
        cancelled.set()
        # A blocked socket read is not itself an observed stop.
        assert worker.is_alive()
        release.set()
        worker.join(5)
        assert not worker.is_alive()
        assert len(errors) == 1 and isinstance(errors[0], DeadlineExceeded)
        assert not (tmp_path / 'models/tiny.bin').exists()
    finally:
        release.set()
        worker.join(5)
        server.shutdown()
        server.server_close()
        server_thread.join(5)
