"""Downloading a default's missing models into the person's own ComfyUI: one binding confirmation."""
from __future__ import annotations

import hashlib
import http.server
import os
import shutil
import threading
from types import SimpleNamespace

import httpx
import pytest

from astrid.core.generation.asset_gaps import (
    AttachedDownloadRefused, apply_attached_model_download, plan_attached_model_download,
    render_attached_download_plan,
)
from astrid.core.generation.backends.comfy_target import (
    ComfyModelMissing, ComfyTarget, ComfyTargetClient, _capture_model_resolution,
)
from astrid.core.generation.engine_contracts import ModelSpec
from astrid.core.model_catalog.registry import ModelRegistry
from tests.core.generation.backends.test_comfy_model_resolution import target_info

URL = "http://127.0.0.1:8188"
BLOBS = {
    "/unet.bin": b"unet-bytes-0123456789",
    "/clip.bin": b"clip-bytes-abc",
    "/vae.bin": b"vae-bytes",
}
# folder key -> (file the default needs, URL path of its source)
WANTED = {
    "diffusion_models": ("z_image_turbo_bf16.safetensors", "/unet.bin"),
    "text_encoders": ("qwen_3_4b.safetensors", "/clip.bin"),
    "vae": ("ae.safetensors", "/vae.bin"),
}
TOTAL = sum(len(blob) for blob in BLOBS.values())


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.requested.append(self.path)
        blob = self.server.blobs.get(self.path)
        if blob is None:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(blob)))
        self.end_headers()
        if self.path in self.server.truncated:
            self.wfile.write(blob[:3])
            self.wfile.flush()
            self.close_connection = True
            return  # the promised length never arrives: an interrupted download
        self.wfile.write(blob)

    def log_message(self, *args):
        pass


@pytest.fixture
def file_server():
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.blobs, server.requested, server.truncated = dict(BLOBS), [], set()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield server
    server.shutdown()
    server.server_close()


@pytest.fixture
def models(tmp_path):
    """ComfyUI lists the legacy folder before the canonical one, as the real server does."""
    root = tmp_path / "ComfyUI" / "models"
    folders = {
        "diffusion_models": [root / "unet", root / "diffusion_models"],
        "text_encoders": [root / "clip", root / "text_encoders"],
        "vae": [root / "vae"],
    }
    for paths in folders.values():
        for path in paths:
            path.mkdir(parents=True)
    return root, {key: [str(path) for path in paths] for key, paths in folders.items()}


def _sources(server):
    base = f"http://127.0.0.1:{server.server_port}"
    return {
        f"{key}/{name}": ModelSpec(name, None, None, None, base + path, f"{key}/{name}",
                                   len(BLOBS[path]), hashlib.sha256(BLOBS[path]).hexdigest())
        for key, (name, path) in WANTED.items()
    }


@pytest.fixture
def comfy(monkeypatch, models):
    """The fake ComfyUI: object_info lists none of the files; only two reads are ever answered."""
    _, folders = models
    info = target_info("t2i", vaes=(), unets=())
    info["CLIPLoader"]["input"]["required"]["clip_name"] = [[]]
    seen = []

    def request_json(self, path, payload=None, **_):
        seen.append((path, payload))
        if path == "/object_info":
            return info
        if path == "/internal/folder_paths":
            return folders
        raise AssertionError(f"ComfyUI must not be touched beyond two reads: {path}")

    monkeypatch.setattr(ComfyTargetClient, "request_json", request_json)
    return SimpleNamespace(client=ComfyTargetClient(ComfyTarget(URL, "attached")), seen=seen, info=info)


def _http():
    return httpx.Client(trust_env=False)


def _plan(comfy, server):
    return plan_attached_model_download(comfy.client, "z-image", "t2i", sources=_sources(server))


def _apply(comfy, server, digest, **kwargs):
    return apply_attached_model_download(comfy.client, "z-image", "t2i", plan_sha256=digest,
                                         sources=_sources(server), http_client=_http(), **kwargs)


