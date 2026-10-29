"""Disposable runner's release-host-only HTTPS fixture."""
import http.server
import os
from pathlib import Path
import ssl
import sys

case = Path(sys.argv[1])
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        name = {'/install': 'install', '/install-manifest.json': 'manifest.json'}.get(self.path)
        if name is None:
            self.send_error(404)
            return
        payload = (case / name).read_bytes()
        self.send_response(200)
        self.send_header('Content-Length', str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
    def log_message(self, fmt, *args):
        print(fmt % args, flush=True)

httpd = http.server.HTTPServer(('127.0.0.1', 443), Handler)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain(str(case / 'leaf.pem'), str(case / 'leaf.key'))
httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
(case / 'server.pid').write_text(str(os.getpid()))
print('fixture ready', flush=True)
httpd.serve_forever()
