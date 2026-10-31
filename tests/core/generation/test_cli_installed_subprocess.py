"""Exercise the public module gateway from a non-editable package install."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytestmark = pytest.mark.installed


def test_installed_module_generate_subprocesses(tmp_path):
    repo = Path(__file__).resolve().parents[3]
    target = tmp_path / "installed"
    install = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--no-deps", "--no-build-isolation",
         "--target", str(target), str(repo)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert install.returncode == 0, install.stdout + install.stderr

    hits = []

    class ComfyHandler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), ComfyHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    sitecustomize = tmp_path / "sitecustomize.py"
    sitecustomize.write_text(
        "from contextlib import nullcontext\n"
        "from urllib.request import urlopen\n"
        "from astrid.sdk.generation import generate\n"
        "from astrid.core.generation import cli\n"
        "class Result:\n"
        "  ok = True\n"
        "  def __init__(self, values): self.values = values\n"
        "  def to_dict(self): return self.values\n"
        "def image(self, **kwargs):\n"
        "  if kwargs['target'].startswith('http'):\n"
        "    urlopen(kwargs['target'] + '/system_stats', timeout=5).read()\n"
        "  return Result({'entry': kwargs['entry'], 'target': kwargs['target'], 'dry_run': kwargs['dry_run']})\n"
        "type(generate).image = image\n"
        "cli._open_client = lambda: nullcontext(object())\n"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join((str(tmp_path), str(target)))
    try:
        dry = subprocess.run(
            [sys.executable, "-m", "astrid", "generate", "image", "--entry", "z-image",
             "--mode", "t2i", "--target", "managed", "--dry-run", "--json", "a studio test image"],
            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
        )
        assert dry.returncode == 0, dry.stdout + dry.stderr
        assert json.loads(dry.stdout) == {"dry_run": True, "entry": "z-image", "target": "managed"}

        url = f"http://127.0.0.1:{server.server_port}"
        live = subprocess.run(
            [sys.executable, "-m", "astrid", "generate", "image", "--entry", "z-image",
             "--mode", "t2i", "--target", url, "--json", "a studio test image"],
            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
        )
        assert live.returncode == 0, live.stdout + live.stderr
        assert json.loads(live.stdout) == {"dry_run": False, "entry": "z-image", "target": url}
        assert hits == ["/system_stats"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
