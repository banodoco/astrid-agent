"""AstridClient.release_generation: managed-only, idle-gated, claims only proven facts."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from astrid.core.generation import preflight
from astrid.sdk.client import AstridClient


class _FakeComfy:
    def __init__(self, *, running=(), pending=(), free_fails=False):
        self.running = list(running)
        self.pending = list(pending)
        self.free_fails = free_fails
        self.vram_free = 20_000_000_000
        self.posts: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep test output quiet
                return

            def _send(self, payload):
                body = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/system_stats":
                    self._send({"devices": [{"vram_total": 24_000_000_000, "vram_free": outer.vram_free}]})
                elif self.path == "/queue":
                    self._send({"queue_running": outer.running, "queue_pending": outer.pending})
                else:
                    self.send_response(404)
                    self.end_headers()

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                self.rfile.read(length)
                outer.posts.append(self.path)
                if self.path == "/api/free":
                    if outer.free_fails:
                        self.send_response(500)
                        self.end_headers()
                        return
                    outer.vram_free = 22_000_000_000
                    self._send({"models_unloaded": True, "memory_freed": True})
                    return
                if self.path == "/queue":
                    self._send({})
                    return
                self.send_response(404)
                self.end_headers()

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def _client():
    return AstridClient(remote=object())


def _registered(monkeypatch, url):
    monkeypatch.setattr(preflight, "_registered_managed_engine", lambda: {
        "owner": "astrid", "endpoint": url, "root": "/opt/astrid/engines/comfyui",
    } if url else None)


def test_attached_target_is_refused():
    with pytest.raises(Exception) as info:
        _client().release_generation(engine="comfyui", target="http://127.0.0.1:8188")
    assert "refuses attached or unmanaged targets" in str(info.value)


def test_no_managed_engine_is_missing_dependency(monkeypatch):
    _registered(monkeypatch, None)
    with pytest.raises(Exception) as info:
        _client().release_generation(engine="comfyui", target="managed")
    assert "no managed ComfyUI engine is installed or registered" in str(info.value)


def test_busy_queue_is_refused_and_nothing_is_freed(monkeypatch):
    with _FakeComfy(running=[[1, "p1"]]) as fake:
        _registered(monkeypatch, fake.url)
        with pytest.raises(Exception) as info:
            _client().release_generation(engine="comfyui", target="managed")
        assert "managed engine is busy" in str(info.value)
        assert fake.posts == []


def test_idle_engine_is_released_through_the_owner_unload_path(monkeypatch):
    with _FakeComfy() as fake:
        _registered(monkeypatch, fake.url)
        result = _client().release_generation(engine="comfyui", target="managed")
        posts = list(fake.posts)
    assert posts == ["/queue", "/api/free"]
    assert result["ok"] is True and result["released"] is True
    assert result["queue_idle"] is True
    assert result["models_unloaded"] is True
    assert result["process_exited"] is False
    assert result["memory_observed_returned"] is True
    assert result["vram_total_bytes"] == 24_000_000_000
    assert result["vram_free_before_bytes"] == 20_000_000_000
    assert result["vram_free_after_bytes"] == 22_000_000_000


def test_failed_free_is_not_claimed_as_released(monkeypatch):
    with _FakeComfy(free_fails=True) as fake:
        _registered(monkeypatch, fake.url)
        result = _client().release_generation(engine="comfyui", target="managed")
    assert result["ok"] is False and result["released"] is False
    assert result["models_unloaded"] is False
    assert result["process_exited"] is False
    assert result["memory_observed_returned"] is False


def test_non_loopback_endpoint_is_refused(monkeypatch):
    _registered(monkeypatch, "http://10.0.0.5:8188")
    with pytest.raises(ValueError, match="loopback"):
        _client().release_generation(engine="comfyui", target="managed")
