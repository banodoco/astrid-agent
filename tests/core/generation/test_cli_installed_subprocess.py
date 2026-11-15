"""Exercise the public module gateway from a non-editable package install."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


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
        "  if kwargs['placement'].startswith('http'):\n"
        "    urlopen(kwargs['placement'] + '/system_stats', timeout=5).read()\n"
        "  return Result({'entry': kwargs['entry'], 'target': kwargs['placement'], 'dry_run': kwargs['dry_run']})\n"
        "type(generate).image = image\n"
        "cli._open_client = lambda: nullcontext(object())\n"
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join((str(tmp_path), str(target)))
    try:
        dry = subprocess.run(
            [sys.executable, "-m", "astrid", "generate", "image", "--entry", "z-image",
             "--mode", "t2i", "--placement", "managed", "--dry-run", "--json", "a studio test image"],
            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
        )
        assert dry.returncode == 0, dry.stdout + dry.stderr
        assert json.loads(dry.stdout) == {"dry_run": True, "entry": "z-image", "target": "managed"}

        url = f"http://127.0.0.1:{server.server_port}"
        live = subprocess.run(
            [sys.executable, "-m", "astrid", "generate", "image", "--entry", "z-image",
             "--mode", "t2i", "--placement", url, "--json", "a studio test image"],
            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30,
        )
        assert live.returncode == 0, live.stdout + live.stderr
        payload = json.loads(live.stdout)
        timing = payload["timing"]
        assert payload == {"dry_run": False, "entry": "z-image", "target": url, "timing": timing}
        assert timing["schema"] == "astrid.generation.timing.v2"
        block = timing["cli"]
        assert block["same_clock"] is False
        assert block["total"]["seconds"] >= 0
        assert set(block["phases"]) == {"cli_startup", "submit", "wait_for_claim", "host", "result_fetch_and_print"}
        assert block["unaccounted"]["seconds"] >= 0
        assert hits == ["/system_stats"]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
