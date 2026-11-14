"""R4.H: a default whose model files the server does not list is not ready, in every command that says so."""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from astrid.core.generation import asset_gaps, cli, defaults, listing, settings
from astrid.core.generation.backends.comfy_target import ComfyTarget, ComfyTargetClient
from astrid.packs.vibecomfy import local
from tests.core.generation.backends.test_comfy_model_resolution import target_info

ENTRY = "qwen-image-2.1-turbo"
REASON = "required models missing or ambiguous"
FILES = {
    "UNETLoader": ("unet_name", "qwen_image_2.1_turbo_int8_convrot.safetensors"),
    "CLIPLoader": ("clip_name", "qwen3vl_8b_int8_convrot.safetensors"),
    "VAELoader": ("vae_name", "qwen_image_2.1_vae_bf16.safetensors"),
}


class _Fake(ThreadingHTTPServer):
    """A ComfyUI whose loader choices follow ``installed``, as a real server's follow its folders."""

    def __init__(self, folders):
        super().__init__(("127.0.0.1", 0), _Handler)
        self.folders = folders
        self.installed: set[str] = set()

    def object_info(self):
        info = target_info("t2i", vaes=(), unets=())
        for cls, (field, name) in FILES.items():
            info[cls]["input"]["required"][field] = [[name] if name in self.installed else []]
        return info


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = {"/system_stats": {"system": {"os": "linux", "argv": []}, "devices": []},
                "/internal/folder_paths": self.server.folders,
                "/userdata?dir=workflows": [],
                "/object_info": None}.get(self.path, "missing")
        if body == "missing":
            self.send_error(404)
            return
        data = json.dumps(self.server.object_info() if self.path == "/object_info" else body).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


@pytest.fixture
def server(monkeypatch, tmp_path, generation_settings_store):
    root = tmp_path / "ComfyUI" / "models"
    folders = {}
    for key in ("diffusion_models", "text_encoders", "vae"):
        (root / key).mkdir(parents=True)
        folders[key] = [str(root / key)]
    fake = _Fake(folders)
    threading.Thread(target=fake.serve_forever, daemon=True).start()
    scan = local.scan
    monkeypatch.setattr(local, "_bounded_pinokio_discovery", lambda *args: (None, {}, [], []))
    monkeypatch.setattr(local, "scan", lambda ports: scan([fake.server_port]))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("VIBECOMFY_COMFYUI_URL", raising=False)
    settings.update(lambda document: document.__setitem__(
        "placement", {"kind": "attached", "endpoint": f"http://127.0.0.1:{fake.server_port}"}))
    fake.url = f"http://127.0.0.1:{fake.server_port}"
    yield fake
    fake.shutdown()
    fake.server_close()


def _slot():
    return {row["slot"]: row for row in defaults.listing()["slots"]}["image.t2i"]


def _list_row(url):
    return next(row for row in listing.generation_rows("image")
                if row["id"] == ENTRY and row["mode"] == "t2i" and row["target"] == url)


def test_defaults_says_not_ready_when_the_server_lists_none_of_the_files(server):
    slot = _slot()
    assert slot["entry"] == ENTRY
    assert slot["ready"] is False
    assert slot["reason"] == REASON
    assert _list_row(server.url)["missing_roles"] == ["diffusion", "text_encoder", "vae"]


def test_list_and_defaults_agree_when_files_are_missing(server):
    row = _list_row(server.url)
    slot = _slot()
    assert (row["ready"], row["reason"]) == (slot["ready"], slot["reason"]) == (False, REASON)


def test_a_partly_installed_default_names_only_what_is_missing(server):
    server.installed = {"qwen_image_2.1_vae_bf16.safetensors"}
    slot = _slot()
    assert (slot["ready"], slot["reason"]) == (False, REASON)
    assert _list_row(server.url)["missing_roles"] == ["diffusion", "text_encoder"]


def test_defaults_is_ready_once_the_files_are_listed(server):
    server.installed = {name for _, name in FILES.values()}
    slot = _slot()
    row = _list_row(server.url)
    assert slot["ready"] is True and row["ready"] is True
    assert slot["reason"] == row["reason"]


@pytest.mark.parametrize("installed", [False, True])
def test_the_download_plan_and_readiness_agree(server, installed):
    if installed:
        server.installed = {name for _, name in FILES.values()}
    client = ComfyTargetClient(ComfyTarget(server.url, "attached"))
    try:
        plan = asset_gaps.plan_attached_model_download(client, ENTRY, "t2i")
        planned = sorted(f["file"] for f in plan["files"]) + sorted(m["file"] for m in plan["manual"])
    except asset_gaps.AttachedDownloadRefused:
        planned = []
    ready = _slot()["ready"]
    assert ready is (not planned)
    assert bool(planned) is (not installed)


def test_the_plan_for_a_ready_default_says_nothing_is_missing(server):
    server.installed = {name for _, name in FILES.values()}
    client = ComfyTargetClient(ComfyTarget(server.url, "attached"))
    plan = asset_gaps.plan_attached_model_download(client, ENTRY, "t2i")
    assert plan["files"] == [] and plan["manual"] == []
    assert asset_gaps.render_attached_download_plan(plan) == (
        f"Nothing is missing for {ENTRY} t2i on your ComfyUI at {server.url}; it is ready.")


def test_the_plan_for_a_missing_default_keeps_its_header(server):
    client = ComfyTargetClient(ComfyTarget(server.url, "attached"))
    plan = asset_gaps.plan_attached_model_download(client, ENTRY, "t2i")
    text = asset_gaps.render_attached_download_plan(plan)
    assert text.startswith(f"Missing models for {ENTRY}/t2i on your ComfyUI at {server.url}:")