def _files(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def test_plan_lists_every_file_size_folder_and_total_and_writes_nothing(comfy, models, file_server):
    root, _ = models
    plan = _plan(comfy, file_server)
    text = render_attached_download_plan(plan)
    for key, (name, path) in WANTED.items():
        assert name in text
        assert f"{len(BLOBS[path])} bytes" in text
    # canonical folders, never the legacy unet/clip siblings listed first
    for folder in ("diffusion_models", "text_encoders", "vae"):
        assert str(root / folder) in text
    assert str(root / "unet") not in text and str(root / "clip") not in text
    assert f"{TOTAL} bytes" in text and "total" in text.lower()
    assert "free" in text.lower()
    assert plan["plan_sha256"] in text and len(plan["plan_sha256"]) == 64
    assert plan["total_bytes"] == TOTAL
    assert _files(root) == []
    assert file_server.requested == []  # planning fetches nothing


def test_without_the_digest_nothing_is_written(comfy, models, file_server):
    root, _ = models
    _plan(comfy, file_server)
    for missing in (None, ""):
        with pytest.raises(AttachedDownloadRefused):
            _apply(comfy, file_server, missing)
    assert _files(root) == [] and file_server.requested == []


def test_with_the_digest_each_file_lands_in_the_folder_the_server_reports(comfy, models, file_server):
    root, _ = models
    plan = _plan(comfy, file_server)
    result = _apply(comfy, file_server, plan["plan_sha256"])
    assert _files(root) == ["diffusion_models/z_image_turbo_bf16.safetensors",
                            "text_encoders/qwen_3_4b.safetensors", "vae/ae.safetensors"]
    for key, (name, path) in WANTED.items():
        assert (root / key / name).read_bytes() == BLOBS[path]
    assert result["downloaded"] and result["total_bytes"] == TOTAL
    assert result["restarted"] is False


def test_an_interrupted_download_leaves_no_partial_file(comfy, models, file_server):
    root, _ = models
    plan = _plan(comfy, file_server)
    file_server.truncated.add("/vae.bin")
    with pytest.raises(Exception):
        _apply(comfy, file_server, plan["plan_sha256"])
    assert not (root / "vae" / "ae.safetensors").exists()
    # nothing but verified, complete files; no temp, part or receipt litter anywhere under models
    left = _files(root)
    assert all(name.endswith(".safetensors") for name in left), left
    assert "vae/ae.safetensors" not in left
    assert not (root / ".vibecomfy").exists()
    for name in left:
        key = name.split("/")[0]
        assert (root / name).read_bytes() == BLOBS[WANTED[key][1]]


def test_a_failed_checksum_never_leaves_the_file_under_its_final_name(comfy, models, file_server):
    root, _ = models
    plan = _plan(comfy, file_server)
    file_server.blobs["/vae.bin"] = b"vae-BYTES"  # same size, other bytes
    with pytest.raises(Exception):
        _apply(comfy, file_server, plan["plan_sha256"])
    assert not (root / "vae" / "ae.safetensors").exists()
    assert not [n for n in _files(root) if n.startswith("vae/")]


def test_refuses_when_free_space_is_below_the_total(comfy, models, file_server, monkeypatch):
    root, _ = models
    plan = _plan(comfy, file_server)
    real = shutil.disk_usage
    monkeypatch.setattr(shutil, "disk_usage", lambda path: real(path)._replace(free=TOTAL - 1))
    with pytest.raises(AttachedDownloadRefused) as caught:
        _apply(comfy, file_server, plan["plan_sha256"])
    assert "free" in str(caught.value).lower() and caught.value.code == "insufficient_space"
    assert _files(root) == [] and file_server.requested == []
    assert render_attached_download_plan(_plan(comfy, file_server)).lower().count("not enough") == 1


def test_free_space_margin_is_part_of_the_check_and_is_stated(comfy, models, file_server, monkeypatch):
    plan = _plan(comfy, file_server)
    assert plan["margin_bytes"] > 0
    real = shutil.disk_usage
    monkeypatch.setattr(shutil, "disk_usage",
                        lambda path: real(path)._replace(free=TOTAL + plan["margin_bytes"] - 1))
    with pytest.raises(AttachedDownloadRefused) as caught:
        _apply(comfy, file_server, plan["plan_sha256"])
    assert "margin" in str(caught.value)


def test_nothing_else_on_the_comfyui_is_touched_and_it_is_never_restarted(comfy, models, file_server):
    plan = _plan(comfy, file_server)
    result = _apply(comfy, file_server, plan["plan_sha256"])
    assert {path for path, _ in comfy.seen} <= {"/object_info", "/internal/folder_paths"}
    assert all(payload is None for _, payload in comfy.seen)  # reads only: no POST
    assert result["restarted"] is False
    assert "rescan" in result["next_step"].lower() or "object_info" in result["next_step"]


def test_remote_endpoint_gets_the_put_it_there_message_only(comfy, models, file_server):
    root, _ = models
    remote = SimpleNamespace(client=SimpleNamespace(
        target=SimpleNamespace(kind="attached", url="http://192.168.1.20:8188"),
        object_info=comfy.client.object_info, request_json=comfy.client.request_json))
    with pytest.raises(AttachedDownloadRefused) as caught:
        plan_attached_model_download(remote.client, "z-image", "t2i", sources=_sources(file_server))
    assert caught.value.code == "not_same_machine"
    assert "put " in str(caught.value).lower() and "z_image_turbo_bf16.safetensors" in str(caught.value)
    assert _files(root) == [] and file_server.requested == []


def test_wrong_or_stale_digest_is_refused(comfy, models, file_server):
    root, _ = models
    plan = _plan(comfy, file_server)
    with pytest.raises(AttachedDownloadRefused) as caught:
        _apply(comfy, file_server, "0" * 64)
    assert caught.value.code == "plan_changed"
    # a source that changed after the plan was shown makes the old digest stale
    stale = _sources(file_server)
    key = "vae/ae.safetensors"
    stale[key] = ModelSpec("ae.safetensors", None, None, None, stale[key].url, key,
                           stale[key].bytes, hashlib.sha256(b"other").hexdigest())
    with pytest.raises(AttachedDownloadRefused):
        apply_attached_model_download(comfy.client, "z-image", "t2i", plan_sha256=plan["plan_sha256"],
                                      sources=stale, http_client=_http())
    assert _files(root) == [] and file_server.requested == []


def test_an_existing_file_is_never_overwritten(comfy, models, file_server):
    root, _ = models
    plan = _plan(comfy, file_server)
    existing = root / "vae" / "ae.safetensors"
    existing.write_bytes(b"the person's own file")
    with pytest.raises(AttachedDownloadRefused) as caught:
        _apply(comfy, file_server, plan["plan_sha256"])
    assert caught.value.code == "file_exists"
    assert existing.read_bytes() == b"the person's own file"
    assert file_server.requested == []


def test_folder_choice_prefers_the_canonical_name_in_the_plan_and_the_missing_message(comfy, models, file_server):
    root, _ = models
    plan = _plan(comfy, file_server)
    assert {f["folder"] for f in plan["files"]} == {str(root / "diffusion_models"), str(root / "text_encoders"),
                                                    str(root / "vae")}
    with pytest.raises(ComfyModelMissing) as caught:
        _capture_model_resolution(comfy.client, ModelRegistry.load_default(), "z-image", "t2i")
    text = str(caught.value)
    assert str(root / "diffusion_models") in text and str(root / "text_encoders") in text
    assert str(root / "unet") not in text and str(root / "clip") not in text


def test_an_unsourced_file_is_listed_for_manual_placement_never_downloaded(comfy, models, file_server):
    root, _ = models
    sources = _sources(file_server)
    del sources["vae/ae.safetensors"]  # no verified source declared for this one
    plan = plan_attached_model_download(comfy.client, "z-image", "t2i", sources=sources)
    assert sorted(f["file"] for f in plan["files"]) == ["qwen_3_4b.safetensors", "z_image_turbo_bf16.safetensors"]
    assert len(plan["manual"]) == 1 and plan["manual"][0]["file"].startswith("ae.safetensors")
    text = render_attached_download_plan(plan)
    assert "ae.safetensors" in text and str(root / "vae") in text and "put " in text.lower()
