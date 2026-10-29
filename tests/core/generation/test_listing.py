from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from astrid.core.generation import listing


def _row(**changes):
    return {**dict(id="z-image-turbo", kind="image", mode="t2i", backend="local", template="image/z_image", source="direct-image", availability="available", reason=""), **changes}


@pytest.fixture
def comfy(monkeypatch):
    class Handler(BaseHTTPRequestHandler):
        schema = {
            "UNETLoader": {"input": {"required": {"unet_name": [["z_image_bf16.safetensors"]]}}},
            "CLIPLoader": {"input": {"required": {"clip_name": [["qwen_3_4b.safetensors"]]}}},
            "VAELoader": {"input": {"required": {"vae_name": [["ae.safetensors"]]}}},
        }
        def do_GET(self):
            if self.path == "/system_stats":
                body = {"system": {"os": "win32", "argv": []}, "devices": []}
            elif self.path == "/internal/folder_paths":
                body = {"custom_nodes": ["C:\\ComfyUI\\custom_nodes"]}
            elif self.path == "/userdata?dir=workflows":
                body = []
            elif self.path == "/object_info":
                body = self.schema
            else:
                self.send_error(404)
                return
            data = json.dumps(body).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    from astrid.packs.vibecomfy import local
    scan = local.scan
    monkeypatch.setattr(local, "_bounded_pinokio_discovery", lambda *args: (None, {}, [], []))
    monkeypatch.setattr(local, "scan", lambda ports: scan([server.server_port]))
    yield server, Handler
    server.shutdown()
    thread.join()
    server.server_close()


def test_attached_windows_probe_real_template_and_identity_mismatch(comfy, monkeypatch):
    monkeypatch.setattr(listing, "catalog_rows", lambda: [_row()])
    rows = listing.generation_rows("image")
    assert len(rows) == 1
    row = rows[0]
    assert row["target"] == f"http://127.0.0.1:{comfy[0].server_port}"
    assert row["models_present"] == ["ae.safetensors", "qwen_3_4b.safetensors", "z_image_bf16.safetensors"]
    assert row["ready"] is False
    assert "Base checkpoint" in row["reason"]
    del comfy[1].schema["VAELoader"]
    row = listing.generation_rows("image")[0]
    assert row["ready"] is False
    assert "unavailable" in row["reason"]


def test_cloud_presence_only_json_and_filter(monkeypatch, capsys):
    monkeypatch.setattr(listing, "catalog_rows", lambda: [_row(id="cloud-model", backend="cloud", template=""), _row(kind="video", backend="cloud", id="video-model", template="")])
    monkeypatch.delenv("FAL_KEY", raising=False)
    assert listing.main(["list", "--kind", "image", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 1 and rows[0]["reason"] == "no key"
    assert "FAL_KEY" in rows[0]["fix"]
    monkeypatch.setenv("FAL_KEY", "synthetic-secret-must-not-appear")
    listing.main(["list", "--json"])
    text = capsys.readouterr().out
    assert "synthetic-secret-must-not-appear" not in text
    assert all(row["ready"] for row in json.loads(text))


def test_unavailable_and_fixture_declarations(monkeypatch):
    records = [_row(id="unavailable", backend="", availability="unavailable", reason="not implemented"), _row(id="fixture", backend="", availability="fixture-only", reason="fixture-only route")]
    monkeypatch.setattr(listing, "catalog_rows", lambda: records)
    rows = listing.generation_rows()
    assert all(not row["ready"] and row["fix"] for row in rows)
    assert {row["availability"] for row in rows} == {"unavailable", "fixture-only"}


def test_bad_object_info_preserves_attached_row(comfy, monkeypatch):
    monkeypatch.setattr(listing, "catalog_rows", lambda: [_row()])
    comfy[1].schema = None
    row = listing.generation_rows()[0]
    assert not row["ready"] and "probe unavailable" in row["reason"]


def test_gateway_and_console_dispatch(monkeypatch, capsys):
    from astrid import console
    from astrid.core import gateway
    monkeypatch.setattr(listing, "generation_rows", lambda kind=None: [])
    assert console.main(["generate", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []
    assert gateway.main(["generate", "list", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_real_registry_consumes_availability_and_cloud(monkeypatch):
    from scripts.gen_capability_index import _in_repo_packs_only
    monkeypatch.setattr(listing, "_targets", lambda: [])
    monkeypatch.delenv("FAL_KEY", raising=False)
    with _in_repo_packs_only():
        rows = listing.generation_rows()
    unavailable = next(row for row in rows if row["id"] == "video_editing.thumbnail_maker")
    assert unavailable["availability"] == "unavailable" and not unavailable["ready"]
    assert "not implemented" in unavailable["reason"]
    fixture = next(row for row in rows if row["id"] == "astrid_motion:preview/scenes.json")
    assert fixture["availability"] == "fixture-only" and not fixture["ready"]
    cloud = next(row for row in rows if row["id"] == "z-image" and row["backend"] == "cloud")
    assert not cloud["ready"] and cloud["reason"] == "no key" and "FAL_KEY" in cloud["fix"]
